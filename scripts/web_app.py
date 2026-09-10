"""s07-c / C 方案 web_app：自定义前端，彻底解耦 chainlit。

为什么存在这个文件：chainlit 的聊天区是它自己的 WS + React 状态，"会话
切换 = 服务端往聊天区推历史"只能靠 contextvars 快照跨线程搬运，效果仍
不可靠（重放 fire-and-forget、失败全静默）。C 方案换标准做法——
"会话是记录，聊天区是视图，切换 = 换 id + 读历史 + 自己渲染"。

   浏览器 ──fetch（同源 127.0.0.1:8765）──► web_app（本文件）
                                            ├─ 1 个 SidecarShell（1 个 sidecar 子进程，独占写者）
                                            ├─ 静态文件服务（public/：index.html + app.js）
                                            └─ JSON API + 事件轮询 + 审批登记

关键架构决定：单壳单 sidecar。多 tab 只是同一个 backend 的多个浏览器
视图——"每 tab 一个 sidecar 共享 .sessions/"时代的竞态（启动清账误伤、
跨进程 resume 双运行时、计数器竞态）被架构性消灭，不是修好。

三大机制（对照 chainlit 版各自的病灶）：
  1. 历史 = 纯读：GET /api/sessions/<sid>/messages 直接
     JsonlSessionStore(root).load(sid).messages（replay fold），不经过
     sidecar RPC、不碰运行中的 turn、closed 会话秒开。零静默失败：
     成功 = 前端自己拿到数组并渲染，没有"桥有没有跑"这种问题。
  2. 直播/审批 = 事件环（线程安全 deque + seq）+ threading.Event：
     on_event 回调（sidecar 事件）+ user_prompt 回调（审批请求）都往
     同一条流里塞；前端轮询 GET /api/events?after=N。审批不用 asyncio
     Future——user_prompt 线程 Event.wait(300)，HTTP 线程 POST 回执
     set()。"loop 已死 / 上下文过期"这两个失败模式从根上不存在。
  3. 发消息带显式 sid：Single 壳下不能靠"当前会话指针"（多 tab 互相
     抢），shell.send_to(sid, msg)，见 shell.py 的 send_to。

运行：python scripts/web_app.py → 浏览器开 http://127.0.0.1:8765
（离线可用：不设 DEEPSEEK_API_KEY 时 choose_model 给 FakeModel）。
"""

import json
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# 项目根进 sys.path：保证 src.* / scripts.* 可导入（照 shell.py 的模式）
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.shell import SidecarShell          # noqa: E402
from src.harness.jsonl_store import JsonlSessionStore  # noqa: E402
from src.harness.session import (              # noqa: E402
    SessionLifecycleError,
    SessionNotFoundError,
)

HOST = "127.0.0.1"
PORT = 8765
PUBLIC_DIR = Path(_PROJECT_ROOT) / "public"
SESSIONS_ROOT = Path(_PROJECT_ROOT) / ".sessions"
# 审批超时秒数：沉默 = 拒绝（fail-closed，与 chainlit 版语义一致）
APPROVAL_TIMEOUT = 300
EVENT_RING_MAX = 500

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ═══════════════════════════════════════════════════════════════
# EventRing — 线程安全的事件环（直播 + 审批请求共用一条流）
# ═══════════════════════════════════════════════════════════════

class EventRing:
    """有界事件环：on_event 回调 / 审批登记都往里塞，前端按 seq 轮询取。

    写=多线程（sidecar RPC 线程 + HTTP handler 线程），读=HTTP handler
    线程，所以 append / after 都过锁。超过 maxlen 丢最旧的（直播缺角可
    接受——历史永远以 messages API 为准，这是写进边界的教学取舍）。
    """

    def __init__(self, maxlen: int = EVENT_RING_MAX) -> None:
        self._deque: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, event: str, data: dict) -> int:
        """追加一条事件并返回它的 seq。data 会被并入条目（event 名独占键）。"""

        with self._lock:
            self._seq += 1
            self._deque.append({"seq": self._seq, "event": event, **data})
            return self._seq

    def after(self, after: int = 0) -> tuple[list[dict], int]:
        """seq > after 的事件（顺序保持）；(事件列表, 当前最大 seq)。"""

        with self._lock:
            return [item for item in self._deque if item["seq"] > after], self._seq


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
                 approval_timeout: float = APPROVAL_TIMEOUT) -> None:
        self.ring = EventRing()
        self.board = ApprovalBoard(self.ring, approval_timeout)
        self.store = JsonlSessionStore(store_root or SESSIONS_ROOT)
        if shell is not None:
            self.shell = shell
        else:
            self.shell = SidecarShell(
                user_prompt=self.board.request,
                on_event=self._record_event,
            )

    def _record_event(self, data: dict) -> None:
        """SidecarShell.on_event 包装：data["event"] 是事件名，其余是载荷。"""

        name = data.get("event", "event")
        payload = {k: v for k, v in data.items() if k != "event"}
        self.ring.append(name, payload)

    def start(self) -> dict:
        """启动 sidecar 子进程并建首个会话（main() 在 serve_forever 前调用）。"""

        return self.shell.start()

    def stop(self) -> None:
        """干净收尾（幂等）：shell.stop() 全套（shutdown/EOF/join/terminate）。"""

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
            new_sid = shell.new_session()   # 关旧开新，旧记录保留
            return {"ok": True, "detail": f"已新建 {new_sid}",
                    "sessionId": new_sid}
        if action == "close":
            result = shell.close_session(sid)
            if "error" in result:
                return {"ok": False, "detail": result["error"]}
            return {"ok": True,
                    "detail": f"已关闭 {result.get('closed', sid)}（记录保留）"}
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
        events_list, latest = self.ring.after(after)
        return {"events": events_list, "latest": latest}

    def approve(self, ticket: str, approved: bool) -> dict:
        if self.board.respond(ticket, approved):
            return {"ok": True, "detail": "已回执"}
        return {"ok": False, "detail": "无效或已超时的审批单"}


# ═══════════════════════════════════════════════════════════════
# HTTP 层 — 路由 /api/* 到 WebApp，其余从 public/ 服务静态文件
# ═══════════════════════════════════════════════════════════════

def _static_path(rel: str) -> Optional[Path]:
    """把 URL 相对路径解析到 public/ 下的真实文件；目录穿越 / 不存在回 None。

    rel 是净化后的相对路径（已去 query/hash，以 / 开头）。目录 → 目录内
    index.html（/ 和裸路径都落到首页）。
    """

    rel = rel.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel = rel + "index.html"   # 裸路径 / 目录 → 目录内 index.html
    abs_root = PUBLIC_DIR.resolve()
    target = (abs_root / rel).resolve()
    if not target.is_relative_to(abs_root):
        return None          # 目录穿越：拒绝
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return None
    return target


class _Handler(BaseHTTPRequestHandler):
    """极简 JSON API + 静态文件。app 是 main() 里装配好的 WebApp 单例。"""

    app: Optional[WebApp] = None

    def log_message(self, *args: Any) -> None:
        pass  # 静默：前端 5s 轮询别刷日志

    # ── 输出工具 ────────────────────────────────────────────

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(path.suffix.lower(),
                                                   "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── GET ─────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        app = self.app
        if app is None:
            self._send_json(500, {"error": "web_app 未初始化"})
            return
        if path == "/api/sessions":
            self._send_json(200, app.sessions())
        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            sid = path[len("/api/sessions/"):-len("/messages")]
            self._send_json(200, app.messages(sid))
        elif path == "/api/events":
            try:
                after = int((query.get("after") or ["0"])[0])
            except ValueError:
                after = 0
            self._send_json(200, app.events(after))
        elif path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
        else:
            target = _static_path(parsed.path)
            if target is None:
                self.send_response(404)
                self.end_headers()
            else:
                self._serve_file(target)

    # ── POST ────────────────────────────────────────────────

    def _read_json(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        app = self.app
        if app is None:
            self._send_json(500, {"error": "web_app 未初始化"})
            return
        body = self._read_json()
        if path == "/api/sessions":
            self._send_json(200, app.action(body.get("action", ""),
                                            body.get("sid", "")))
        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            sid = path[len("/api/sessions/"):-len("/messages")]
            self._send_json(200, app.send(sid, body.get("message", "")))
        elif path.startswith("/api/approvals/"):
            ticket = path[len("/api/approvals/"):]
            self._send_json(200, app.approve(ticket, body.get("approved")))
        else:
            self._send_json(404, {"error": "not found"})


# ═══════════════════════════════════════════════════════════════
# main — 装配 + 起服务（Windows spawn 守则：重活全在 __main__ 里）
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    app = WebApp()
    _Handler.app = app
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"[web_app] sidecar 启动中…")
    try:
        pong = app.start()
        print(f"[web_app] sidecar/ping → {pong.get('status')}")
        print(f"[web_app] 首个会话 → {app.shell.session_id}")
        print(f"[web_app] 服务已就绪：http://{HOST}:{PORT}（Ctrl+C 退出）")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web_app] 正在收尾…")
    finally:
        server.server_close()
        app.stop()
        print("[web_app] 已退出")


if __name__ == "__main__":
    main()