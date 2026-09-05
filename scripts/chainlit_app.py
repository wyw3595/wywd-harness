"""Chainlit 网页聊天：同一个 Harness 的第二个入口。

运行（先设置密钥 DEEPSEEK_API_KEY）：
    .\\.venv\\Scripts\\chainlit.exe run scripts/chainlit_app.py
浏览器自动打开 http://localhost:8000。

📚 这层刻意保持"薄"：所有逻辑都在 Harness 里，这里只做接线——
   事件 -> run_agent -> 界面；s04 之后多一根线：ASK 决策 -> 审批
   按钮 -> 回灌。也因此它不配单元测试（框架胶水 + 异步 UI 属于
   集成测试领域），141 条核心测试就是它的安全网。
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

from scripts.toolbox import (
    MAX_HISTORY_MESSAGES,
    build_model,
    build_policy,
    build_registry,
    build_system_prompt,
)
from src.harness.agent import run_agent
from src.harness.memory import trim_history
from src.harness.permissions import (
    AuditTrail,
    GovernedToolRunner,
    PermissionDecision,
)

# 审批卡片的超时秒数：5 分钟没人点 = 拒绝。审批的默认答案必须是"不"
# （fail-closed）——沉默和"点了个拒绝"在这里是同一个结局。
APPROVAL_TIMEOUT = 300


@cl.on_chat_start
async def on_chat_start() -> None:
    """用户打开新聊天页时执行：造模型、造注册表、装配治理、清空会话记忆。"""

    cl.user_session.set("model", build_model())
    cl.user_session.set("registry", build_registry())
    # s04 网页闸门：策略和审计轨迹都是会话级工件——每个聊天页一份，
    # 互不串账。审批员不在这里造：它必须闭包住 on_message 的事件循环。
    cl.user_session.set("policy", build_policy())
    cl.user_session.set("audit", AuditTrail())
    cl.user_session.set("history", None)

    await cl.Message(
        content="你好！我是你亲手搭的 Agent（天气 / 加法 / 列目录 / 读文件 / 写文件）。"
        "写文件等敏感操作会先弹审批按钮征求你的同意。问点什么吧！"
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

    # ---- s04 网页审批闸门 --------------------------------------------
    # 和上面 on_event 用的是同一座桥（run_coroutine_threadsafe），但用法
    # 相反：播报卡片"发了就不管"（fire-and-forget），审批必须拿到结论
    # ——所以多要一张回程票：Future.result() 让工作线程原地等用户的指尖。
    # 治理内核与终端入口完全同一套（policy -> decide -> approver ->
    # registry -> audit），只有审批员不同：CLI 是 input()，这里是按钮。
    policy = cl.user_session.get("policy")
    audit = cl.user_session.get("audit")

    async def ask_approval(decision: PermissionDecision) -> bool:
        """在主事件循环上画审批卡片；用户点按钮或超时，都给出明确结论。"""

        response = await cl.AskActionMessage(
            content=f"⚠️ 需要审批 [{decision.rule_id}]\n{decision.reason}",
            actions=[
                cl.Action(name="approve", payload={}, label="✅ 允许"),
                cl.Action(name="reject", payload={}, label="⛔ 拒绝"),
            ],
            timeout=APPROVAL_TIMEOUT,
        ).send()
        # 超时/没点 -> response 是 None；bool(None and ...) == False
        # ——不回应即拒绝，fail-closed 的落点。
        return bool(response and response.get("name") == "approve")

    def web_approver(decision: PermissionDecision) -> bool:
        """网页审批员（同步签名，s04 的 Approver 协议一个字不差）。

        任何意外（桥断了、等待超时）都当拒绝——审批路径上，
        "没等到点头"和"点了拒绝"必须是同一个结局。
        """

        future = asyncio.run_coroutine_threadsafe(ask_approval(decision), loop)
        try:
            # 卡片自己有 300 秒超时；这里多留 10 秒缓冲，保证先到的
            # 是卡片的"超时=拒绝"，而不是这里的 TimeoutError。
            return future.result(timeout=APPROVAL_TIMEOUT + 10)
        except Exception:
            return False

    runner = GovernedToolRunner(
        policy=policy,
        approver=web_approver,
        registry=registry,
        audit=audit,
    )

    # 成本刹车（练习 19）：网页聊得再久，账单也不许线性涨——调 run_agent
    # 之前先把历史瘦到窗口大小。
    if history:
        history = trim_history(history, MAX_HISTORY_MESSAGES)
    # 目录常驻：system 提示（含延迟工具目录）每次调到最前，幂等。
    system_message = {"role": "system", "content": build_system_prompt()}
    if not history:
        history = [system_message]
    elif history[0].get("role") != "system":
        history = [system_message] + history
    # run_agent 是同步的（会阻塞着等 HTTP），扔进线程池跑，界面不卡。
    # runner 就位后，网页和终端过的是同一道闸门：decide -> 审批 -> 执行。
    result = await cl.make_async(run_agent)(
        task, model=model, registry=registry, history=history,
        on_event=on_event, runner=runner,
    )

    # 更新会话记忆：下一个问题带着完整历史。
    cl.user_session.set("history", result.messages)

    # 失败不该和成功长得一样（练习 16）：print 是终端的嘴，网页的嘴是
    # cl.Message——两个入口共用 RunResult，不共用显示代码。
    if result.status == "failed":
        await cl.Message(content=f"⚠️ {result.output}").send()
    else:
        await cl.Message(content=result.output).send()
