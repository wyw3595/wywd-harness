"""Chainlit 网页聊天：同一个 Harness 的第二个入口。

运行（先设置密钥 DEEPSEEK_API_KEY）：
    .\\.venv\\Scripts\\chainlit.exe run scripts/chainlit_app.py
浏览器自动打开 http://localhost:8000。

📚 这层刻意保持"薄"：所有逻辑都在 Harness 里，这里只做接线——
   事件 -> run_agent -> 界面。也因此它不配单元测试（框架胶水 +
   异步 UI 属于集成测试领域），27 条核心测试就是它的安全网。
"""

import asyncio
import json
import sys
from pathlib import Path

# chainlit 加载本文件时不会像 python -m 那样把项目根放进 sys.path，
# 所以自己加进去：保证 src.* 和 scripts.* 两个包都能被导入。
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import chainlit as cl

from scripts.toolbox import MAX_HISTORY_MESSAGES, build_model, build_registry
from src.harness.agent import run_agent
from src.harness.memory import trim_history


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

    # 实时直播：on_event 回调发生在 run_agent 的工作线程里，而界面操作
    # 必须回到主事件循环——run_coroutine_threadsafe 就是那座桥。
    loop = asyncio.get_running_loop()

    async def emit_step(name: str, output: str) -> None:
        async with cl.Step(name=name, type="tool") as step:
            step.output = output

    def on_event(event: str, data: dict) -> None:
        if event == "tool_start":
            args = json.dumps(data["arguments"], indent=2, ensure_ascii=False)
            asyncio.run_coroutine_threadsafe(
                emit_step(f"⚙️ 调用 {data['name']}", f"参数：\n{args}"), loop
            )
        elif event == "tool_end":
            asyncio.run_coroutine_threadsafe(
                emit_step(f"✅ 结果（{data['name']}）", str(data["content"])), loop
            )

    # 成本刹车（练习 19）：网页聊得再久，账单也不许线性涨——调 run_agent
    # 之前先把历史瘦到窗口大小。
    if history:
        history = trim_history(history, MAX_HISTORY_MESSAGES)
    # run_agent 是同步的（会阻塞着等 HTTP），扔进线程池跑，界面不卡。
    result = await cl.make_async(run_agent)(
        task, model=model, registry=registry, history=history, on_event=on_event
    )

    # 更新会话记忆：下一个问题带着完整历史。
    cl.user_session.set("history", result.messages)

    # 失败不该和成功长得一样（练习 16）：print 是终端的嘴，网页的嘴是
    # cl.Message——两个入口共用 RunResult，不共用显示代码。
    if result.status == "failed":
        await cl.Message(content=f"⚠️ {result.output}").send()
    else:
        await cl.Message(content=result.output).send()
