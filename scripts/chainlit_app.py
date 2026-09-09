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
import contextvars
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
from scripts import sidecar_panel
from src.harness.sidecar import ConnectionClosed

# 面板后端：进程内 HTTP 小服务（8765）。custom_js 侧边栏要拉/操作会话，
# fetch 到这个服务——请求进度里的 chainlit 进程才能触达跑的 SidecarShell。
# 幂等：模块被 chainlit 加载一次启一次（daemon 线程，进程退出自动收）。
sidecar_panel.ensure_server()

# 审批卡片的超时秒数：5 分钟没人点 = 拒绝。审批的默认答案必须是"不"
# （fail-closed）——沉默和"点了个拒绝"在这里是同一个结局。
APPROVAL_TIMEOUT = 300

# 历史重放的署名：/replay 命令与面板聊天桥共用同一套呈现
HISTORY_LABELS = {"user": "🧑 你", "assistant": "🤖 agent",
                  "system": "⚙️ 系统", "tool": "🔧 工具"}


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
        红线：UI 回调失败绝对不许拖垮 agent 循环——刷新后旧 loop 已关闭，
        run_coroutine_threadsafe 会抛 RuntimeError，若不挡会一路炸穿
        turn，把会话打进 SessionState.ERROR（实际事故现场）。所以失败
        静默（丢一张卡片而已，副作用不丢——工具结果已进会话历史）。
        """

        event = data.get("event")
        if event == "tool_start":
            args = json.dumps(data["arguments"], indent=2, ensure_ascii=False)
            _safe_step(loop, emit_step, f"⚙️ 调用 {data['name']}", f"参数：\n{args}")
        elif event == "tool_end":
            _safe_step(loop, emit_step, f"✅ 结果（{data['name']}）",
                       str(data["content"]))

    return SidecarShell(user_prompt=web_user_prompt, on_event=web_on_event)


def _safe_step(loop, fn, name: str, output: str) -> None:
    """往主事件循环投一张步骤卡片；loop 已死/事件循环忙时静默丢弃。"""

    try:
        asyncio.run_coroutine_threadsafe(fn(name, output), loop)
    except Exception:
        pass  # UI 直播失败不该影响 agent 执行（合同里没有它）


async def emit_step(name: str, output: str) -> None:
    """网页的"一张工具步骤卡片"（在 on_message 定义并闭包 loop 用）。"""

    async with cl.Step(name=name, type="tool") as step:
        step.output = output


async def _send_history(header: str, msgs: list[dict]) -> None:
    """把一段会话历史逐条发进聊天区（/replay 与面板桥共用的呈现层）。

    红线与 _safe_step 同源：呈现失败绝不外抛——桥的调用方在面板线程里，
    处理不了异常；会话切换本身更不该被一张卡片拖垮。
    """

    try:
        await cl.Message(content=header).send()
        for item in msgs:
            role = item.get("role", "?")
            text = (item.get("content") or "").strip()
            if not text and item.get("tool_calls"):
                names = ", ".join(t.get("name", "?")
                                  for t in item["tool_calls"])
                text = "调用工具：" + names
            tag = HISTORY_LABELS.get(role, role)
            await cl.Message(content=f"**【{tag}】** {text}").send()
    except Exception:
        pass


async def _clear_chat_area() -> None:
    """清空聊天区：逐条删除 chat_context 里的消息。

    chainlit 没有"一键清屏"API，但 cl.chat_context 是公开的会话级消息
    台账（user 气泡 + assistant 卡片都在里面，源码核实 2.12），配合
    Message.remove()（发 delete_message 给前端）就能整页清掉——这是
    "点会话，主聊天区**变成**那个会话的对话"的清屏半场。删不动的
    （对象过期/前端丢失）静默跳过：清屏是呈现，不是契约。
    """

    try:
        for msg in cl.chat_context.get():
            try:
                await msg.remove()
            except Exception:
                pass  # 单条失败不挡后面的
    except Exception:
        pass  # context 缺失（桥的上下文过期）：退化为追加式重放


def _make_chat_bridge(shell: SidecarShell, loop: asyncio.AbstractEventLoop,
                      ctx: contextvars.Context):
    """面板操作的聊天区桥：resume → 清屏重放历史；create → 新会话分隔卡。

    为什么需要桥：cl.Message / cl.chat_context 靠 contextvars 找到"发往
    哪个页面"，而面板后端跑在 HTTP handler 线程里，没有 chainlit 的 ws
    上下文——直接 run_coroutine_threadsafe 建的 Task 拿不到它（Task 在
    loop 线程里拷贝的是那边的空上下文）。所以过桥带三样东西：
      shell（拉历史：阻塞 RPC 留在面板线程做，别卡主循环）
      loop  （回主事件循环——UI 操作必须在主循环上执行）
      ctx   （ws 上下文的快照；loop.create_task(coro, context=ctx) 把它
             显式还回去——run_coroutine_threadsafe 传不了 context）
    ctx 在 on_chat_start / on_message / on_window_message 里都会重新捕获
    ——F5 换了 ws 连接，也不会对着旧嘴说话。
    """

    def bridge(action: str, sid: str, detail: str = "") -> None:
        try:
            if action == "create":
                async def run():
                    await cl.Message(
                        content=f"🆕 已切换到新会话 `{sid}`"
                                "（旧会话留档，可从左侧面板切回）").send()
            else:  # resume：拉全量历史 → 回主循环清屏 + 逐条重放
                result = shell.messages(sid)
                if result.get("error"):
                    return
                msgs = result.get("messages") or []
                header = (f"📜 会话 `{sid}`（{detail}）——"
                          f"以下为它的完整历史（{len(msgs)} 条）")

                async def run():
                    await _clear_chat_area()
                    await _send_history(header, msgs)

            coro = run()
            # call_soon_threadsafe + create_task(context=)：把协程放回主循环，
            # 并显式带上 ws 上下文（这是与 run_coroutine_threadsafe 的关键差异）
            loop.call_soon_threadsafe(
                lambda: loop.create_task(coro, context=ctx))
        except RuntimeError:
            pass  # loop 已关（页面没了）：桥到不了就算了，切换本身已完成
    return bridge


def _register_bridge(shell: SidecarShell) -> None:
    """把本页的聊天桥登记进面板注册表（loop/ctx 缺失就跳过——面板降级无重放）。"""

    loop = cl.user_session.get("loop")
    ctx = cl.user_session.get("ws_ctx")
    if loop is None or ctx is None:
        return
    sidecar_panel.register_bridge(cl.user_session.get("id"),
                                  _make_chat_bridge(shell, loop, ctx))


def _ensure_shell() -> SidecarShell:
    """取会话的壳；死了（刷新间被清/异常）才重建。

    刷新语义（s10 修复）：F5 刷新会触发 on_chat_end，但 on_chat_end 现在
    **不杀壳**（见下），所以刷新回来这里的旧壳还活着，直接复用——
    会话 sid 不断、对话上下文不丢，看起来就是"刷新没刷新"。
    """

    shell: SidecarShell | None = cl.user_session.get("shell")
    if shell is not None and shell.is_alive():
        # 壳还在（刷新回来了）：重新登记进面板注册表，刷新活跃时间。
        sidecar_panel.register(cl.user_session.get("id"), shell)
        _register_bridge(shell)
        return shell
    if shell is not None:
        shell.stop()  # 壳死了才收尸
    shell = _make_web_shell(cl.user_session.get("loop"))
    shell.start()
    cl.user_session.set("shell", shell)
    # 登记进面板注册表（thread 标识来自 chainlit 的 user_session["id"]）：
    # 侧边栏靠它找到本页面这个壳，才能拉清单/做操作；桥让切换的
    # 清屏重放能送回本页聊天区。
    sidecar_panel.register(cl.user_session.get("id"), shell)
    _register_bridge(shell)
    return shell


@cl.on_chat_start
async def on_chat_start() -> None:
    """用户打开新聊天页时执行：起一个 sidecar 壳（每个聊天页一个子进程）。"""

    # 先收割僵尸壳：关掉的旧页面再没人回来，壳也别永远挂着
    sidecar_panel.reap_idle()

    # loop 必须在异步上下文里捕获并存进 session——回调在工作线程里调
    # asyncio.get_running_loop() 会炸，网页版必须提前抓住这个对象。
    cl.user_session.set("loop", asyncio.get_running_loop())
    # ws 上下文锚点：面板桥要用（cl.Message 靠 contextvars 找页面，HTTP
    # 线程里没有）——这里捕获一份，on_message / on_window_message 里刷新。
    cl.user_session.set("ws_ctx", contextvars.copy_context())
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


def _session_list_text(shell: SidecarShell) -> str:
    """当前会话清单文本——/resume、/forget 缺参数时给用户指路。

    closed 的会话也在清单里（live=False）：记录 ≠ 运行时，s07 的核心一课。
    """

    lines = []
    for s in shell.sessions()["sessions"]:
        lines.append(f"  `{s['id']}`  {s['status']}（live={s['live']}，gen={s['runtimeGeneration']}）")
    return "\n".join(lines) or "  （暂无会话）"


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """用户发来一条消息时执行：交给 sidecar 里的 agent，直播事件、显示结果。"""

    # 刷新 ws 上下文锚点：F5 后是新的连接，而 on_chat_start 刷新后不重跑
    # （chainlit 生命周期坑）——不刷新的话，面板桥还对着旧嘴说话。
    cl.user_session.set("ws_ctx", contextvars.copy_context())
    shell = _ensure_shell()
    task = message.content.strip()

    if task == "/clear":
        shell.clear()
        await cl.Message(content="记忆已清空（sidecar 里换了新会话）。").send()
        return

    if task == "/close":
        # 关当前会话（不传参=当前）：记录保留，closed ≠ 消失。
        result = shell.close_session()
        if "error" in result:
            await cl.Message(content=f"⚠️ {result['error']}").send()
        else:
            await cl.Message(
                content=f"会话 {result.get('closed')} 已关闭（记录保留）。\n"
                "`/resume <id>` 可复活，`/clear` 可开新会话。").send()
        return

    if task.startswith("/resume"):
        parts = task.split()
        if len(parts) < 2:
            await cl.Message(
                content=f"用法：`/resume sess_0001`\n当前会话清单：\n{_session_list_text(shell)}"
            ).send()
            return
        sid = parts[1]
        result = shell.resume_session(sid)   # 成功侧 shell._sid 已换成它；live 的会被拒
        if "error" in result:
            await cl.Message(content=f"⚠️ {result['error']}").send()
        else:
            await cl.Message(
                content=f"✅ 已复活 `{sid}`（generation {result['generation']}）。现在直接发消息就行。"
            ).send()
        return

    if task.startswith("/replay"):
        # 历史重放：把指定会话的完整对话逐条打进聊天区（只读，不切换、
        # 不清屏——与面板切换的差别就在这，命令是"查看"，切换是"变成"）。
        parts = task.split()
        sid = parts[1] if len(parts) > 1 else (shell.session_id or "")
        result = shell.messages(sid)
        msgs = result.get("messages") or []
        if result.get("error"):
            await cl.Message(content=f"⚠️ {result['error']}").send()
        elif not msgs:
            await cl.Message(content=f"会话 `{sid}` 还没有历史。").send()
        else:
            await _send_history(
                f"📜 会话 `{sid}` 的历史（共 {len(msgs)} 条，当前对话区内）",
                msgs)
        return

    if task.startswith("/forget"):
        parts = task.split()
        if len(parts) < 2:
            await cl.Message(
                content=f"用法：`/forget sess_0001`\n当前会话清单：\n{_session_list_text(shell)}"
            ).send()
            return
        sid = parts[1]
        result = shell.forget_session(sid)   # live 的会被拒：先 /close 再 /forget
        if "error" in result:
            await cl.Message(content=f"⚠️ {result['error']}").send()
        else:
            await cl.Message(content=f"✅ 已遗忘 `{sid}`。").send()
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
    """聊天页关闭/刷新时触发——**不再杀壳**（这是"刷新=新建对话"的根）。

    背景：F5 刷新也会无条件触发 on_chat_end，而 on_chat_start 刷新后不
    重跑。老行为在这里 shell.stop() 杀掉 sidecar，刷新后 _ensure_shell
    只能重建新壳新 sid——用户视角就是"一刷新就开新对话"。
    现在改为不杀：壳留在进程级注册表里，刷新回来 _ensure_shell 直接
    复用旧壳，会话上下文不断。代价：页面**真关了**（不是刷新）壳会
    常驻，由 on_chat_start 的 reap_idle（默认 30 分钟超时）兜底收割。
    """


@cl.on_window_message
async def on_window_message(data) -> None:
    """前端页面加载时会 window.postMessage 打一声招呼（见 sessions_panel.js）。

    为什么需要：F5 后 on_chat_start 不重跑，面板桥里捕获的 ws 上下文
    还是**旧连接**的——点会话的清屏重放会发到已死的连接上（静默失败）。
    这个钩子在**当前连接**的 ws 上下文里跑（socket.py 的 window_message
    handler 先 init_ws_context 再调它，源码核实），正好用来刷新锚点、
    重注册桥。数据本身不消费——要的只是"在正确的上下文里被叫一声"。
    """

    cl.user_session.set("ws_ctx", contextvars.copy_context())
    shell = cl.user_session.get("shell")
    if shell is not None:
        _register_bridge(shell)


@cl.on_stop
async def on_stop() -> None:
    """chainlit 服务退出时：停掉所有常驻 sidecar，别留僵尸 python 进程。"""

    sidecar_panel.stop_all()
