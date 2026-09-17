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

📚 s11（工作区/上传目录）：新增 workspace/set + workspace/get 两条领域路由，
  让"沙箱根"在进程活着的时候也能换——这是"上传一个目录，模型在这个目录里
  工作"在 sidecar 架构下的落点。机制靠**注入的工厂**：harness 不认识
  Workspace，装配层把"收 {kind, path} → 返回 (registry, policy, info)"
  的构建器交进来（依赖方向仍然只有一边：装配层 import harness，反之不成立）。
  换的为什么是 registry + policy + runner 而不是 model：工具 schema 只由
  名字/描述/签名决定，与沙箱根无关（说明书可以复用）；而 registry 和
  policy 本身就是边界——它们才是必须换的东西。
"""

import json
import socket
import threading
import time
import uuid
from typing import Any, Callable, Optional

from src.harness.agent import run_agent
from src.harness.compact import (
    COMPACT_THRESHOLD_TOKENS,
    compact,
    model_summarizer,
)
from src.harness.permissions import Approver, AuditTrail, GovernedToolRunner
from src.harness.session import (
    SessionLifecycleError,
    SessionManager,
    SessionState,
    SessionStore,
)
from src.harness.tools import ToolRegistry

# 工作区运行件工厂（s11 上传目录）：由装配层注入，harness 不认识 Workspace。
# 契约：收一个 JSON 化的请求 {"kind": "dir"|"zip", "path": ...}，成功返回
# (registry, policy, info)——info 是给 UI 看的不透明字典（id/root/kind），
# harness 只负责原样转交；失败**必须翻成 ValueError / OSError 抛出**
# （RPC 层只有这一条错误通道，与 session 四操作的约定一致）。
WorkspaceRuntimeBuilder = Callable[[dict], tuple]


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
      runtime_builder —— 工作区工厂（s11，可选）：换沙箱时现做一套运行件，
                  None = 不支持换沙箱（老装配零感知）
      initial_workspace —— 启动态工作区的描述（s11，可选）：装配层把自己的
                  默认根如实告知（如 {"kind": "default", "root": "..."}）。
                  给它的唯一理由是**显示**：复位后 UI 要能告诉用户"现在在
                  哪个目录"，而 sidecar 手上只有 registry，认不出路径。
                  不给则回落到 {"kind": "default"}（老装配零感知）。
      idle_timeout —— 空闲回收阈值（秒）：某代运行时闲置超过它就被 close
                  （**记录保留**，前端点一下即可 resume）。**0 = 不回收**，
                  这是默认值：回收会让"随时发消息都能用"变成"可能要先复活"，
                  该由调用方明确选择，而不是悄悄改掉所有人的手感。
      sweep_interval —— 后台扫描周期（秒），只在 idle_timeout > 0 时有意义。

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
        max_steps: int = 30,
        history_seed: Optional[Callable[[], list[dict]]] = None,
        store: Optional[SessionStore] = None,
        runtime_builder: Optional[WorkspaceRuntimeBuilder] = None,
        initial_workspace: Optional[dict] = None,
        idle_timeout: float = 0.0,
        sweep_interval: float = 60.0,
        externalize: Optional[Callable[[str, str], str]] = None,
        compact_budget: int = COMPACT_THRESHOLD_TOKENS,
    ) -> None:
        self._model = model
        self._registry = registry
        self._policy = policy
        # s11 复位用：留住启动时那套运行件。为什么记在进程里而不是让装配层
        # 再解析一次"默认路径"——"默认"是**这个进程的启动态**，不是某个目录。
        # 装配层传的是什么（项目根/别处），reset 就回到什么，无需它再表态。
        # 描述（root 等）同样留住：registry 认不出路径，UI 却要显示它。
        self._initial_registry = registry
        self._initial_policy = policy
        self._initial_workspace_info: dict = dict(initial_workspace
                                                 or {"kind": "default"})
        # s11：工作区工厂（None = 这个 sidecar 不支持换沙箱，老装配零感知）。
        # 默认 info 说明"我跑在启动时那套边界上"，UI 拿它显示"项目根"。
        self._runtime_builder = runtime_builder
        self._workspace_info: dict = dict(self._initial_workspace_info)
        self._max_history = max_history
        # 单轮步数上限（2026-09-16）：run_agent 的签名默认是 5，那只是"循环
        # 保险丝"；但用户接触的是这一层，默认值就是用户实际拿到的上限，
        # 所以要给一个能干活的值。装配层会显式传 toolbox.resolve_max_agent_steps()
        # （环境变量 WYWD_MAX_STEPS，默认 30）——harness 不能反向 import
        # 装配层（依赖方向铁律），所以这里是同一数值的第二处声明，
        # 改默认值时两处一起改。
        self._max_steps = max_steps
        # 工具输出外化（s13）：装配层用 ArtifactStore.externalize 填这个洞。
        # None = 不外化（既有行为零变化）——和 runner/history_seed 一样，
        # harness 只留接缝，不决定磁盘布局。
        self._externalize = externalize
        # s14：压缩预算（token）。超过它才启动四层管线——低于阈值时 compact
        # 只做一次深拷贝就返回，零改动、零成本。
        self._compact_budget = compact_budget
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
        # s08 续：每轮 token 账也走同一个接缝（TurnRunner 协议只回
        # (output, messages)，装不下 usage——和 status 当初一样的处境）。
        self._last_turn_usage: dict = {}
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
        # 空闲回收：阈值 0 = 不起线程（默认）。
        # 用独立的 stop 事件，而不是复用 _shutdown：回收线程该能单独停——
        # 复用 _shutdown 会连同连接循环一起掀掉（测试里想"停下来单独验
        # resume"就做不到，只能跟回收赛跑）。
        self._idle_timeout = idle_timeout
        self._sweep_interval = sweep_interval
        self._reaper_stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None
        if idle_timeout > 0:
            self._start_reaper()

    # ── 空闲回收（后台线程）──────────────────────────────────

    def _start_reaper(self) -> None:
        """起后台回收线程。daemon=True：它是清理工，不该拦住进程退出。"""

        self._reaper = threading.Thread(
            target=self._reaper_loop, name="wywd-idle-reaper", daemon=True)
        self._reaper.start()

    def stop_reaper(self) -> None:
        """停掉回收线程并等它退出（幂等）。关机路径与测试都用它。"""

        self._reaper_stop.set()
        if self._reaper is not None:
            self._reaper.join(timeout=2)

    def _reaper_loop(self) -> None:
        """每 sweep_interval 秒扫一次，把空闲超时的运行时 close 掉。

        为什么用后台线程，而不是"顺手在每次 RPC 里扫一遍"：空闲回收要解决的
        恰恰是**没有请求时**的内存占用——挂在请求上等于"有人来才打扫"，
        没人来就永远不打扫，等于没做。
        """

        while not self._shutdown:
            # 用 Event.wait 而不是 sleep：停线程时能被立刻叫醒，
            # 不用让关机卡在等满一个扫描周期上。
            if self._reaper_stop.wait(self._sweep_interval):
                break
            try:
                reaped = self._manager.reap_idle(self._idle_timeout)
            except Exception as exc:
                # 后台清理出错绝不能拖垮整个 sidecar：记一行、下轮再来。
                self._log(f"idle reap failed: {exc}")
                continue
            if reaped:
                self._log(f"idle reap: released {len(reaped)} runtime(s) "
                          f"({', '.join(reaped)})"
                          " —— records kept, resume to continue")

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
        self.rpc_handlers["session/messages"] = self._handle_session_messages
        self.rpc_handlers["agent/send"] = self._handle_agent_send
        self.rpc_handlers["tool/list"] = self._handle_tool_list
        # s11：工作区（上传目录）——换沙箱 + 读当前。
        self.rpc_handlers["workspace/set"] = self._handle_workspace_set
        self.rpc_handlers["workspace/get"] = self._handle_workspace_get

    def _make_summarizer(self) -> Callable[[list[dict]], Optional[str]]:
        """L4 摘要器（s14）：直接复用 compact.model_summarizer。

        留这个方法只是为了让"摘要用哪个模型"这件事在 sidecar 里有个明确的
        落点——将来若要换更便宜的档位（比如用 router 的 lite 槽）摘要，
        改这一处即可，不必动管线。
        """

        return model_summarizer(self._model)

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
            # s14：先做四层压缩（预算内不动；超了从最便宜的层开始压），
            # 再让条数上限兜底。两道闸各管一件事：token 管"贵不贵"，
            # 条数管"太长"（很多条极短的寒暄——token 不高但条数吓人）。
            view, report = compact(
                history, budget_tokens=self._compact_budget,
                summarizer=self._make_summarizer(), keep=self._max_history)
            if report.changed:
                print(f"[sidecar] 上下文压缩：{report.render()}")
            result = run_agent(
                message, model=self._model, registry=self._registry,
                history=view,
                max_steps=self._max_steps,
                externalize=self._externalize,   # s13：超阈值输出换到磁盘
                on_event=self._on_event, runner=self._runner)
            self._last_turn_status = result.status
            # usage 是这一轮的账（run_agent 在轮内跨 step 累加出来的 totals）。
            # dict() 复制是防御习惯：存引用的话，将来谁在返回后动了那个字典，
            # 这里会跟着变——"快照存的应该是那一刻的值"（练习 08 的老教训）。
            self._last_turn_usage = dict(result.usage)
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
            # s11：当前工作区（UI 用它显示"模型现在在哪个目录里干活"）。
            "workspace": dict(self._workspace_info),
            # 空闲回收：0 表示没开。运维抽屉靠它显示"回收开着没、阈值多少"——
            # "运行时为什么被释放了"必须有个能看见的答案。
            "idleTimeout": self._idle_timeout,
            # 单轮步数上限：运维面板要能看见"一个任务最多能走几步"——
            # 它和 idleTimeout 一样属于"行为参数"，不该只活在代码里。
            "maxSteps": self._max_steps,
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
        self.stop_reaper()        # 回收线程是独立开关，得单独放它走
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
            return {"error": f"没有这个会话：{sid}（可能已被删除）"}
        self._log(f"session forgotten: {sid}")
        return {"status": "ok"}

    def _handle_tool_list(self, params: dict) -> dict:
        """工具清单从真实 registry 出（不照抄教材硬编码）。"""

        return {"tools": [
            {"name": tool.name, "description": tool.description}
            for tool in self._registry._tools.values()
        ]}

    # ── 工作区（s11：上传目录）──────────────────────────────

    def _handle_workspace_set(self, params: dict) -> dict:
        """换工作区：让装配层现做一套运行件，换掉本进程的 registry/policy。

        三层连带更新，缺一层就是"半换沙箱"（且失败是静默的）：
          1. registry —— 工具闭包捕获的 root（换成新目录）；
          2. policy   —— WorkspaceScope 的根（决策层必须和执行层同一个根，
             否则模型越界时决策层以为在界内、执行层才炸）；
          3. runner   —— 它**构造时**绑定 registry/policy，不重建就还在用
             旧沙箱（这一层最容易漏：前两层改了、跑起来还是老边界）。

        model 刻意不换：工具 schema 与沙箱根无关（名字/描述/签名不变），
        说明书可以复用；换它反而是浪费（RealModel 每次重建都有连接池成本）。

        已建会话的历史**不动**：历史属于会话（应用层状态），工作区属于
        边界——混在一起会让"换个目录看看"变成"清空对话"。代价是旧历史里
        的路径在新沙箱可能不存在（模型会收到"没有这个路径"，它会自己适应）。

        kind="default" 是**复位**：回启动态（构造时注入的那套运行件），
        不走工厂——"默认"不是一个可解析的目录，而是这个进程出生的地方。
        所以复位永远可用，哪怕没注入工厂（`_runtime_builder is None`）。
        """

        if params.get("kind") == "default":
            # 复位：不碰工厂，直接把初始运行件放回去（info 也回启动态）。
            registry, policy, info = (
                self._initial_registry, self._initial_policy,
                dict(self._initial_workspace_info))
        else:
            if self._runtime_builder is None:
                return {"error": "这个 sidecar 没接工作区能力（装配层未注入 runtime_builder）"}
            try:
                registry, policy, info = self._runtime_builder(params)
            except (ValueError, OSError) as exc:
                # 唯一错误通道：装配层负责把库异常翻译成人话（见契约注释）。
                return {"error": str(exc)}
        self._registry = registry
        self._policy = policy
        self._workspace_info = dict(info)
        if self._conn is not None:
            # runner 是 per-connection 的（approver 绑 socket）：连接还在，
            # 直接就地重建；没连接说明是直调（测试），留给 handle_connection。
            self._runner = GovernedToolRunner(
                policy=self._policy, approver=self._make_approver(self._conn),
                registry=self._registry, audit=AuditTrail())
        self._log("workspace switched: "
                  + str(self._workspace_info.get("root")
                        or self._workspace_info.get("kind", "?")))
        return {"status": "ok", "workspace": self._workspace_info}

    def _handle_workspace_get(self, params: dict) -> dict:
        """读当前工作区（UI 渲染用）。默认态也如实回答（kind=default）。"""

        return {"workspace": dict(self._workspace_info)}

    def _handle_session_messages(self, params: dict) -> dict:
        """读一个会话的完整消息历史（历史重放/审计的只读通道）。

        与 session/list（摘要）的区别：这里回传 record.messages 全量——
        closed 的会话一样能读（记录还在，Store 里存的就是它）；id 不存在
        抛 SessionNotFoundError → 翻译成 {"error"} 人话（s07-b 惯例）。
        """

        sid = params.get("sessionId", "")
        try:
            record = self._manager.load_record(sid)
        except SessionLifecycleError as exc:
            return {"error": str(exc)}
        self._log(f"session messages requested: {sid}")
        return {"sessionId": sid, "messages": record.messages}

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
        - usage 同路（_last_turn_usage）：金额和轮次都得让前端看得见，
          "成本可观测"不该只有终端能看。离线模型 usage 是空字典——诚实
          地回 {}，不编造 0
        """
        sid = params.get("sessionId","")
        text = params.get("message","")
        runtime = self._manager.get_session(sid)
        if runtime is None:
            # 这句会直接弹给用户看：说清"为什么发不出去"和"怎么办"。
            # 记录还在但运行时没了 = 已关闭，复活即可（历史接着用）。
            return {"error": (f"会话 {sid} 发不了：不存在或已关闭"
                              "（已关闭的点\"复活\"后可以继续对话）")}
        try:
            output = runtime.run_turn(text)
        except SessionLifecycleError as exc:
            return {"error": str(exc)}
        return {"output": output, "status": self._last_turn_status,
                "usage": self._last_turn_usage}


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
