"""Agent Loop 的回归测试（练习 08 起为结构化协议）。"""

import unittest

from src.harness.agent import run_agent
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.tools import Tool, ToolRegistry


def add(a: int, b: int) -> int:
    """测试用工具：计算两个整数的和。"""

    return a + b


class AgentLoopTests(unittest.TestCase):
    def test_finishes_when_model_says_final_answer(self) -> None:
        model = ScriptedModel([ModelReply(kind="final", text="任务已完成")])

        result = run_agent("测试任务", model=model)

        self.assertTrue(result.run_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.task, "测试任务")
        self.assertEqual(result.output, "任务已完成")

    def test_stops_at_max_steps_when_model_never_finishes(self) -> None:
        # 剧本永远只请求工具 -> 只能靠保险丝熔断；
        # output 应说明最后一轮请求了哪些工具。
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 1, "b": 1})],
                )
            ]
        )

        result = run_agent("不会完成的任务", model=model, max_steps=3)

        self.assertEqual(result.status, "max_steps")
        self.assertEqual(result.output, "请求调用工具：add")
        self.assertTrue(result.run_id)

    def test_truncated_final_answer_reports_truncated_status(self) -> None:
        # s01 整合：模型自报 truncated（finish_reason="length" 的映射）时，
        # 循环不能假装 completed——诚实的状态是 truncated"答案不完整"。
        model = ScriptedModel(
            [ModelReply(kind="final", text="回答到一半", truncated=True)]
        )

        result = run_agent("截断任务", model=model)

        self.assertEqual(result.status, "truncated")
        # output 保留现有部分文本，不凭空补全。
        self.assertEqual(result.output, "回答到一半")
        # 回答仍入历史——下一问带着不完整的上下文继续。
        self.assertEqual(result.messages[-1]["content"], "回答到一半")

    def test_final_answer_on_the_last_allowed_step_still_wins(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 1, "b": 1})],
                ),
                ModelReply(kind="final", text="压线完成"),
            ]
        )

        result = run_agent("边界任务", model=model, max_steps=3)

        self.assertEqual(result.output, "压线完成")


class AgentToolLoopTests(unittest.TestCase):
    """工具回灌。setUp 给每条测试一个干净的注册表。"""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(name="add", description="计算两个整数的和", handler=add)
        )

    def test_tool_result_is_fed_back_to_model(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})],
                ),
                ModelReply(kind="final", text="2 加 3 等于 5"),
            ]
        )

        result = run_agent("加法任务", model=model, registry=self.registry)

        self.assertEqual(result.output, "2 加 3 等于 5")
        # received_inputs[1] 是第二轮模型收到的完整消息列表，
        # [-1] 是最后一条——也就是刚回灌的工具结果。
        last_message = model.received_inputs[1][-1]
        self.assertEqual(last_message["role"], "tool")
        self.assertEqual(last_message["tool_call_id"], "call_1")
        self.assertIn("工具 add 返回：5", last_message["content"])

    def test_unknown_tool_error_is_fed_back(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_9", "no_such_tool", {"x": 1})],
                ),
                ModelReply(kind="final", text="工具不可用，我直接回答"),
            ]
        )

        result = run_agent("会失败的任务", model=model, registry=ToolRegistry())

        self.assertEqual(result.output, "工具不可用，我直接回答")
        self.assertIn("出错", model.received_inputs[1][-1]["content"])

    def test_multiple_tool_calls_in_one_turn(self) -> None:
        # 结构化通道的新能力：一轮携带多个工具调用，
        # 循环必须逐个执行、逐个回灌，且互不拖累。
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[
                        ToolCall("call_a", "add", {"a": 1, "b": 2}),
                        ToolCall("call_b", "add", {"a": 10, "b": 20}),
                    ],
                ),
                ModelReply(kind="final", text="两个结果分别是 3 和 30"),
            ]
        )

        result = run_agent("双加法任务", model=model, registry=self.registry)

        self.assertEqual(result.output, "两个结果分别是 3 和 30")
        tool_messages = [
            message
            for message in model.received_inputs[1]
            if message["role"] == "tool"
        ]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_a")
        self.assertEqual(tool_messages[1]["tool_call_id"], "call_b")


class AgentHistoryTests(unittest.TestCase):
    """对话历史直接以消息列表传给模型（不再渲染成文本）。"""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(name="add", description="计算两个整数的和", handler=add)
        )

    def test_history_is_passed_to_model_in_order(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})],
                ),
                ModelReply(kind="final", text="2 加 3 等于 5"),
            ]
        )

        run_agent("加法任务", model=model, registry=self.registry)

        second_round = model.received_inputs[1]
        self.assertEqual(
            [message["role"] for message in second_round],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(second_round[0]["content"], "加法任务")
        self.assertEqual(second_round[1]["tool_calls"][0]["name"], "add")

    def test_assistant_tool_call_turn_enters_history(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})],
                ),
                ModelReply(kind="final", text="想好了，做完了"),
            ]
        )

        run_agent("随想任务", model=model, registry=self.registry)

        second_round = model.received_inputs[1]
        self.assertEqual(second_round[1]["role"], "assistant")
        self.assertEqual(
            second_round[1]["tool_calls"][0]["arguments"], {"a": 2, "b": 3}
        )

    def test_second_question_receives_full_history(self) -> None:
        model_a = ScriptedModel(
            [ModelReply(kind="final", text="北京下紫色雪花")]
        )
        result_a = run_agent("北京天气怎么样？", model=model_a)

        model_b = ScriptedModel(
            [ModelReply(kind="final", text="上海也是紫色雪花")]
        )
        result_b = run_agent(
            "那上海呢？", model=model_b, history=result_a.messages
        )

        self.assertEqual(result_b.output, "上海也是紫色雪花")
        # 第二问的模型看到了：上一问的任务 + 回答 + 新问题。
        self.assertEqual(
            [m["role"] for m in model_b.received_inputs[0]],
            ["user", "assistant", "user"],
        )
        # 结果里带出完整对话，供下一问继续延续。
        self.assertEqual(
            [m["role"] for m in result_b.messages],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(result_b.messages[-1]["content"], "上海也是紫色雪花")

    def test_max_steps_result_carries_messages(self) -> None:
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 1, "b": 1})],
                )
            ]
        )

        result = run_agent("不会完成的任务", model=model, max_steps=2)

        self.assertEqual(result.status, "max_steps")
        # 两轮工具调用：每轮 assistant + tool 各一条，加开头的 user 共 5 条。
        self.assertEqual(
            [m["role"] for m in result.messages],
            ["user", "assistant", "tool", "assistant", "tool"],
        )

    def test_history_is_copied_not_shared(self) -> None:
        # 防御性复制的回归测试：run_agent 不许在调用方的 history 上原地追加。
        history = [{"role": "user", "content": "第一问"}]
        model = ScriptedModel([ModelReply(kind="final", text="答完了")])

        run_agent("第二问", model=model, history=history)

        self.assertEqual(history, [{"role": "user", "content": "第一问"}])


class AgentEventHookTests(unittest.TestCase):
    """练习 15：事件钩子——循环在关键节点广播过程。"""

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(name="add", description="计算两个整数的和", handler=add)
        )

    def test_on_event_receives_lifecycle(self) -> None:
        events: list[tuple[str, dict]] = []
        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})],
                ),
                ModelReply(kind="final", text="等于 5"),
            ]
        )

        run_agent(
            "加法任务",
            model=model,
            registry=self.registry,
            on_event=lambda event, data: events.append((event, data)),
        )

        # 两轮的完整事件序列：每轮 round_start + model_reply，
        # 工具轮多一对 tool_start + tool_end。
        self.assertEqual(
            [event for event, _ in events],
            [
                "round_start",
                "model_reply",
                "tool_start",
                "tool_end",
                "round_start",
                "model_reply",
            ],
        )
        tool_start = events[2][1]
        self.assertEqual(tool_start["name"], "add")
        self.assertEqual(tool_start["arguments"], {"a": 2, "b": 3})
        self.assertIn("返回：5", events[3][1]["content"])
        # 最终轮的 model_reply 报告 final + 文本。
        self.assertEqual(events[5][1]["kind"], "final")
        self.assertEqual(events[5][1]["text"], "等于 5")

    def test_no_hook_is_silent(self) -> None:
        # 不传 on_event 时一切照旧——钩子是可选的，默认行为不能变。
        model = ScriptedModel([ModelReply(kind="final", text="安静完成")])

        result = run_agent("普通任务", model=model)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output, "安静完成")


class UsageAccumulationTests(unittest.TestCase):
    """练习 18：token 用量跨轮求和——成本可观测的离线验证。"""

    def test_usage_sums_across_rounds(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool(name="add", description="计算两个整数的和", handler=add)
        )

        model = ScriptedModel(
            [
                ModelReply(
                    kind="tool_calls",
                    tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})],
                    usage={"prompt_tokens": 100, "completion_tokens": 10},
                ),
                ModelReply(
                    kind="final",
                    text="等于 5",
                    usage={"prompt_tokens": 150, "completion_tokens": 5},
                ),
            ]
        )

        result = run_agent("加法任务", model=model, registry=registry)

        self.assertEqual(result.status, "completed")
        # 两轮账单按键求和：100+150、10+5。
        self.assertEqual(
            result.usage,
            {"prompt_tokens": 250, "completion_tokens": 15},
        )


class RunAgentFailureTests(unittest.TestCase):
    """练习 16：失败也是出口——模型层的异常不能炸穿 Harness。"""

    def test_model_exception_becomes_failed_result(self) -> None:
        # 满足 Model 协议（只有一个 generate 方法）就能塞进 run_agent——
        # 协议的妙处：不用继承任何东西。
        class ExplodingModel:
            def generate(self, messages: list[dict]) -> ModelReply:
                raise RuntimeError("模拟：API 密钥无效")

        result = run_agent("随便什么任务", model=ExplodingModel())

        self.assertEqual(result.status, "failed")
        self.assertIn("API 密钥无效", result.output)
        # 用户的问题已入列，失败也不许弄丢。
        self.assertEqual(result.messages[0]["role"], "user")
        self.assertEqual(result.messages[0]["content"], "随便什么任务")


if __name__ == "__main__":
    unittest.main()
