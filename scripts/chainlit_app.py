"""Chainlit 网页聊天：最终形态——网页 UI 接 SidecarShell，agent 在 sidecar。

运行（先设置密钥 DEEPSEEK_API_KEY）：
    .\\.venv\\Scripts\\chainlit.exe run scripts/chainlit_app.py
浏览器自动打开 http://localhost:8000。

📚 这层刻意保持"薄"：所有逻辑都在 harness + shell 里，这里只做接线——
   审批按钮 -> SidecarShell.user_prompt；事件 -> 步骤卡片（on_event）。
   s06.5 之后：不再直跑 run_agent——agent 在独立 sidecar 子进程里跑，
   网页只是第三个 UI（终端 sidecar_shell、桌面 electron_shell、网页本文件）。

   ⚠️ chainlit 生命周期坑（源码级核实）：
   - F5 刷新会触发 on_chat_end（停壳），但 on_chat_start 不重跑——会话里
     留下已 stop 的死壳。解法：_ensure_shell() 惰性重建（is_alive 检查）。
     代价：刷新 = 失忆（新壳新 sid，旧历史随进程销毁），可接受的边界。
   - 同会话消息不串行化——SidecarShell 内的 _rpc_lock 负责串行化。
   - 任何 mp target 都不要定义在本文件（chainlit 用 console shim 启动，
     spawn 重导入语义绕）——target 一律在 scripts/shell.py。
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

from scripts.shell import SidecarShell
from src.harness.sidecar import ConnectionClosed

# 审批卡片的超时秒数：5 分钟没人点 = 拒绝。审批的默认答案必须是"不"
# （fail-closed）——沉默和"点了个拒绝"在这里是同一个结局。
APPROVAL_TIMEOUT = 300


def _make_web_shell(loop: asyncio.AbstractEventLoop) -> SidecarShell:
    """造一个"网页 UI 版"的壳：按钮审批 + 步骤卡片直播。

    审批签名零适配：sidecar 跨进程只传 rule_id + reason 两个字符串
    （_make_approver 拆好的），网页审批员直接收 (rule_id, reason)——
    不需要造 PermissionDecision/ToolRequest，比 s04-b 的 web_approver 干净。
    """

    async def ask_approval(rule_id: str, reason: str) -> bool:
        """在主事件循环上画审批卡片；用户点按钮或超时，都给出明确结论。"""

        response = await cl.AskActionMessage(
            content=f"⚠️ 需要审批 [{rule_id}]\n{reason}",
            actions=[
                cl.Action(name="approve", payload={}, label="✅ 允许"),
                cl.Action(name="reject", payload={}, label="⛔ 拒绝"),
            ],
            timeout=APPROVAL_TIMEOUT,
        ).send()
        # 超时/没点 -> response 是 None；bool(None and ...) == False
        # ——不回应即拒绝，fail-closed 的落点。
        return bool(response and response.get("name") == "approve")

    def web_user_prompt(rule_id: str, reason: str) -> bool:
        """网页审批员（MainProcessClient.user_prompt 的字面协议，同步签名）。

        回调发生在 cl.make_async(shell.send) 的工作线程里，界面操作必须
        回主事件循环——run_coroutine_threadsafe + Future.result() 就是
        那座桥（s04-b 的红线：任何意外都当拒绝）。
        """

        future = asyncio.run_coroutine_threadsafe(
            ask_approval(rule_id, reason), loop)
        try:
            # 卡片自己有 300 秒超时；这里多留 10 秒缓冲，保证先到的
            # 是卡片的"超时=拒绝"，而不是这里的 TimeoutError。
            return future.result(timeout=APPROVAL_TIMEOUT + 10)
        except Exception:
            return False

    def web_on_event(data: dict) -> None:
        """把 sidecar 直播的事件变成网页步骤卡片（练习 15 的 emit_step 桥）。

        data["event"] 是事件名，data["name"] 才是工具名（s06 的坑）。
        """

        event = data.get("event")
        if event == "tool_start":
            args = json.dumps(data["arguments"], indent=2, ensure_ascii=False)
            asyncio.run_coroutine_threadsafe(
                emit_step(f"⚙️ 调用 {data['name']}", f"参数：\n{args}"), loop)
        elif event == "tool_end":
            asyncio.run_coroutine_threadsafe(
                emit_step(f"✅ 结果（{data['name']}）", str(data["content"])), loop)

    return SidecarShell(user_prompt=web_user_prompt, on_event=web_on_event)


async def emit_step(name: str, output: str) -> None:
    """网页的"一张工具步骤卡片"（在 on_message 定义并闭包 loop 用）。"""

    async with cl.Step(name=name, type="tool") as step:
        step.output = output


def _ensure_shell() -> SidecarShell:
    """取会话的壳；死了（F5 刷新停过壳）就重建。

    坑：on_chat_end 在 F5 刷新也触发（无条件停壳），on_chat_start 刷新后
    不重跑。所以 on_message 开头必须走这里兜底——壳不活就重起一个。
    """

    shell: SidecarShell | None = cl.user_session.get("shell")
    if shell is not None and shell.is_alive():
        return shell
    if shell is not None:
        shell.stop()  # 收掉刷新残留的死壳
    shell = _make_web_shell(cl.user_session.get("loop"))
    shell.start()
    cl.user_session.set("shell", shell)
    return shell


@cl.on_chat_start
async def on_chat_start() -> None:
    """用户打开新聊天页时执行：起一个 sidecar 壳（每个聊天页一个子进程）。"""

    # loop 必须在异步上下文里捕获并存进 session——回调在工作线程里调
    # asyncio.get_running_loop() 会炸，网页版必须提前抓住这个对象。
    cl.user_session.set("loop", asyncio.get_running_loop())
    try:
        shell = _ensure_shell()
        tools = ", ".join(t["name"] for t in shell.tools()) or "（无工具）"
        await cl.Message(
            content=f"你好！我是你亲手搭的 Agent（agent 在独立 sidecar 进程里跑）。"
            f"可用工具：{tools}。写文件等敏感操作会先弹审批按钮征求同意。"
            "问点什么吧！"
        ).send()
    except Exception as error:
        await cl.Message(content=f"⚠️ sidecar 启动失败：{error}").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """用户发来一条消息时执行：交给 sidecar 里的 agent，直播事件、显示结果。"""

    shell = _ensure_shell()
    task = message.content.strip()

    if task == "/clear":
        shell.clear()
        await cl.Message(content="记忆已清空（sidecar 里换了新会话）。").send()
        return

    try:
        # make_async：shell.send 是同步阻塞（等 agent 跑完），扔线程池跑，界面不卡。
        result = await cl.make_async(shell.send)(task)
    except ConnectionClosed:
        # sidecar 进程已死（外部杀/崩溃）：重置壳，让下一条消息重建。
        cl.user_session.set("shell", None)
        await cl.Message(content="⚠️ sidecar 进程已退出，已重置。请重新发送。").send()
        return

    if "error" in result:
        await cl.Message(content=f"⚠️ {result['error']}").send()
    else:
        await cl.Message(content=result["output"]).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    """聊天页关闭/刷新时：停掉本页的 sidecar 子进程（别留僵尸）。"""

    shell: SidecarShell | None = cl.user_session.get("shell")
    if shell is not None:
        shell.stop()
    cl.user_session.set("shell", None)
