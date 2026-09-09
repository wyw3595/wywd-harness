"""s06 Sidecar Server：真多进程 + JSON-RPC over socket。

📚 这一课在做什么（承接 s05）
  s05 把 UI 和 agent 拆成两个进程，但 agent 还住在 main 里。s06 把 agent
  再拎出来，变成独立的 Sidecar 进程——"主进程不跑 agent, Sidecar 来跑"。
  壳（main）只负责路由，通过 JSON-RPC over socket 连 Sidecar。

  ┌────────────────────┐   JSON-RPC over socket   ┌────────────────────┐
  │ Main（壳/终端 UI）   │◄──────────────────────►│ Sidecar（agent）    │
  │ MainProcessClient   │    newline-delimited     │ SidecarServer      │
  │   call() + 分派环    │      JSON，id 配对        │   领域路由          │
  └────────────────────┘                          │   RingBuffer       │
                                                   └────────────────────┘

📚 三个机制（写之前必读，代码注释会反复引用）：
  1. JSON-RPC 协议：外层请求用 id 配对（request id=1 → response id=1）；
     审批/事件用 id:null 的"通知" + params.request_id 回程票——id 命名空间
     只属于外层请求-响应，领域级配对交给 request_id（与 s05 同构）。
  2. 收信分派环（main 侧）：call 阻塞等响应期间，socket 里会混入 sidecar
     发来的 approval/request（要审批）和 event（直播）。有 method 字段 =
     请求/通知，就地消费（弹 y/n / 打印）后 continue；没有 method 且
     id 匹配外层 = 我的响应，返回。
  3. 单线程顺序嵌套：main 先卡（recv 等 id=1）→ sidecar 处理时 approver
     嵌套 recv 等回执 → 两边至多一个嵌套 recv 存活，无死锁。
     死锁只来自四种违约：①分派环把审批/事件当结果返回；②user_prompt
     里重入 call()；③两个线程同时 recv（RPCConnection 非线程安全）；
     ④main 不消费 event（event 排在 result 前面会挡住它）。全写进注释。

📚 Windows spawn 守则（同 s05）：
  - mp.Process target 必须模块级函数；装配在子进程内各自做；
    if __name__ == "__main__" 保护；干净退出协议。
  - socket 跨进程：socket.socketpair() 的一端传进 args，CPython 在
    Windows 注册了 socket reducer（multiprocessing.reduction 的
    DupSocket/WSADuplicateSocket），官方支持 pickle；父进程 start() 后
    必须 close() 自己那份句柄。
📚 s07-b（会话接线）：sessions 裸字典退役，换成 s07 的 SessionManager。
  session/destroy（一删全没）→ session/close（释放运行时、留记录、幂等）；
  新增 session/resume（旧 id + generation+1 的新运行时，live 拒绝）和
  session/forget（真删除，必须先 close）；agent/send 改走 run_turn——
  并发拒绝、晚到结果拒收从此由状态机接管，不再靠手工回写字典。
"""

import json
import socket
import threading
import time
import uuid
from typing import Any, Callable, Optional

from src.harness.agent import run_agent
from src.harness.memory import trim_history
from src.harness.permissions import Approver, AuditTrail, GovernedToolRunner
from src.harness.session import (
    SessionLifecycleError,
    SessionManager,
    SessionState,
    SessionStore,
)
from src.harness.tools import ToolRegistry


# ═══════════════════════════════════════════════════════════════
# RingBuffer — 有界环形缓冲区（捕获 sidecar 日志，满了覆盖最旧）
# ═══════════════════════════════════════════════════════════════

class RingBuffer:
    """固定大小的环形日志：写满后新数据覆盖最旧数据——内存有界。

    生产侧 sidecar 用它捕获所有子进程的 stdout/stderr；本课捕获
    sidecar 自己的运行日志。不是数据库——只保留最近，不做永久审计
    （s04 的 AuditTrail 才是审计，两者分工）。
    """

    def __init__(self, size: int = 1024 * 1024) -> None:
        self.size = size
        self.buffer = bytearray(size)
        self.write_pos = 0          # 写头：下一个字节写到哪
        self.total_written = 0      # 累计写过的字节数（区分"没满"和"满了"）
        self._lock = threading.Lock()  # 并发写保护

    def write(self, data: bytes | str) -> None:
        """写一段数据；写满后新数据覆盖最旧字节。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
            # 编码放锁外：纯本地转换无需同步；下面的写才需要临界区
        with self._lock:
            for byte in data:
                self.buffer[self.write_pos] = byte
                # 游标取模绕回：到 size 归零，实现"满了覆盖最旧"
                self.write_pos = (self.write_pos + 1) % self.size
                # 非原子累加，靠锁保证并发下计数不丢
                self.total_written += 1

    def read_all(self) -> str:
        """读出所有有效数据（旧→新顺序）。"""
        if self.total_written < self.size:
            return self.buffer[:self.write_pos].decode("utf-8", errors="replace")
        # 写满了：从写头（最旧）绕一圈回到写头（最新）——两段拼接。
        # 只取 [write_pos:total_written] 会缺前半圈（bytearray 切片超长
        # 会被裁到末尾，只剩 buffer[write_pos:] 那半段）。
        return (
            self.buffer[self.write_pos:].decode("utf-8", errors="replace")
            + self.buffer[:self.write_pos].decode("utf-8", errors="replace")
        )

    @property
    def used(self) -> int:
        """当前有效字节数：没写满就是 total_written，写满就是 size。"""

        return min(self.total_written, self.size)

    @property
    def is_full(self) -> bool:
        return self.total_written >= self.size


# ═══════════════════════════════════════════════════════════════
# RPCConnection — newline-delimited JSON framing over socket
# ═══════════════════════════════════════════════════════════════

class RPCConnection:
    """一条 socket 连接的 JSON-RPC 帧协议。

    每条消息是一个 JSON 对象 + \n。比长度前缀简单，文本调试友好。
    非线程安全——同一时刻只能一个线程 recv（写进 main 侧注释的死锁违约③）。
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._recv_buffer = b""     # 半帧残留：recv 可能一截一截到

    def send_message(self, msg: dict) -> None:
        """发送一条 JSON-RPC 消息（JSON + \n）。"""

        self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def recv_message(self) -> Optional[dict]:
        """按 \n 取出一帧；对端正常关闭返回 None。

        注意：对端被强杀时 recv 会抛 OSError（不是返回 b""），由调用方兜。
        """
        while b"\n" not in self._recv_buffer:
            chunk = self.sock.recv(65536)
            if not chunk: return None        # 对端关闭（EOF）
            self._recv_buffer += chunk       # 累积半帧
        line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


# ═══════════════════════════════════════════════════════════════
# SidecarServer — JSON-RPC 服务端 + 领域化路由 + agent 宿主
# ═══════════════════════════════════════════════════════════════

class SidecarServer:
    """Sidecar 进程大脑：收 JSON-RPC → 按领域路由 → 跑 agent → 回结果。

    依赖构造器注入（与 ElectronMain 同哲学，harness 不 import 装配层）：
      model    —— Model 协议（FakeModel / ScriptedModel / RealModel）
      registry —— ToolRegistry（run_agent 校验、闸门放行后执行工具）
      policy   —— PermissionPolicy（toolbox.build_policy() 产出）
      max_history —— 会话历史窗口大小（成本刹车，默认 20）
      history_seed —— Callable[[], list[dict]]：新建会话的起步历史
                  （装配层传 lambda: with_system([])，system 常驻）

    agent 执行走现有 run_agent（s01~s03 的循环 + s04 的权限闸门），
    session 存 history（练习 10 铁律：Harness 无状态，记忆归应用层）。

    s07-b：会话控制面 = s07 的 SessionManager。Record（身份/历史）进
    store，运行时（turn 锁/状态机）只活在本进程；close 留记录、resume
    换代重建、forget 才真删。turn_runner 闭包把 run_agent 借给 Manager
    （session 层不认识 agent 层——s07 的依赖方向约定）。
    """

    def __init__(
        self,
        model: Any,
        registry: ToolRegistry,
        policy: Any,
        max_history: int = 20,
        history_seed: Optional[Callable[[], list[dict]]] = None,
        store: Optional[SessionStore] = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._policy = policy
        self._max_history = max_history
        self._history_seed = history_seed or (lambda: [])
        self.ring_buffer = RingBuffer()
        # s09：持久化注入 + 启动清账。
        # 1. store 透传（None → Manager 自己落 InMemory，离线测试零感知）：
        self._manager = SessionManager(
            turn_runner=self._make_turn_runner(), store=store)
        # 2. 启动清账（startup reconciliation）：崩溃/直接退出遗留的
        #    creating/idle/running/closing 记录没有 runtime，证据文件却写着
        #    "活着"。close_session 对不 live 的记录会把状态抹成 closed（s07
        #    写好的幂等路径），正好用。放过 error：它是有效终态（原因留在
        #    last_error 里，resume 可直接接管）。InMemory 下 list() 为空清账空转。
        for record in self._manager.store.list():
            if record.status not in (SessionState.CLOSED, SessionState.ERROR):
                self._manager.close_session(record.id)
        self._last_turn_status = "completed"
        self.rpc_handlers: dict[str, Callable] = {}
        # 实例属性，不是类属性（教材的坑：类属性多实例共享）。
        self._start_time = time.time()
        self._shutdown = False
        # 下面三个在 handle_connection 里 per-connection 装配（approver
        # 必须闭包住 conn——socket 是连接才知道的，构造期不存在）。
        self._conn: Optional[RPCConnection] = None
        self._runner: Optional[GovernedToolRunner] = None
        self._on_event: Optional[Callable[[str, dict], None]] = None
        self._register_channels()

    def _register_channels(self) -> None:
        """注册领域化 RPC handler——未来 s07 会话 / s16 skills / s17 MCP
        都往这些领域里填，这里是"领域路由"这个模式的起点。"""

        self.rpc_handlers["sidecar/ping"] = self._handle_ping
        self.rpc_handlers["sidecar/status"] = self._handle_status
        self.rpc_handlers["sidecar/shutdown"] = self._handle_shutdown
        self.rpc_handlers["sidecar/logs"] = self._handle_logs
        self.rpc_handlers["session/create"] = self._handle_session_create
        self.rpc_handlers["session/list"] = self._handle_session_list
        # s07-b 四操作路由（老 session/destroy"一删全没"已退役）
        self.rpc_handlers["session/close"] = self._handle_session_close
        self.rpc_handlers["session/resume"] = self._handle_session_resume
        self.rpc_handlers["session/forget"] = self._handle_session_forget
        self.rpc_handlers["agent/send"] = self._handle_agent_send
        self.rpc_handlers["tool/list"] = self._handle_tool_list

    def _make_turn_runner(self):
        """造 turn_runner 闭包：session 层的执行入口 → 本文件的 run_agent。

        两个设计点：
          - 闭包捕获 self、调用那一刻才读 self._runner / self._on_event：
            这俩是 per-connection 的，构造期还是 None，连接建立才有值
            （s06 handle_connection 的装配顺序决定了只能这么写）；
          - self._last_turn_status 是协议接缝的记账：TurnRunner 协议的
            返回值只有 (output, messages)，装不下 run_agent 的 status——
            但 agent/send 的 RPC 响应要诚实汇报 max_steps/failed。安全
            前提：同一时刻至多一个 turn 在跑（handle_connection 单线程 +
            壳的 _rpc_lock 串行化）；跨连接并发 turn 是已知边界（status
            可能串台，output 不受影响——它走返回值）。
        """
        def turn_runner(message: str, history: list[dict]):
            result = run_agent(
                message, model=self._model, registry=self._registry,
                history=trim_history(history, self._max_history),
                on_event=self._on_event, runner=self._runner)
            self._last_turn_status = result.status
            return result.output, result.messages
        return turn_runner

    def _log(self, msg: str) -> None:
        """写进 RingBuffer（模拟生产侧捕获子进程 stdout/stderr）。"""

        ts = time.strftime("%H:%M:%S")
        self.ring_buffer.write(f"[{ts}] [sidecar] {msg}\n")

    # ── 连接循环 + per-connection 装配 ─────────────────────────

    def handle_connection(self, conn: RPCConnection) -> None:
        """处理一条 RPC 连接：循环收消息 → 路由 → 回 result/error。"""
        self._conn = conn
        self._runner = GovernedToolRunner(
            policy=self._policy, approver=self._make_approver(conn),
            registry=self._registry, audit=AuditTrail())
        self._on_event = lambda event, data: conn.send_message(
            {"jsonrpc":"2.0","method":"event",
             "params":{"event":event,**data},"id":None})
        self._log("new RPC connection established")
        while not self._shutdown:
            try: req = conn.recv_message()
            except Exception: break          # 对端强杀 OSError，同样退出
            if req is None: break            # EOF
            method = req.get("method",""); params = req.get("params") or {}
            req_id = req.get("id")
            self._log(f"RPC: {method} (id={req_id})")
            handler = self.rpc_handlers.get(method)
            if handler:
                try:
                    result = handler(params)
                    conn.send_message({"jsonrpc":"2.0","result":result,"id":req_id})
                except Exception as error:
                    conn.send_message({"jsonrpc":"2.0",
                        "error":{"code":-32603,"message":str(error)},"id":req_id})
            else:
                conn.send_message({"jsonrpc":"2.0",
                    "error":{"code":-32601,"message":f"Method not found: {method}"},
                    "id":req_id})
        conn.close()
        self._log("RPC connection closed")

    def _make_approver(self, conn: RPCConnection) -> Approver:
        """审批回程票闭包（s05 同构，只是从队列换成 socket）。"""
        def approver(decision) -> bool:
            ticket = str(uuid.uuid4())
            conn.send_message({"jsonrpc":"2.0","method":"approval/request",
                "params":{"request_id":ticket,"rule_id":decision.rule_id,
                          "reason":decision.reason},"id":None})
            while True:
                try: msg = conn.recv_message()
                except Exception: return False
                if msg is None: return False
                p = msg.get("params") or {}
                if (msg.get("method")=="approval/response"
                        and p.get("request_id")==ticket):
                    return bool(p.get("approved"))
        return approver

    # ── RPC Handlers ──────────────────────────────────────────

    def _handle_ping(self, params: dict) -> dict:
        self._log("ping received")
        return {"status": "ok", "uptime": time.time() - self._start_time}

    def _handle_status(self, params: dict) -> dict:
        """sidecar 状态总览。

        /status 数的是 Manager 的记录总数——closed 但没 forget 的会话也计入
        "记录还在"和"运行时活着"是两回事（s07 学习目标⑤）。
        """
        status = {
            "sessions": len(self._manager.list_sessions()),
            "ringBufferUsed": self.ring_buffer.used,
            "ringBufferTotal": self.ring_buffer.size,
            "ringBufferFull": self.ring_buffer.is_full,
            "handlers": len(self.rpc_handlers),
        }
        # s08：模型挂了路由器就捎上成本表。duck typing（getattr 三参：
        # 读属性，没有就给 None）——裸 FakeModel/RealModel 没有
        # cost_summary → status 完全不带 modelCost 键（不是空表：空表
        # 会误导 UI 以为挂了个空路由器），老模型零行为变化。
        cost = getattr(self._model, "cost_summary", None)
        if cost is not None:
            status["modelCost"] = cost()
        return status

    def _handle_logs(self, params: dict) -> dict:
        """日志走 RPC 返回——真多进程下 main 碰不到子进程的 RingBuffer。"""

        return {"logs": self.ring_buffer.read_all()[-2000:]}

    def _handle_shutdown(self, params: dict) -> dict:
        self._shutdown = True
        self._log("shutdown requested")
        return {"status": "shutting down"}

    # ── 会话四操作（s07-b：SessionManager 之上的 RPC 薄翻译层）────────
    #
    # 翻译约定：领域异常一律翻译成 {"error": "人话"} 放进 result——和 s06
    # 的约定一致（shell 检查 "error" in result）。捕获范围：
    #   SessionLifecycleError —— 四操作的状态机拒绝（含 SessionNotFoundError
    #     / SessionAlreadyRunningError 两个子类）
    #   ValueError / OSError  —— create/resume 的 cwd/mode 校验
    #     （resume 会重新校验 cwd：close 期间目录可能被删了；FileNotFoundError
    #     是 OSError 子类，ValueError 是 mode 非法）
    # 其他异常穿透给 handle_connection 的兜底 except → JSON-RPC error。

    def _handle_session_create(self, params: dict) -> dict:
        """建会话：新身份 + 第 1 代运行时 + 起步历史（system 常驻）。

        起步历史写进两个副本——live runtime 的工作副本（run_turn 从它
        拿历史）和 store 存档（resume 从它拿历史）；只写一个 = 另一条路
        丢 system 提示（两个副本是 deepcopy 防串账的代价）。
        """
        sid = self._manager.create_session(
            cwd=params.get("cwd", "."),
            mode=params.get("mode", "craft"),
            title=params.get("title", "未命名会话"))
        runtime = self._manager.get_session(sid)
        runtime.record.messages = self._history_seed()
        self._manager.store.save(runtime.record)
        self._log(f"session created: {sid}")
        return {"sessionId": sid}

    def _handle_session_list(self, params: dict) -> dict:
        """会话清单：Record.summary() + live 标志（s07 的 UI 安全视图）。

        close 过的会话还在清单里（status="closed"、live=False）——记录
        保留正是 close 的本意；真正消失只有 forget。
        """
        return {"sessions": self._manager.list_sessions()}

    def _handle_session_close(self, params: dict) -> dict:
        """close：释放运行时，记录和 transcript 保留（幂等）。

        与老 destroy 的行为差异：destroy 一删全没，close 后清单里还在
        （closed）；二次 close 不报错，对不存在的 id 才报错。
        """
        try:
            closed = self._manager.close_session(params.get("sessionId", ""))
        except (SessionLifecycleError, ValueError, OSError) as exc:
            return {"error": str(exc)}
        self._log(f"session closed: {params.get('sessionId', '')} ({closed})")
        return {"status": "ok", "closed": closed}

    def _handle_session_resume(self, params: dict) -> dict:
        """resume：旧身份 + generation+1 的新运行时；该 id 已 live 则拒绝。"""
        sid = params.get("sessionId", "")
        try:
            self._manager.resume_session(sid)
        except (SessionLifecycleError, ValueError, OSError) as exc:
            return {"error": str(exc)}
        generation = self._manager.load_record(sid).runtime_generation
        self._log(f"session resumed: {sid} (generation {generation})")
        return {"sessionId": sid, "generation": generation}

    def _handle_session_forget(self, params: dict) -> dict:
        """forget：真删除逻辑记录；live 的必须先 close（防手滑丢历史）。"""
        sid = params.get("sessionId", "")
        try:
            forgot = self._manager.forget_session(sid)
        except SessionLifecycleError as exc:
            return {"error": str(exc)}
        if not forgot:
            return {"error": f"session not found: {sid}"}
        self._log(f"session forgotten: {sid}")
        return {"status": "ok"}

    def _handle_tool_list(self, params: dict) -> dict:
        """工具清单从真实 registry 出（不照抄教材硬编码）。"""

        return {"tools": [
            {"name": tool.name, "description": tool.description}
            for tool in self._registry._tools.values()
        ]}

    def _handle_agent_send(self, params: dict) -> dict:
        """跑一个 turn：状态机接管（并发拒绝 / close 竞态拒收都在里面）。

        - get_session 只回 live 运行时：closed 的会话在这里就是 None——
          "记录还在"和"运行时活着"是两回事（s07 学习目标⑤）
        - run_turn 内部走 turn_runner 闭包（_make_turn_runner）：run_agent +
          trim_history + 权限 runner + 事件直播的拼装全在里面；历史回写由
          _commit_transcript_if_running 原子落账
        - status 从 _last_turn_status 接缝读；run_turn 抛
          SessionLifecycleError = 并发 turn 被拒 / close 竞态丢结果，
          翻译成 {"error"} 人话
        """
        sid = params.get("sessionId","")
        text = params.get("message","")
        runtime = self._manager.get_session(sid)
        if runtime is None:
            return {"error": f"session not found or closed: {sid}"}
        try:
            output = runtime.run_turn(text)
        except SessionLifecycleError as exc:
            return {"error": str(exc)}
        return {"output": output, "status": self._last_turn_status}


# ═══════════════════════════════════════════════════════════════
# MainProcessClient — 壳侧客户端：call + 收信分派环
# ═══════════════════════════════════════════════════════════════

class ConnectionClosed(Exception):
    """sidecar 进程已退出（EOF / 强杀）——call 的诚实失败，不是卡死。"""


def _terminal_prompt(rule_id: str, reason: str) -> bool:
    """默认审批交互：终端弹 y/n（对齐 chat.py / electron_shell 心智）。"""

    print(f"  ⚠️ 需要审批 [{rule_id}] {reason}")
    answer = input("     允许这次工具调用吗？(y/n) ").strip().lower()
    return answer == "y"


class MainProcessClient:
    """主进程的 RPC 客户端：call 阻塞期间，分派环就地消费审批与事件。

    user_prompt / on_event 注入可测；默认终端 y/n + 打印。
    单线程约定：同一时刻最多一个进行中的 call（写进死锁违约②/③）。
    """

    def __init__(
        self,
        user_prompt: Optional[Callable[[str, str], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._conn: Optional[RPCConnection] = None
        self._rpc_id = 0
        self._user_prompt = user_prompt or _terminal_prompt
        self._on_event = on_event

    def connect(self, sock: socket.socket) -> None:
        self._conn = RPCConnection(sock)

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """发一个 JSON-RPC 请求并等它 id 配对的响应（阻塞；分派环消费审批/事件）。"""
        self._rpc_id += 1
        req_id = self._rpc_id
        try:
            self._conn.send_message({"jsonrpc":"2.0","method":method,
                "params":params or {}, "id":req_id})
        except OSError:
            # 对端已关闭：sendall 也会抛（WinError 10038），一样诚实失败。
            raise ConnectionClosed("sidecar 连接中断") from None
        while True:
            try: msg = self._conn.recv_message()
            except OSError: raise ConnectionClosed("sidecar 连接中断") from None
            if msg is None: raise ConnectionClosed("sidecar 进程已退出")
            if "method" in msg:                  # 请求/通知：就地消费
                if msg["method"]=="approval/request":
                    p = msg.get("params") or {}
                    ok = self._user_prompt(p.get("rule_id",""), p.get("reason",""))
                    self._conn.send_message({"jsonrpc":"2.0",
                        "method":"approval/response",
                        "params":{"request_id":p.get("request_id"),"approved":ok},
                        "id":None})
                elif msg["method"]=="event" and self._on_event:
                    self._on_event(msg.get("params") or {})
                continue                         # 不是我的答案，继续等
            if msg.get("id") == req_id:          # 无 method 且 id 匹配 → 我的响应
                return msg


    def close(self) -> None:
        if self._conn:
            self._conn.close()
