"""s07-b 网页会话面板：进程内 HTTP 小服务——custom_js 侧边栏的后端。

为什么存在这个文件：Chainlit 没有官方"自定义侧边栏"API，但 [UI] custom_js
允许注入脚本。侧边栏要拉取/操作 sidecar 会话，必须触达跑在 chainlit 进程
里的 SidecarShell（会话存在 user_session / SidecarShell 内部），所以起一个
本机 HTTP 服务（纯 stdlib，零新依赖），前端 JS 用 fetch 跟它对话：

    浏览器页面 ──fetch──► 127.0.0.1:8765（本模块，chainlit 进程内）
    ◄───────────────────     ├─ GET  /api/sessions   → 会话清单
                              └─ POST /api/sessions   → create/resume/close/forget

    chainlit_app ──register_bridge──► _bridges[thread]：resume/create 成功后
    回调对应页面——清空聊天区并重放该会话历史（"点会话，主聊天区直接
    变成那个会话的对话"）。桥跑在 HTTP 线程里没有 ws 上下文，所以它
    自带 loop + 捕获的 contextvars.Context 过桥（见 chainlit_app）。

架构约定：
  - _SHELLS 是进程级注册表：session_id（cl.user_session 的 id）→ SidecarShell。
    chainlit_app.py 在 _ensure_shell 注册、on_chat_end 注销。
  - 只绑 127.0.0.1 + 无鉴权：教学/本机工具口径，别暴露到局域网。
  - 多 tab：HTTP 请求不带 thread 时就作用于"最近注册的壳"（_default_thread），
    多窗口混用时的已知边界，注释写清楚即可。

⚠️ s09 共享证据目录的多进程边界（.sessions/ 被所有 sidecar 进程共享）：
  - 去重：_collect_sessions 按 sid 去重（每个壳看到的是同一份全量清单）。
  - 清账误伤：新 sidecar 启动清账会把**别的 tab 正在用的** idle 会话在
    证据里抹成 closed——对方运行时还活着，下次 save 会写回，证据来回翻；
    侧边栏上表现为状态短暂显示 closed（装饰性问题）。
  - 双运行时：resume 只查本进程的 _runtimes——B tab 可以 resume A tab
    正在用的会话，两个运行时写同一份证据（append 交错，前缀检查可能
    报警）。根治需要跨进程文件锁（教学边界，单 tab 使用无此问题）。
  - 计数器竞态：两个 tab 的 sidecar 在同一瞬间启动，可能都从 store 摸到
    同一个最大编号 → 第二个 "session already exists" 启动失败，重开即可。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# 注册表：屏幕另一边的 SidecarShell 都在这里（值 = 壳 + 最后活跃时间）
_shells: dict[str, tuple[Any, float]] = {}
_shells_lock = threading.Lock()
_default_thread: Optional[str] = None  # 最近注册的 thread（多 tab 兜底靶子）

# 聊天桥注册表：thread_id → bridge(action, sid, detail)。
# chainlit_app 在 _ensure_shell 里登记；面板的 resume/create 成功后回调，
# 把"清空聊天区 + 重放该会话历史"送进对应页面。桥失败静默（呈现层，
# 不配拖垮会话操作本身）——详见 chainlit_app._make_chat_bridge。
_bridges: dict[str, Any] = {}

# 刷新不再杀壳 —— 壳由超时收割兜底：页面真关了，30 分钟没人用就停掉
IDLE_REAP_SECONDS = 30 * 60

_server: Optional[ThreadingHTTPServer] = None
_server_lock = threading.Lock()

HOST = "127.0.0.1"
PORT = 8765


# ── 注册表操作（chainlit_app 调用）──────────────────────────────

def register(thread_id: str, shell: Any) -> None:
    """登记一个在线壳（幂等：重复登记只刷新活跃时间）；立为默认靶子。"""

    global _default_thread
    with _shells_lock:
        _shells[thread_id] = (shell, time.time())
        # 多 tab 界面的简化约定：最后注册的壳 = 面板操作的默认对象。
        # 前端不带 thread 参数时走它——用完即走的教学口径。
        _default_thread = thread_id


def register_bridge(thread_id: str, bridge: Any) -> None:
    """登记一个页面的聊天桥（幂等覆盖；随壳的注销/收割一起清理）。"""

    with _shells_lock:
        _bridges[thread_id] = bridge


def touch(thread_id: str) -> None:
    """刷新一个壳的活跃时间（on_message 每次到达都摸一下）。"""

    with _shells_lock:
        entry = _shells.get(thread_id)
        if entry is not None:
            _shells[thread_id] = (entry[0], time.time())


def unregister(thread_id: str) -> None:
    """注销一个壳（显式收尾/测试用；常驻壳的清理走 reap_idle）。"""

    global _default_thread
    with _shells_lock:
        _shells.pop(thread_id, None)
        _bridges.pop(thread_id, None)   # 桥随壳一起走，别留对着死页面的嘴
        if _default_thread == thread_id:
            _default_thread = next(reversed(_shells), None) \
                if _shells else None


def reap_idle(stale_seconds: int = IDLE_REAP_SECONDS) -> int:
    """停掉超时没活跃的壳（页面关了但 on_chat_end 没杀壳的收尾）。

    锁内只摘表和记受害者，stop 放到锁外（stop 会 join 子进程，别卡住
    HTTP handler 线程）。默认靶子若被收割，指到剩下的活壳。
    """

    global _default_thread
    now = time.time()
    victims: list[tuple[str, Any]] = []
    with _shells_lock:
        for tid in list(_shells):
            shell, last = _shells[tid]
            if now - last > stale_seconds:
                victims.append((tid, shell))
                del _shells[tid]
                _bridges.pop(tid, None)  # 桥跟壳同生共死
        if _default_thread in {tid for tid, _ in victims}:
            _default_thread = next(reversed(_shells), None) if _shells else None
    for _, shell in victims:
        try:
            shell.stop()
        except Exception:
            pass  # 收尾失败就算了，进程本身会随解释器退出
    return len(victims)


def stop_all() -> int:
    """停掉全部在线壳（chainlit 服务退出时兜底：常驻壳打不过父进程退出）。"""

    with _shells_lock:
        victims = list(_shells.values())
        _shells.clear()
        _bridges.clear()
    for shell, _ in victims:
        try:
            shell.stop()
        except Exception:
            pass
    return len(victims)


def ensure_server() -> None:
    """幂等启动面板服务（daemon 线程，进程退出自动收）。

    端口被占（残留进程/别的服务）时**降级不炸主程序**：chainlit 照常起，
    只是侧边栏拉不到数据（前端会显示"面板后端没起来"）——面板是装饰，
    不配当单点故障。教学口径下 8765 固定，不做端口协商。
    """

    global _server
    with _server_lock:
        if _server is not None:
            return
        try:
            _server = ThreadingHTTPServer((HOST, PORT), _Handler)
        except OSError as exc:
            print(f"[sidecar_panel] 面板服务启动失败（{HOST}:{PORT} 被占用？"
                  f"{exc}）——侧边栏不可用，聊天不受影响")
            return
        threading.Thread(target=_server.serve_forever, daemon=True).start()


# ── 会话清单/操作（HTTP 侧）───────────────────────────────────

def _target_shell(thread: Optional[str]) -> Any:
    """按 thread 取壳；没指定就用默认靶子。存在性由调用方兜底。"""

    with _shells_lock:
        if thread and thread in _shells:
            return _shells[thread][0]    # 注册表值是 (shell, last_used)
        if _default_thread is not None:
            entry = _shells.get(_default_thread)
            return entry[0] if entry else None
        return None


def _collect_sessions(thread: Optional[str]) -> list[dict]:
    """把在线壳的会话清单聚合成一张表（侧边栏渲染用）。

    s09 之后所有 sidecar 进程共享同一个 .sessions/ 证据目录——每个壳的
    session/list 返回的都是**同一份**全量清单。所以这里按 sid 去重：
    N 个 tab 在线 ≠ 清单重复 N 遍。

    thread 参数 = "请求者视角"（前端每次都带自己页面的线程 id）：current /
    own 一律按它判定，而不是全局 _default_thread——多 tab 下每个页面只看
    自己的当前会话、只标记自己的可操作行，互不串台。

    live 聚合（s07-c 修复）：live 是"sid 在**本进程** _runtimes 里"的
    进程本地事实——别页正在跑的会话，本页壳看它是 live=False。去重时
    若只留本页视角，别页的活会话会被当成 closed 显示，还会放出
    resume/forget 按钮（跨进程双运行时的误伤源头）。所以聚合规则：
    任何一个壳报告 live=True，合并行就是 live=True，liveThread 记下
    运行时住在哪个 tab——前端据此把"活的但不是我的当前"标成只读。
    """

    owner = thread or _default_thread  # 没带就退回"最近活跃"的兜底
    with _shells_lock:
        snapshot = dict(_shells)  # 锁内快照，handler 线程间安全
    sessions: list[dict] = []    # 输出行（含死壳 error 行，保持出现顺序）
    merged: dict[str, dict] = {}  # sid → 展示行（本页视角优先）
    live_threads: dict[str, str] = {}  # sid → 运行时所在的 thread
    for tid, (shell, _last_used) in snapshot.items():
        try:
            rows = shell.sessions().get("sessions", [])
        except Exception as exc:  # 壳刚死/连接断：整行标注来源错误，别炸请求
            sessions.append({"thread": tid, "error": str(exc)})
            continue
        is_owner = (tid == owner)
        for row in rows:
            sid = row.get("id")
            if row.get("live"):
                live_threads[sid] = tid  # 任一壳的运行时里有它 → 活的
            row["thread"] = tid
            # own：这个 sid 在本页壳的清单里（s09 后共享 .sessions/，基本
            # 都 own）——前端按"own 才能操作/切换"渲染，thread 只做来源标注。
            row["own"] = is_owner
            row["current"] = (is_owner
                              and row.get("live", False)
                              and row.get("id") == shell.session_id)
            first = merged.get(sid)
            if first is None:
                merged[sid] = row
                sessions.append(row)
            elif row["current"] or row["own"]:
                # 本页视角优先：current 行 = 权威归属；own 行 = 本页可操作
                # （没 current 时别让别家 thread 的标注盖住本页的）
                sessions[sessions.index(first)] = row
                merged[sid] = row
    # 聚合 live：展示行可能是本页视角（live=False），用全量观测盖回真值
    for sid, row in merged.items():
        lt = live_threads.get(sid)
        row["live"] = lt is not None
        row["liveThread"] = lt or ""
    return sessions


def _notify_bridge(thread: Optional[str], action: str, sid: str,
                   detail: str = "") -> None:
    """操作成功后通知对应页面的聊天桥（resume/create 的呈现环节）。

    路由口径比 _target_shell **更严**：带了 thread 就只认这个 thread 的桥，
    绝不回落 default——重放串到别的页面 = 对着错误的嘴说话。桥失败
    （页面已关/上下文过期）静默放过：会话切换是事实，聊天区重放只是呈现。
    """

    with _shells_lock:
        tid = thread or _default_thread
        bridge = _bridges.get(tid) if tid else None
    if bridge is None:
        return
    try:
        bridge(action, sid, detail)
    except Exception:
        pass  # 桥炸了不拖垮操作结果


def _run_action(action: str, sid: str, thread: Optional[str]) -> dict:
    """把一次面板操作翻译成 shell 调用；翻译约定与终端/命令一致。

    操作精确路由到 thread 指定的壳（前端每次带自己页面的线程），不再有
    全局守卫——"谁操作的页面"由请求者自己声明，多 tab 各自操作自己的壳。
    """

    shell = _target_shell(thread)
    if shell is None:
        return {"ok": False, "detail": "没有可操作的会话（先打开一个聊天页）"}
    try:
        if action == "create":
            new_sid = shell.new_session()   # 关旧开新，旧记录保留
            _notify_bridge(thread, "create", new_sid)  # 聊天区：新会话分隔卡
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
            _notify_bridge(thread, "resume", sid,
                           f"generation {result['generation']}")
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
            # defaultThread：告诉前端"哪个线程是本页的壳"——前端据此区分
            # 可切换的会话（本页）和只能看的会话（别的页面）。
            # threads：在线壳的 thread id 全名单——前端校验自己 sessionStorage
            # 里记住的锚点还有效（壳可能被 reap/换新），失效就重新认领。
            with _shells_lock:
                threads = list(_shells)
            self._send_json(200, {"sessions": _collect_sessions(thread),
                                  "defaultThread": _default_thread,
                                  "threads": threads})
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