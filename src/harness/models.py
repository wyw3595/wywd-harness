"""模型契约与离线测试使用的模型实现（FakeModel / ScriptedModel）。

练习 08 起的契约是结构化的：
- 模型收完整的消息列表（真实 Chat API 的 messages 格式）；
- 返回 ModelReply —— 每一轮要么是最终回答，要么是工具调用，
  不再有"最终回答："这类字符串暗号。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolCall:
    """模型发起的一次工具调用（对齐 wire 格式里的 message.tool_calls）。

    wire 格式中 arguments 是 JSON 字符串，由模型适配层负责解析；
    协议内部统一使用"已经解析好"的参数字典。
    """

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelReply:
    """模型单轮输出的结构化形态，歧义从这里消失。

    kind 只有两个取值：
    - "final"：最终回答，文本在 text 里；
    - "tool_calls"：要调用工具，调用清单在 tool_calls 里。
    """

    kind: Literal["final", "tool_calls"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 练习 18：本次 API 调用的 token 用量（wire 响应里的 usage：
    # prompt_tokens / completion_tokens / total_tokens）。离线模型不产生
    # 用量，默认空字典——协议加字段，FakeModel / ScriptedModel 零改动。
    usage: dict[str, int] = field(default_factory=dict)


class Model(Protocol):
    """任何可供 Harness 调用的模型都必须遵守的接口。"""

    def generate(self, messages: list[dict]) -> ModelReply:
        """根据消息历史生成一轮结构化回复。"""


@dataclass(frozen=True)
class FakeModel:
    """不请求网络、结果固定可预测的模型实现。"""

    response_prefix: str = "模拟模型回复："

    def generate(self, messages: list[dict]) -> ModelReply:
        # 假模型只关心最初的任务——它就是第一条 user 消息的内容。
        return ModelReply(
            kind="final",
            text=f"{self.response_prefix}{messages[0]['content']}",
        )


class ScriptedModel:
    """按剧本依次返回结构化回复的模型，让 Agent Loop 可以离线测试。

    它同样满足 Model 协议。每一轮实际收到的消息列表会记录在
    received_inputs 里，方便测试断言"历史真的按原样传给了模型"。

    用法示例：
        ScriptedModel([
            ModelReply(kind="tool_calls",
                       tool_calls=[ToolCall("call_1", "add", {"a": 2, "b": 3})]),
            ModelReply(kind="final", text="2 加 3 等于 5"),
        ])
    """

    def __init__(self, replies: list[ModelReply]) -> None:
        # 复制一份：调用方之后改动他手里的原列表，不应影响我们的剧本。
        self._replies = list(replies)
        self.received_inputs: list[list[dict]] = []

    def generate(self, messages: list[dict]) -> ModelReply:
        """依次返回下一条结构化回复；只剩最后一条时一直重复它。"""

        # 必须快照一份外壳（list(messages)）：循环还会继续往它自己的
        # 消息列表里追加新消息，如果直接存引用，received_inputs 里所有
        # 轮次都会指向同一个越变越长的列表，测试看到的是"最终状态"
        # 而不是"当时收到的样子"——共享可变引用的经典陷阱。
        self.received_inputs.append(list(messages))

        if not self._replies:
            raise ValueError("剧本里没有台词")

        if len(self._replies) > 1:
            return self._replies.pop(0)

        return self._replies[0]
