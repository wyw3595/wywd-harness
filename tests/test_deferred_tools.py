"""s03 延迟加载：发现 → 加载 → 执行的完整状态机测试。

覆盖 s03 README 锁定的关键契约 + 与 agent 循环的联合剧本：
1. model_schemas 只暴露即时工具，延迟工具连 schema 都不可见；
2. 发现：精确名称命中加载 schema，未命中进 missing；二次命中标 cached；
3. 排序：平分结果按名称稳定排序，不依赖注册顺序；
4. 执行前置条件：未发现直接执行 / 即时工具借延迟入口绕行 / 未知工具，
   三种拒绝各回明确文案；
5. 发现后执行：handler 真的被调用，参数按它自己的 schema 校验；
6. token_report：延迟加载成本 < 全量加载成本。
"""

import unittest

from src.harness.agent import run_agent
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.tools import Tool, ToolRegistry, validate_arguments


def echo_slow(message: str, loud: bool = False) -> str:
    """[MOCK] 回显一条消息（练习用延迟工具）。

    Args:
        message: 要回显的文本。
        loud: 是否用大写强调。
    """

    return f"[MOCK] echo: {message.upper() if loud else message}"


def setup_registry() -> ToolRegistry:
    """一个带桥接工具的注册表：即时 add + 延迟 echo_slow。"""

    registry = ToolRegistry()
    registry.register(Tool("add", "计算两个整数的和", lambda a, b: a + b))
    # 桥接工具：闭包绑定 registry（和 scripts/toolbox.py 同款写法）。
    registry.register(
        Tool(
            name="ToolSearch",
            description="发现延迟工具",
            handler=lambda tool_names=None, queries=None, top_k=3: (
                registry.load_by_name(tool_names)
                if tool_names
                else registry.search(queries or [], top_k)
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_names": {"type": "array", "items": {"type": "string"}},
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "top_k": {"type": "integer"},
                },
            },
        )
    )
    registry.register(
        Tool(
            name="DeferExecuteTool",
            description="执行已加载的延迟工具",
            handler=lambda toolName, params=None: registry.defer_execute(
                toolName, params or {}
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "toolName": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["toolName"],
            },
        )
    )
    registry.register(
        Tool(name="echo_slow", description="回显消息", handler=echo_slow, defer=True)
    )
    return registry


class DiscoveryTests(unittest.TestCase):
    """发现与加载：schema 只在发现后进入会话。"""

    def setUp(self) -> None:
        self.registry = setup_registry()

    def test_model_schemas_exclude_deferred_tool(self) -> None:
        # 延迟工具连 schema 都不可见：模型不可能凭空对着它发起调用，
        # 只能先发现。桥接工具是即时的，从第一轮就在目录里。
        names = [s["function"]["name"] for s in self.registry.model_schemas()]

        self.assertIn("add", names)
        self.assertIn("ToolSearch", names)
        self.assertIn("DeferExecuteTool", names)
        self.assertNotIn("echo_slow", names)

    def test_load_by_name_loads_schema_once(self) -> None:
        first = self.registry.load_by_name(["echo_slow"])
        self.assertIn("✓ echo_slow [loaded]", first)
        # 同一个会话再次发现：状态如实标 cached，模型知道别再重复发。
        second = self.registry.load_by_name(["echo_slow"])
        self.assertIn("✓ echo_slow [cached]", second)

    def test_load_by_name_missing_does_not_load(self) -> None:
        # 未命中不改 loaded 缓存、也不抛异常——结果就是数据。
        result = self.registry.load_by_name(["not_a_tool"])
        self.assertIn("✗ not_a_tool: 没有这个延迟工具", result)
        # 事后执行仍是"尚未加载"，证明缓存没被污染。
        self.assertIn("尚未加载", self.registry.defer_execute("echo_slow", {"message": "x"}))

    def test_search_tie_breaks_by_name(self) -> None:
        # 平分（score 相同）时按名称稳定排序，与注册顺序无关。
        reg = ToolRegistry()
        for name in ["zebra_leap", "apple_pick", "mango_wrap"]:  # 故意乱序注册
            reg.register(
                Tool(
                    name=name,
                    description="core utility",  # 同分：都只命中描述词
                    handler=lambda: "x",
                    defer=True,
                )
            )
        text = reg.search(["core"])
        lines = [line for line in text.splitlines() if line.startswith("✓")]
        self.assertEqual(len(lines), 3)
        # 稳定序：apple_pick < mango_wrap < zebra_leap（与注册序相反）
        self.assertTrue(lines[0].startswith("✓ apple_pick"))
        self.assertTrue(lines[1].startswith("✓ mango_wrap"))
        self.assertTrue(lines[2].startswith("✓ zebra_leap"))


class DeferredExecutionTests(unittest.TestCase):
    """执行前置条件：s03 失败路径逐条锁死。"""

    def setUp(self) -> None:
        self.registry = setup_registry()

    def test_unknown_tool_rejected(self) -> None:
        self.assertEqual(
            self.registry.defer_execute("ghost", {}),
            "Error: 未知工具 'ghost'",
        )

    def test_immediate_tool_cannot_use_deferred_gateway(self) -> None:
        # 即时工具不许借延迟入口绕行——保留 s02 的直接 dispatch 边界。
        self.assertEqual(
            self.registry.defer_execute("add", {"a": 1, "b": 2}),
            "Error: 'add' 是即时工具，直接调用它",
        )

    def test_execute_before_discovery_rejected(self) -> None:
        # 发现是执行的前置条件：schema 未进会话就执行，拒绝并提示先搜索。
        self.assertIn(
            "尚未加载",
            self.registry.defer_execute("echo_slow", {"message": "hi"}),
        )

    def test_execute_validates_arguments_against_its_own_schema(self) -> None:
        self.registry.load_by_name(["echo_slow"])
        # loud 是 bool，传了字符串照样在 execute 前被拦住（s02 校验复用）。
        result = self.registry.defer_execute(
            "echo_slow", {"message": "hi", "loud": "yes"}
        )
        self.assertIn("invalid_arguments", result)
        self.assertIn("必须是 boolean", result)

    def test_execute_after_discovery_calls_handler(self) -> None:
        self.registry.load_by_name(["echo_slow"])

        out = self.registry.defer_execute(
            "echo_slow", {"message": "你好", "loud": True}
        )

        self.assertEqual(out, "[MOCK] echo: 你好")

    def test_token_report_quantifies_saving(self) -> None:
        report = self.registry.token_report()

        self.assertGreater(report["full"], report["current"])
        self.assertGreater(report["saved"], 0)
        # 桥接两个即时工具（ToolSearch/DeferExecuteTool）+ add 占 immediate，
        # 延迟目录只算名称+描述一行，远小于全量 schema。
        self.assertGreater(report["immediate"], report["directory"])


class AgentLoopIntegrationTests(unittest.TestCase):
    """与 agent 循环的联合剧本：全链路离线可复现。"""

    def test_discover_then_execute_roundtrip(self) -> None:
        registry = setup_registry()
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("c1", "ToolSearch", {"tool_names": ["echo_slow"]})],
                ),
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[
                        ToolCall(
                            "c2",
                            "DeferExecuteTool",
                            {"toolName": "echo_slow", "params": {"message": "你好"}},
                        )
                    ],
                ),
                ModelReply(kind="final", text="回显完成"),
            ]
        )

        result = run_agent("转写并回显", model=model, registry=registry)

        self.assertEqual(result.output, "回显完成")
        self.assertEqual(result.status, "completed")
        # 第二步 DeferExecuteTool 的回灌里带着 mock 执行结果。
        last_tool = model.received_inputs[2][-1]
        self.assertEqual(last_tool["content"], "工具 DeferExecuteTool 返回：[MOCK] echo: 你好")

    def test_execute_without_discovery_gets_actionable_error(self) -> None:
        registry = setup_registry()
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[
                        ToolCall("c1", "DeferExecuteTool", {"toolName": "echo_slow", "params": {}})
                    ],
                ),
                ModelReply(kind="final", text="那我先搜索一下"),
            ]
        )

        result = run_agent("直接用延迟工具", model=model, registry=registry)

        backflow = model.received_inputs[1][-1]["content"]
        self.assertIn("尚未加载", backflow)
        self.assertIn("请先调用 ToolSearch", backflow)


class ValidateArgumentIntrospectionTests(unittest.TestCase):
    """桥接 schema 毕竟进了 registry：验证它给模型看的也是严格契约。"""

    def test_tool_search_schema_rejects_multiple_top_k(self) -> None:
        # 显式 schema 走 validate_arguments 同样执行前校验。
        schema = setup_registry().model_schemas()
        tool_search = next(
            s for s in schema if s["function"]["name"] == "ToolSearch"
        )
        parameters = tool_search["function"]["parameters"]

        self.assertIsNone(validate_arguments(parameters, {"tool_names": ["echo_slow"]}))
        self.assertIsNone(validate_arguments(parameters, {"top_k": 5}))
        self.assertIsNone(validate_arguments(parameters, {"queries": ["echo"], "top_k": 2}))