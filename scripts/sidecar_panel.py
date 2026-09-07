"""s07-b 网页会话面板：进程内 HTTP 小服务——custom_js 侧边栏的后端。

为什么存在这个文件：Chainlit 没有官方"自定义侧边栏"API，但 [UI] custom_js
允许注入脚本。侧边栏要拉取/操作 sidecar 会话，必须触达跑在 chainlit 进程
里的 SidecarShell（会话存在 user_session / SidecarShell 内部），所以起一个
本机 HTTP 服务（纯 stdlib，零新依赖），前端 JS 用 fetch 跟它对话：

    浏览器页面 ──fetch──► 127.0.0.1:8765（本模块，chainlit 进程内）
    ◄───────────────────     ├─ GET  /api/sessions   → 会话清单
                              └─ POST /api/sessions   → create/resume/close/forget

架构约定：
  - _SHELLS 是进程级注册表：session_id（cl.user_session 的 id）→ SidecarShell。
    chainlit_app.py 在 _ensure_shell 注册、on_chat_end 注销。
  - 只绑 127.0.0.1 + 无鉴权：教学/本机工具口径，别暴露到局域网。
  - 多 tab：HTTP 请求不带 thread 时就作用于"最近注册的壳"（_default_thread），
    多窗口混用时的已知边界，注释写清楚即可。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# 注册表：屏幕另一边的 SidecarShell 都在这里
_shells: dict[str, Any] = {}
_shells_lock = threading.Lock()
_default_thread: Optional[str] = None  # 最近注册的 thread（多 tab 兜底靶子）

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()

HOST = "127.0.0.1"
PORT = 8765


# ── 注册表操作（chainlit_app 调用）──────────────────────────────

def register(thread_id: str, shell: Any) -> None:
    """登记一个在线壳；同时把它立为默认靶子（最近活跃的 tab）。"""

    global _default_thread
    with _shells_lock:
        _shells[thread_id] = shell
        # 多 tab 界面的简化约定：最后注册的壳 = 面板操作的默认对象。
        # 前端不带 thread 参数时走它——用完即走的教学口径。
        _default_thread = thread_id


def unregister(thread_id: str) -> None:
    """注销一个壳（on_chat_end / 壳死时）。"""

    global _default_thread
    with _shells_lock:
        _shells.pop(thread_id, None)
        if _default_thread == thread_id:
            _default_thread = next(reversed(_shells), None) \
                if _shells else None


def ensure_server() -> None:
    """幂等启动面板服务（daemon 线程，进程退出自动收）。"""

    global _server
    with _server_lock:
        if _server is not None:
            return
        _server = ThreadingHTTPServer((HOST, PORT), _Handler)
        threading.Thread(target=_server.serve_forever, daemon=True).start()


# ── 会话清单/操作（HTTP 侧）───────────────────────────────────

def _target_shell(thread: Optional[str]) -> Any:
    """按 thread 取壳；没指定就用默认靶子。存在性由调用方兜底。"""

    with _shells_lock:
        if thread and thread in _shells:
            return _shells[thread]
        if _default_thread is not None:
            return _shells.get(_default_thread)
        return None


def _collect_sessions(thread: Optional[str]) -> list[dict]:
    """把所有在线壳的会话清单聚合成一张表（侧边栏渲染用）。

    每个壳的 sidecar 是独立 store（各 tab 历史互不可见），这里按 thread
    标注来源，前端按需展示；单 tab 场景只有一行来源，不碍事。
    """

    with _shells_lock:
        snapshot = dict(_shells)  # 锁内快照，handler 线程间安全
    sessions: list[dict] = []
    for tid, shell in snapshot.items():
        try:
            rows = shell.sessions().get("sessions", [])
            for row in rows:
                row["thread"] = tid
                row["current"] = (tid == _default_thread
                                  and row.get("live", False)
                                  and row.get("id") == shell.session_id)
            sessions.extend(rows)
        except Exception as exc:  # 壳刚死/连接断：整行标注来源错误，别炸请求
            sessions.append({"thread": tid, "error": str(exc)})
    return sessions


def _run_action(action: str, sid: str, thread: Optional[str]) -> dict:
    """把一次面板操作翻译成 shell 调用；翻译约定与终端/命令一致。"""

    shell = _target_shell(thread)
    if shell is None:
        return {"ok": False, "detail": "没有可操作的会话（先打开一个聊天页）"}
    try:
        if action == "create":
            new_sid = shell.new_session()   # 关旧开新，旧记录保留
            return {"ok": True, "detail": f"已新建 {new_sid}"}
        if action == "close":
            result = shell.close_session(sid)   # 不传 sid = 当前会话
            if "error" in result:
                return {"ok": False, "detail": result["error"]}
            return {"ok": True, "detail": f"已关闭 {result.get('closed', sid)}（记录保留）"}
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
    except Exception as exc:  # RPC 层兜底（连接断之类），翻译成人话
        return {"ok": False, "detail": str(exc)}


# ── HTTP handler ─────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    """极简 JSON API：GET 清单 / POST 操作。CORS 放行给本机页面。"""

    def log_message(self, *args: Any) -> None:
        pass  # 静默：面板轮询别刷日志

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:   # POST 带 JSON 头会先有 preflight
        self._send_json(204, {})

    def do_GET(self) -> None:
        thread = self._query_param("thread")
        if self.path.startswith("/api/sessions"):
            self._send_json(200, {"sessions": _collect_sessions(thread)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            raw = self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0)
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "detail": "请求体不是合法 JSON"})
            return
        if not self.path.startswith("/api/sessions"):
            self._send_json(404, {"ok": False, "detail": "not found"})
            return
        result = _run_action(
            body.get("action", ""), body.get("sid", ""), body.get("thread"))
        self._send_json(200, result)

    def _query_param(self, name: str) -> Optional[str]:
        """从 URL query 里取一个参数（没有/不带 ? 都返回 None）。"""

        if "?" not in self.path:
            return None
        query = self.path.split("?", 1)[1]
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == name:
                return value
        return None