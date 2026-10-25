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
  2. 客户端 = 读线程 + Future 多路复用（2026-09-26 重构）：读线程是唯一
     recv 的线程——响应按 id 塞进 future 字典；通知（approval/event）进
     单线程分派队列（FIFO 保序，审批阻塞只堵自己，不堵响应接收）。
     call() = 发请求（写锁保护）+ future.result(timeout)——任意多线程
     可并发 call，壳层那把全局 _rpc_lock 因此退役（"一轮 turn 堵死
     前端"的客户端一半从此不存在）。
  3. 死锁四违约的现况：①分派环吞结果 ③双线程同 recv ④event 无人消费
     ——全部**结构性消失**（读线程只按 id 配对、唯一 recv、常驻收信）；
     ②user_prompt 里重入 call() 仍是禁手：读线程活着所以不再必然死锁，
     但重入的 call 若再触发审批，审批通知会排在分派队列里、而分派线程
     正卡在这个 user_prompt 里——单消费者队列自己套自己 = 死锁回归。

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
import os
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from src.harness.agent import run_agent
from src.harness.automations import (
    AutomationError,
    AutomationScheduler,
    new_automation_id,
    next_run,
    parse_rrule,
)
from src.harness.compact import (
    COMPACT_THRESHOLD_TOKENS,
    compact,
    model_summarizer,
)
from src.harness.experts import (
    ExpertError,
    ExpertIndex,
    render_specialization,
)
from src.harness.permissions import Approver, AuditTrail, GovernedToolRunner
from src.harness.session import (
    SessionLifecycleError,
    SessionManager,
    SessionNotFoundError,
    SessionProcess,
    SessionState,
    SessionStore,
)
from src.harness.skills import SkillIndex, skill_marker
from src.harness.sqlite_store import HarnessDB
from src.harness.tools import ToolRegistry

# 工作区运行件工厂（s11 上传目录）：由装配层注入，harness 不认识 Workspace。
# 契约：收一个 JSON 化的请求 {"kind": "dir"|"zip", "path": ...}，成功返回
# (registry, policy, info)——info 是给 UI 看的不透明字典（id/root/kind），
# harness 只负责原样转交；失败**必须翻成 ValueError / OSError 抛出**
# （RPC 层只有这一条错误通道，与 session 四操作的约定一致）。
WorkspaceRuntimeBuilder = Callable[[dict], tuple]


class _SessionTurnControl:
    """TurnControl 的 sidecar 端实现（2026-09-27）：读 SessionProcess 的
    停止 / 暂停两个 Event。

    每次检查查一次 manager 字典（便宜）；运行时没了 = close 把它摘走，
    abort 按"已置"算——fail-closed：turn 不陪一个已死的会话跑完全程。
    pause 则要求运行时还在（会话都没了，暂停没有意义，且下一轮
    run_turn 开头本来就会清信号）。
    """

    def __init__(self, manager: SessionManager, sid: str) -> None:
        self._manager = manager
        self._sid = sid

    def abort_requested(self) -> bool:
        runtime = self._manager.get_session(self._sid)
        return runtime is None or runtime.abort_requested()

    def pause_requested(self) -> bool:
        runtime = self._manager.get_session(self._sid)
        return runtime is not None and runtime.pause_requested()


def _resolve_turn_workers() -> int:
    """并发 turn 上限（`WYWD_TURN_WORKERS`，默认 2）。

    为什么默认 2 而不是更大：每个 turn 占一个模型会话的配额与本地资源，
    单用户同时盯着两个会话已是常态、三个以上罕见；更激进只会放大"同时
    改同一批文件"的混乱。0/负数按默认处理并喊一声——静默吞错配置比
    报错更难查（shell.py 的 _idle_reap_seconds 同一条规矩）。
    """

    default = 2
    raw = (os.environ.get("WYWD_TURN_WORKERS") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[sidecar] WYWD_TURN_WORKERS 不是数字（{raw!r}），用默认 {default}")
        return default
    if value < 1:
        print(f"[sidecar] WYWD_TURN_WORKERS 必须 ≥1（{value}），用默认 {default}")
        return default
    return value


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
        default_session_cwd: str = ".",
        idle_timeout: float = 0.0,
        sweep_interval: float = 60.0,
        externalize: Optional[Callable[[str, str], str]] = None,
        compact_budget: int = COMPACT_THRESHOLD_TOKENS,
        skill_index: Optional[SkillIndex] = None,
        expert_index: Optional["ExpertIndex"] = None,
        metrics_db: Optional["HarnessDB"] = None,
        automation_interval: float = 0.0,
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
        # 新建会话不传 cwd 时的落点（2026-09-27）：装配层传什么就是什么
        # （web 装配传 .sandbox 沙箱——"默认就在项目根里干活"退役）。
        # 默认值 "." 保持老装配（测试 / 终端）行为不变。
        self._default_session_cwd = default_session_cwd
        # s11：工作区工厂（None = 这个 sidecar 不支持换沙箱，老装配零感知）。
        # 默认 info 说明"我跑在启动时那套边界上"，UI 拿它显示"项目根"。
        self._runtime_builder = runtime_builder
        self._workspace_info: dict = dict(self._initial_workspace_info)
        # "一个会话一个目录"：按 **cwd** 缓存的运行时（registry/policy/info）。
        # 同目录的多个会话共用一份（它们本就是同一个工作区），不同目录互不影响。
        self._runtime_cache: dict = {}
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
        # 技能索引（s16）：None = 这个 sidecar 不带技能（老装配零感知）。
        # 目录由装配层放进 history_seed，正文由这里的 _inject_skills 按需展开。
        self._skill_index = skill_index
        # 专家索引（s18）：None = 不带专家（老装配零感知）。激活发生在
        # session/create（expertId 参数）——specialization 作为一条 system
        # 消息写进起步历史，转录即证据，resume 自然带着人格回来。
        self._expert_index = expert_index
        # SQLite 统计库（s21）：None = 不启用（老装配零感知）。只增不删的
        # 投影——会话元数据镜像 + 用量/工具账；真相在 JSONL，这里只管
        # 查询面（SUM/GROUP BY 不用重放转录）。
        self._metrics_db = metrics_db
        # s22 自动化调度：有统计库就能调度（状态全在 SQLite）；
        # interval>0 才起 tick 线程（默认 0 = 只留 RPC，测试手动 tick）。
        self._scheduler = (AutomationScheduler(metrics_db,
                                               self._run_automation)
                           if metrics_db is not None else None)
        if self._scheduler is not None and automation_interval > 0:
            threading.Thread(target=self._automation_loop,
                             args=(automation_interval,),
                             daemon=True, name="automation-tick").start()
        self._history_seed = history_seed or (lambda: [])
        self.ring_buffer = RingBuffer()
        # s09：持久化注入 + 启动清账。
        # 1. store 透传（None → Manager 自己落 InMemory，离线测试零感知）：
        self._manager = SessionManager(
            turn_runner=self._make_turn_runner(),   # 兜底：没有 factory 时才用它
            store=store,
            # "一个会话一个目录"：每个会话按自己的 cwd 造 runner
            # （registry/policy 绑到它自己的沙箱根）。
            turn_runner_factory=self._make_turn_runner_for)
        # 2. 启动清账（startup reconciliation）：崩溃/直接退出遗留的
        #    creating/idle/running/closing 记录没有 runtime，证据文件却写着
        #    "活着"。close_session 对不 live 的记录会把状态抹成 closed（s07
        #    写好的幂等路径），正好用。放过 error：它是有效终态（原因留在
        #    last_error 里，resume 可直接接管）。InMemory 下 list() 为空清账空转。
        for record in self._manager.store.list():
            if record.status not in (SessionState.CLOSED, SessionState.ERROR):
                self._manager.close_session(record.id)
        # ── turn 后台化（并发改造第 2 步，2026-09-26）──────────────
        # 执行器在服务级（不是连接级）：连接换代不丢任务。max_workers 见
        # _resolve_turn_workers——"同一时刻至多一个 turn"从此只对**同一
        # 会话**成立（SessionProcess 的 turn 锁），不同会话并行跑。
        self._executor = ThreadPoolExecutor(
            max_workers=_resolve_turn_workers(), thread_name_prefix="wywd-turn")
        # 审批回程票注册表：approver（执行器线程）发票 → future；主循环
        # 读到 approval/response 完成它。旧的"嵌套 recv"在背景化之后会跟
        # 主循环抢 socket，必须换成这张表。
        self._approvals_lock = threading.Lock()
        self._pending_approvals: dict[str, Future] = {}
        # 并行 turn 的记账：status/usage 改线程本地。旧的两个实例属性在
        # 并行 turn 下会串台（A 的 status 被读成 B 的）；threading.local
        # 让每个执行器线程只看得到自己的账。
        self._turn_ctx = threading.local()
        # 全链发送锁：主循环（响应）、执行器（事件）、审批（发票）三个
        # 线程都会写同一条 socket——newline 帧不许被两线程撕开。
        self._conn_lock = threading.Lock()
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
        # s18 专家：目录给 UI 挑人用；激活走 session/create 的 expertId。
        self.rpc_handlers["expert/list"] = self._handle_expert_list
        # s21 统计：SQLite 索引层的查询面（用量/工具排行）。
        self.rpc_handlers["stats/summary"] = self._handle_stats_summary
        # s22 自动化：create/list/delete（软）；执行由 tick 驱动。
        self.rpc_handlers["automation/create"] = self._handle_automation_create
        self.rpc_handlers["automation/list"] = self._handle_automation_list
        self.rpc_handlers["automation/delete"] = self._handle_automation_delete
        # s07-b 四操作路由（老 session/destroy"一删全没"已退役）
        self.rpc_handlers["session/close"] = self._handle_session_close
        self.rpc_handlers["session/resume"] = self._handle_session_resume
        self.rpc_handlers["session/forget"] = self._handle_session_forget
        self.rpc_handlers["session/messages"] = self._handle_session_messages
        self.rpc_handlers["agent/send"] = self._handle_agent_send
        # 停止（2026-09-27）：协作式——置 abort 信号立刻返回，turn 在
        # run_agent 的循环检查点收场，结局走 turn_end（status=aborted）。
        self.rpc_handlers["agent/stop"] = self._handle_agent_stop
        # 暂停（2026-09-27）：与停止共用同一批检查点，只差状态名与文案。
        self.rpc_handlers["agent/pause"] = self._handle_agent_pause
        self.rpc_handlers["tool/list"] = self._handle_tool_list
        # s11：工作区（上传目录）——换沙箱 + 读当前。
        self.rpc_handlers["workspace/set"] = self._handle_workspace_set
        self.rpc_handlers["workspace/get"] = self._handle_workspace_get

    def _inject_skills(self, view: list[dict], message: str) -> list[dict]:
        """把命中触发词的技能**正文**追加进本轮视图（正文已在视图里就跳过）。

        分工：**目录**由装配层放进 history_seed（常驻、便宜）；**正文**在这里
        按需展开（贵、只在命中时）。

        去重扫的是**全视图**，不只 system 消息：技能正文有第二个入口——
        模型主动调 load_skill 工具，那份正文随 **tool** 消息进了 transcript
        （会落盘、会出现在后续轮次的视图里）。只扫 system 的话，这条路径
        加载过的技能会被触发词再注入一遍，同一份正文进上下文两次。
        标记头是 skill_marker（单一真源），两个入口共用，子串匹配认得出
        任何角色消息里的正文。

        刻意的取舍：
        - **去重的范围是"本轮视图"而不是"全部历史"**：正文若已被 compact
          压出视图，模型就真的看不见了，此时重新注入是**正确**行为——
          所以不维护"已加载集合"那类跨轮状态，看视图说话；
        - **重复命中就跳过**：同一个技能在长对话里可能被命中很多次，每次都追加
          就是白烧 token，而"再提醒一遍"的收益几乎为零——上文里已经有它了；
        - 自动注入的正文**不落盘**（它是本轮 view 的尾巴，appended 切片
          天然把它排除）——每轮按需重生，本来就不该进证据。load_skill
          那条路恰好相反：tool 消息是模型动作的记录，照常落盘可审计。
        - 任何异常都**不影响本轮**：技能是锦上添花，不是关键路径。
        """

        if self._skill_index is None:
            return view

        try:
            hits = self._skill_index.match(message)
        except Exception as error:
            print(f"[sidecar] 技能匹配失败（不影响本轮）：{error}")
            return view

        existing = "\n".join(str(item.get("content", "")) for item in view)
        for skill in hits:
            marker = skill_marker(skill.name)
            if marker in existing:
                continue
            try:
                body = self._skill_index.load(skill.name)
            except Exception as error:
                print(f"[sidecar] 技能 {skill.name} 加载失败（跳过）：{error}")
                continue
            view.append({"role": "system",
                         "content": f"{marker}（命中触发词后自动展开）\n\n{body}"})
        return view

    def _make_summarizer(self) -> Callable[[list[dict]], Optional[str]]:
        """L4 摘要器（s14）：直接复用 compact.model_summarizer。

        留这个方法只是为了让"摘要用哪个模型"这件事在 sidecar 里有个明确的
        落点——将来若要换更便宜的档位（比如用 router 的 lite 槽）摘要，
        改这一处即可，不必动管线。
        """

        return model_summarizer(self._model)

    def _make_turn_runner(self, registry=None, runner=None, sid: str = ""):
        """造 turn_runner 闭包：session 层的执行入口 → 本文件的 run_agent。

        `registry` / `runner` 不传就用 sidecar 级那套（老装配：所有会话共享）；
        传了就用传进来的——**这是"一个会话一个目录"的落点**：每个会话的 runner
        绑着它自己 cwd 的 registry 与 policy，"互不越界"才成立。

        两个设计点：
          - 闭包捕获 self、调用那一刻才读 self._runner / self._on_event：
            这俩是 per-connection 的，构造期还是 None，连接建立才有值
            （s06 handle_connection 的装配顺序决定了只能这么写）；
          - status/usage 是协议接缝的记账，走 self._turn_ctx
            （threading.local）：TurnRunner 协议的返回值只有
            (output, messages)，装不下 run_agent 的 status——run_turn
            返回后由执行器线程立刻读走，并行 turn 各记各的账不串台
            （第 2 步起 turn 在执行器线程跑，"同一时刻至多一个 turn"
            只对**同一会话**成立——SessionProcess 的 turn 锁）。结局
            由 _run_turn_task 广播进 turn_end 事件。
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
            # s16：命中触发词的技能正文在这里**按需展开**（目录早就在 system 里）。
            view = self._inject_skills(view, message)
            # 事件盖 sessionId 章（并发第 3 步）：round_start/tool_start 这些
            # 中途事件本来不带会话信息，多 tab 前端没法分流（按 sid 画直播行
            # 的前提）。turn_runner 是**按会话**造的（_make_turn_runner_for
            # 传 record.id），在这里盖章正合适。self._on_event 保持"调用那
            # 一刻才读"（per-connection，s06 的装配顺序不能破坏）。
            def _stamped(event: str, data: dict) -> None:
                # s21 工具账：中途事件不经过 _emit_event（那条路只走
                # turn_start/turn_end），钩子挂在这里才接得住 tool_end。
                # 记在订阅判断**之外**——没有直播订阅者账也要记，统计
                # 不依赖有没有人看。
                if (event == "tool_end" and self._metrics_db is not None
                        and data.get("name")):
                    try:
                        self._metrics_db.record_tool_call(
                            sid, str(data["name"]))
                    except Exception as exc:
                        self._log(f"metrics tool record failed: {exc}")
                base = self._on_event
                if base is not None:
                    base(event, {**data, "sessionId": sid})

            result = run_agent(
                message, model=self._model,
                registry=registry if registry is not None else self._registry,
                history=view,
                max_steps=self._max_steps,
                externalize=self._externalize,   # s13：超阈值输出换到磁盘
                on_event=_stamped if sid else self._on_event,
                runner=runner if runner is not None else self._runner,
                control=(_SessionTurnControl(self._manager, sid)
                         if sid else None))
            # 记账走**线程本地**（第 2 步起 turn 在执行器线程跑，可能并行）：
            # 每个线程只看得到自己这轮的 status/usage，同线程的调用方
            # （_run_turn_task）在 run_turn 返回后立刻读——并行不串台。
            self._turn_ctx.status = result.status
            # usage 是这一轮的账（run_agent 在轮内跨 step 累加出来的 totals）。
            # dict() 复制是防御习惯：存引用的话，将来谁在返回后动了那个字典，
            # 这里会跟着变——"快照存的应该是那一刻的值"（练习 08 的老教训）。
            self._turn_ctx.usage = dict(result.usage)

            # 落盘的必须是【原始历史 + 本轮新增】——压缩视图只发给模型，
            # **不进证据**。
            #
            # 为什么不直接 return result.messages：jsonl_store 是 append-only，
            # save 时校验「record.messages 的前缀 == 已落盘的内容」，而
            # session.py 写明的不变量是「record.messages **单调增长**，
            # trim 从不回写 Record」。早先直接把 result.messages 回写，
            # 压缩一旦真改了内容（L1 截断 / L2 去重 / L3 修剪 / L4 摘要**
            # 任一出面**），前缀就变了 → 第二轮保存被拒
            # （TranscriptCorruptionError），整个会话卡死。
            # 正确的分工：**模型看压缩视图，证据记原始历史**。
            #
            # 副产品：_inject_skills 展开的技能正文也不再落盘了——它每轮
            # 按需重生，本来就不该进证据。
            appended = result.messages[len(view):]
            return result.output, list(history) + appended
        return turn_runner

    def _runtime_for(self, cwd: str) -> tuple:
        """按目录取运行时三件套（registry, policy, info）——**带缓存**。

        为什么按目录缓存而不是按会话：同一个目录开两个会话时，它们**本该**共用
        同一套沙箱边界（那是同一个"工作区"），各建一份只会白吃内存；而不同目录
        **必须**互不影响——这正是"会话级沙箱"要的效果。

        没有 runtime_builder 的 sidecar（老装配）退回启动时那套：行为与改造前一致。
        """

        cached = self._runtime_cache.get(cwd)
        if cached is not None:
            return cached
        if self._runtime_builder is None:
            bundle = (self._initial_registry, self._initial_policy,
                      dict(self._initial_workspace_info))
        else:
            # 参数形状跟 workspace/set 保持一致（kind + path）——**同一座工厂**，
            # 不要为"按会话建"再造第二种方言。
            registry, policy, info = self._runtime_builder(
                {"kind": "dir", "path": cwd})
            bundle = (registry, policy, dict(info))
        self._runtime_cache[cwd] = bundle
        return bundle

    def _make_turn_runner_for(self, record: "SessionRecord"):
        """给**一个会话**造 runner：它的 cwd 决定用哪套沙箱。

        这是 SessionProcess 的 turn_runner_factory 入口（"一个会话一个目录"）。
        approver 绑当前连接的 socket——RPC 进来时连接一定在，所以拿得到
        （和 handle_connection 里造 runner 的时机同理）。
        """

        registry, policy, _info = self._runtime_for(record.cwd)
        runner = GovernedToolRunner(
            policy=policy,
            approver=self._make_approver(self._conn),
            registry=registry,
            audit=AuditTrail(),
        )
        return self._make_turn_runner(registry=registry, runner=runner,
                                      sid=record.id)

    def _log(self, msg: str) -> None:
        """写进 RingBuffer（模拟生产侧捕获子进程 stdout/stderr）。"""

        ts = time.strftime("%H:%M:%S")
        self.ring_buffer.write(f"[{ts}] [sidecar] {msg}\n")

    def _send(self, msg: dict) -> None:
        """线程安全的写（第 2 步起主循环/执行器/审批三个线程都会写 socket）：
        newline-delimited 的帧必须一气呵成，不许被两个线程撕成两半。"""

        with self._conn_lock:
            self._conn.send_message(msg)

    def _emit_event(self, event: str, data: dict) -> None:
        """后台线程安全的事件广播。连接不在（换代/已关/已停）就静默——
        事件是直播不是证据；发送失败也不能炸掉正在收尾的 turn。
        （中途事件的 sessionId 盖章与 s21 工具账都在 _stamped 那层——
        本方法只服务 turn_start/turn_end 这两个框架级事件。）
        """

        if self._on_event is None:
            return
        try:
            self._on_event(event, data)
        except Exception as exc:
            self._log(f"event emit failed: {exc}")

    def _record_usage(self, sid: str, usage: dict) -> None:
        """s21：一轮的 token 用量入 SQLite 索引。

        失败只喊一声不炸 turn——统计缺一轮可以忍，收尾路径不能陪葬。
        轮级汇总记在 craft 主档位名下；router 的分档账在装配层，本层
        只存 token 原始量（成本可后算，反之不行）。
        """

        if self._metrics_db is None or not usage:
            return
        try:
            self._metrics_db.record_usage(
                sid, "craft",
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0))
        except Exception as exc:
            self._log(f"metrics record failed: {exc}")

    def _metrics_upsert_session(self, sid: str) -> None:
        """s21：会话元数据镜像进 SQLite（查询面，不是真源）。"""

        if self._metrics_db is None:
            return
        try:
            record = self._manager.load_record(sid)
            self._metrics_db.upsert_session({
                "id": record.id,
                "cwd": record.cwd,
                "title": record.title,
                "status": record.status,
                "mode": record.mode,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created_at)),
                "updated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.updated_at)),
            })
        except Exception as exc:
            self._log(f"metrics upsert failed: {exc}")

    # ── 连接循环 + per-connection 装配 ─────────────────────────

    def handle_connection(self, conn: RPCConnection) -> None:
        """处理一条 RPC 连接：循环收消息 → 路由 → 回 result/error。

        第 2 步起本循环是**自由的**：agent/send 只提交不等待，turn 在执行
        器线程里跑——循环随时能收下一条请求（另一个会话发消息、查状态、
        关会话都不再被 turn 堵住）。它多了一项新职责：**兑审批回执**——
        approver 活在执行器线程，回执只能由本循环读进来完成 future。
        """
        self._conn = conn
        self._runner = GovernedToolRunner(
            policy=self._policy, approver=self._make_approver(conn),
            registry=self._registry, audit=AuditTrail())
        self._on_event = lambda event, data: self._send(
            {"jsonrpc": "2.0", "method": "event",
             "params": {"event": event, **data}, "id": None})
        self._log("new RPC connection established")
        while not self._shutdown:
            try: req = conn.recv_message()
            except Exception: break          # 对端强杀 OSError，同样退出
            if req is None: break            # EOF
            method = req.get("method",""); params = req.get("params") or {}
            req_id = req.get("id")
            self._log(f"RPC: {method} (id={req_id})")
            if method == "approval/response":
                self._complete_approval(params)   # 兑票：不进领域路由
                continue
            handler = self.rpc_handlers.get(method)
            if handler:
                try:
                    result = handler(params)
                    self._send({"jsonrpc":"2.0","result":result,"id":req_id})
                except Exception as error:
                    self._send({"jsonrpc":"2.0",
                        "error":{"code":-32603,"message":str(error)},"id":req_id})
                finally:
                    # agent/send 的 started 帧已上线（哪怕走的是 error 分支）
                    # ——放行执行器。别的请求没有 gate，这是空操作。
                    self._release_started_gate()
            else:
                self._send({"jsonrpc":"2.0",
                    "error":{"code":-32601,"message":f"Method not found: {method}"},
                    "id":req_id})
        # 循环退出（EOF/关停）：挂起的审批全部 fail-closed——执行器线程里
        # 等回执的 approver 会拿到 False，turn 不会陪连接一起挂死。
        self._fail_pending_approvals()
        conn.close()
        self._log("RPC connection closed")

    def _complete_approval(self, params: dict) -> None:
        """主循环兑票：按 request_id 找到挂着的 future 并完成它。"""

        with self._approvals_lock:
            future = self._pending_approvals.get(params.get("request_id"))
        if future is None:
            # 票已超时被收走 / 重复回执：喊一声，不炸。
            self._log("approval/response for unknown ticket (ignored)")
            return
        future.set_result(bool(params.get("approved")))

    def _fail_pending_approvals(self) -> None:
        """连接收尾时把所有挂起审批置为失败（approver 侧 fail-closed 返回 False）。"""

        with self._approvals_lock:
            pending = list(self._pending_approvals.values())
            self._pending_approvals.clear()
        for future in pending:
            try:
                future.set_exception(ConnectionClosed("sidecar 连接中断"))
            except Exception:
                pass   # 极端竞态下已 done——收尾逻辑不许再炸

    def _make_approver(self, conn: RPCConnection) -> Approver:
        """审批回程票（2026-09-26 改造）：发票 → future 注册表，主循环兑票。

        背景化之后 approver 活在**执行器线程**，而主循环是另一个线程——
        旧的"嵌套 recv"会跟主循环抢同一条 socket（两个 recv 者把帧撕开）。
        现在：发票走 _send 锁；回执由主循环读进来完成 future；连接断/
        关停时 future 被置异常 → fail-closed 返回 False（turn 拿到
        permission_blocked，不陪连接挂死）。
        """
        def approver(decision) -> bool:
            ticket = str(uuid.uuid4())
            future: Future = Future()
            with self._approvals_lock:
                self._pending_approvals[ticket] = future
            try:
                self._send({"jsonrpc":"2.0","method":"approval/request",
                    "params":{"request_id":ticket,"rule_id":decision.rule_id,
                              "reason":decision.reason},"id":None})
                return bool(future.result())
            except Exception:
                # 断连 / 关停 / 收尾炸掉 future：没人批准 = 拒绝（fail-closed）
                return False
            finally:
                with self._approvals_lock:
                    self._pending_approvals.pop(ticket, None)
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
        # 执行器：不再接新任务，排队的取消；**正在跑的 turn 让它跑完**——
        # 它的 turn_end 会照常广播（连接可能已关，事件静默丢弃）。
        self._executor.shutdown(wait=False, cancel_futures=True)
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

        不传 cwd 时的默认落点由装配层决定（self._default_session_cwd，
        web 装配传 .sandbox 沙箱）——"新建会话默认在项目根里干活"退役。

        s18 专家激活（expertId 可选）：specialization 作为一条 system
        消息追加在起步历史**末尾**（叠加不覆盖），写进两个副本——
        转录即证据，resume/重开自然带着人格回来，不需要额外字段。
        未知专家/坏包翻成 ValueError 人话（RPC 的唯一错误通道）。
        """
        sid = self._manager.create_session(
            cwd=params.get("cwd", self._default_session_cwd),
            mode=params.get("mode", "craft"),
            title=params.get("title", "未命名会话"))
        runtime = self._manager.get_session(sid)
        seed = self._history_seed()
        expert_id = str(params.get("expertId") or "").strip()
        if expert_id:
            if self._expert_index is None:
                raise ValueError(
                    "这个装配没有专家索引（.workbuddy/experts/）——无法激活专家")
            try:
                package = self._expert_index.load(expert_id)
            except ExpertError as error:
                raise ValueError(str(error)) from error
            seed.append({"role": "system",
                         "content": render_specialization(package)})
            self._log(f"expert activated: {expert_id}")
        runtime.record.messages = seed
        self._manager.store.save(runtime.record)
        self._metrics_upsert_session(sid)      # s21：元数据镜像进索引
        self._log(f"session created: {sid}")
        return {"sessionId": sid}

    def _handle_expert_list(self, params: dict) -> dict:
        """专家目录（s18）：给 UI 的摘要清单——正文等激活时才加载。"""

        if self._expert_index is None:
            return {"experts": []}
        return {"experts": self._expert_index.list()}

    def _handle_stats_summary(self, params: dict) -> dict:
        """统计查询面（s21）：用量汇总 + 工具排行，SQLite 出，不重放转录。

        month='YYYY-MM' 时多给一份月度视图；未启用统计库的装配明确说
        没有，而不是给一串零——零和"没这功能"是两个答案。
        """

        if self._metrics_db is None:
            return {"enabled": False,
                    "reason": "这个装配没有启用 SQLite 统计库"}
        month = str(params.get("month") or "").strip() or None
        summary = {
            "enabled": True,
            "total": self._metrics_db.usage_summary(),
            "top_tools": self._metrics_db.top_tools(5),
        }
        if month:
            summary["monthly"] = self._metrics_db.usage_summary(month)
        return summary

    # ── s22 自动化调度 ────────────────────────────────────────

    def _automation_loop(self, interval: float) -> None:
        """tick 线程：睡 interval 秒 → tick 一次。单次失败只记日志——
        调度线程崩了等于所有自动任务停摆，必须兜住一切异常。"""

        import time as _time
        while True:
            _time.sleep(interval)
            try:
                self._scheduler.tick()
            except Exception as exc:
                self._log(f"automation tick failed: {exc}")

    def _run_automation(self, spec: dict) -> str:
        """隔离会话执行：新建会话跑 prompt，跑完关闭——不复用任何
        用户会话，转录即证据（哪次自动任务干了什么，事后可查）。"""

        sid = self._manager.create_session(
            cwd=self._default_session_cwd, mode="craft",
            title=f"自动任务:{spec['name']}")
        runtime = self._manager.get_session(sid)
        runtime.record.messages = self._history_seed()
        self._manager.store.save(runtime.record)
        self._metrics_upsert_session(sid)
        try:
            return runtime.run_turn(spec["prompt"])
        finally:
            self._manager.close_session(sid)

    def _handle_automation_create(self, params: dict) -> dict:
        """注册自动化：once 给 scheduled_at，recurring 给合法 RRULE。"""

        if self._scheduler is None:
            return {"error": "这个装配没有启用自动化调度（无统计库）"}
        name = str(params.get("name") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        schedule_type = str(params.get("schedule_type") or "").strip()
        if not name or not prompt:
            return {"error": "name 与 prompt 都不能为空"}
        if schedule_type not in ("once", "recurring"):
            return {"error": "schedule_type 只支持 once / recurring"}
        rrule = str(params.get("rrule") or "").strip() or None
        scheduled_at = str(params.get("scheduled_at") or "").strip() or None
        from datetime import datetime, timezone
        if schedule_type == "recurring":
            try:
                parse_rrule(rrule or "")
                upcoming = next_run(
                    rrule, datetime.now(timezone.utc))
            except AutomationError as error:
                return {"error": f"rrule 不合法：{error}"}
            upcoming_iso = upcoming.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            if not scheduled_at:
                return {"error": "once 类型必须给 scheduled_at（ISO 时间）"}
            upcoming_iso = scheduled_at
        auto_id = new_automation_id()
        self._metrics_db.create_automation({
            "id": auto_id, "name": name, "prompt": prompt,
            "schedule_type": schedule_type, "rrule": rrule,
            "scheduled_at": scheduled_at, "status": "ACTIVE",
        })
        self._metrics_db.upsert_runtime_state(
            auto_id, None, upcoming_iso, None)
        self._log(f"automation created: {auto_id} ({schedule_type})")
        return {"automationId": auto_id, "nextRun": upcoming_iso}

    def _handle_automation_list(self, params: dict) -> dict:
        if self._scheduler is None:
            return {"automations": []}
        return {"automations": self._metrics_db.list_automations(
            include_deleted=bool(params.get("includeDeleted")))}

    def _handle_automation_delete(self, params: dict) -> dict:
        """软删除（NEVER DELETE FROM）——行保留，可恢复。"""

        if self._scheduler is None:
            return {"error": "这个装配没有启用自动化调度（无统计库）"}
        auto_id = str(params.get("automationId") or "").strip()
        if not auto_id:
            return {"error": "automationId 不能为空"}
        self._metrics_db.set_automation_status(auto_id, "deleted")
        self._log(f"automation soft-deleted: {auto_id}")
        return {"status": "ok"}

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
        self._metrics_upsert_session(params.get("sessionId", ""))  # s21
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
        self._metrics_upsert_session(sid)      # s21：generation/状态变了
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
        # s21：索引层软删除（行保留，查询面不再出现）——JSONL 侧由
        # forget 真删（用户明确要求的删除），两层各自的语义。
        if self._metrics_db is not None:
            try:
                self._metrics_db.soft_delete_session(sid)
            except Exception as exc:
                self._log(f"metrics soft delete failed: {exc}")
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
        """换**这个会话**的工作目录（"一个会话一个目录"）。

        目录属于**会话**（`Record.cwd` 本来就带着它），不属于进程——所以这里
        不再换本进程的 registry/policy，而是：

          1. 按请求造一套运行件（kind=dir 直接引用本机目录 / default 复位）；
          2. 把新目录写进**这个会话的 record.cwd**（可序列化，重启后还在）；
          3. 让它**重建运行时**——close 掉，下次发消息时前端会自动 resume，
             resume 走的 factory 会按新 cwd 造 runner。

        为什么用"重建"而不是"就地换 runner"：runner 是**运行时**的资源
        （它构造时绑死 registry/policy），"重建一代"比"给活着的运行时换心脏"
        干净——这也正是 SessionProcess 的设计意图（resume 不是复活旧进程，
        而是用旧记录造一个新的运行时）。

        已建会话的历史**不动**：历史属于会话，目录属于边界——混在一起会让
        "换个目录看看"变成"清空对话"。代价是旧历史里的路径在新沙箱可能不存在
        （模型会收到"没有这个路径"，它会自己适应）。

        kind="default" 是**复位**：cwd 回到启动态那个根（装配时注入的）。
        """

        sid = str(params.get("sessionId") or "").strip()
        runtime = self._manager.get_session(sid) if sid else None
        if runtime is None:
            return {"error": f"会话 {sid or '（未指定）'} 不在运行——先把它打开再换目录"}

        if params.get("kind") == "default":
            # 复位：不碰工厂，回启动态那套运行件（info 也回启动态）。
            registry, policy = self._initial_registry, self._initial_policy
            info = dict(self._initial_workspace_info)
            new_root = str(self._initial_workspace_info.get("root") or ".")
        else:
            if self._runtime_builder is None:
                return {"error": "这个 sidecar 没接工作区能力（装配层未注入 runtime_builder）"}
            try:
                registry, policy, info = self._runtime_builder(params)
            except (ValueError, OSError) as exc:
                # 唯一错误通道：装配层负责把库异常翻译成人话（见契约注释）。
                return {"error": str(exc)}
            new_root = str(info.get("root") or params.get("path") or "")

        # 写进 cwd 前先确认目录真的存在。cwd 会被 resume 用 strict=True 校验，
        # 存一个不存在的路径进去 = 这个会话**下次打不开**——而且要到那时候才
        # 报错，离现场太远。fail-closed 在这里的含义是：现在就说。
        if new_root:
            try:
                Path(new_root).expanduser().resolve(strict=True)
            except (OSError, ValueError) as exc:
                return {"error": f"这个目录用不了：{exc}"}

        # 新造好的运行时顺手进缓存：别的会话再选同一个目录就直接复用。
        if new_root:
            self._runtime_cache[new_root] = (registry, policy, dict(info))

        # 写进这个会话的记录（cwd 是 Record 字段，能进 Store、能序列化）。
        runtime.record.cwd = new_root
        self._manager.store.save(runtime.record)

        # 重建运行时：close + **立刻 resume**——resume 走的 factory 会用新 cwd
        # 造 runner。为什么立刻 resume 而不是"等下次发消息自动 resume"：
        # ① 连改两次目录时（dir → default），第二次会找不到 live runtime 而失败；
        # ② 改完就能接着发消息，不用等前端点一下。
        self._manager.close_session(sid)
        reloaded = False
        try:
            self._manager.resume_session(sid)
            reloaded = True
        except (SessionLifecycleError, SessionNotFoundError, OSError, ValueError) as exc:
            # 重建失败不回滚 cwd（记录已经写进去了），但要如实说"没活过来"。
            self._log(f"workspace reload failed for {sid}: {exc}")

        self._log(f"workspace switched (session {sid}): {new_root or info.get('kind', '?')}")
        return {"status": "ok", "workspace": dict(info), "reloaded": reloaded}

    def _handle_workspace_get(self, params: dict) -> dict:
        """读**这个会话**的工作目录（UI 渲染用）。

        会话不在运行也能读——它记在 record 里（`store.load` 取回）。
        没有任何线索时退回启动态描述（保持老行为，UI 不会看到空值）。
        """

        sid = str(params.get("sessionId") or "").strip()
        record = None
        runtime = self._manager.get_session(sid) if sid else None
        if runtime is not None:
            record = runtime.record
        elif sid:
            try:
                record = self._manager.store.load(sid)
            except (SessionNotFoundError, SessionLifecycleError, OSError, ValueError):
                record = None

        if record is None or not record.cwd:
            return {"workspace": dict(self._initial_workspace_info)}
        cached = self._runtime_cache.get(record.cwd)
        if cached is not None:
            return {"workspace": dict(cached[2])}
        return {"workspace": {"kind": "dir", "root": record.cwd}}

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
        """提交一个 turn（后台化，第 2 步）：立刻回 started，结局走事件。

        - get_session 只回 live 运行时：closed 的会话在这里就是 None——
          "记录还在"和"运行时活着"是两回事（s07 学习目标⑤）
        - **预检**：会话不在 IDLE（正在跑上一轮）就同步拒绝——人话直接回
          给本次请求，而不是提交之后在后台失败。竞态窗口（两个同时发、
          都过了预检）仍存在，那时由 run_turn 的 turn 锁兜底，turn_end
          事件如实报 failed
        - turn 在执行器线程里跑（_run_turn_task）：run_agent 的拼装、
          压缩、审批、事件直播全在既有闭包里；结束后广播 turn_end
          （带 status/output/usage）——壳层等它收结果，主循环继续伺候
          其他 RPC。"一轮堵全部"在服务端的那一半就此解除
        - turnId 由壳生成、这里原样回传并在 turn_end 里带回——壳按它
          配对结局（按 sid 配对会被快速连发串台：A 的结局灌给 B 的等待）
        """
        sid = params.get("sessionId", "")
        text = params.get("message", "")
        turn_id = str(params.get("turnId") or "")
        runtime = self._manager.get_session(sid)
        if runtime is None:
            # 这句会直接弹给用户看：说清"为什么发不出去"和"怎么办"。
            # 记录还在但运行时没了 = 已关闭，复活即可（历史接着用）。
            return {"error": (f"会话 {sid} 发不了：不存在或已关闭"
                              "（已关闭的点\"复活\"后可以继续对话）")}
        if runtime.status != SessionState.IDLE:
            return {"error": f"会话 {sid} 正在跑上一轮，等它结束再发"}
        # started 帧必须**先于一切 turn 事件**上线——这是后台化的隐性契约，
        # 不钉住就会错位：submit 一返回，执行器线程就可能已经把整个 turn
        # 跑完（离线模型微秒级），事件流先于 started 出现在线上。按序收
        # 结果的客户端会把 started 当成**下一个请求**的响应，此后所有
        # 响应整体错位一格（实测：test_session_resume_continues_history
        # 的 resume 拿到的是 close 的结果，KeyError: generation）。
        # 修法：执行器拿着 gate 等主循环把 started 帧发完再开跑——
        # 发布点在 handle_connection 的 finally（_release_started_gate）。
        gate = threading.Event()
        self._started_gate = gate
        self._executor.submit(self._run_turn_task, sid, text, runtime,
                              turn_id, gate)
        self._log(f"turn submitted: {sid} (turnId={turn_id[:8]})")
        return {"sessionId": sid, "status": "started", "turnId": turn_id}

    def _release_started_gate(self) -> None:
        """started 帧已上线（或响应已失败）——放行执行器。无 gate 是空操作。"""

        gate = getattr(self, "_started_gate", None)
        if gate is not None:
            self._started_gate = None
            gate.set()

    def _run_turn_task(self, sid: str, text: str,
                       runtime: SessionProcess, turn_id: str,
                       started_gate: threading.Event | None = None) -> None:
        """执行器线程里跑一个 turn，结束时广播 turn_end（壳等它收结果）。

        三种结局都如实上报：正常完成（status 来自 run_agent 的诚实汇报）、
        被状态机拒绝（并发 turn / close 竞态——SessionLifecycleError）、
        内部异常。连接已死时事件静默丢弃（_emit_event 兜住），turn 照常收尾。
        """
        # 等 started 帧上线（防乱序）。正常情况主循环发完响应立刻 set；
        # 5 秒超时是 fail-open——响应丢了 turn 也不能陪着挂死。
        # **只挂在 RPC 连接上才需要等**：直调 handler 的路径（测试等）
        # 没有 socket，"响应先行"无从谈起，gate 视为已放开。
        if (started_gate is not None and self._conn is not None
                and not started_gate.wait(timeout=5.0)):
            self._log("started 帧超时未上线，turn 照常开跑")
        self._emit_event("turn_start", {"sessionId": sid, "turnId": turn_id})
        try:
            output = runtime.run_turn(text)
            status = getattr(self._turn_ctx, "status", "completed")
            usage = getattr(self._turn_ctx, "usage", {})
            self._emit_event("turn_end", {"sessionId": sid, "turnId": turn_id,
                                          "status": status, "output": output,
                                          "usage": usage})
            self._record_usage(sid, usage)   # s21：用量入账（失败不炸收尾）
        except SessionLifecycleError as exc:
            self._log(f"turn rejected: {sid}: {exc}")
            self._emit_event("turn_end", {"sessionId": sid, "turnId": turn_id,
                                          "status": "failed", "output": "",
                                          "usage": {}, "error": str(exc)})
        except Exception as exc:
            self._log(f"turn crashed: {sid}: {exc!r}")
            self._emit_event("turn_end", {"sessionId": sid, "turnId": turn_id,
                                          "status": "failed", "output": "",
                                          "usage": {},
                                          "error": f"turn 内部异常：{exc}"})

    def _handle_agent_stop(self, params: dict) -> dict:
        """停止一个在跑的 turn（协作式，2026-09-27）。

        置 abort 信号**立刻返回**——turn 在 run_agent 的下一个检查点
        收场（最迟等完当前的模型调用 / 工具 / 审批），结局由 turn_end
        事件广播（status=aborted），不是本响应的职责。没有在跑的 turn
        回人话：用户点了停止却石沉大海，比报错更糟。
        """
        sid = str(params.get("sessionId") or "").strip()
        runtime = self._manager.get_session(sid) if sid else None
        if runtime is None or runtime.status != SessionState.RUNNING:
            return {"error": (f"会话 {sid or '（未指定）'} 没有在跑的 turn"
                              "——停不了没在跑的东西")}
        runtime.abort()
        self._log(f"stop requested: {sid}")
        return {"sessionId": sid, "status": "stop_requested"}

    def _handle_agent_pause(self, params: dict) -> dict:
        """暂停一个在跑的 turn（协作式，2026-09-27）。

        与停止共用同一批检查点，只差状态名与文案（paused，不是失败）。
        置信号立刻返回；turn 在下一个检查点收场（最迟等完当前的模型
        调用 / 工具 / 审批），结局走 turn_end 事件。「继续」即恢复。
        """
        sid = str(params.get("sessionId") or "").strip()
        runtime = self._manager.get_session(sid) if sid else None
        if runtime is None or runtime.status != SessionState.RUNNING:
            return {"error": (f"会话 {sid or '（未指定）'} 没有在跑的 turn"
                              "——暂停不了没在跑的东西")}
        runtime.request_pause()
        self._log(f"pause requested: {sid}")
        return {"sessionId": sid, "status": "pause_requested"}


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
    """主进程的 RPC 客户端（2026-09-26 重构）：读线程 + Future 多路复用。

    旧版（s06~s16）是"单线程 + 收信分派环"：call 阻塞在 recv，半路混进
    来的审批/事件就地消费。致命约束是**同一时刻至多一个 call**（死锁违约
    ③：双线程同 recv）——壳只能拿一把 _rpc_lock 把所有调用串成单队，
    一轮长 turn 堵住整个前端。

    新版把"读"和"等"拆开：
      - 读线程（wywd-rpc-reader）：**唯一** recv 的线程。响应按 id 塞进
        future 字典；通知（approval/event）丢进单线程分派队列。读线程
        永不阻塞——任何时候都在收，④event 排在 result 前面挡住它的问题
        从此不存在。
      - 通知分派线程（wywd-rpc-notifier）：单消费者 FIFO。事件保序；
        审批在这里调 user_prompt（可能阻塞到审批超时）——只堵自己，
        读线程照常收响应，审批期间其余 RPC 不再挨饿。
      - call()：发请求（_send_lock 保护写）+ future.result(timeout)。
        任意多线程可并发；timeout 到点抛 TimeoutError，晚到的响应由
        读线程按"未知 id"静默丢弃，不串台。

    旧死锁四违约的现况（详见模块 docstring 机制③）：①③④结构性消失；
    ②user_prompt 里重入 call() 仍是禁手——单消费者队列自套死锁。
    """

    def __init__(
        self,
        user_prompt: Optional[Callable[[str, str], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self._conn: Optional[RPCConnection] = None
        self._rpc_id = 0
        self._user_prompt = user_prompt or _terminal_prompt
        self._on_event = on_event
        # 连接断/关的回调（第 2 步：壳靠它唤醒"等 turn 结局"的 send_to）。
        # 幂等：多个退出路径都会触发，_disconnect_notified 挡重复。
        self._on_disconnect = on_disconnect
        self._disconnect_notified = False
        # 并发三件套：id 分配锁（防撞号）/ 写锁（帧不许被两线程撕开）/
        # future 登记（读线程配对响应、call 等待、close 唤醒，三方共用）。
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._futures_lock = threading.Lock()
        self._futures: dict[int, Future] = {}
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._notifier: Optional[threading.Thread] = None
        self._closed = False

    def connect(self, sock: socket.socket) -> None:
        if self._conn is not None:
            raise RuntimeError("MainProcessClient 已连接，不要重复 connect")
        self._conn = RPCConnection(sock)
        # 分派线程先起：读线程一开工就可能往队列里丢通知。
        self._notifier = threading.Thread(target=self._dispatch_loop,
                                          name="wywd-rpc-notifier", daemon=True)
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="wywd-rpc-reader", daemon=True)
        self._notifier.start()
        self._reader.start()

    def call(self, method: str, params: Optional[dict] = None,
             timeout: Optional[float] = None) -> dict:
        """发一个 JSON-RPC 请求并等它 id 配对的响应。

        多线程安全（可并发 call）。timeout（秒）到点抛 TimeoutError：
        future 从登记表摘除，晚到的响应被读线程按未知 id 丢弃——调用方
        放弃了这次配对，就不能让迟到的结果再灌给谁。
        """
        if self._conn is None:
            raise ConnectionClosed("客户端未连接（先 connect）")
        if self._closed:
            raise ConnectionClosed("sidecar 连接中断")
        with self._id_lock:
            self._rpc_id += 1
            req_id = self._rpc_id
        future: Future = Future()
        with self._futures_lock:
            self._futures[req_id] = future
        try:
            self._send({"jsonrpc": "2.0", "method": method,
                        "params": params or {}, "id": req_id})
        except OSError:
            # 对端已关闭：sendall 也会抛（WinError 10038），一样诚实失败。
            with self._futures_lock:
                self._futures.pop(req_id, None)
            raise ConnectionClosed("sidecar 连接中断") from None
        try:
            return future.result(timeout)
        except TimeoutError:
            with self._futures_lock:
                self._futures.pop(req_id, None)
            raise

    def _send(self, msg: dict) -> None:
        """线程安全的写。call 的请求线程与审批回执的分派线程都会写 socket：
        newline-delimited 的帧必须一气呵成，不许被两个线程撕成两半。"""

        with self._send_lock:
            self._conn.send_message(msg)

    def _notify_disconnect(self) -> None:
        """连接断/关回调（幂等）：壳靠它唤醒"等 turn 结局"的等待者。"""

        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        if self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception as exc:
                print(f"[rpc] on_disconnect 回调失败：{exc}")

    def _reader_loop(self) -> None:
        """唯一 recv 的线程：响应按 id 配对给 future，通知丢进分派队列。

        它阻塞 = 全体 call 挂死，所以最外层还有一道兜底：任何没料到的
        异常都要把挂起的 call 全部唤醒（诚实失败），而不是悄悄死掉。
        """
        try:
            while True:
                try:
                    msg = self._conn.recv_message()
                except OSError:
                    self._fail_pending(ConnectionClosed("sidecar 连接中断"))
                    self._notify_disconnect()
                    return
                if msg is None:
                    self._fail_pending(ConnectionClosed("sidecar 进程已退出"))
                    self._notify_disconnect()
                    return
                if "method" in msg:
                    # 通知：丢给分派线程。读线程绝不自己处理——处理可能
                    # 阻塞（审批要等人点头），堵在这里 = 所有响应挨饿。
                    self._queue.put(msg)
                else:
                    with self._futures_lock:
                        future = self._futures.pop(msg.get("id"), None)
                    if future is not None:
                        future.set_result(msg)
                    # 没有配对对象的响应（超时被弃 / close 后晚到）→ 静默丢弃
        except Exception as exc:
            self._fail_pending(ConnectionClosed(f"读线程异常退出：{exc!r}"))
            self._notify_disconnect()

    def _dispatch_loop(self) -> None:
        """通知的单消费者：FIFO 保事件顺序，审批阻塞不外溢。"""
        while True:
            msg = self._queue.get()
            if msg is None:            # close() 的收尾哨兵
                return
            try:
                self._handle_notification(msg)
            except Exception as exc:
                # 通知处理失败不影响 RPC 主流程：喊一声，继续下一个。
                print(f"[rpc] 通知处理失败：{exc}")

    def _handle_notification(self, msg: dict) -> None:
        method = msg.get("method", "")
        if method == "approval/request":
            p = msg.get("params") or {}
            try:
                ok = bool(self._user_prompt(p.get("rule_id", ""),
                                            p.get("reason", "")))
            except Exception as exc:
                # approver（UI 那侧）坏了 = 没人批准 → fail-closed 拒绝：
                # sidecar 会回灌 permission_blocked，循环照常走。
                print(f"[rpc] approver 抛异常，按拒绝处理：{exc}")
                ok = False
            self._send({"jsonrpc": "2.0", "method": "approval/response",
                        "params": {"request_id": p.get("request_id"),
                                   "approved": ok},
                        "id": None})
        elif method == "event" and self._on_event:
            self._on_event(msg.get("params") or {})

    def _fail_pending(self, exc: Exception) -> None:
        """把所有挂着的 call 唤醒并如实失败——close/断连时没人该被挂死。

        在 _futures_lock 下"取走全部 + 清空"，两个退出路径（close 与读
        线程自己的收尾）谁先到谁清账，后到的拿到空表，天然幂等。
        """
        with self._futures_lock:
            pending = list(self._futures.values())
            self._futures.clear()
        for future in pending:
            try:
                future.set_exception(exc)
            except Exception:
                pass   # 极端竞态下已 done——收尾逻辑不许再炸

    def close(self) -> None:
        """干净收尾（幂等）：关 socket → 叫醒分派线程 → 失败所有挂起 call。

        先 fail_pending 再 join 读线程：等响应的人不该陪收尾排队。读线程
        自己的退出路径也会 fail_pending——与这里是同一个幂等路径。
        """
        if self._closed:
            return
        self._closed = True
        if self._conn is not None:
            try:
                self._conn.close()   # EOF：读线程 recv 返回 None / OSError
            except OSError:
                pass
        self._queue.put(None)        # 分派线程的收尾哨兵
        self._fail_pending(ConnectionClosed("sidecar 连接中断"))
        if self._notifier is not None:
            self._notifier.join(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=2)
        self._notify_disconnect()
