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

# "没有记忆"的哨兵文案。抽成常量是因为它要在**两个模块**里对齐：
# get_context_for_agent 返回它，build_history_seed 靠它判断"要不要往
# 起步历史里加第二条 system"。跨模块靠字符串字面量比对是
# "改一处漏一处"的经典来源（漏了的症状还很隐蔽：记忆永远注入不进去）。
NO_MEMORY_PLACEHOLDER = "(no workspace memory yet)"


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
    """蒸馏的可观察结果（给调度方 / CLI / 审计层）。

    五个计数的**单位不一样**，读的时候别搞混：`scanned` / `eligible` 数的是
    **事实**，`created` / `updated` 数的是**条目**。于是一组 3 条事实只建出
    1 条条目时，`created` 是 1 而不是 3——这也是为什么下面 `skipped` 不能
    简单定义成"被门槛拦下的事实数"。
    """

    scanned: int      # 过了年龄线、且尚未进过任何条目的事实数
    eligible: int     # 过了两道门槛、真的参与晋升/合并的事实数
    created: int      # 新建条目数
    updated: int      # 合并了证据的条目数
    skipped: int      # scanned - created - updated：没换来条目的事实数
                      # （被门槛拦下的 + 同组里除代表外被折叠进来的）。
                      # 用它收口是为了保证账永远平


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


def _collapse_whitespace(content: str) -> str:
    """空白折叠（连续空白含换行压成单空格），不动大小写。纯函数。

    为什么先有它、再有 _normal_form：两者用途不同——**存证据时只能折叠空白，
    不能改大小写**（把 'SQLite WAL' 存成 'sqlite wal' 是篡改原文），而**算
    key 时必须再叠一层 casefold**（大小写不同的两条才算同一条事实）。
    合成一个函数会让"存储"和"寻键"被迫用同一种强度。
    """

    return re.sub(r"\s+", " ", content).strip()


def _normal_form(content: str) -> str:
    """归一化：空白折叠 + casefold（比 lower 更激进的大小写归一——
    'SQLite WAL' 和 'sqlite  wal' 是同一条事实）。"""

    return _collapse_whitespace(content).casefold()


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
    """一个项目一份持久记忆：追加证据、策略蒸馏、有界注入。"""

    def __init__(self, project_dir: Path) -> None:
        """定下 scope 与存储布局。

        三个要点：
          - **`resolve()` 先行**：相对路径 / 软链接归一到同一个绝对路径，
            `workspace_id` 才稳定。不解析就会出现"同一个项目因写法不同而
            生出两套记忆"——教材"只按字符串路径隔离"那个误区的落点；
          - `workspace_id` 是 scope 的唯一标识，会被写进每条 fact 与
            `curated.json`。读到别家的 id 就是串线，当场 `MemoryScopeError`；
          - 布局在这里**一次定死**，其余方法只管拼路径（`daily_log_path`
            已经给好了）——路径散在各处拼，是"改一个地方漏三个地方"的源头。
        """

        self.project_dir = Path(project_dir).expanduser().resolve()
        self.workspace_id = hashlib.sha256(
            str(self.project_dir).encode("utf-8")).hexdigest()[:16]
        self.memory_dir = self.project_dir / ".memory"
        self.daily_dir = self.memory_dir / "daily"
        self.curated_file = self.memory_dir / "curated.json"
        self.memory_file = self.memory_dir / "MEMORY.md"
        # 目录先落地：后面所有写路径都假设它存在（makedirs 幂等，重复构造无害）
        self.daily_dir.mkdir(parents=True, exist_ok=True)

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

        校验是**信任边界**：事实一进证据日志就不再回头，所以宁可在门口多问
        几句。四条各有各的坏处——空内容是无意义证据；超长会把日志和注入
        上下文一起撑爆；越界的 importance 会让蒸馏门槛失灵；未知 kind 则连
        "该不该晋升"都判不了（`STABLE_KINDS` 查不到它）。

        落盘用 s09 同款三连（write -> flush -> fsync）：证据日志的全部价值
        就在"断电了它也还在"。开 `"ab"` 而不是 `"a"`——文本模式会替我们
        翻译换行符，那就不是我们要写的那串字节了（今天在 file_tools 上栽过
        这个坑，同一个坑不踩第二遍）。

        `asdict` 是 dataclass -> 字典的深度转换（嵌套 dataclass 会递归拆开）；
        直接 `json.dumps(dataclass)` 会炸，它是中间的必经站。
        """

        text = _collapse_whitespace(content)
        if not text:
            raise ValueError("事实内容不能是空白——记不住的东西不该进证据日志")
        if len(text) > MAX_FACT_CHARS:
            raise ValueError(
                f"事实内容过长（{len(text)} 字符，上限 {MAX_FACT_CHARS}）"
                f"——一条事实应该是一句话，不是一篇文档"
            )
        if not 1 <= importance <= 5:
            raise ValueError(f"importance 必须在 1..5 之间，收到 {importance}")

        # kind 允许传 Enum 也允许传字符串（工具箱那边从 JSON 进来的是字符串）
        kind_value = kind.value if isinstance(kind, FactKind) else str(kind)
        known_kinds = [item.value for item in FactKind]
        if kind_value not in known_kinds:
            raise ValueError(
                f"未知的事实类型 {kind!r}——可选：{'、'.join(known_kinds)}"
            )

        when = _as_utc(recorded_at)
        fact = MemoryFact(
            fact_id=uuid.uuid4().hex,
            workspace_id=self.workspace_id,
            # isoformat() 给出 "+00:00"，换成 "Z"——存储格式统一成一种写法，
            # 免得以后两个解析路径各认一种。
            recorded_at=when.isoformat().replace("+00:00", "Z"),
            kind=kind_value,
            content=text,
            source=source,
            importance=importance,
            evidence=dict(evidence or {}),
        )

        encoded = (json.dumps(asdict(fact), ensure_ascii=False,
                              sort_keys=True) + "\n").encode("utf-8")
        path = self.daily_log_path(when.date())
        with open(path, "ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return fact

    def _read_log(self, path: Path) -> list[MemoryFact]:
        """读一个日志文件：partial tail 放过，中间坏行 / 串线当场炸。

        三种情况区别对待，判断标准只有一条——**这是"没写完"还是"写坏了"**：

          - **partial tail**（最后一行没有换行符）：进程在写那一行的中途死了。
            它是未完成的写入，不是损坏的证据，放过并忽略——下次追加会自然
            接在它后面（所以它也不会永远赖在那里）；
          - **完整的坏行**（有换行符却解析不了）：文件真坏了（被手改过、磁盘
            出错）。不能装作没看见——继续读下去只会让记忆悄悄失真，而失真
            的记忆比没有记忆更坏；
          - **空行**跳过：手动编辑留下的空行不该算证据。

        `keepends=True` 是这套判断的地基：留着换行符，才分得清"最后一行写完
        了没有"。这就是 s09 `read_events` 的第三次上岗——"日志是证据"这条
        规矩，每多一个消费方就得再钉一遍。
        """

        if not path.exists():
            return []

        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # 证据日志连"是不是文本"都不能含糊：乱码当空行跳过 = 悄悄丢事实
            raise MemoryCorruptionError(
                f"日志 {path.name} 不是合法的 UTF-8 文本"
            ) from exc

        facts: list[MemoryFact] = []
        for raw in raw_text.splitlines(keepends=True):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                if not raw.endswith("\n"):
                    break  # partial tail：写到一半就崩了，认它没写完
                raise MemoryCorruptionError(
                    f"日志 {path.name} 里有完整却无法解析的行：{raw[:80]!r}"
                ) from exc

            fact = MemoryFact.from_dict(payload)
            if fact.schema_version != SCHEMA_VERSION:
                raise MemoryCorruptionError(
                    f"日志 {path.name} 里的 schema_version="
                    f"{fact.schema_version} 不受支持（本代码只认 {SCHEMA_VERSION}）"
                )
            if fact.workspace_id != self.workspace_id:
                raise MemoryScopeError(
                    f"日志 {path.name} 里的记忆属于另一个 workspace"
                    f"（{fact.workspace_id}）——这是串线，直接拒绝"
                )
            facts.append(fact)
        return facts

    def read_all_facts(self) -> list[MemoryFact]:
        """所有日志文件按日期拼接后的全部事实（按记录时间排序）。

        排序**只用 recorded_at，不拿 fact_id 当平局判据**。`fact_id` 是
        `uuid4`，把它当二级键等于"把同一时刻写入的两条事实随机洗牌"；而
        `sorted` 本身是稳定排序，只按时间排就能保住"文件内按追加顺序"这个
        有意义的事实（谁先写的谁在前）。

        文件名 glob 用 `????-??-??.jsonl` 而不是 `*.jsonl`：前者顺带要求
        日期格式合法，顺手把"某某备份.jsonl"这类杂鱼挡在外面（且字典序 = 日期序）。
        """

        facts: list[MemoryFact] = []
        for path in sorted(self.daily_dir.glob("????-??-??.jsonl")):
            facts.extend(self._read_log(path))
        return sorted(facts, key=lambda item: item.recorded_at)

    def _atomic_write_text(self, path: Path, content: str) -> None:
        """原子替换：要么是完整的新文件，要么旧文件原封不动（本课新机制）。

        四步，顺序不能换：

          1. `tempfile.mkstemp(dir=path.parent)` —— 临时文件必须和目标
             **同目录**。`os.replace` 只保证"同一文件系统内"原子；跨盘会
             退化成"复制 + 删除"，那个窗口里文件是半截的。
          2. 写入 + `flush` + `os.fsync` —— fsync 把数据真正推给磁盘。少了
             它，改名之后断电仍可能丢内容（改名是原子的，但数据可能还躺在
             操作系统的页缓存里）。
          3. `os.replace(tmp, path)` —— 原子改名：任何观察者要么看到旧的完整
             文件、要么看到新的完整文件，不存在中间态。
          4. `finally: tmp.unlink(missing_ok=True)` —— 成功时 tmp 已经不存在
             （`missing_ok=True` 所以不炸），失败时清掉残骸。

        为什么不直接 `open(path, "w")`：写一半崩溃 = 半个文件顶着正式名字，
        `MEMORY.md` / `curated.json` 当场就坏了。tempfile + replace 保证崩溃点
        只会留下一个多余的 `.tmp`（下次覆盖），canonical 永远是完整版本。

        落盘走 `"wb"`（二进制）而不是 `"w"`：文本模式会把 `\\n` 翻译成
        `os.linesep`，写出来的字节就不是你给的那一串了——2026-09-12 在
        file_tools 上刚栽过这个坑（CR 翻倍），同一个坑不踩第二遍。

        与 `file_tools._atomic_write_bytes` 是同一套机制的**两份实现**（工具线
        那天也补了一份）。这里先各写一份：s10 的教学点就在这四步里，直接调用
        会把这一课省掉。等这一课过了，再把其中一份改成调用另一份，收敛成单
        实现（记为已知待办）。
        """

        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

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

        lines = [
            "# Workspace Memory",
            "",
            "Derived from append-only project facts. "
            "Edit the source log or policy, not this view.",
        ]
        sections = (
            (FactKind.DECISION, "Decisions"),
            (FactKind.CONVENTION, "Conventions"),
            (FactKind.PITFALL, "Pitfalls"),
        )
        for kind, title in sections:
            group = sorted(
                (item for item in entries if item.kind == kind.value),
                key=lambda item: (item.content, item.key),
            )
            if not group:
                # 空节（只有标题没有内容）会让人以为"这里本该有东西但丢了"——
                # 宁可不写这一节。outcome 一节干脆不在 sections 里：它从不
                # 晋升，curated 里根本没有它。
                continue
            lines.append("")
            lines.append(f"## {title}")
            lines.extend(
                f"- {item.content} (seen {item.occurrences}x; "
                f"evidence: {len(item.evidence_ids)})"
                for item in group
            )
        lines.append("")
        return "\n".join(lines)

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
        """按策略晋升事实：不删证据、幂等（重复跑不重复计）。

        三段式——**筛、分组、落地**：

        1. **筛**：先按年龄线（`cutoff`）过一遍，再剔掉已经进过条目的证据
           （`processed`）。后者就是**幂等的钥匙**：晋升只建立"证据 -> 条目"
           的指针，原始日志一行不动，所以重复跑时靠"这条事实已经指向某个
           条目了"来跳过，而不是靠删掉它。
        2. **分组**：`_entry_key(kind, content)` 是内容寻键——同一件事的多次
           出现（大小写/空格差异被 `_normal_form` 吃掉）会自动落进同一组。
           本课没有修订链、没有 supersession，"同内容"就是"同一条记忆"。
        3. **落地**：一组要么**新建**一个条目（证据整体搬进 `evidence_ids`），
           要么**合并**进已有条目（取并集）。合并是幂等更新，不是新条目。

        代表事实的选法是 `(-importance, recorded_at, fact_id)` 取最小——**最
        重要优先，同重要度取最早的那条措辞**。为什么要在意这个：同一内容的
        几种写法里总要挑一个显示，挑法必须确定，否则每次 distill 出来的
        `MEMORY.md` 都可能不一样，派生视图就失去了"可复现"这个前提。

        账目（`DistillReport`）：`scanned` 是**过了年龄线且没处理过**的事实数
        ——把"已处理"也算进去的话，第二次跑就永远是"扫了 N 条却什么都没发生"，
        看不出来是幂等还是坏了。`skipped` 用 `scanned - created - updated`
        收口，保证恒等式不会因为某个分支漏写而失衡（它的准确含义见
        `DistillReport` 的字段说明，**不等于**"被门槛拦下的事实数"）。
        """

        active = policy or DistillPolicy()
        # timedelta 是日期算术："30 天前"就是现在减去一个 timedelta
        cutoff = _as_utc(as_of) - timedelta(days=active.minimum_age_days)

        existing = self._load_curated()
        by_key = {entry.key: entry for entry in existing}
        processed = {fid for entry in existing for fid in entry.evidence_ids}

        pending = [
            fact for fact in self.read_all_facts()
            if _parse_timestamp(fact.recorded_at) < cutoff
            and fact.fact_id not in processed
        ]
        groups: dict[str, list[MemoryFact]] = {}
        for fact in pending:
            if fact.kind not in STABLE_KINDS:
                continue  # outcome 不进分组：一次测试通过不配变成长期规则
            groups.setdefault(_entry_key(fact.kind, fact.content), []).append(fact)

        eligible = 0
        created = 0
        updated = 0
        for key in sorted(groups):  # 排序遍历：落盘顺序确定，diff 才稳定
            facts = groups[key]
            qualifies = (
                max(item.importance for item in facts) >= active.minimum_importance
                or len(facts) >= active.repeat_threshold
            )
            if not qualifies:
                continue
            eligible += len(facts)

            stamps = sorted(item.recorded_at for item in facts)
            incoming = sorted({item.fact_id for item in facts})
            entry = by_key.get(key)
            if entry is None:
                representative = min(
                    facts,
                    key=lambda item: (-item.importance, item.recorded_at,
                                      item.fact_id),
                )
                by_key[key] = CuratedEntry(
                    key=key,
                    kind=representative.kind,
                    content=representative.content,
                    first_seen=stamps[0],
                    last_seen=stamps[-1],
                    evidence_ids=incoming,
                    occurrences=len(incoming),
                )
                created += 1
            else:
                merged = sorted(set(entry.evidence_ids) | set(incoming))
                entry.evidence_ids = merged
                entry.occurrences = len(merged)
                entry.first_seen = min(entry.first_seen, stamps[0])
                entry.last_seen = max(entry.last_seen, stamps[-1])
                updated += 1

        # 没有变化就不落盘：派生视图每次读都会和 canonical 比对，白写一次
        # 只会多两次原子替换
        if created or updated:
            self._save_curated(list(by_key.values()))

        return DistillReport(
            scanned=len(pending),
            eligible=eligible,
            created=created,
            updated=updated,
            skipped=len(pending) - created - updated,
        )

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

        parts: list[str] = []

        # 第一段：策展视图（蒸馏出来的长期记忆）。read_memory_md 顺手会修复
        # 陈旧的 MEMORY.md，所以这里拿到的永远是"和 canonical 一致"的版本。
        curated = self.read_memory_md().strip()
        if curated:
            parts.append(curated)

        # 第二段：最近 N 条原始事实——补上"刚发生但还没老到能晋升"的那些。
        # recent_limit <= 0 时整段不要。注意**不能**直接写 facts[-recent_limit:]：
        # 0 取负还是 0，而 facts[-0:] 等于整个列表（练习 19 记过这个坑）。
        recent: list[MemoryFact] = []
        if recent_limit > 0:
            recent = self.read_all_facts()[-recent_limit:]
        if recent:
            lines = ["# Recent Workspace Facts", ""]
            lines.extend(
                f"- [{fact.kind}] {fact.content} ({fact.recorded_at[:10]})"
                for fact in recent
            )
            parts.append("\n".join(lines))

        # 两段都是 markdown，用空行分开才不会粘成一坨。
        return "\n\n".join(parts) if parts else NO_MEMORY_PLACEHOLDER

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
