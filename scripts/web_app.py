"""s07-c / C 方案 web_app：自定义前端，彻底解耦 chainlit。

为什么存在这个文件：chainlit 的聊天区是它自己的 WS + React 状态，"会话
切换 = 服务端往聊天区推历史"只能靠 contextvars 快照跨线程搬运，效果仍
不可靠（重放 fire-and-forget、失败全静默）。C 方案换标准做法——
"会话是记录，聊天区是视图，切换 = 换 id + 读历史 + 自己渲染"。

   浏览器 ──fetch（同源 127.0.0.1:8765）──► web_app（本文件）
                                            ├─ 1 个 SidecarShell（1 个 sidecar 子进程，独占写者）
                                            ├─ 静态文件服务（public/：index.html + src/）
                                            └─ JSON API + SSE 事件流 + 审批登记

关键架构决定：单壳单 sidecar。多 tab 只是同一个 backend 的多个浏览器
视图——"每 tab 一个 sidecar 共享 .sessions/"时代的竞态（启动清账误伤、
跨进程 resume 双运行时、计数器竞态）被架构性消灭，不是修好。

三大机制（对照 chainlit 版各自的病灶）：
  1. 历史 = 纯读：GET /api/sessions/<sid>/messages 直接
     JsonlSessionStore(root).load(sid).messages（replay fold），不经过
     sidecar RPC、不碰运行中的 turn、closed 会话秒开。零静默失败：
     成功 = 前端自己拿到数组并渲染，没有"桥有没有跑"这种问题。
  2. 直播/审批 = 事件环（Condition + 有界 deque + seq）+ threading.Event：
     on_event 回调（sidecar 事件）+ user_prompt 回调（审批请求）都往
     同一条流里塞；GET /api/events/stream 用 SSE 长连往外推（早先是
     前端每 500ms 轮询，现在服务端有事件才写）。审批不用 asyncio
     Future——user_prompt 线程 Event.wait(60)，HTTP 线程 POST 回执
     set()。"loop 已死 / 上下文过期"这两个失败模式从根上不存在。
  3. 发消息带显式 sid：Single 壳下不能靠"当前会话指针"（多 tab 互相
     抢），shell.send_to(sid, msg)，见 shell.py 的 send_to。

运输层（2026-09-25 换 FastAPI）：上面这套领域层（WebApp / EventRing /
ApprovalBoard / sse_frame）一行没动——它们从写下的第一天起就不碰
HTTP，这是当初"领域逻辑与 HTTP 分开测"的直接回报。换掉的只是运输层：

  - http.server 手写 Handler → FastAPI 路由 + uvicorn。同步领域层照旧
    用同步 `def` 端点（FastAPI 自动丢线程池），**不要**改成 async def：
    领域层全是阻塞调用（_rpc_lock、socket recv），async def 会卡死事件循环。
  - SSE：StreamingResponse 包住原来的 event_stream 生成器，协议不变。
  - 守门员：纯 ASGI 中间件（_GuardMiddleware + _guard_rejection 纯函数）
    ——Host 校验（杀 DNS rebinding）、X-Wywd-Ui 全量 POST 校验（杀 CSRF，
    从"只查 browse"扩到所有 /api POST）、可选 WYWD_API_TOKEN、
    Content-Length 上限早退（413）。决定逻辑是纯函数，测试直调不碰 HTTP。
  - 兜底异常处理：任何穿透到 HTTP 层的异常回 JSON 500 人话，不再出现
    "连接被空响应关掉、浏览器只见 Failed to fetch"。
  - HOST/PORT 可用 WYWD_HOST / WYWD_PORT 覆盖（原先硬编码）。

  已知的没修（诚实边界）：全局 _rpc_lock 的"一轮 turn 堵住所有 RPC"是
  sidecar 单通道协议决定的，换运输层不解决；治理它要动 RPC 客户端
  （读线程 + Future）与 sidecar（turn 后台化），是下一步的事。

运行：python scripts/web_app.py → 浏览器开 http://127.0.0.1:8765
（离线可用：不设 DEEPSEEK_API_KEY 时 choose_model 给 FakeModel）。
⚠ workers 必须是 1：WebApp / sidecar 是进程内单例。
"""

import base64
import binascii
import json
import os
import string
import sys
import tempfile
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 项目根进 sys.path：保证 src.* / scripts.* 可导入（照 shell.py 的模式）
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts import native_dialog             # noqa: E402
from scripts.shell import SidecarShell          # noqa: E402
from src.harness.jsonl_store import JsonlSessionStore  # noqa: E402
from src.harness.session import (              # noqa: E402
    SessionLifecycleError,
    SessionNotFoundError,
)


def _resolve_host() -> str:
    """监听地址。默认 127.0.0.1——整个安全模型建立在"只服务本机"上
    （配合 _guard_rejection 的 Host 校验），要改先想清楚那两道闸。"""

    return (os.environ.get("WYWD_HOST") or "").strip() or "127.0.0.1"


def _resolve_port() -> int:
    default = 8765
    raw = (os.environ.get("WYWD_PORT") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[web_app] WYWD_PORT 不是数字（{raw!r}），用默认 {default}")
        return default
    if not 0 < value < 65536:
        print(f"[web_app] WYWD_PORT 越界（{value}），用默认 {default}")
        return default
    return value


HOST = _resolve_host()
PORT = _resolve_port()
SESSIONS_ROOT = Path(_PROJECT_ROOT) / ".sessions"
PUBLIC_DIR = Path(_PROJECT_ROOT) / "public"
# 审批超时秒数：沉默 = 拒绝（fail-closed，与 chainlit 版语义一致）。
#
# **2026-09-23：300 → 60。** 审批是**阻塞式**的，而且占着唯一的 RPC 通道
# （scripts/shell.py 的 _rpc_lock）——300 秒意味着"没人应答时整个前端要堵
# 5 分钟"（实测就是这个现象：除审批回执本身外，所有请求都在排队）。
# 60 秒是"够人反应、又不至于把界面堵死"的量级。
# `WYWD_APPROVAL_TIMEOUT` 可覆盖（排查时想放宽就设它）。
def resolve_approval_timeout() -> float:
    """审批超时（秒）。`WYWD_APPROVAL_TIMEOUT` 可覆盖；非法值喊一声退回默认。"""

    default = 60.0
    raw = (os.environ.get("WYWD_APPROVAL_TIMEOUT") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"[web_app] WYWD_APPROVAL_TIMEOUT 不是数字（{raw!r}），用默认 {default}")
        return default
    if value <= 0:
        print(f"[web_app] WYWD_APPROVAL_TIMEOUT 必须为正数（{value}），用默认 {default}")
        return default
    return value


APPROVAL_TIMEOUT = resolve_approval_timeout()
EVENT_RING_MAX = 500
# SSE 心跳间隔：静默这么久就写一行注释保活，同时借此发现对面已经走了。
# 15 秒是常识区间——小于常见中间设备 60 秒空闲断连，又不至于太吵。
SSE_HEARTBEAT = 15.0
# 工作区上传（s11）：压缩包原始字节的上限，以及整个请求体（base64 后套
# JSON 外壳）的上限。后者必须更大：base64 有 33% 膨胀。
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 48 * 1024 * 1024

# 这三个会话动作必须带**显式** sid（值是动作的中文名，用来拼错误文案）。
# 为什么在 web 层硬拦：壳侧的 close/resume/forget 都是 `target = sid or self._sid`
# ——空 sid 会被静默解释成"当前会话"（终端不带参数是有意的）。可网页是多 tab 的，
# 这一层根本没有"当前会话"这个概念。空 sid 漏下去 = "删除这一行"变成"删除当前
# 那个"，删错了还不报错，是最难查的那种坏法。
_SID_REQUIRED_ACTIONS = {"close": "关闭会话", "resume": "复活会话",
                         "forget": "遗忘会话"}


# ═══════════════════════════════════════════════════════════════
# EventRing — 线程安全的事件环（直播 + 审批请求共用一条流）
# ═══════════════════════════════════════════════════════════════

class EventRing:
    """有界事件环：on_event 回调 / 审批登记都往里塞，SSE 长连往外推。

    写=多线程（sidecar RPC 线程 + HTTP handler 线程），读=每条 SSE 连接
    一个线程，所以 append / after 都过锁。超过 maxlen 丢最旧的（直播缺角
    可接受——历史永远以 messages API 为准，这是写进边界的教学取舍）。

    **为什么是 Condition 而不是 Lock**（这一步的核心改动）：轮询时代
    服务端只会被动应答，锁就够了；要做推送就得"没有事件时睡着、有事件
    时被叫醒"——Condition 正是这个语义。它复用同一把锁，于是
    "读游标 + 判空 + 等待"是一个原子动作（用独立 Event 做通知会漏：
    判完空、还没 wait，事件就到了，然后白睡一整个心跳周期）。

    wait_for 的超时不是错误，是心跳节奏：静默期到点返回空列表，让
    HTTP 层有机会往连接里写一行注释保活（也是探测"对面还在不在"）。
    """

    def __init__(self, maxlen: int = EVENT_RING_MAX) -> None:
        self._deque: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    def append(self, event: str, data: dict) -> int:
        """追加一条事件、唤醒所有等待者，返回它的 seq。data 并入条目。"""

        with self._changed:
            self._seq += 1
            self._deque.append({"seq": self._seq, "event": event, **data})
            self._changed.notify_all()
            return self._seq

    def after(self, after: int = 0) -> tuple[list[dict], int]:
        """seq > after 的事件（顺序保持）；(事件列表, 当前最大 seq)。

        保留给调试端点 /api/events 和直调测试用——前端已经改走 SSE，
        不再靠它轮询。
        """

        with self._lock:
            return [item for item in self._deque if item["seq"] > after], self._seq

    def latest(self) -> int:
        """当前最大 seq。SSE 首次连接用它把游标顶到"现在"，跳过历史重放。"""

        with self._lock:
            return self._seq

    def wait_for(self, after: int, timeout: float) -> tuple[list[dict], int]:
        """等到有 seq > after 的事件，或超时。返回值形状同 after()。

        被 notify_all 叫醒后重新过滤——多个 SSE 连接各自带不同游标，
        一个人被叫醒不等于你就有新东西，条件变量必须重查判据。
        """

        with self._changed:
            if self._seq <= after:
                self._changed.wait(timeout)
            return [item for item in self._deque if item["seq"] > after], self._seq

    def wake_all(self) -> None:
        """叫醒所有等待者（收尾用）：让 SSE 线程立刻看到 stopping 退出，
        而不是各自把 15 秒心跳睡完——关服务不该卡在等心跳上。
        """

        with self._changed:
            self._changed.notify_all()

    def close(self) -> None:
        """收尾的另一个名字：唤醒所有等待者。

        合流（2026-09-15）说明：s11 那条线的 EventRing 用的是"订阅队列 +
        哨兵"式实现，它有个 close() 负责给每个订阅者发哨兵让他们收摊；
        本文件采用的是 Condition 式实现（wait_for / wake_all），合流时
        选了后者（Vue 前端与 master 的 SSE 测试都是按这套写的）。
        保留 close() 这个名字是为了让 s11 那边在工作区测试的 tearDown 里
        写的 `ring.close()` 继续可用——语义对齐到 wake_all，没有第二套机制。
        """

        self.wake_all()


# ═══════════════════════════════════════════════════════════════
# SSE 帧编码 — 纯函数，跟 HTTP 无关，所以能单测
# ═══════════════════════════════════════════════════════════════

def sse_frame(item: dict) -> str:
    """一条事件 → 一个 SSE 帧。

    两个格式决定：
      - data 用一层 JSON 把整条事件（含 event 名）装进去。SSE 的 data 行
        不能有裸换行，json.dumps 天然满足；事件名留在载荷里，前端一个
        onmessage 就能全收——不必为每种事件 addEventListener（具名事件
        会让 onmessage 静默失效，是个很容易踩的坑）。
      - id 必须是事件的 seq：浏览器断线重连时会把它放进 Last-Event-ID
        头，服务端据此接着推，续传游标不用前端自己管。
    """

    payload = json.dumps(item, ensure_ascii=False)
    return f"id: {item['seq']}\ndata: {payload}\n\n"


# 心跳帧：SSE 规范里的注释行（冒号开头），客户端直接忽略。
SSE_HEARTBEAT_FRAME = ": ping\n\n"


# ═══════════════════════════════════════════════════════════════
# ApprovalBoard — 审批登记/回执（threading.Event，不是 asyncio Future）
# ═══════════════════════════════════════════════════════════════

class ApprovalBoard:
    """审批的中转站：user_prompt 线程请求 → 前端看到卡 → HTTP 线程回执。

    对比 chainlit 版的 run_coroutine_threadsafe + Future：这里 Event 是
    跨线程的天然栅栏，同一进程内的两个线程各等各的——"loop 已死"、
    "ws 上下文过期"这两个失败模式在进程内 HTTP + Event 的模型下根本
    不存在。超时 = 返回 False（fail-closed 的落点，默认为拒绝）。
    """

    def __init__(self, ring: Optional[EventRing] = None,
                 timeout: float = APPROVAL_TIMEOUT) -> None:
        self._ring = ring
        self._timeout = timeout
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    # 供 SidecarShell.user_prompt 注入（同签名：rule_id, reason -> bool）
    def request(self, rule_id: str, reason: str) -> bool:
        """登记一次审批并阻塞等待回执。横幅进事件环，前端画卡。"""

        ticket = str(uuid.uuid4())
        entry: dict[str, Any] = {"value": None, "event": threading.Event()}
        with self._lock:
            self._pending[ticket] = entry
        if self._ring is not None:
            self._ring.append("approval_request", {
                "request_id": ticket, "rule_id": rule_id, "reason": reason})
        entry["event"].wait(self._timeout)
        with self._lock:
            self._pending.pop(ticket, None)
        return bool(entry["value"])

    def respond(self, ticket: str, approved: bool) -> bool:
        """前端 POST 回执：找到了就叫醒等待的 request 线程。没找到返回 False。"""

        with self._lock:
            entry = self._pending.get(ticket)
            if entry is None:
                return False
            entry["value"] = approved
        entry["event"].set()
        return True

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


# ═══════════════════════════════════════════════════════════════
# WebApp — 领域逻辑（HTTP 之外的纯方法，测试可直接直调）
# ═══════════════════════════════════════════════════════════════

class WebApp:
    """一个 backend 的全部领域逻辑：壳代理 + 历史直读 + 事件环 + 审批。

    shell 可注入（测试传 FakeShell）；默认构造真 SidecarShell 但**不
    启动**（start 才 spawn 子进程）。store 默认指向 _PROJECT_ROOT/.sessions
    ——和 sidecar 子进程写的是同一份证据目录（s09 跨进程语义）。
    """

    def __init__(self, shell: Any = None, store_root: Optional[str] = None,
                 approval_timeout: float = APPROVAL_TIMEOUT,
                 shell_factory: Any = None) -> None:
        self.ring = EventRing()
        self.board = ApprovalBoard(self.ring, approval_timeout)
        self.store = JsonlSessionStore(store_root or SESSIONS_ROOT)
        # 收尾标志：SSE 是长连，每条约占一个线程。停服务时先竖旗、再
        # wake_all()，它们醒来看到旗就自己收摊。
        self.stopping = False
        # 造壳的工厂：默认造真 SidecarShell（**不启动**），测试可以注入假壳
        # ——revive_shell() 靠它就地换一个新壳，不给测试留口子就没法单测。
        self._shell_factory = shell_factory or (lambda: SidecarShell(
            user_prompt=self.board.request,
            on_event=self._record_event,
        ))
        self.shell = shell if shell is not None else self._shell_factory()

    def revive_shell(self) -> bool:
        """sidecar 不在就就地换一个新的。返回是否真的换了。

        为什么需要这一步：sidecar 是**子进程**，会因环境原因退出（本机实测：
        宿主的"安全删除"拦下会话证据文件的 unlink，子进程当场死掉）。它一死，
        所有走 RPC 的端点在 handler 里抛 ConnectionClosed —— 这条请求的连接
        被直接关掉、一个字节的响应都没有，浏览器只看到 "Failed to fetch"，
        用户以为**整个后端没起**。而 web_app 自己还活着，所以"重启服务"这个
        动作在用户看来既不明显也不该是必需的。

        代价：每次请求多一次进程存活查询（is_alive 就是 OS 层的一次状态读），
        可以忽略。假壳（测试）没有 is_alive 时什么都不做，保持既有行为。
        """

        probe = getattr(self.shell, "is_alive", None)
        if probe is None:
            return False                   # 假壳：不动
        if probe():
            return False                   # 活着：零成本放行

        try:
            self.shell.stop()              # 幂等：把半死的壳收干净
        except Exception:
            pass                           # 收尾失败也得继续换新的
        self.shell = self._shell_factory()
        # 沿用网页入口的口径：启动不建会话（懒建），第一次发消息再建。
        self.shell.start(create_session=False)
        # 工作区不用在这边恢复：它是**会话级**的（2026-09-23），目录跟
        # 会话记录走，sidecar 在 resume 时按 cwd 重建运行时——进程换代
        # 不会丢边界。曾经有一段"web 层记根 + 换代设回"的实现，服务
        # 2026-09-17 的"工具静默换根"事故；会话级化后职责整体移交
        # sidecar，那段代码连同测试已删（2026-09-26 清理）。
        print("[web_app] sidecar 已退出 → 自动起了一个新的"
              "（已建会话的记录仍在 .sessions/，点一下会话即可 resume）")
        return True

    def _record_event(self, data: dict) -> None:
        """SidecarShell.on_event 包装：data["event"] 是事件名，其余是载荷。"""

        name = data.get("event", "event")
        payload = {k: v for k, v in data.items() if k != "event"}
        self.ring.append(name, payload)

    def start(self) -> dict:
        """启动 sidecar 子进程（main() 在 serve_forever 前调用）。

        **懒建会话**：传 create_session=False，启动时不建会话。理由是网页
        入口每次重启都 spawn 新 sidecar，而前端恢复的是 localStorage 里
        记着的更早那个 sid——启动会话就永远没人打开，只会变成侧边栏里的
        「未命名会话」残留。真正的建会话时机交给前端：第一次发消息，或
        点侧边栏的 ＋（两者都走 POST /api/sessions {action:"create"}）。
        """

        return self.shell.start(create_session=False)

    def stop(self) -> None:
        """干净收尾（幂等）：先竖旗叫醒 SSE，再走 shell.stop() 全套。"""

        self.stopping = True
        self.ring.wake_all()   # 别让 SSE 线程把 15 秒心跳睡完才走
        self.shell.stop()

    # ── API：会话清单 / 操作 ────────────────────────────────

    def sessions(self) -> dict:
        """清单走 RPC（live 是 sidecar 进程内 _runtimes 的事实）。"""

        return self.shell.sessions()

    def action(self, action: str, sid: str = "") -> dict:
        """清单外的四个动作：create / close / resume / forget（翻译约定
        与既有终端/侧边栏一致：成功 {"ok": True, "detail": ...}）。"""

        shell = self.shell
        if action == "create":
            try:
                new_sid = shell.new_session()   # 关旧开新，旧记录保留
            except Exception as exc:
                # 建会话是硬失败（没有新 id 可用），但也不能让 handler 崩掉——
                # 翻译成人话，前端摆一张"没能开始"的卡（＋ 按钮点了没反应最糟）
                return {"ok": False, "detail": f"新建会话失败：{exc}"}
            return {"ok": True, "detail": f"已新建 {new_sid}",
                    "sessionId": new_sid}
        # 这三个动作必须带显式 sid，理由见 _SID_REQUIRED_ACTIONS 的注释。
        # 注意 create 不该被一起拦掉（它本来就不需要 sid）。
        if action in _SID_REQUIRED_ACTIONS and not sid.strip():
            return {"ok": False,
                    "detail": f"{_SID_REQUIRED_ACTIONS[action]}必须指定会话 id"}
        if action == "close":
            result = shell.close_session(sid)
            if "error" in result:
                return {"ok": False, "detail": result["error"]}
            # 文案里用请求的 sid，不用 result["closed"]——那是"是否真关了
            # 一个 runtime"的布尔值（sidecar 契约，test_sidecar 钉着），
            # 早先照抄成 sid 会吐出"已关闭 False（记录保留）"。
            return {"ok": True,
                    "detail": f"已关闭 {sid or '当前会话'}（记录保留）"}
        if action == "resume":
            result = shell.resume_session(sid)
            if "error" in result:
                return {"ok": False, "detail": result["error"]}
            return {"ok": True,
                    "detail": f"已复活 {sid}（generation {result['generation']}）"}
        if action == "forget":
            result = shell.forget_session(sid)
            if "error" in result:
                return {"ok": False, "detail": result["error"]}
            return {"ok": True, "detail": f"已遗忘 {sid}"}
        return {"ok": False, "detail": f"未知操作: {action}"}

    # ── API：sidecar 自述状态（成本表挂在里面）───────────────

    def status(self) -> dict:
        """会话数 / RingBuffer 用量 / handler 数（+ modelCost）。

        不另开 /api/cost：成本表本来就是 sidecar/status 的一段（s08 的
        duck typing——裸模型没有 cost_summary，status 就完全不带这个键）。
        一个真源，终端 /status 和网页看的是同一份。
        """

        return self.shell.status()

    def logs(self) -> dict:
        """sidecar 最近日志（RingBuffer 尾巴）。shell.logs() 签名是 str，
        且它自己已经把 RPC 失败翻译成一行说明——这里原样装进 dict 给前端。"""

        return {"logs": self.shell.logs()}

    # ── API：历史直读（C 的灵魂）─────────────────────────────

    def messages(self, session_id: str) -> dict:
        """完整历史 = fold 直读证据文件（不经过 sidecar RPC）。

        closed 会话照样能读——记录还在，store 里存的就是它。id 不存在 /
        不安全 id 抛 SessionLifecycleError → {"error"} 人话（s07-b 惯例）。
        """

        try:
            record = self.store.load(session_id)
        except (SessionNotFoundError, SessionLifecycleError) as exc:
            return {"error": str(exc)}
        return {"sessionId": session_id, "messages": record.messages}

    # ── API：发一轮（同步等 turn 完成）────────────────────────

    def send(self, session_id: str, text: str) -> dict:
        """发一轮：显式 sid 交给 shell.send_to；turn 内部异常翻译成人话。"""

        if not text.strip():
            return {"error": "空消息，没东西可发"}
        return self.shell.send_to(session_id, text)

    # ── API：事件轮询 / 审批回执 ─────────────────────────────

    def events(self, after: int = 0) -> dict:
        """一次性取事件（调试端点 /api/events 用；前端已改走 SSE）。"""

        events_list, latest = self.ring.after(after)
        return {"events": events_list, "latest": latest}

    def event_stream(self, last_id: int = 0, heartbeat: float = SSE_HEARTBEAT):
        """无限生成 SSE 帧：有事件就发事件，静默到点就发心跳。

        写成生成器（而不是在 handler 里写循环）是为了可测：喂一个假
        ring、喂一个短的 heartbeat，就能在单测里断言"第一帧是什么、
        什么时候结束"，完全不碰 HTTP。

        退出只有一条路：app.stopping 被竖起来。这是长连的收尾契约——
        main() 的 finally 里 stop() 会竖旗 + wake_all()，睡着的线程
        立刻醒来看到旗，不会卡住关服务。
        """

        cursor = max(0, last_id)
        while not self.stopping:
            items, _latest = self.ring.wait_for(cursor, heartbeat)
            if not items:
                yield SSE_HEARTBEAT_FRAME   # 静默期保活（也是探活）
                continue
            for item in items:
                cursor = item["seq"]
                yield sse_frame(item)

    def approve(self, ticket: str, approved: bool) -> dict:
        if self.board.respond(ticket, approved):
            return {"ok": True, "detail": "已回执"}
        return {"ok": False, "detail": "无效或已超时的审批单"}

    # ── API：文件系统浏览（选工作区的数据源）────────────────

    def list_fs(self, path: str = "") -> dict:
        """列一个目录的子目录，给前端的目录选择器当数据源。

        **为什么由后端来列**：浏览器拿不到本地路径（File 只有 name/size/type），
        所以"让用户点着选一个真实目录"这条路，只能由后端提供清单、前端照着
        画。这正是 Jupyter / code-server / Filebrowser 的做法——它们都不弹
        系统原生框，因为原生框要求服务进程能把窗口画到用户桌面上，而那条路
        在远程 / 容器 / 沙箱里必死（native_dialog 的注释记过实测）。

        **为什么只回子目录不回文件**：这个端点服务的是「选一个目录当沙箱」，
        文件在这一步是噪声，也省得把大目录整棵树搬给前端。

        不带 path = 起点态：返回盘符（Windows）或根目录，让用户先挑个盘。

        **安全口径**：响应只回目录名、不回文件内容；Host 校验（守卫中间件）
        挡住了 DNS rebinding 式的跨站读——"只听 127.0.0.1"本身不再是安全
        叙述的全部，Host 头才是那道闸。
        """

        target = (path or "").strip()
        if not target:
            return {"roots": _fs_roots()}

        try:
            resolved = Path(target).expanduser().resolve(strict=True)
        except (OSError, ValueError) as exc:
            return {"error": f"打不开这个目录：{exc}"}
        if not resolved.is_dir():
            return {"error": f"不是一个目录：{resolved}"}

        entries: list[dict] = []
        try:
            children = sorted(resolved.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError) as exc:
            return {"error": f"列不出这个目录的内容：{exc}"}
        for child in children:
            if not child.is_dir():
                continue                      # 只挑目录（选的是沙箱根）
            entries.append({"name": child.name})

        parent = resolved.parent
        # 盘符根（如 D:\）的 parent 是它自己——那就没有"上一级"可去，
        # 前端这时该给出"换一个盘"的入口。
        parent_out = None if parent == resolved else str(parent)
        return {"path": str(resolved), "parent": parent_out, "entries": entries}

    # ── API：工作区（上传目录）──────────────────────────────

    def workspace(self, sid: str = "") -> dict:
        """读当前工作区。壳不认识 workspace/get 时如实降级为"默认"。"""

        return self.shell.workspace(sid)

    def set_workspace(self, action: str, path: str = "",
                      filename: str = "", data: str = "",
                      sid: str = "") -> dict:
        """工作区操作：browse（弹系统选框）/ open（用给定路径）/ upload（上传
        zip）/ reset（回到默认工作区）。

        返回 {"ok": True, "detail", "workspace"} 或 {"ok": False, "detail"}
        ——与 action() 四操作同一个人话约定（前端只认 ok/detail）。
        browse 取消时额外带 "cancelled": True（取消不是错误，别弹红字）；
        机制不可用时额外带 "fallback": "manual"（前端降级为手输路径）。
        """

        if not sid:
            return {"ok": False, "detail": "没指定会话——先打开一个会话再换目录"}

        if action == "browse":
            # 弹系统选框这条路自己拿路径，走完再交给下面同一段收尾
            picked = self._browse_workspace(sid)
            if isinstance(picked, dict):      # 取消 / 降级：直接就是答复
                return picked
            result = self.shell.set_workspace("dir", picked, sid)
        elif action == "open":
            if not path.strip():
                return {"ok": False, "detail": "没给路径"}
            result = self.shell.set_workspace("dir", path.strip(), sid)
        elif action == "upload":
            result = self._upload_zip(filename, data, sid)
        elif action == "reset":
            # 空路径：复位不需要坐标，"默认"是 sidecar 的启动态（见 sidecar
            # 的 _handle_workspace_set）。浏览器拿不到项目根路径，这也正是
            # 必须由后端说"回哪儿"的原因。
            result = self.shell.set_workspace("default", "", sid)
        else:
            return {"ok": False, "detail": f"未知的工作区操作: {action}"}

        if "error" in result:
            return {"ok": False, "detail": result["error"]}
        workspace = result.get("workspace") or {}
        # detail 是给人看的一句话：root 是常态，但复位回的默认工作区可能
        # 压根不报 root（装配层没告知时只有 kind），别拼出"已切到 "这种半句。
        root = workspace.get("root") or ""
        if root:
            detail = f"工作区已切到 {root}"
        elif workspace.get("kind") == "default":
            detail = "已回到默认工作区"
        else:
            detail = "工作区已切换"
        return {"ok": True, "detail": detail, "workspace": workspace}

    def _browse_workspace(self, sid: str = "") -> "str | dict":
        """弹系统目录选择框，返回路径字符串；返回 dict 表示"这一趟到此为止"
        （用户取消 / 机制不可用要降级），那个 dict 就是最终答复。

        为什么要降级而不是报错：弹不出框的原因可能是"这台机器根本没有图形
        环境"（服务跑在服务器上/精简解释器）。那是**环境限制，不是用户做错
        了什么**——此时退回手输路径，功能还在。把它当错误弹红字，用户只会
        觉得"按钮坏了还没法绕"。

        全程打日志：服务端看不到屏幕，"点了按钮没反应"这种反馈，除了调用链
        本身没有别的证据来源。日志分三段（开始/子进程结果/最终去向），
        哪一段缺了就知道卡在哪。
        """

        # 起始目录：当前就在某个工作区里，就从那儿开始，省得每次从头翻
        current = (self.workspace(sid).get("workspace") or {}).get("root") or ""
        print(f"[web_app] browse 开始（起始目录 {current or '用户主目录'}）")
        try:
            picked = native_dialog.pick_directory(
                initial=current if Path(current).is_dir() else "")
        except native_dialog.DialogUnavailable as exc:
            print(f"[web_app] browse 降级为手输：{exc}")
            return {"ok": False, "fallback": "manual",
                    "detail": f"这台机器弹不出系统选框（{exc}），改成手输路径吧"}
        except TimeoutError as exc:
            print(f"[web_app] browse 超时：{exc}")
            return {"ok": False, "detail": str(exc)}

        if not picked:
            # 取消不是错误：前端不该弹红字，也不该改任何状态
            print("[web_app] browse 用户取消（没选任何目录）")
            return {"ok": False, "cancelled": True, "detail": "已取消选择"}

        print(f"[web_app] browse 选中 → {picked}")
        return picked

    def _upload_zip(self, filename: str, data_b64: str, sid: str = "") -> dict:
        """把浏览器上传的 zip 落成临时文件 → 交 sidecar 解压 → 删临时文件。

        为什么走 base64 JSON 而不是 multipart/form-data：multipart 要手写
        边界解析（一个很容易写出漏洞、且很难读的活）。本项目宁可用 33% 的
        编码膨胀换一份能一眼读完的解析代码——教学边界在这儿，不在省流量。

        临时文件放**系统 temp**、解开后立刻删：上传的包只是运输工具，
        留痕的是解压出来的工作区（workspaces/<id>/）。子进程读得它——同一
        文件系统，与父进程谁写谁读无关。

        三道校验都在"花力气之前"：base64 合法 → 非空 → 大小合适。
        """

        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            return {"error": "上传内容不是合法的 base64"}
        if not raw:
            return {"error": "上传的压缩包是空的"}
        if len(raw) > MAX_UPLOAD_BYTES:
            return {"error": (f"压缩包过大（{len(raw)} 字节，"
                              f"上限 {MAX_UPLOAD_BYTES}）——"
                              f"只打包需要模型处理的部分")}

        suffix = Path(filename or "upload.zip").suffix or ".zip"
        descriptor, temp_name = tempfile.mkstemp(prefix="wywd-upload-",
                                                suffix=suffix)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as sink:
                sink.write(raw)
            return self.shell.set_workspace("zip", str(temp_path), sid)
        finally:
            # 解压由 sidecar 在 RPC 内完成：返回即意味着包已经不需要了。
            temp_path.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
# 守门员 — 纯 ASGI 中间件 + 纯函数决定（决定逻辑可脱离 HTTP 单测）
# ═══════════════════════════════════════════════════════════════

_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class _ScopeHeaders:
    """ASGI scope 头的只读视图：.get(name) 大小写不敏感。

    守卫决定函数要的只是"按名取头"，自己造一个视图而不是 import
    starlette 的 Headers——决定函数因此不绑任何框架类型。
    """

    def __init__(self, scope: dict) -> None:
        self._raw = {k.decode("latin-1").lower(): v.decode("latin-1")
                     for k, v in scope.get("headers") or []}

    def get(self, name: str) -> str:
        return self._raw.get(name.lower(), "")


def _host_hostname(host_header: str) -> str:
    """Host 头 → 纯主机名：去端口；IPv6 的 [::1]:8765 去方括号。"""

    host = (host_header or "").strip()
    if host.startswith("["):
        return host[1:host.index("]")] if "]" in host else host
    return host.rsplit(":", 1)[0]


def _guard_rejection(method: str, path: str, headers: Any) -> Optional[tuple[int, dict]]:
    """每个请求过一遍的守门员。返回 None = 放行；(状态码, 响应体) = 拒。

    纯函数（只读 method/path/headers/环境变量，零副作用），所以三道闸
    全部能脱离 HTTP 单测。**都发生在碰到任何业务逻辑之前**。

      1. Host 校验（杀 DNS rebinding）。本服务的一切安全叙述都建立在
         "只有本机能访问"上，但"只听 127.0.0.1"防不了浏览器这一侧：恶意
         网页把自己的域名 rebind 到 127.0.0.1，请求对浏览器就是**同源**的
         （读响应、带凭证全畅通）。唯一的拦法是服务端验 Host 头——rebind
         攻击的 Host 是攻击者的域名，不是 127.0.0.1。
      2. X-Wywd-Ui（杀 CSRF）。跨站"简单请求"（text/plain 表单）发不出
         自定义头、也**读不到**响应，但副作用照样发生。所以所有 /api POST
         都要这个头（2026-09-25 从"只查 browse"扩到全量——发消息、审批
         回执、删会话，一个都不该被恶意页触发）。自定义头必须先过 CORS
         预检，而本服务不答预检，简单请求带不上它。
      3. WYWD_API_TOKEN（可选）。给 curl / 脚本 / 局域网场景用的令牌：
         设了就要求 POST 带 x-wywd-token。不开 = 维持纯本机体验。
      4. Content-Length 上限（杀内存打爆）。客户端声称的长度超限就直接
         413，**不读 body**——先读再判等于把内存交给客户端控制。中间件
         根本不调 receive，这条"早退"由结构保证，不靠自觉。
    """

    if _host_hostname(headers.get("host") or "") not in _LOCAL_HOSTNAMES:
        return 403, {"error": "拒绝：Host 不是本机回环地址"}
    if method == "POST" and path.startswith("/api/"):
        if (headers.get("x-wywd-ui") or "").strip() != "1":
            return 403, {"ok": False, "detail": "拒绝：缺少界面标识头"}
        token = (os.environ.get("WYWD_API_TOKEN") or "").strip()
        if token and (headers.get("x-wywd-token") or "") != token:
            return 401, {"ok": False, "detail": "拒绝：令牌不匹配"}
        claimed = headers.get("content-length")
        if claimed:
            try:
                length = int(claimed)
            except ValueError:
                return 400, {"error": "Content-Length 不是数字"}
            if length > MAX_BODY_BYTES:
                return 413, {"error": f"请求体过大（上限 {MAX_BODY_BYTES} 字节）"}
    return None


class _GuardMiddleware:
    """纯 ASGI 守门员。

    为什么不用 BaseHTTPMiddleware（@app.middleware 那层糖）：它给每个
    响应套一道中转队列，SSE 长连最忌讳多一层缓冲；纯 ASGI 直通。
    413 的"早退"在这里也天然成立——拒绝路径根本不会去 receive body。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            rejection = _guard_rejection(scope["method"], scope["path"],
                                         _ScopeHeaders(scope))
            if rejection is not None:
                code, payload = rejection
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers = [(b"content-type", b"application/json; charset=utf-8")]
                if code == 413:
                    # 声称超限的 body 还在连接里：让连接跟着关掉，残留字节
                    # 不许被当成下一条请求的帧头。
                    headers.append((b"connection", b"close"))
                await send({"type": "http.response.start", "status": code,
                            "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _fs_roots() -> list[str]:
    r"""起点态给用户挑的"顶层入口"：Windows 是各盘符，其他平台是 /。

    为什么需要这一步：Windows 上 D:\ 的 parent 还是 D:\，没有"再往上"。
    不给盘符入口，用户就永远到不了另一块盘上他的项目目录。
    只列**存在**的盘：光驱/不存在的盘符列出来点了也没内容。
    """

    if os.name != "nt":
        return ["/"]
    roots: list[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            roots.append(drive)
    return roots


# ═══════════════════════════════════════════════════════════════
# FastAPI 装配 — 路由 /api/* 到 WebApp，其余从 public/ 服务静态文件
# ═══════════════════════════════════════════════════════════════

class _NoStoreStaticFiles(StaticFiles):
    """开发期禁缓存：改了前端文件必须立刻生效，否则"后端已经修好了、
    你还看到旧界面"的白折腾。本地开发服务，禁缓存的代价是零。"""

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


async def _lifespan(app: FastAPI):
    """装配 / 收尾（旧 main() 的 try/finally 搬到这里）。

    web 是 create_app 注入的（测试 / 冒烟脚本）：生命周期归注入方管，
    这里只保证收尾时叫醒 SSE 线程；web 为空（生产）：这里造、这里启、
    这里收——start 失败抛出让 uvicorn 起不来，别留半个服务。
    """

    web = getattr(app.state, "web", None)
    if web is None:
        web = WebApp()
        app.state.web = web
        print("[web_app] sidecar 启动中…")
        pong = web.start()
        print(f"[web_app] sidecar/ping → {pong.get('status')}")
        print("[web_app] 会话懒建：启动不建会话，首次发消息 / 点 ＋ 才建")
        try:
            yield
        finally:
            web.stop()
            print("[web_app] 已退出")
    else:
        try:
            yield
        finally:
            web.ring.wake_all()   # 别让 SSE 线程把 15 秒心跳睡完才走


def create_app(web: Optional[WebApp] = None) -> FastAPI:
    """装配 FastAPI 应用。web 缺省 = 生产（lifespan 里造+启+收）；
    注入 web = 测试/冒烟（不碰生命周期）。

    **端点全部是同步 `def`**：领域层全是阻塞调用（_rpc_lock、socket
    recv），FastAPI 会把同步端点丢进线程池——这正是旧 ThreadingHTTPServer
    的并发形态。别把它们改成 async def：那会把事件循环卡死。
    """

    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)   # 不暴露 /docs：本服务不对公网开放
    app.state.web = web
    app.add_middleware(_GuardMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """兜底：任何穿透到 HTTP 层的异常都回 JSON 人话。

        旧版没这层：sidecar 半路死掉、RPC 抛 ConnectionClosed，连接被
        空响应关掉，浏览器只看到 "Failed to fetch"——用户以为整个后端
        没起。回 500 + 人话至少让"坏了"可见。
        """

        return JSONResponse({"error": f"服务内部错误：{exc!r}"}, status_code=500)

    async def _json_body(request: Request) -> dict:
        """宽容 JSON 解析（旧 _read_json 的语义）：坏体/非对象一律 {}。

        解析失败不回 422 而是给空 dict，让"未知操作"这类**领域错误**去接
        ——错误形状是前端已经接住的 ok/detail 人话，不该因换运输层变化。
        """

        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_web(request: Request) -> WebApp:
        web = request.app.state.web
        # sidecar 死了就地换一个（原 do_GET / do_POST 开头那一步，收进依赖
        # = 一次声明、所有 /api 端点生效，不再每个 handler 手写一遍）。
        web.revive_shell()
        return web

    # ── GET ─────────────────────────────────────────────────

    @app.get("/api/sessions")
    def get_sessions(web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.sessions())

    @app.get("/api/sessions/{sid}/messages")
    def get_messages(sid: str, web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.messages(sid))

    @app.get("/api/status")
    def get_status(web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.status())

    @app.get("/api/logs")
    def get_logs(web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.logs())

    @app.get("/api/events")
    def get_events(after: str = "",
                   web: WebApp = Depends(get_web)) -> JSONResponse:
        try:
            cursor = int(after)
        except (TypeError, ValueError):
            cursor = 0
        return JSONResponse(web.events(cursor))

    @app.get("/api/events/stream")
    def stream_events(request: Request, after: str = "",
                      web: WebApp = Depends(get_web)) -> StreamingResponse:
        """SSE 长连：协议与旧实现一字未变（帧格式 / Last-Event-ID 续传 /
        after=latest / 心跳），只是外层从手写循环换成 StreamingResponse。

        续传游标优先取 Last-Event-ID 头：浏览器断线重连时自己带上来的
        上一帧 id——前端一行游标代码都不用写。首次连接没有这个头，才看
        ?after=：给数字就从头补，给 "latest" 就顶到当下。
        """

        raw = request.headers.get("last-event-id") or after
        if raw == "latest":
            last_id = web.ring.latest()
        else:
            try:
                last_id = int(raw)
            except (TypeError, ValueError):
                last_id = 0
        return StreamingResponse(
            web.event_stream(last_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # 反向代理缓冲会毁掉 SSE；本地用不上，写上省得接 nginx 时踩
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/workspace")
    def get_workspace(sid: str = "",
                      web: WebApp = Depends(get_web)) -> JSONResponse:
        # sid 走查询串：工作区是**会话级**的，读谁的要说明白
        return JSONResponse(web.workspace(sid))

    @app.get("/api/fs/list")
    def get_fs_list(path: str = "",
                    web: WebApp = Depends(get_web)) -> JSONResponse:
        # 工作区选择器的数据源：列一个目录的子目录（纯读，无副作用）
        return JSONResponse(web.list_fs(path))

    @app.get("/api/{rest:path}")
    def api_get_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    # ── POST ────────────────────────────────────────────────
    # X-Wywd-Ui 校验不在端点里——守卫中间件对**所有** /api POST 生效
    # （见 _guard_rejection 第 2 道闸）。

    @app.post("/api/sessions")
    def post_sessions(payload: dict = Depends(_json_body),
                      web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.action(payload.get("action", ""),
                                       payload.get("sid", "")))

    @app.post("/api/sessions/{sid}/messages")
    def post_message(sid: str, payload: dict = Depends(_json_body),
                     web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.send(sid, payload.get("message", "")))

    @app.post("/api/approvals/{ticket}")
    def post_approval(ticket: str, payload: dict = Depends(_json_body),
                      web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.approve(ticket, payload.get("approved")))

    @app.post("/api/workspace")
    def post_workspace(payload: dict = Depends(_json_body),
                       web: WebApp = Depends(get_web)) -> JSONResponse:
        return JSONResponse(web.set_workspace(
            payload.get("action", ""), payload.get("path", ""),
            payload.get("filename", ""), payload.get("data", ""),
            payload.get("sid", "")))

    @app.post("/api/{rest:path}")
    def api_post_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    # 静态文件挂在最后：/api/* 的具体路由与 catch-all 都排在它前面，
    # 路由按注册顺序匹配。目录穿越防护 StaticFiles 自带（realpath + 亲缘）。
    app.mount("/", _NoStoreStaticFiles(directory=PUBLIC_DIR, html=True),
              name="static")
    return app


# ═══════════════════════════════════════════════════════════════
# main — 起服务（Windows spawn 守则：重活全在 __main__ 里）
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    # workers 必须是 1：WebApp / sidecar 是进程内单例，多 worker = 多个
    # sidecar 抢同一份 .sessions/（本文件不提供 workers 入口，uvicorn
    # 命令行上也不要开）。uvicorn 延迟导入：测试只 import 本模块拿
    # create_app / 守卫纯函数，不需要装 uvicorn。
    import uvicorn

    print(f"[web_app] 服务即将启动：http://{HOST}:{PORT}（Ctrl+C 退出）")
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
