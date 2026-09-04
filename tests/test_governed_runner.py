"""src.harness.permissions 执行层(GovernedToolRunner)的离线单元测试(练习 s04 · B 批)。

覆盖教材锁定的执行契约:
  1. to_protocol_block 三态编码:BLOCKED 带规则号 / SUCCEEDED 原文 / FAILED 带文案;
  2. AuditTrail 三类记录 + by_kind 过滤;
  3. Runner 真拦截:DENY 与"ASK 但老板拒绝"都不碰 handler,返回 BLOCKED;
  4. Runner 真执行:ALLOW 放行,handler 返回值成为 SUCCEEDED 的输出;
  5. handler 抛异常 -> FAILED 而不是炸穿 runner;
  6. BLOCKED 时审计也要记满三笔(被拦也是一种决策轨迹)。
"""

import unittest

from src.harness.agent import run_agent
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import (
    AuditTrail,
    GovernedToolRunner,
    PermissionAction,
    PermissionPolicy,
    PermissionRule,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolRequest,
    build_default_policy,
)
from src.harness.tools import Tool, ToolRegistry


def req(name: str, arguments: object, rid: str = "r1") -> ToolRequest:
    return ToolRequest(tool_use_id=rid, name=name, arguments=arguments)


class ProtocolBlockTests(unittest.TestCase):
    """结果 -> agent 可回的文本。"""

    def test_blocked_carries_rule_and_reason(self) -> None:
        """BLOCKED 文案带状态前缀 + 规则来源,模型才懂为什么被拦。"""

        r = ToolExecutionResult(
            req("bash", {"command": "sudo x"}),
            ToolExecutionStatus.BLOCKED,
            "bash.hard_deny: 命令属于危险集",
        )
        block = r.to_protocol_block()
        self.assertIn("permission_blocked", block)  # 状态前缀可识别
        self.assertIn("bash.hard_deny", block)       # 规则号可追,不是一句模糊拒绝

    def test_succeeded_passes_output_through(self) -> None:
        """SUCCEEDED 原文透传,不加前缀。"""

        r = ToolExecutionResult(
            req("add", {"a": 1, "b": 2}), ToolExecutionStatus.SUCCEEDED, "3"
        )
        self.assertEqual(r.to_protocol_block(), "3")

    def test_failed_carries_error_prefix(self) -> None:
        """FAILED 文案带 execution_error 前缀。"""

        r = ToolExecutionResult(
            req("read_file", {"path": "x"}),
            ToolExecutionStatus.FAILED,
            "PermissionError: 越界",
        )
        self.assertIn("execution_error", r.to_protocol_block())


class AuditTests(unittest.TestCase):
    """三笔账 + 按 kind 过滤。"""

    def test_records_three_kinds(self) -> None:
        """request / permission / result 三种 kind 各能记、能筛。"""

        audit = AuditTrail()
        audit.record("request", tool="read_file")
        audit.record("permission", tool="read_file", rule="path.read_allow")
        audit.record("result", tool="read_file", status="succeeded")
        self.assertEqual(len(audit.records), 3)
        self.assertEqual(len(audit.by_kind("permission")), 1)
        self.assertEqual(audit.records[1]["rule"], "path.read_allow")


class RunnerBlockedTests(unittest.TestCase):
    """拦截路径:不碰 handler。"""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(name="add", description="加法", handler=lambda a, b: a + b)
        )
        self.audit = AuditTrail()
        self.runner = GovernedToolRunner(
            policy=build_default_policy(),
            approver=lambda decision: True,  # 老板无脑同意——仍应被 DENY 拦住
            registry=self.registry,
            audit=self.audit,
        )

    def test_deny_blocks_without_touching_handler(self) -> None:
        """DENY:返回 BLOCKED,handler 没被调。"""

        result = self.runner.run(req("bash", {"command": "sudo rm -rf /"}))
        self.assertIs(result.status, ToolExecutionStatus.BLOCKED)
        self.assertIn("permission_blocked", result.to_protocol_block())

    def test_deny_still_audited_fully(self) -> None:
        """被拦也要记满三笔——拦截本身是决策轨迹,不是空白。"""

        self.runner.run(req("bash", {"command": "sudo rm -rf /"}))
        self.assertEqual(len(self.audit.by_kind("request")), 1)
        self.assertEqual(len(self.audit.by_kind("permission")), 1)
        self.assertEqual(len(self.audit.by_kind("result")), 1)
        self.assertEqual(self.audit.by_kind("result")[0]["status"], "blocked")

    def test_unknown_tool_blocks(self) -> None:
        """default.deny 兜底:没注册也不在规则里的工具,被拦。"""

        result = self.runner.run(req("curl", {"url": "https://x"}))
        self.assertIs(result.status, ToolExecutionStatus.BLOCKED)


class RunnerExecuteTests(unittest.TestCase):
    """放行路径:handler 真的被调。"""

    def setUp(self) -> None:
        tracked = {"calls": 0, "last_args": None}

        def handler(a: int, b: int) -> int:
            tracked["calls"] += 1
            tracked["last_args"] = (a, b)
            return a + b

        self.tracked = tracked
        self.registry = ToolRegistry()
        self.registry.register(Tool(name="add", description="加法", handler=handler))
        self.audit = AuditTrail()
        # 直接放行工具(工具不在策略里则会 default.deny——用 ALLOW 规则):
        allow_add = PermissionPolicy([
            PermissionRule(
                "everything", PermissionAction.ALLOW,
                lambda request: request.name == "add",
                explain=lambda request: "本注册表全是安全工具",
            ),
        ])
        self.runner = GovernedToolRunner(
            policy=allow_add,
            approver=lambda decision: True,
            registry=self.registry,
            audit=self.audit,
        )

    def test_allow_executes_handler(self) -> None:
        """ALLOW:handler 被调 1 次,返回值成为 SUCCEEDED 的输出。"""

        result = self.runner.run(req("add", {"a": 1, "b": 2}))
        self.assertIs(result.status, ToolExecutionStatus.SUCCEEDED)
        self.assertEqual(result.output, "3")
        self.assertEqual(self.tracked["calls"], 1)
        self.assertEqual(self.tracked["last_args"], (1, 2))
        self.assertEqual(self.audit.by_kind("result")[0]["status"], "succeeded")

    def test_handler_exception_is_failed(self) -> None:
        """handler 抛异常 -> FAILED,且 runner 不炸(继续返回结果)。"""

        def boom(a: int, b: int) -> int:
            raise ZeroDivisionError("boom")

        registry = ToolRegistry()
        registry.register(Tool(name="add", description="加法", handler=boom))
        runner = GovernedToolRunner(
            policy=PermissionPolicy([
                PermissionRule(
                    "ok", PermissionAction.ALLOW,
                    lambda r: r.name == "add",
                    explain=lambda r: "ok",
                ),
            ]),
            approver=lambda decision: True,
            registry=registry,
        )
        result = runner.run(req("add", {"a": 1, "b": 0}))
        self.assertIs(result.status, ToolExecutionStatus.FAILED)
        self.assertIn("execution_error", result.to_protocol_block())


class RunnerAuditCompletenessTests(unittest.TestCase):
    """每个出口都记满三笔账——FAILED / 参数毒 BLOCKED 不许从复盘里消失。"""

    def setUp(self) -> None:
        self.audit = AuditTrail()
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(name="add", description="加法", handler=lambda a, b: a + b)
        )
        self.runner = GovernedToolRunner(
            policy=PermissionPolicy([
                PermissionRule(
                    "everything", PermissionAction.ALLOW,
                    lambda request: request.name == "add",
                    explain=lambda request: "安全",
                ),
            ]),
            approver=lambda decision: True,
            registry=self.registry,
            audit=self.audit,
        )

    def test_handler_exception_fully_audited(self) -> None:
        """handler 抛异常 -> FAILED,result 审计也在(带 error 归因)。"""

        def boom(a: int, b: int) -> int:
            raise ZeroDivisionError("boom")

        registry = ToolRegistry()
        registry.register(Tool(name="add", description="加法", handler=boom))
        runner = GovernedToolRunner(
            policy=PermissionPolicy([
                PermissionRule(
                    "ok", PermissionAction.ALLOW,
                    lambda r: r.name == "add", explain=lambda r: "ok",
                ),
            ]),
            approver=lambda decision: True,
            registry=registry,
            audit=self.audit,
        )
        result = runner.run(req("add", {"a": 1, "b": 0}, rid="c1"))
        self.assertIs(result.status, ToolExecutionStatus.FAILED)
        results = self.audit.by_kind("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("boom", results[0]["error"])

    def test_poison_arguments_fully_audited(self) -> None:
        """参数不是 dict -> BLOCKED,result 审计也在(带 error 归因)。"""

        result = self.runner.run(req("add", "not a dict", rid="c2"))
        self.assertIs(result.status, ToolExecutionStatus.BLOCKED)
        results = self.audit.by_kind("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "blocked")
        self.assertIn("不是对象", results[0]["error"])

    def test_call_id_links_all_three_records(self) -> None:
        """三笔账都带同一个 call_id——按调用可完整复盘多轮里的每次调用。"""

        self.runner.run(req("add", {"a": 1, "b": 2}, rid="trace-1"))
        for kind in ("request", "permission", "result"):
            records = self.audit.by_kind(kind)
            self.assertEqual(len(records), 1, kind)
            self.assertEqual(records[0]["call_id"], "trace-1", kind)


class AgentLoopIntegrationTests(unittest.TestCase):
    """s04 整合:agent loop 把 runner 的 BLOCKED 结果回灌给模型。"""

    def test_blocked_call_flows_back_to_model(self) -> None:
        """被拦的调用不产生执行,回灌文案带 permission_blocked 前缀。"""

        # handler 带类型注解:无注解的参数会被反射成 string,校验层
        # 会先于权限把 int 参数拒掉(s02 的约定,别在这里踩)。
        def add(a: int, b: int) -> int:
            return a + b

        registry = ToolRegistry()
        registry.register(Tool(name="add", description="加法", handler=add))
        policy = PermissionPolicy([
            PermissionRule(
                "no.add", PermissionAction.DENY,
                lambda r: r.name == "add",
                explain=lambda r: "演示:一切 add 都拒绝",
            ),
        ])
        runner = GovernedToolRunner(
            policy=policy, approver=lambda d: True, registry=registry,
        )
        model = ScriptedModel([
            ModelReply(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(call_id="c1", name="add",
                             arguments={"a": 1, "b": 2})
                ],
            ),
            ModelReply(kind="final", text="收到,被拒了"),
        ])

        result = run_agent(
            "算一下", model=model, registry=registry, runner=runner,
        )

        self.assertEqual(result.status, "completed")
        tool_messages = [m for m in result.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        # 回灌文案由 to_protocol_block 统一编码:状态前缀 + 规则号都在,
        # 模型看得懂"为什么被拒",才会换路径而不是反复重试。
        self.assertIn("permission_blocked", tool_messages[0]["content"])
        self.assertIn("no.add", tool_messages[0]["content"])

    def test_allowed_call_still_executes_through_runner(self) -> None:
        """放行的调用结果正常回灌——runner 不是只添乱的关卡。"""

        def add(a: int, b: int) -> int:
            return a + b

        registry = ToolRegistry()
        registry.register(Tool(name="add", description="加法", handler=add))
        runner = GovernedToolRunner(
            policy=build_default_policy(safe_tools=["add"]),
            approver=lambda d: True,
            registry=registry,
        )
        model = ScriptedModel([
            ModelReply(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(call_id="c1", name="add",
                             arguments={"a": 2, "b": 3})
                ],
            ),
            ModelReply(kind="final", text="2 加 3 等于 5"),
        ])

        result = run_agent(
            "算一下", model=model, registry=registry, runner=runner,
        )

        self.assertEqual(result.status, "completed")
        tool_messages = [m for m in result.messages if m["role"] == "tool"]
        self.assertIn("5", tool_messages[0]["content"])


if __name__ == "__main__":
    unittest.main()