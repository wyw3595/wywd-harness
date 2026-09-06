"""s07 Session Management：逻辑会话可恢复，运行时必须重建。

📚 这一课在做什么（承接 s06）
  s06 的 session 是字典里的一行：

      self.sessions[sid] = {"id": ..., "messages": [...], "status": "running"}

  身份（对话历史）和"正在执行它的那套东西"混在一起，带来三个真实问题：
    1. session/destroy 一删全没——"关掉运行时"和"删除历史"没有分开；
    2. 崩溃后 status="running" 成了僵尸——记录说活着，进程早没了。
       持久化里的 running 只是最后一次观测，不是存活证明；
    3. 没有"恢复"——想接着上次的对话，没有正规入口。

  s07 把两类对象拆开（本课最重要的一张表）：

  ┌──────────────────────────┬────────────────────────────────┐
  │ SessionRecord（逻辑会话）  │ SessionProcess（运行时代）       │
  ├──────────────────────────┼────────────────────────────────┤
  │ 可跨 runtime 存活          │ 只属于一代 runtime（generation） │
  │ id / cwd / mode /          │ turn 锁 / abort 信号 /          │
  │   transcript / generation  │   turn 执行入口                  │
  │ 能进 Store、能序列化        │ 绝不能序列化——只能重建           │
  └──────────────────────────┴────────────────────────────────┘

  恢复语义一句话：resume 不是复活旧进程，而是用旧记录造一个新的运行时。

  四个生命周期操作（各改什么）：
    create  新 id + 第 1 代 runtime；启动失败留 error + last_error，
            记录不静默消失
    close   释放当前 runtime，记录和 transcript 保留（幂等）
    resume  旧 id + generation+1，全新 runtime；该 id 已 live 则拒绝
            （两个执行器写同一段 transcript = 竞争与副作用失序）
    forget  真正删除记录；必须先 close（防手滑丢历史）

  transcript ≠ 长期记忆（本课学习目标④）：
    transcript 只属于一个 session id，按 turn 顺序追加，用于"继续这段
    对话"；记忆跨会话，要提取/筛选/遗忘。memory.trim_history 是
    transcript 的"截取最近窗口"读取策略，不是记忆系统（那是 s10+ 的事）。

  与教材的差异（架构适配，不是偷工减料）：
  - 教材每代 runtime 起一个 ACP-like HTTP listener（bind 端口 0 原子
    分配）。我们不复制它：transport 只保留一套（s06 的 JSON-RPC
    sidecar——"机制只保留一套实现"）。我们这代 runtime 的临时资源 =
    turn 锁 + abort 信号 + 注入的 turn_runner。"端口课"照上：sidecar
    进程、RPC 连接就是我们的"端口"——resume 后一样全部重建，一样
    绝不进 Record。
  - 教材 provider 写死 Anthropic SDK；我们注入 turn_runner：
        (message, history) -> (output, 完整的新 messages)
    本文件不 import run_agent——session 层不认识 agent 层；离线测试
    给假 runner，真接线（s07-b）才闭包 run_agent + 治理 runner。

  学习目标（教材 README 五问）：
    ① "会话仍存在"和"会话进程仍活着"为什么是两回事 → Record vs Process
    ② create/resume/close/forget 各改什么 → 模块头四行表 + Manager
    ③ 端口/线程/锁/client 为什么不能持久化恢复 → Record 刻意不含它们
    ④ transcript 和 memory 的区别 → 上面的表
    ⑤ UI 上的 running 为什么不是存活证明 → Manager._runtimes 才是权威
"""

import copy
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol

# turn 的执行入口（依赖注入）：入参 (message, history)，
# 出参 (output, 完整的新 messages)。失败就 raise。
TurnRunner = Callable[[str, list[dict]], tuple[str, list[dict]]]

# 会话模式（s06 的 session/create 已收 mode 参数，这里给出合法值清单）
MODE_CRAFT = "craft"   # 立即动手
MODE_PLAN = "plan"     # 先计划再动手
MODE_ASK = "ask"       # 只聊天，不用工具
SESSION_MODES = frozenset({MODE_CRAFT, MODE_PLAN, MODE_ASK})


# ═══════════════════════════════════════════════════════════════
# 状态机 — Manager / 运行时 / UI 共用的状态词汇表
# ═══════════════════════════════════════════════════════════════

class SessionState(str, Enum):
    """六态生命周期。

    继承 str 的红利：(str, Enum) 混血让 SessionState.IDLE **就是** "idle"
    ——SessionState.IDLE == "idle" 为 True，json.dumps 直接序列化成
    "idle"，进 Record、进 Store、进 JSON 都不用手动 .value。
    """

    CREATING = "creating"   # 正在分配运行时资源
    IDLE = "idle"           # runtime 就绪，等待输入
    RUNNING = "running"     # 正在执行一个 turn
    CLOSING = "closing"     # 正在释放运行时资源
    CLOSED = "closed"       # 没有 live runtime；记录和 transcript 还在
    ERROR = "error"         # 当前 generation 失败，原因在 last_error


# TODO 1（你来填）：合法迁移表——状态机的唯一真源（防各处随手改字符串）。
#   按下面的表写 dict[str, frozenset[str]]（键值用 SessionState 常量）：
#     creating → {idle, closing, error}
#     idle     → {running, closing, error}
#     running  → {idle, closing, error}
#     closing  → {closed, error}
#     closed   → {}            （终点；复活的唯一途径是 Manager resume）
#     error    → {closing, closed}
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = ...


class SessionLifecycleError(RuntimeError):
    """非法生命周期操作（状态机拒绝 / 违反使用顺序）。"""



class SessionNotFoundError(SessionLifecycleError):
    """session id 没有逻辑记录。"""


class SessionAlreadyRunningError(SessionLifecycleError):
    """同一 id 已有 live runtime，拒绝 resume 出第二个执行器。"""


# ═══════════════════════════════════════════════════════════════
# SessionRecord — 可以跨 runtime 存活的"恢复事实"
# ═══════════════════════════════════════════════════════════════

# TODO 2（你来填）：可变 dataclass。注意：这次**不是** frozen=True——
#   Record 会被运行时随时改 status / messages，冻结就没法改了
#   （前几课 frozen 的是值对象 Tool/PermissionDecision；Record 是状态载体）。
# 字段（除 id / cwd 外都有默认值）：
#   id: str                          —— "sess_0001" 式逻辑身份
#   cwd: str                         —— 工作目录（resume 时要重新校验）
#   mode: str = SessionState 所在模块顶部的 MODE_CRAFT
#   title: str = "未命名会话"
#   status: str = SessionState.CREATING   （str 混血：枚举成员就是字符串）
#   created_at: float = field(default_factory=time.time)
#   updated_at: float = field(default_factory=time.time)
#   runtime_generation: int = 1      —— 第几代运行时；resume 时 +1
#   messages: list[dict] = field(default_factory=list)
#   last_error: Optional[str] = None
# ⚠️ 刻意没有的字段：锁、线程、端口、server、provider client、abort 信号
#   ——它们属于 SessionProcess，只能重建、不能序列化（本课主旨）。
# 再加一个 summary() 方法：返回 UI 安全的 dict（不含任何运行时对象）：
#   {"id", "cwd", "title", "status", "mode", "runtimeGeneration",
#    "messages": len(self.messages), "lastError"}
@dataclass
class SessionRecord:
    ...


# ═══════════════════════════════════════════════════════════════
# SessionStore — 存储端口（Manager 只认这个形状）
# ═══════════════════════════════════════════════════════════════

class SessionStore(Protocol):
    """存储端口：换 SQLite 适配器（教材练习①）不用改 Manager。

    和 models.py 的 Model 协议同一套路：结构子类型，实现类不用继承谁。
    """

    def create(self, record: SessionRecord) -> None: ...

    def save(self, record: SessionRecord) -> None: ...

    def load(self, session_id: str) -> SessionRecord: ...

    def list(self) -> list[SessionRecord]: ...

    def delete(self, session_id: str) -> bool: ...


class InMemorySessionStore:
    """线程安全的教学存储：证明生命周期契约，不冒充磁盘持久化。

    TODO 3（你来填）——五个方法 + __init__，全部在锁内干活：
      __init__：self._records: dict[str, SessionRecord] = {}
                self._lock = threading.RLock()
      create(record)：id 已存在 raise SessionLifecycleError(f"session
                already exists: {record.id}")；存 copy.deepcopy(record)
      save(record)：  id 不在 raise SessionNotFoundError(record.id)；
                存 deepcopy
      load(id)：      KeyError 翻译成 SessionNotFoundError(id) ... from exc
                （低级异常链——练习 16/17 的老规矩）；返回 deepcopy
      list()：        返回 [copy.deepcopy(r) for r in self._records.values()]
      delete(id)：    return self._records.pop(id, None) is not None

    ⚠️ deepcopy 是防串账的关键：存取两端都是副本，调用方改自己手里那份
    record 不会渗透进 store。这是练习 10"防御性复制"的升级——浅拷贝
    list() 挡不住 messages 里 dict 的共享，deepcopy 连内层一起复制。
    （ScriptedModel 的浅拷贝快照 bug 就是这类的活教材。）

    为什么 RLock 不是 Lock：SessionProcess._publish 的注释（同线程重入）。
    """

    def __init__(self) -> None:
        raise NotImplementedError("TODO 3")


# ═══════════════════════════════════════════════════════════════
# SessionProcess — 一代运行时：临时资源 + 状态迁移 + turn 执行
# ═══════════════════════════════════════════════════════════════

# 回写钩子：SessionProcess 每次改动 record 就调它（Manager 传 store.save）
RecordSink = Callable[[SessionRecord], None]


class SessionProcess:
    """拥有一次 runtime generation 的全部临时资源。

    每次状态或 transcript 变化都通过 record_sink 回写 store——Store 里的
    记录永远是"最后一次观测"，而"活着"的权威在 Manager._runtimes。
    """

    def __init__(
        self,
        record: SessionRecord,
        record_sink: RecordSink,
        turn_runner: TurnRunner,
    ) -> None:
        self.record = copy.deepcopy(record)   # 这一代自己的副本
        self._record_sink = record_sink       # 变化回写 store 的钩子
        self._turn_runner = turn_runner
        # ── 临时资源区（resume 后这些全是新的——"必须重建"的含义）──
        self._state_lock = threading.RLock()       # 保护 record 的读改写
        self._turn_lock = threading.Lock()         # 一个会话同时最多一个 turn
        self._abort_requested = threading.Event()  # 协作式中断信号

    # ── 只读视图（给 UI / Manager / 测试看）────────────────────

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def cwd(self) -> str:
        return self.record.cwd

    @property
    def mode(self) -> str:
        return self.record.mode

    @property
    def status(self) -> str:
        return self.record.status

    @property
    def messages(self) -> list[dict]:
        return self.record.messages

    # ── 记录写回三件套：状态机的唯一通道 ────────────────────────

    def _transition(self, next_state: str) -> None:
        """迁移状态：查表（TODO 1）→ 改 record.status → _publish。

        TODO 4a（你来填）：
          整个读改写在 with self._state_lock 内：
            1. next_state == 现状 → return（同值迁移幂等，不算违法）；
            2. allowed = _ALLOWED_TRANSITIONS.get(现状, frozenset())；
               next_state 不在 allowed → raise SessionLifecycleError(
                   f"invalid session transition: {现状} -> {next_state}")；
            3. self.record.status = next_state；调 self._publish()。
        """

    def _publish(self) -> None:
        """把 record 的最新样子回写给 store（record_sink）。

        TODO 4b（你来填）：
          with self._state_lock 内：
            self.record.updated_at = time.time()
            self._record_sink(copy.deepcopy(self.record))

        ⚠️ 为什么 _state_lock 用 RLock 而不是 Lock：_transition **拿着**
        这把锁调 _publish，_publish 还要再拿同一把锁。普通 Lock 同线程
        重入会当场死锁（自己等自己）；RLock 内部计数，同线程可重入，
        不同线程照样互斥。这里必须 RLock。
        """

    def _commit_transcript_if_running(self, messages: list[dict]) -> bool:
        """turn 结果落盘的原子闸门：只有还处于 RUNNING 才收。

        TODO 4c（你来填）：
          with self._state_lock 内：
            1. status 不是 RUNNING → return False（close 抢先了，
               晚到的结果被拒之门外——本课最精妙的竞态防线）；
            2. record.messages = messages；record.last_error = None；
               _publish()；return True。
        """

    # ── 生命周期：start / run_turn / abort / close ─────────────

    def start(self) -> None:
        """把一代新运行时点亮：creating → idle。

        TODO 5（你来填）：
          1. status 不是 creating → raise SessionLifecycleError(
                 f"cannot start session {self.id} from {self.status}")；
          2. （教材这里 bind 端口 0 起 HTTP listener；我们的运行时资源
             ——锁和信号——构造期已就位，无需再分配，直接下一步）
          3. self._transition(SessionState.IDLE)。
        """
        raise NotImplementedError("TODO 5")

    def run_turn(self, user_message: str) -> str:
        """执行一个 turn：idle → running → idle 的封闭旅行。

        TODO 6（你来填）：
          1. if not self._turn_lock.acquire(blocking=False):   # 非阻塞抢锁
                 raise SessionLifecycleError(
                     f"session {self.id} already has a running turn")
             （抢不到 = 已有 turn 在跑：拒绝并发输入，绝不排队）
          2. try: ... finally: self._turn_lock.release()       # 锁必放
             try 体里依次：
             a. status 不是 idle → raise SessionLifecycleError(
                    f"session {self.id} cannot accept input while {self.status}")
             b. self._abort_requested.clear()  # 上一轮的 abort 不传染
             c. self._transition(SessionState.RUNNING)
             d. 内层 try/except：
                try:
                    output, new_messages = self._turn_runner(
                        user_message, list(self.record.messages))
                    if not self._commit_transcript_if_running(new_messages):
                        raise SessionLifecycleError(
                            "turn discarded: session runtime closed mid-turn")
                    if self.status == SessionState.RUNNING:
                        self._transition(SessionState.IDLE)
                    return output
                except Exception as exc:
                    # 只有还处于 RUNNING 才算本代失败（close 竞态不算）：
                    if self.status == SessionState.RUNNING:
                        self.record.last_error = str(exc)
                        self._transition(SessionState.ERROR)
                    raise          # 原样再抛，翻译留给调用方
        """
        raise NotImplementedError("TODO 6")

    def abort(self) -> None:
        """请求协作式中断：只置信号，不删记录、不动其他资源。

        教材的诚实边界照抄：同步调用里 abort 打不断正在阻塞的请求，
        只能在循环边界生效。我们 run_agent 的"每轮开头查一次信号"
        留到 s07-b 接线时再讨论；本课 abort 的实际效力 = close 前
        先礼让一次（见 close）。
        """
        self._abort_requested.set()

    def close(self) -> None:
        """释放运行时资源，保留逻辑记录。幂等：重复 close 直接返回。

        TODO 7（你来填）：
          1. status 已是 CLOSED → return（幂等的关键一行，在最先）；
          2. status 不是 CLOSING 也不是 ERROR → _transition(CLOSING)；
          3. self._abort_requested.set()（先请正在跑的 turn 合作停下）；
          4. status 不是 CLOSED → _transition(CLOSED)。
          （教材这里还要 shutdown HTTP server + join 线程；我们没有
          listener，锁和信号随对象回收。新一代 SessionProcess 会拿到
          全新的锁和 Event——旧信号天然不传染，这也是 run_turn 里
          还要 clear 一次的双保险原因。）
        """
        raise NotImplementedError("TODO 7")


# ═══════════════════════════════════════════════════════════════
# SessionManager — 控制面：身份（Record/Store）与运行时的对照表
# ═══════════════════════════════════════════════════════════════

class SessionManager:
    """Sidecar 的会话控制面：create / resume / close / forget。

    self._runtimes 是"活着"的唯一权威：sid 在不在这个表里 = 有没有
    live runtime。Store 里的 status 只是回写出来的最后观测（学习目标⑤）。
    """

    def __init__(
        self,
        turn_runner: TurnRunner,
        store: Optional[SessionStore] = None,
    ) -> None:
        self.store = store or InMemorySessionStore()
        self._turn_runner = turn_runner
        # live 运行时登记表：sid → SessionProcess
        self._runtimes: dict[str, SessionProcess] = {}
        # 重启后计数器接着最大编号走（从 store 里摸出来）
        self._counter = self._highest_existing_counter(self.store.list())

    @staticmethod
    def _highest_existing_counter(records: list[SessionRecord]) -> int:
        """从已有 id 里摸出最大编号："sess_0007" → 7。

        TODO 8a（你来填）：
          "sess_0007".rpartition("_") → ("sess", "_", "0007")（三段元组：
          分隔符左边、分隔符本身、右边；找不到分隔符则左边两个为空串）。
          遍历 records：prefix == "sess" 且 suffix.isdigit() 的收 int(suffix)；
          return max(计数列表, default=0)（store 空时返回 0）。
        """
        raise NotImplementedError("TODO 8a")

    def create_session(
        self,
        cwd: str,
        mode: str = MODE_CRAFT,
        title: str = "未命名会话",
    ) -> str:
        """创建新逻辑身份 + 第 1 代运行时，返回 session id。

        TODO 8b（你来填）：
          1. resolved = self._validate_options(cwd, mode)
          2. self._counter += 1；sid = f"sess_{self._counter:04d}"
          3. record = SessionRecord(id=sid, cwd=resolved, mode=mode, title=title)
          4. self.store.create(record)      # 先有身份
          5. self._start_runtime(record)    # 再有第 1 代运行时
          6. return sid
        """
        raise NotImplementedError("TODO 8b")

    @staticmethod
    def _validate_options(cwd: str, mode: str) -> str:
        """cwd 必须是存在的目录、mode 必须合法；返回 resolve 后的绝对路径。

        resolve(strict=True)：路径不存在当场 FileNotFoundError——不存在的
        工作目录不该拿到会话。resume 也走这个检查：目录可能在你 close
        期间被删了（教材：resume 要重新校验）。
        """
        path = Path(cwd).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"session cwd must be a directory: {path}")
        if mode not in SESSION_MODES:
            raise ValueError(f"unsupported session mode: {mode}")
        return str(path)

    def _start_runtime(self, record: SessionRecord) -> None:
        """造一代新运行时（create 的第 1 代 / resume 的第 N 代都走这里）。

        启动失败时 SessionProcess 已把 error + last_error 回写 store，
        这里把异常原样抬给调用方——记录不静默消失（create 语义的另一半）。
        """
        runtime = SessionProcess(record, self.store.save, self._turn_runner)
        runtime.start()
        self._runtimes[record.id] = runtime

    def resume_session(self, session_id: str) -> str:
        """用旧身份造新运行时：generation + 1，资源和信号全新。

        TODO 9a（你来填）：
          1. session_id in self._runtimes → raise
             SessionAlreadyRunningError(session_id)
             （两个执行器写同一段 transcript = 竞争与副作用失序）；
          2. record = self.store.load(session_id)
          3. self._validate_options(record.cwd, record.mode)
          4. record.status = SessionState.CREATING
             （Manager 直写——此刻没有 runtime 持有它，不走 _transition）
          5. record.runtime_generation += 1；record.last_error = None
          6. self.store.save(record)
          7. self._start_runtime(record)；return session_id
        """
        raise NotImplementedError("TODO 9a")

    def close_session(self, session_id: str) -> bool:
        """关掉 live runtime，保留记录和 transcript。返回是否真关了一个。

        TODO 9b（你来填）：
          1. runtime = self._runtimes.pop(session_id, None)
          2. runtime 不为 None → runtime.close()；return True
          3. 没有 live runtime（可能早已关闭，也可能只是记录还标着
             running 的僵尸）：record = self.store.load(session_id)；
             if record.status != SessionState.CLOSED:
                 record.status = SessionState.CLOSED
                 record.updated_at = time.time()
                 self.store.save(record)
             return False
             ⚠️ 第 3 步就是"running 不是存活证明"的落点：僵尸 running
             被抹成 closed，之后才能 resume。幂等：重复 close 已关会话
             走第 3 步，status 已 closed，不再改写。
        """
        raise NotImplementedError("TODO 9b")

    def forget_session(self, session_id: str) -> bool:
        """真正删除逻辑记录；live 的必须先 close。

        TODO 9c（你来填）：
          1. session_id in self._runtimes → raise SessionLifecycleError(
                 f"close session {session_id} before forgetting its record")
          2. return self.store.delete(session_id)
        """
        raise NotImplementedError("TODO 9c")

    def shutdown_all(self) -> None:
        """关掉全部 live runtime；记录全留，都可 resume。

        TODO 9d（你来填）：
          for sid in list(self._runtimes):   # list() 快照——边遍历边删会炸
              self.close_session(sid)
        """
        raise NotImplementedError("TODO 9d")

    # ── 只读视图 ──────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[SessionProcess]:
        """只返回 live 运行时；关了的会话用 load_record 拿记录。"""
        return self._runtimes.get(session_id)

    def load_record(self, session_id: str) -> SessionRecord:
        return self.store.load(session_id)

    def list_sessions(self) -> list[dict]:
        """UI 安全清单：Record.summary() + 展示期注入的 live 标志。

        live 是"sid 在 _runtimes 里"的实时事实，在展示层现场注入——
        Record 自己永远不知道运行时的死活（学习目标⑤的落点）。
        """
        summaries: list[dict] = []
        for record in self.store.list():
            summary = record.summary()
            summary["live"] = record.id in self._runtimes
            summaries.append(summary)
        return summaries
