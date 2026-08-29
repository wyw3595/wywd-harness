"""Chainlit 网页聊天：同一个 Harness 的第二个入口。

运行（先设置密钥 DEEPSEEK_API_KEY）：
    .\\.venv\\Scripts\\chainlit.exe run scripts/chainlit_app.py
浏览器自动打开 http://localhost:8000。

📚 这层刻意保持"薄"：所有逻辑都在 Harness 里，这里只做接线——
   事件 -> run_agent -> 界面。也因此它不配单元测试（框架胶水 +
   异步 UI 属于集成测试领域），27 条核心测试就是它的安全网。
"""

import sys
from pathlib import Path

# chainlit 加载本文件时不会像 python -m 那样把项目根放进 sys.path，
# 所以自己加进去：保证 src.* 和 scripts.* 两个包都能被导入。
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import json

import chainlit as cl

from scripts.toolbox import build_model, build_registry
from src.harness.agent import run_agent


@cl.on_chat_start
async def on_chat_start() -> None:
    """用户打开新聊天页时执行：造模型、造注册表、清空会话记忆。"""

    cl.user_session.set("model", build_model())
    cl.user_session.set("registry", build_registry())
    cl.user_session.set("history", None)

    await cl.Message(
        content="你好！我是你亲手搭的 Agent（天气 / 加法 / 列目录 / 读文件）。问点什么吧！"
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """用户发来一条消息时执行：取状态 -> 跑 Harness -> 回放工具步骤 -> 回答。"""

    model = cl.user_session.get("model")
    registry = cl.user_session.get("registry")
    history = cl.user_session.get("history")

    task = message.content.strip()
    if task == "/clear":
        cl.user_session.set("history", None)
        await cl.Message(content="记忆已清空。").send()
        return

    # run_agent 是同步的（会阻塞着等 HTTP），扔进线程池跑，界面不卡。
    result = await cl.make_async(run_agent)(
        task, model=model, registry=registry, history=history
    )

    # 回放式步骤展示：只回放"本轮新增"的消息——
    # history 里的旧消息上一问已经展示过，user 提问链式聊天界面自己会显示。
    prior_len = len(history) if history else 0
    new_messages = result.messages[prior_len + 1 :]

    for m in new_messages:
        if m["role"] == "assistant" and "tool_calls" in m:
            for call in m["tool_calls"]:
                async with cl.Step(name=call["name"], type="tool") as step:
                    step.output = (
                        "参数："
                        + json.dumps(call["arguments"], indent=2, ensure_ascii=False)
                    )
        elif m["role"] == "tool":
            async with cl.Step(
                name=f"工具结果（{m['tool_call_id']}）", type="tool"
            ) as step:
                step.output = m["content"]

    # 更新会话记忆：下一个问题带着完整历史。
    cl.user_session.set("history", result.messages)

    await cl.Message(content=result.output).send()
