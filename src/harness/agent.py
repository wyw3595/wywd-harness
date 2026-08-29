"""
============================================================
Harness 练习 03 ~ 08：Agent Loop、工具回灌、对话历史与结构化
============================================================

📚 为什么需要它
  run() 只会"一问一答"。真实任务需要多轮：模型先说要做什么，
  看到中间结果后继续，直到它认为任务完成。Agent Loop 就是把
  "生成 → 判断 → 再生成"的过程跑起来，并保证它一定会停下来。

📚 本课核心
  - while 循环 + 步骤计数器：控制最多循环几轮，忘了更新计数器就是死循环。
  - 两个退出条件缺一不可：正常出口（最终回答）+ 保险丝（max_steps）。
  - 工具出错不能炸掉整个循环：错误本身也是要回灌给模型的信息；
    一轮里的多个工具调用互不拖累，逐个执行、逐个回灌。
  - 对话历史：{"role": ..., "content": ...} 消息列表，user / assistant /
    tool 三种角色，每轮发言先入历史；列表直接交给模型，不再渲染成文本。
  - 练习 08 起全部结构化：模型返回 ModelReply（kind="final" 或
    "tool_calls"），循环不再做任何字符串前缀匹配。
    FINAL_ANSWER_PREFIX / TOOL_REQUEST_PREFIX / _coerce_arg /
    _render_messages / ROLE_LABELS 已全部退役。

📚 这层的职责
  run_agent：驱动模型多轮执行，并在合适的时机停下。
  它不认识任何具体模型，只依赖 Model 协议 —— FakeModel、ScriptedModel、
  DeepSeek 的 RealModel 都能直接塞进来，循环代码完全一样。

完成标志：tests/test_agent.py 的测试全部通过。
============================================================
"""

from typing import Any, Callable
from uuid import uuid4

from src.harness.main import RunResult
from src.harness.models import FakeModel, Model, ModelReply, ScriptedModel, ToolCall
from src.harness.tools import Tool, ToolRegistry


def run_agent(
    task: str,
    model: Model | None = None,
    registry: ToolRegistry | None = None,
    max_steps: int = 5,
    history: list[dict] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
) -> RunResult:
    """驱动模型最多 max_steps 轮，直到它给出最终回答。

    每一轮的流程（练习 08 起为结构化）：
    - 把消息列表直接交给模型，拿回一个 ModelReply；
    - kind == "final"：回复先入历史，返回 status="completed" 的结果；
    - kind == "tool_calls"：调用清单先入历史，然后逐个执行——每个工具
      的结果（或错误）作为带 tool_call_id 的 tool 消息追加进历史，
      下一轮整体喂回给模型（回灌）。

    会话记忆（练习 10 起）：传入 history 就接着聊，不传冷启动；
    结束时把完整对话放在返回结果的 messages 字段里，
    调用方用 history = result.messages 延续会话。
    Harness 本身无状态——记多久、记不记，是应用层的事。

    事件钩子（练习 15 起）：传入 on_event(event, data) 后，循环在每个
    关键节点都会广播事件——round_start / model_reply / tool_start /
    tool_end——调用方借此"实时直播"过程（终端打印、网页步骤卡、
    日志、计费统计都吃同一份事件流）。不传则完全安静。

    保险丝：连续 max_steps 轮没等到最终回答 -> 返回 status="max_steps"
    的 RunResult，output 说明最后一轮请求了哪些工具。
    """

    if model is None:
        model = FakeModel()

    if registry is None:
        # 空注册表：任何工具调用都会命中"工具不存在"，走错误反馈分支。
        registry = ToolRegistry()

    step = 0
    last_reply = ""
    if history is not None:
        # 会话记忆：从调用方给的历史"复制"起步——复制是为了
        # 不在调用方的列表上原地追加（共享可变引用，第三次登门）。
        messages = list(history)
    else:
        messages = []
    messages.append({"role": "user", "content": task})

    def emit(event: str, **data: Any) -> None:
        """广播事件；没人订阅（on_event 为 None）就什么都不发生。"""

        if on_event is not None:
            on_event(event, data)

    while step < max_steps:
        emit("round_start", step=step)
        # 失败也是出口（练习 16）：模型层的异常（RealModel 重试耗尽等）不再
        # 炸穿调用方，而是落地成 status="failed" 的结果——失败是结果，
        # 不是异常。messages 照常返回：用户的问题已入列，不许弄丢。
        try:
            reply = model.generate(messages)
        except Exception as error:
            return RunResult(
                run_id=str(uuid4()),
                task=task,
                output=f"调用模型失败：{error}",
                status="failed",
                messages=messages,
            )
        emit(
            "model_reply",
            kind=reply.kind,
            text=reply.text,
            tool_names=[call.name for call in reply.tool_calls],
        )

        if reply.kind == "final":
            # 每轮发言先入历史——最终回答也留在对话记录里。
            messages.append({"role": "assistant", "content": reply.text})
            return RunResult(
                run_id=str(uuid4()),
                task=task,
                output=reply.text,
                status="completed",
                messages=messages,
            )

        # 工具调用轮：先把调用清单存入历史（这是我们的方言，
        # 练习 09 的 RealModel 负责把它翻译成 wire 格式）。
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in reply.tool_calls
                ],
            }
        )

        for call in reply.tool_calls:
            emit(
                "tool_start",
                call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
            )
            try:
                tool_output = registry.execute(call.name, **call.arguments)
                content = f"工具 {call.name} 返回：{tool_output}"
            except Exception as error:
                # 错误也是信息：回灌给模型让它自己决定下一步，
                # 一次工具失败绝不拖垮同轮的其他调用。
                content = f"工具 {call.name} 出错：{error}"

            emit(
                "tool_end",
                call_id=call.call_id,
                name=call.name,
                content=content,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": content,
                }
            )

        last_reply = "请求调用工具：" + "、".join(
            call.name for call in reply.tool_calls
        )
        step += 1

    return RunResult(
        run_id=str(uuid4()),
        task=task,
        output=last_reply,
        status="max_steps",
        messages=messages,
    )


if __name__ == "__main__":
    def get_weather(city: str) -> str:
        """演示用工具：查询一个城市的天气。"""

        return f"{city}今天晴，25 度"

    registry = ToolRegistry()
    registry.register(
        Tool(name="get_weather", description="查询城市天气", handler=get_weather)
    )

    scripted = ScriptedModel(
        [
            ModelReply(
                kind="tool_calls",
                tool_calls=[ToolCall("call_1", "get_weather", {"city": "北京"})],
            ),
            ModelReply(kind="final", text="北京今天晴，25 度，适合出门。"),
        ]
    )
    result = run_agent("北京天气怎么样？", model=scripted, registry=registry)
    print(result)
