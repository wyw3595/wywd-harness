"""s09 JSONL Transcript：证据只追加，运行时状态可重建。

📚 这一课在做什么（承接 s07 / s07-b）
  s07 把会话拆成 Record（逻辑身份）+ Process（运行时），但 Store 只有
  InMemory 一种实现——sidecar 进程一重启，记录全丢、resume 无从谈起。
  s09 给出持久化的第一种落地：每个会话一个 .jsonl 文件，只追加、永不
  修改；重启后读文件、回放（replay）事件、fold 重建 Record。

  核心哲学（教材原话）：**日志是证据，replay state 是派生结果**。
  内存里的 Record 随时可丢——只要证据还在磁盘上，就能重建。

  ┌────────────────────────────────────────────────────┐
  │ ~/.claude/projects/…（Claude Code 同款布局）          │
  │   sess_0001.jsonl                                   │
  │   {"…envelope…","type":"record","status":"idle",…}  │ ← 元数据状态（迁移留痕）
  │   {"…envelope…","type":"messages_appended",         │ ← 新增消息
  │    "messages":[{…}]}                                │
  │   {"…envelope…","type":"record","status":"closed"}  │
  │   （文件只长不缩；崩溃？读文件重放）                     │
  └────────────────────────────────────────────────────┘

📚 为什么是 JSONL 不是 SQLite（教材的五条理由，浓缩）
  对话是**流**不是表：append-only 天然契合；崩溃恢复不需要事务日志
  （文件本身就是日志）；每行独立解析，加字段不用迁移；cat/grep 就能
  看；一个会话一个文件，没有锁竞争。SQLite 适合元数据索引（s21+），
  教材原则：能从 JSONL 重建的，不进数据库。

📚 与教材的差异（架构适配，不是偷工减料）
  - 教材 JSONLTranscript 是独立 demo；我们落成 s07 SessionStore 协议的
    实现（JsonlSessionStore）——Manager 一行不改，换 store 参数即得
    跨进程持久化。s07 端口设计的兑现时刻。
  - 教材 6 种事件（message/reasoning/function_call/…）对应 WorkBuddy
    的 wire 级事件流；我们的运行时只在 turn 结束时提交 messages 快照
    （s07 已知边界），所以只有两种事件：record（元数据）+
    messages_appended（消息尾巴）。reasoning/快照事件源在 s15/s10+。
  - 教材的 select_memory_candidate 是 s10-s12 memory 的入口边界，
    本课不做：transcript ≠ memory（s07 学习目标④原样成立）。
  - 教材没做启动清账；我们补上（s07 遗留的"无 startup
    reconciliation"简化已在 sidecar 接线时落地）。

📚 损坏策略（教材的灵魂，一行不能少）
  最后一行没换行且 JSON 不完整 = 写到一半崩了 → 忽略并报告
  （ignored_partial_tail=True）；完整坏行 / 中间坏行 / sequence 跳号 =
  证据链不可信 → TranscriptCorruptionError，绝不静默跳过
  （"对任意坏行都静默跳过，会把证据损坏伪装成一次成功恢复"）。
"""

import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.harness.session import (
    SessionLifecycleError,
    SessionNotFoundError,
    SessionRecord,
    SessionStore,
)

SCHEMA_VERSION = 1

# 两种事件类型（见模块 docstring"与教材的差异"）
TYPE_RECORD = "record"                 # 元数据状态：迁移留痕，fold 取最后一条
TYPE_MESSAGES = "messages_appended"    # 新增消息尾巴：fold 全量拼接

# 信封字段只归 SessionTranscript 管——payload 里出现即拒绝（教材同款防御）
RESERVED_ENVELOPE_FIELDS = frozenset(
    {"schema_version", "sequence", "recorded_at", "session_id", "event_id"}
)

# Record 的元数据字段（dataclasses.asdict 排除 messages 的等价写法）：
# fold 重建 Record 时按这张表搬运
_RECORD_META_FIELDS = (
    "id", "cwd", "mode", "title", "status",
    "created_at", "updated_at", "runtime_generation", "last_error",
)


class TranscriptCorruptionError(RuntimeError):
    """证据链不可信：完整坏行 / sequence 跳号 / 前缀被改写。"""


class TranscriptValidationError(ValueError):
    """调用方想 append 违反信封约定的事件（占用保留字段）。"""


# ═══════════════════════════════════════════════════════════════
# SessionTranscript — 一个会话一个 .jsonl 文件：追加 + 校验读取
# ═══════════════════════════════════════════════════════════════

class SessionTranscript:
    """管一个会话的 JSONL 证据文件（对应教材的 JSONLTranscript）。

    行格式 = 信封（envelope）+ payload：
      {"schema_version":1, "sequence":3, "recorded_at":"2026-…",
       "session_id":"sess_0001", "event_id":"transcript:sess_0001:3",
       "type":"record", …元数据…}
    sequence 从 1 连续递增；event_id 是稳定的证据指针（s10+ memory
    靠它引用一条确切证据——教材"显式选择门槛"的地基）。
    """

    def __init__(self, path: Path, session_id: str) -> None:
        self.path = Path(path)
        self.session_id = session_id
        # 上一次 read_events 是否忽略了写到一半的尾部（崩溃现场报告）
        self.ignored_partial_tail = False

    def _envelope(self, sequence: int, event: dict) -> dict:
        """把 payload 装进信封：补 sequence / 时间戳 / 证据 ID。

        deepcopy payload：进证据后调用方再改自己的 dict 也不影响已落盘
        内容（练习 10 防御性复制在"证据"上的变体）。event_id 三段式
        transcript:<session_id>:<sequence>——全局唯一且可反查，s10+
        memory 靠它引用一条确切证据。
        """
        return {
              **copy.deepcopy(event),
              "schema_version": SCHEMA_VERSION,
              "sequence": sequence,
              "recorded_at": datetime.now(timezone.utc).isoformat(),
              "session_id": self.session_id,
              "event_id": f"transcript:{self.session_id}:{sequence}",
        }

    def append(self, event: dict) -> None:
        """追加一个事件：一行 JSON + \\n，立即落盘（flush + fsync）。

        - "a" 模式 = O_APPEND：每次写定位到文件末尾，配合单进程单写者
          实现"只追加"（教材用 os.open(O_APPEND) 是系统调用版）。
        - flush 把 Python 缓冲区推给操作系统；fsync 再逼操作系统把页
          缓存写进磁盘——两连之后才算"落盘"，少一步崩溃都可能丢。
        - 每次都重读全文件拿 sequence（教学规模可接受；生产缓存游标）。
        """
        reserved = sorted(RESERVED_ENVELOPE_FIELDS.intersection(event))
        if reserved:
            # 写入方违反信封约定 → ValidationError（不是 Corruption：
            # Corruption 是"读证据时发现证据坏了"，方向相反）
            raise TranscriptValidationError(
                "event payload contains reserved envelope fields: "
                + ", ".join(reserved))
        existing = self.read_events()
        sequence = existing[-1]["sequence"] + 1 if existing else 1
        encoded = (json.dumps(self._envelope(sequence, event),
                   ensure_ascii=False, sort_keys=True) + "\n")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())

    def read_events(self) -> list[dict]:
        """读全部事件并校验证据链：跳号 / 坏行 fail-closed，尾巴宽容。

        keepends=True 的原因：splitlines 默认丢掉行尾 \\n，我们需要知道
        "最后一行有没有换行符"来区分"写了一半"（无 \\n → 放过并报告）
        和"完整的一行坏数据"（有 \\n → 证据损坏，raise）——partial tail
        判定的唯一依据。
        """
        if not self.path.exists(): return []
        self.ignored_partial_tail = False
        events: list[dict] = []
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        for line_number, line in enumerate(lines, start=1):
            if not line.strip(): continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                # 真换行符是一个字符 "\n"；骨架 docstring 里写的 \\n 是
                # 为了渲染显示，进代码必须是 "\n"（照抄 docstring 的坑）
                partial_tail = (line_number == len(lines)
                                and not line.endswith("\n"))
                if partial_tail:
                    self.ignored_partial_tail = True
                    break            # 写到一半崩了：放过并报告
                raise TranscriptCorruptionError(
                    f"{self.path.name}:{line_number}: "
                    f"invalid complete JSON record") from exc
            if not isinstance(event, dict):
                raise TranscriptCorruptionError("record must be a JSON object")
            expected = len(events) + 1
            if event.get("sequence") != expected:
                raise TranscriptCorruptionError(f"expected sequence {expected}, got {event.get('sequence')}")
            if event.get("session_id") != self.session_id:
                raise TranscriptCorruptionError(f"session_id {self.session_id}")    
            events.append(event)
        return events


# ═══════════════════════════════════════════════════════════════
# JsonlSessionStore — SessionStore 协议的 JSONL 落地
# ═══════════════════════════════════════════════════════════════

class JsonlSessionStore(SessionStore):
    """s07 端口的第一个真后端：文件在，会话就在（跨进程重启）。

    布局（Claude Code 同款思路，教学版放项目内可注入目录）：
      <root>/sess_0001.jsonl   <root>/sess_0002.jsonl   …
    文件名 = session id = 证据文件的归属，delete 就是删文件（forget
    语义：真删，和 InMemory 的 dict.pop 对齐）。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()   # 串行化同一进程内的读写

    def _transcript(self, session_id: str) -> SessionTranscript:
        """sid → 证据文件。sid 进文件名前必须过信任边界检查：
        好人：Manager 生成的 sess_0001 式 id；坏人：../../etc/passwd 式
        路径穿越（store 是协议端口，不能假设调用方都是 Manager）。
        """
        if "/" in session_id or "\\" in session_id \
                or session_id in {".", ".."}:
            raise SessionLifecycleError(f"unsafe session id: {session_id!r}")
        return SessionTranscript(self.root / f"{session_id}.jsonl", session_id)

    # ── 写路径：create / save ─────────────────────────────────

    def _record_event(self, record: SessionRecord) -> dict:
        """Record → record 事件的 payload（元数据九字段，不含 messages）。"""

        payload = {field: getattr(record, field) for field in _RECORD_META_FIELDS}
        return {"type": TYPE_RECORD, **payload}

    def _persisted_messages(self, transcript: SessionTranscript) -> list[dict]:
        """fold：从证据文件里重建已落盘的 messages（load 也要用）。"""

        messages: list[dict] = []
        for event in transcript.read_events():
            if event.get("type") == TYPE_MESSAGES:
                messages.extend(event["messages"])
        return messages

    def create(self, record: SessionRecord) -> None:
        """新会话落第一行证据（record 事件；messages 若非空也一并落——
        Manager 传来的 record.messages 是空列表，但 store 不假设调用方）。
        """
        with self._lock:
            t = self._transcript(record.id)
            if t.path.exists():
                raise SessionLifecycleError(f"session already exists: {record.id}")
            t.append(self._record_event(record))
            if record.messages:
                t.append({"type": TYPE_MESSAGES,
                          "messages": copy.deepcopy(record.messages)})

    def save(self, record: SessionRecord) -> None:
        """保存快照 = 证据追加：diff 出新消息尾巴 + 追加 record 状态。

        协议给的是快照（save(整个 record)），证据文件只收增量——store
        内部做 diff。不变量：record.messages 单调增长（run_turn 只追加；
        trim 在 turn_runner 里做、从不回写 Record）。
        前缀检查是 append-only 的执法者：没有它，一次错误的全量替换会
        静默通过，replay 出的历史和真实证据对不上——证据损坏伪装成
        成功保存。顺序：先 messages 后 record——fold 取"最后一条
        record"当最新状态，messages 先到齐，状态最后定妆。
        """
        with self._lock:
            t = self._transcript(record.id)
            if not t.path.exists():
                raise SessionNotFoundError(record.id)
            persisted = self._persisted_messages(t)
            if persisted != record.messages[:len(persisted)]:
                raise TranscriptCorruptionError(
                    f"{record.id}: 已落盘的消息前缀被改写——"
                    "append-only 证据不可回改")
            new_tail = record.messages[len(persisted):]
            if new_tail:
                t.append({"type": TYPE_MESSAGES,
                          "messages": copy.deepcopy(new_tail)})
            t.append(self._record_event(record))   # 状态迁移留痕

    # ── 读路径：load / list / delete ──────────────────────────

    def load(self, session_id: str) -> SessionRecord:
        """replay fold 重建 Record：元数据取最后一条 record 事件。

        SessionRecord(**{...}) 是字典解包进关键字参数——九个元数据字段
        逐个对应 dataclass 字段名（_RECORD_META_FIELDS 存在的原因：
        字段名两端对齐，fold 才能一行重建）。
        """
        with self._lock:
            t = self._transcript(session_id)
            if not t.path.exists():
                raise SessionNotFoundError(session_id)
            latest_meta: Optional[dict] = None
            for event in t.read_events():
                if event.get("type") == TYPE_RECORD:
                    latest_meta = event     # 最后一条 = 最新状态
            if latest_meta is None:
                raise TranscriptCorruptionError(
                    f"{t.path.name}: no record event to fold")
            return SessionRecord(
                **{field: latest_meta[field] for field in _RECORD_META_FIELDS},
                messages=self._persisted_messages(t))

    def list(self) -> list[SessionRecord]:
        """扫目录列全部会话：glob *.jsonl，逐个 fold。

        stem 是文件名去掉后缀（"sess_0001.jsonl" → "sess_0001"）——
        文件名即身份，倒着查回来。sorted 只为断言稳定（Windows 目录
        顺序不保证）。
        """
        with self._lock:
            records = []
            for path in sorted(self.root.glob("*.jsonl")):
                records.append(self.load(path.stem))   # stem = 去 .jsonl 的 id
            return records

    def delete(self, session_id: str) -> bool:
        """forget 的落点：真删证据文件。存在并删掉返回 True。"""

        with self._lock:
            path = self._transcript(session_id).path
            if path.exists():
                path.unlink()
                return True
            return False
