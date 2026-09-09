"""s10 Workspace Memory：从工作日志蒸馏可恢复的项目记忆。

📚 这一课在做什么（承接 s09）
  s09 的 JSONL transcript 属于某个 session，是完整执行证据；本课不是
  再抄一份聊天记录，而是建立**项目级的选择性记忆**：

      transcript（s09）──忠实记录──┐
                                  │  distill（显式策略，不是 LLM 拍脑袋）
      daily/*.jsonl ──只追加──────┘
                                  ▼
                         curated.json（机器真相，原子替换）
                                  │ render
                                  ▼
                         MEMORY.md（人/prompt 的派生视图，随时重建）

  一句话：**transcript 负责忠实记录，memory 负责有损选择**。

📚 三个文件三种身份（本课的存储布局）
  <项目根>/.memory/
    daily/2026-09-09.jsonl   原始事实日志——O_APPEND 单记录追加，是证据
    curated.json             策展状态的机器真相——临时文件 + os.replace
                             原子替换（本课新机制：崩溃不留半个文件）
    MEMORY.md                人/prompt 看的派生视图——不是证据，随时重建
  curated.json 是 canonical：进程在两次替换之间死了，下次读取以它
  为准修复陈旧的 MEMORY.md（s09 "日志是证据，状态是派生" 的再上岗）。

📚 蒸馏门槛（DistillPolicy——刻意不让 LLM 决定什么值得记住）
  年龄 ≥ 30 天 AND 类型 ∈ {decision, convention, pitfall} AND
  (重要度 ≥ 4 OR 规范化后重复 ≥ 2 次)
  生产系统可以在候选提取用模型，但保留条件由 harness 控制——"模型说
  重要就永久保存"是记忆被提示注入污染的正门（教材"常见误区"第二条）。
  outcome 类刻意不晋升：一次测试通过不配变成长期规则。

📚 与教材的差异（架构适配，不是偷工减料）
  - 教材的 memory_key 冲突域 + 修订链 + supersession（近半代码）砍掉
    留作 s10-b 候选：本课走"内容寻键"路径——kind + 规范化内容 = 稳定
    key，同内容新证据只合并 evidence_ids（幂等），不存在覆盖语义。
  - 教材 schema v1/v2 迁移不需要（我们首发即最终形态）。
  - 教材的 MemoryAwareAgent（Anthropic 直连循环）换成我们的接线：
    memory_write 延迟工具（toolbox，走 ToolSearch 发现）+ 会话起步
    注入有界视图（shell 的 history_seed）——复用 s03/s06 的既有机制，
    不另起一条 agent 循环。
  - workspace_id = 项目绝对路径 sha256 前 16 位（防相对路径/软链接
    给同一项目造出两个 scope——教材"常见误区"第五条的落点）。
"""

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional


SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
MAX_FACT_CHARS = 2_000
MAX_CONTEXT_FACTS = 6


class MemoryScopeError(RuntimeError):
    """持久化记忆属于另一个 workspace（串线 = 数据事故，当场炸）。"""


class MemoryCorruptionError(RuntimeError):
    """损坏的持久记录——不能安全忽略的那种（中间坏行/坏 curated）。"""


class FactKind(str, Enum):
    """四类事实——保留策略的词汇表（str 混血：JSON 直出，s07 同款）。

    decision/convention/pitfall 通常对后续会话有用（可蒸馏）；
    outcome 只留在近期日志，随时间老化（不晋升）。
    """

    DECISION = "decision"      # 决策：选了 SQLite WAL
    CONVENTION = "convention"  # 约定：路径必须相对项目根
    PITFALL = "pitfall"        # 坑：不可把 token 写进记忆
    OUTCOME = "outcome"        # 结果：本次测试通过


STABLE_KINDS = frozenset({FactKind.DECISION.value,
                          FactKind.CONVENTION.value,
                          FactKind.PITFALL.value})


@dataclass(frozen=True)
class MemoryFact:
    """workspace 只追加日志里的一条不可变事实。"""

    fact_id: str
    workspace_id: str
    recorded_at: str          # UTC ISO8601（Z 结尾）
    kind: str
    content: str
    source: str = "agent"
    importance: int = 3       # 1-5
    evidence: dict[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "MemoryFact":
        try:
            return cls(
                fact_id=str(payload["fact_id"]),
                workspace_id=str(payload["workspace_id"]),
                recorded_at=str(payload["recorded_at"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                source=str(payload.get("source", "agent")),
                importance=int(payload.get("importance", 3)),
                evidence=dict(payload.get("evidence") or {}),
                schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"invalid memory fact: {exc}") from exc


@dataclass(frozen=True)
class DistillPolicy:
    """从原始事实到长期记忆的可解释闸门（构造期校验参数域）。"""

    minimum_age_days: int = DEFAULT_RETENTION_DAYS
    minimum_importance: int = 4
    repeat_threshold: int = 2

    def __post_init__(self) -> None:
        if self.minimum_age_days < 0:
            raise ValueError("minimum_age_days must be >= 0")
        if not 1 <= self.minimum_importance <= 5:
            raise ValueError("minimum_importance must be between 1 and 5")
        if self.repeat_threshold < 1:
            raise ValueError("repeat_threshold must be >= 1")


@dataclass
class CuratedEntry:
    """一条长期记忆 + 指回原始事实的证据链接（内容寻键，无修订链）。"""

    key: str                  # _entry_key(kind, 规范化内容) 的 sha256 前 16 位
    kind: str
    content: str
    first_seen: str
    last_seen: str
    evidence_ids: list[str]
    occurrences: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CuratedEntry":
        try:
            evidence_ids = [str(item) for item in payload.get("evidence_ids", [])]
            return cls(
                key=str(payload["key"]),
                kind=str(payload["kind"]),
                content=str(payload["content"]),
                first_seen=str(payload["first_seen"]),
                last_seen=str(payload["last_seen"]),
                evidence_ids=evidence_ids,
                occurrences=int(payload.get("occurrences", len(evidence_ids))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryCorruptionError(f"invalid curated entry: {exc}") from exc


@dataclass(frozen=True)
class DistillReport:
    """蒸馏的可观察结果（给调度方/CLI/审计层）。"""

    scanned: int      # 过了年龄线的事实数
    eligible: int     # 参与了晋升/合并的事实数
    created: int      # 新建条目数
    updated: int      # 合并证据的条目数
    skipped: int      # 被门槛拦下的事实数


# ── 工具函数（给全：都是一行流，课的肉在 WorkspaceMemory）────────

def _as_utc(value: Optional[datetime]) -> datetime:
    """任何 datetime 统一成 UTC（裸时间按 UTC 补时区）。"""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    """ISO8601 字符串 → UTC datetime（坏时间戳 = 损坏，当场炸）。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryCorruptionError(
            f"invalid recorded_at timestamp: {value!r}") from exc
    return _as_utc(parsed)


def _normal_form(content: str) -> str:
    """归一化：空白折叠 + casefold（比 lower 更激进的大小写归一——
    'SQLite WAL' 和 'sqlite  wal' 是同一条事实）。"""
    return re.sub(r"\s+", " ", content).strip().casefold()


def _entry_key(kind: str, content: str) -> str:
    """内容寻键：kind + 规范化内容 → sha256 前 16 位。

    hashlib.sha256（新）：把字节流搅成 64 位十六进制指纹；取前 16 位
    做短 key——同内容必同 key（分组依据），不同内容撞车概率可忽略。
    """
    material = f"{kind}\0{_normal_form(content)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════
# WorkspaceMemory — 项目作用域的事实日志 + 蒸馏 + 派生视图
# ═══════════════════════════════════════════════════════════════

class WorkspaceMemory:
    """一个项目一份持久记忆：追加证据、策略蒸馏、有界注入。

    TODO 1（你来填）——__init__ + 路径布局：
      def __init__(self, project_dir: Path) -> None:
          self.project_dir = Path(project_dir).expanduser().resolve()
          self.workspace_id = hashlib.sha256(
              str(self.project_dir).encode("utf-8")).hexdigest()[:16]
          self.memory_dir = self.project_dir / ".memory"
          self.daily_dir = self.memory_dir / "daily"
          self.curated_file = self.memory_dir / "curated.json"
          self.memory_file = self.memory_dir / "MEMORY.md"
          self.daily_dir.mkdir(parents=True, exist_ok=True)
    三个要点：
      - resolve() 先行：相对路径/软链接归一到同一绝对路径，workspace_id
        才稳定（教材"只按字符串路径隔离"误区的防御）；
      - workspace_id 是 scope 的唯一标识，写进每条 fact / curated——
        读到别家的 id 就是串线，MemoryScopeError；
      - 布局在 __init__ 定死，方法只管拼（daily_log_path 已给）。
    """

    def daily_log_path(self, day: date) -> Path:
        """某 UTC 天的事实日志路径（一天一个文件，文件名即日期）。"""
        return self.daily_dir / f"{day.isoformat()}.jsonl"

    def today_log_path(self) -> Path:
        return self.daily_log_path(datetime.now(timezone.utc).date())

    def append_daily_log(
        self,
        content: str,
        *,
        kind: FactKind | str = FactKind.OUTCOME,
        importance: int = 3,
        source: str = "agent",
        evidence: Mapping[str, str] | None = None,
        recorded_at: datetime | None = None,
    ) -> MemoryFact:
        """追加一条校验过的事实（一行 JSON + fsync，返回构造好的 fact）。

        TODO 2（你来填）：
          1. 校验（信任边界——进证据前把关）：
             text = 归一化空白后的 content；空 → ValueError；
             len(text) > MAX_FACT_CHARS → ValueError；
             not 1 <= importance <= 5 → ValueError；
             kind 转 str 后不在 FactKind 值集合 → ValueError
          2. 造 fact：fact_id=uuid.uuid4().hex、workspace_id=self.workspace_id、
             recorded_at=_as_utc(recorded_at).isoformat().replace("+00:00","Z")
          3. 追加一行（s09 同款三连）：
             encoded = (json.dumps(asdict(fact), ensure_ascii=False,
                        sort_keys=True) + "\\n").encode("utf-8")
             path = self.daily_log_path(时间戳的 .date())
             with open(path, "a", ...)/write/flush/os.fsync
          4. return fact
        asdict（新语法）：dataclass → 字典的深度转换（嵌套 dataclass
        递归拆）——直接 json.dumps(dataclass) 会炸，asdict 是中间站。
        """
        raise NotImplementedError("TODO 2: append_daily_log")

    def _read_log(self, path: Path) -> list[MemoryFact]:
        """读一个日志文件：partial tail 放过，中间坏行/串线炸。

        TODO 3（你来填）：
          if not path.exists(): return []
          逐行（keepends=True——s09 的判定依据）：
            空行跳过；json.loads 失败：最后一行无换行 → break（partial
            tail），否则 MemoryCorruptionError
            MemoryFact.from_dict 后查两件事：
              schema_version 合法性、workspace_id == self.workspace_id
              （不匹配 → MemoryScopeError）
          返回 facts
        def read_all_facts(self) -> list[MemoryFact]:
          所有日志文件（glob "????-??-??.jsonl" 排序）的 facts 拼接，
          按 (recorded_at, fact_id) 排序返回。
        （s09 的 read_events 换了个马甲：JSONL 证据校验的第三次上岗。）
        """
        raise NotImplementedError("TODO 3: _read_log / read_all_facts")

    def _atomic_write_text(self, path: Path, content: str) -> None:
        """原子替换：要么完整新文件，要么旧文件原封不动（本课新机制）。

        TODO 4（你来填）：
          1. descriptor, tmp_name = tempfile.mkstemp(
                 dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
             ——临时文件必须和目标同目录（os.replace 只保证同文件系统
             原子；跨盘会退化成复制）
          2. try:
                 with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                     f.write(content); f.flush(); os.fsync(f.fileno())
                 os.replace(tmp, path)   ← 原子改名：观察者要么看旧要么看新
             finally:
                 tmp.unlink(missing_ok=True)   ← 成功后 tmp 已不存在，
                 失败时清掉残骸（missing_ok=True：不存在也不炸——新语法）
        为什么不直接 open(path, "w")：写一半崩溃 = 半个文件顶着正式名字，
        MEMORY.md/curated.json 就坏了；tempfile + replace 保证崩溃点只
        会留下一个多余的 .tmp（下次覆盖），canonical 永远是完整版本。
        """
        raise NotImplementedError("TODO 4: _atomic_write_text")

    def _load_curated(self) -> list[CuratedEntry]:
        """curated.json → 条目清单（scope/schema 校验 + 结构校验）。"""
        if not self.curated_file.exists():
            return []
        try:
            payload = json.loads(self.curated_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryCorruptionError("curated.json is not valid JSON") from exc
        if payload.get("workspace_id") != self.workspace_id:
            raise MemoryScopeError("curated memory belongs to another workspace")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise MemoryCorruptionError("unsupported curated memory schema")
        entries = [CuratedEntry.from_dict(item) for item in payload.get("entries", [])]
        # 简化版结构校验（教材完整版服务修订链，我们没链）：key 唯一、
        # occurrences 与 evidence 一致
        if len({e.key for e in entries}) != len(entries):
            raise MemoryCorruptionError("curated entry keys must be unique")
        for entry in entries:
            if entry.occurrences != len(set(entry.evidence_ids)):
                raise MemoryCorruptionError(
                    f"curated entry {entry.key} has inconsistent evidence count")
        return entries

    def _render_memory(self, entries: list[CuratedEntry]) -> str:
        """MEMORY.md：按类型分节的人/prompt 视图。

        TODO 5（你来填）：
          lines = ["# Workspace Memory", "",
                   "Derived from append-only project facts. "
                   "Edit the source log or policy, not this view."]
          按 decision → convention → pitfall 三节（outcome 不出现——它
          从不晋升）：
            有条目的类型才出节标题（"## Decisions" / "## Conventions" /
            "## Pitfalls"），条目内排序 (content, key)
            每行：f"- {item.content} (seen {item.occurrences}x; "
                  f"evidence: {len(item.evidence_ids)})"
          结尾补一个空行，return "\\n".join(lines)
        """

    def _save_curated(self, entries: list[CuratedEntry]) -> None:
        """canonical 先写、派生视图后写——都走原子替换。"""
        ordered = sorted(entries, key=lambda item: (item.kind, item.key))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "entries": [asdict(item) for item in ordered],
        }
        self._atomic_write_text(
            self.curated_file,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        self._atomic_write_text(self.memory_file, self._render_memory(ordered))

    def distill(
        self,
        *,
        policy: Optional[DistillPolicy] = None,
        as_of: Optional[datetime] = None,
    ) -> DistillReport:
        """按策略晋升事实，不删证据、幂等（重复跑不重复计）。

        TODO 6（你来填）：
          active = policy or DistillPolicy()
          cutoff = _as_utc(as_of) - timedelta(days=active.minimum_age_days)
          （timedelta 新语法：日期算术——"30 天前"就是减一个 timedelta）
          existing = self._load_curated()
          by_key = {entry.key: entry for entry in existing}
          processed_ids = {已进任何条目的 evidence_id}   ← 幂等的钥匙
          aged = read_all_facts() 里时间 < cutoff 的
          candidates = aged 里 kind ∈ STABLE_KINDS 且 fact_id ∉ processed_ids
          skipped = len(aged) - len(candidates) 起步
          按 _entry_key(fact.kind, fact.content) 分组，每组（sorted 遍历）：
            qualifies = max(importance) >= minimum_importance
                        or len(facts) >= repeat_threshold
            不合格 → skipped += len(facts)，continue
            eligible += len(facts)
            by_key 无此 key → 新建 CuratedEntry：
              代表事实选 (-importance, 时间, fact_id) 最小者（最重要优先，
              同重要度取最早措辞——确定性展示）
              first_seen/last_seen = 组内时间戳 min/max
              evidence_ids = sorted(fact_id 集合)，occurrences = len
              created += 1
            已有 → 合并（同内容新证据 = 幂等更新，不是新条目）：
              evidence_ids = sorted(旧 ∪ 新)，occurrences = len(合并后)
              first_seen/last_seen 拓宽，updated += 1
          changed 才 _save_curated(list(by_key.values()))
          return DistillReport(scanned=len(aged), ...)
        """
        raise NotImplementedError("TODO 6: distill")

    def get_context_for_agent(self, *, recent_limit: int = MAX_CONTEXT_FACTS) -> str:
        """有界注入：curated 视图 + 最近 N 条事实，超预算的截掉。

        TODO 7（你来填）：
          parts = []
          curated = self.read_memory_md().strip()；非空 → parts 收下
          recent = read_all_facts() 的最近 recent_limit 条（recent_limit=0
          则不要）；非空 → parts 追加一段：
            ["# Recent Workspace Facts", ""] + 每条
            f"- [{fact.kind}] {fact.content} ({fact.recorded_at[:10]})"
          return "\\n\\n".join(parts) if parts else "(no workspace memory yet)"
        边界的意义（教材）：memory 的价值不在存得多，在召回时有预算和
        优先级——把全部日志塞回上下文就退化成 s09 了。
        """
        raise NotImplementedError("TODO 7: get_context_for_agent")

    def read_memory_md(self) -> str:
        """读 MEMORY.md；与 canonical 不一致时顺手修复（派生视图可重建）。"""

        if not self.curated_file.exists():
            return (self.memory_file.read_text(encoding="utf-8")
                    if self.memory_file.exists() else "")
        rendered = self._render_memory(self._load_curated())
        current = (self.memory_file.read_text(encoding="utf-8")
                   if self.memory_file.exists() else None)
        if current != rendered:
            self._atomic_write_text(self.memory_file, rendered)
        return rendered
