"""s12 Remote Memory（召回链）：normalize → candidate → score → stable rank → render。

============================================================
📚 为什么需要它（补上 s15 留下的断点）
============================================================

s10/s11 解决"把什么状态持久化到哪里"，但没回答长跨度下的另一个问题：

    当前任务需要过去哪一段信息？

两个容易混的东西必须分开：
  - **存储记录**（StoredMemory）：长期存在、能说明属于谁、来自哪里；
  - **召回结果**（RecallHit）：只对某一次 query 有意义——换一个 query，
    排名和结果都可能完全不同。命中不等于事实正确，更不等于必须进
    Prompt（那是 s15 选择管线的职责）；召回也**不写回**存储（读到
    不等于又发生了一次）。

召回主路径拆成五个可独立验证的阶段，任何一步失败都能定位到
输入（normalize）、候选（candidate）、评分（score）、排序（rank）
或渲染（render），而不是只看到一个无法解释的最终分数。

============================================================
📚 可解释评分与稳定排名
============================================================

教学 ranker 是透明的离线基线（不是生产检索的描述）：

    lexical_contribution = 0.85 × query 词项覆盖率
    recency_contribution = 0.15 × 新近度（30 天线性衰减到 0）
    total = 两项之和

每条命中带完整 breakdown：matched terms、覆盖率、新近度、两项贡献
——"为什么命中、每部分贡献多少"事后可查。

排序键显式固定（并列不靠运气）：

    total 降序 → 覆盖率降序 → 采集时间降序 → memory_id 升序

最后一项消除了语料顺序对并列结果的影响：相同输入、相同 as_of、
相同记录，排名永远相同。

query 的**自包含**纪律：召回服务看不到当前聊天，query 文本必须说清
要找什么历史主题，"继续上次"这种指代在这里什么也召回不到——
工具描述里要向模型讲明这一点（教材的 recall_history 同款要求）。

============================================================
📚 与教材（learn-workbuddy/s12）的差异——都是刻意的
============================================================

  - 教材的 RemoteMemoryStore 是一台"远端服务"的教学模拟：JSONL 落盘
    + advisory lock 防两个 writer 的 check-then-act 竞态。本项目**不建
    第三份存储**（机制只保留一套）：durable 证据已经存在——工作区
    daily log（append-only JSONL）与用户偏好/资料（canonical JSON）。
    StoredMemory 在这里是**投影契约**：装配层（toolbox.recall_corpus）
    每次召回时把现有存储投影成记录，语料增长即时可见，也不存在
    "投影与存储不一致"的同步问题。文件锁随之不需要：投影是纯读，
    单写者写自己的存储（各自的模块已管并发）。
  - 教材只召回 CONVERSATION 记录（profile 走会话启动注入）。本项目
    的语料三类：工作区事实（FACT，daily log 原始证据——MEMORY.md 只是
    蒸馏视图，视图外的大量事实只有 recall 找得回）、用户偏好
    （PREFERENCE）、用户资料（PROFILE）；引擎带 kinds 过滤参数，
    装配层选 FACT——偏好/资料已由 s15 的 seed 常驻，召回它们只会
    制造第二份。
  - 教材的 scope 是"哪个远端用户的数据"。本项目是单用户本地场景：
    投影时所有记录落在当前用户的 scope 下，跨用户泄漏的防线
    （engine 按 query scope 过滤）保留并测试；工作区/用户两层的
    区分交给 provenance.source_type——那是所有权标签，不是安全边界。
  - 教材给 RecallHit 留了 .source/.score 兼容视图（旧 walkthrough 的
    包袱）；本项目没有历史消费者，直接用 provenance/score_breakdown。

完成标志：tests/test_recall.py 全部通过。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Sequence
from uuid import uuid4

# 教材同款评分权重与新近度窗口：改这三个数是有意决策，测试钉住。
LEXICAL_WEIGHT = 0.85
RECENCY_WEIGHT = 0.15
RECENCY_WINDOW_DAYS = 30.0
MAX_RECALL_LIMIT = 10
MAX_RECORD_CHARS = 20_000


class MemoryKind(str, Enum):
    """召回语料的记录类别。"""

    CONVERSATION = "conversation"  # 对话记录（本项目暂无生产者，保留契约）
    FACT = "fact"                  # 工作区事实（daily log 原始证据）
    PREFERENCE = "preference"      # 用户偏好（s11）
    PROFILE = "profile"            # 用户资料（s11）


def _clean_text(value: object, *, field_name: str, max_chars: int) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    if len(text) > max_chars:
        raise ValueError(f"{field_name} too long ({len(text)} > {max_chars})")
    return text


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ═══════════════════════════════════════════════════════════════
# 一、数据契约：记录 / 查询 / 候选 / 评分 / 命中 / 结果
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MemorySource:
    """来源证据：source_id 指向产生记录的原始对象（日志/事件/偏好键）。

    captured_at 是**来源事件**发生的时刻；StoredMemory.stored_at 是记录
    **进入存储**的时刻——两者不能混成一个字段（教材铁律）。投影契约下
    两者常相同（append-only 日志的记录时刻即入存时刻），字段仍分开。
    """

    source_id: str
    source_type: str
    title: str
    captured_at: str  # UTC ISO

    def __post_init__(self) -> None:
        _clean_text(self.source_id, field_name="source_id", max_chars=200)
        _clean_text(self.source_type, field_name="source_type", max_chars=100)
        _clean_text(self.title, field_name="title", max_chars=200)
        _parse_timestamp(self.captured_at)


@dataclass(frozen=True)
class StoredMemory:
    """一条可召回的持久记录（投影契约，见模块头"与教材的差异"）。

    memory_id 必须在语料内唯一——投影方（各存储的主键）保证，
    引擎不重复检查：静默去重比让投影 bug 早暴露更糟。
    """

    memory_id: str
    user_scope: str
    kind: MemoryKind
    content: str
    summary: str
    source: MemorySource
    stored_at: str

    def __post_init__(self) -> None:
        _clean_text(self.memory_id, field_name="memory_id", max_chars=200)
        _clean_text(self.user_scope, field_name="user_scope", max_chars=200)
        if not isinstance(self.kind, MemoryKind):
            # fail-closed：字符串进来必须是合法类别，拼错直接炸。
            try:
                object.__setattr__(self, "kind", MemoryKind(str(self.kind)))
            except ValueError as error:
                allowed = ", ".join(item.value for item in MemoryKind)
                raise ValueError(
                    f"memory kind must be one of: {allowed}") from error
        _clean_text(self.content, field_name="content", max_chars=MAX_RECORD_CHARS)
        _clean_text(self.summary, field_name="summary", max_chars=500)
        _parse_timestamp(self.stored_at)


@dataclass(frozen=True)
class RecallQuery:
    """一次自包含的检索请求。

    text 保留可读原文；normalized_text 是 NFKC + casefold + 空白折叠
    的稳定形态；后续所有阶段只用**稳定排序去重后的 terms**。
    query_id 把 candidates、scores、hits 绑到同一次检索，审计可对齐。
    """

    query_id: str
    text: str
    normalized_text: str
    terms: tuple[str, ...]
    user_scope: str
    limit: int
    issued_at: str


@dataclass(frozen=True)
class RecallCandidate:
    """未评分候选：某条记录与 query 存在词项重合。

    只有重叠，还没有 score/rank，也不复制改写 StoredMemory。把候选
    构造单独建模，换成倒排索引/向量召回时排序契约不用动。
    """

    query_id: str
    record: StoredMemory
    searchable_terms: frozenset[str]
    matched_terms: tuple[str, ...]
    captured_at: datetime


@dataclass(frozen=True)
class RecallScoreBreakdown:
    """可解释评分：total 不是孤立数字，每一项怎么来的都在。"""

    matched_terms: tuple[str, ...]
    query_term_count: int
    lexical_coverage: float
    recency: float
    lexical_contribution: float
    recency_contribution: float
    total: float


@dataclass(frozen=True)
class RecallScope:
    """命中的作用域：用户边界 + 记录类别。"""

    user_scope: str
    memory_kind: MemoryKind


@dataclass(frozen=True)
class RecallHit:
    """一条 StoredMemory 针对当前 query 的检索投影。

    不是复制出来的新记忆：再查一次，rank/score_breakdown 都可能不同。
    scope 说清边界，provenance 指回原始来源，breakdown 解释本次排序。
    """

    query_id: str
    memory_id: str
    rank: int
    snippet: str
    scope: RecallScope
    provenance: MemorySource
    score_breakdown: RecallScoreBreakdown


@dataclass(frozen=True)
class RecallResult:
    """一次召回的完整结果：结构化，供审计/截断/桥接 s15。"""

    query: RecallQuery
    hits: tuple[RecallHit, ...]
    searched_records: int   # 检查了多少条（scope/kind 过滤后）
    candidate_records: int  # 其中多少条有词项重合、进了评分
    empty_reason: str | None  # no_records / no_overlap；命中时为 None

    def to_dict(self) -> dict:
        """JSON 可序列化视图（工具结果走它；分数展示 6 位与渲染一致）。"""

        return {
            "query": {
                "query_id": self.query.query_id,
                "text": self.query.text,
                "normalized_text": self.query.normalized_text,
                "terms": list(self.query.terms),
                "user_scope": self.query.user_scope,
                "limit": self.query.limit,
            },
            "hits": [
                {
                    "memory_id": hit.memory_id,
                    "rank": hit.rank,
                    "snippet": hit.snippet,
                    "scope": {
                        "user_scope": hit.scope.user_scope,
                        "memory_kind": hit.scope.memory_kind.value,
                    },
                    "provenance": {
                        "source_id": hit.provenance.source_id,
                        "source_type": hit.provenance.source_type,
                        "title": hit.provenance.title,
                        "captured_at": hit.provenance.captured_at,
                    },
                    "score": round(hit.score_breakdown.total, 6),
                    "score_breakdown": {
                        "matched_terms": list(hit.score_breakdown.matched_terms),
                        "query_term_count": hit.score_breakdown.query_term_count,
                        "lexical_coverage": round(
                            hit.score_breakdown.lexical_coverage, 6),
                        "recency": round(hit.score_breakdown.recency, 6),
                        "contributions": {
                            "lexical": round(
                                hit.score_breakdown.lexical_contribution, 6),
                            "recency": round(
                                hit.score_breakdown.recency_contribution, 6),
                        },
                        "total": round(hit.score_breakdown.total, 6),
                    },
                }
                for hit in self.hits
            ],
            "searched_records": self.searched_records,
            "candidate_records": self.candidate_records,
            "empty_reason": self.empty_reason,
        }


# ═══════════════════════════════════════════════════════════════
# 二、normalize：唯一的规范化入口
# ═══════════════════════════════════════════════════════════════

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def tokenize(text: str) -> tuple[str, ...]:
    """归一化文本 → 稳定词项：拉丁词组整词一记；连续中文按**二元**切。

    "分层记忆" → 分层 / 层记 / 记忆——单字在中文里歧义太大，二元组
    是最便宜的可检索中文特征（教材同款）。单个汉字的 run 记它本身。
    """

    tokens: list[str] = []
    for chunk in _TOKEN_PATTERN.findall(text):
        if _CJK_RUN.fullmatch(chunk):
            if len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return tuple(tokens)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_recall_query(
    text: str,
    *,
    user_scope: str,
    limit: int = 5,
    as_of: str | datetime | None = None,
) -> RecallQuery:
    """原始文本 → RecallQuery。空白与"只有标点"都明确失败。

    这两个失败不是挑剔：terms 为空的 query 进了管线，后面每一步
    都是"对空集合求覆盖率"的伪计算——早炸比晚炸诚实。
    """

    # 教材纪律：原始输入先**折叠空白**保留可读原文（大小写不动），
    # normalized_text 再做 NFKC + casefold——两步不能并成一步，
    # 否则 query.text 就不是用户看过的样子了。
    readable = _clean_text(re.sub(r"\s+", " ", text),
                           field_name="query text", max_chars=2_000)
    scope = _clean_text(user_scope, field_name="user_scope", max_chars=200)
    if not 1 <= limit <= MAX_RECALL_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_RECALL_LIMIT}")
    normalized = _normalize(readable)
    terms = tuple(sorted(set(tokenize(normalized))))
    if not terms:
        raise ValueError(
            "query has no searchable terms (punctuation-only?)")
    moment = (as_of if isinstance(as_of, datetime)
              else _parse_timestamp(as_of) if as_of else datetime.now(timezone.utc))
    return RecallQuery(
        query_id=f"query-{uuid4().hex[:12]}",
        text=readable,
        normalized_text=normalized,
        terms=terms,
        user_scope=scope,
        limit=limit,
        issued_at=_iso(moment),
    )


# ═══════════════════════════════════════════════════════════════
# 三、candidate / score / stable rank：三个纯函数
# ═══════════════════════════════════════════════════════════════

def build_recall_candidates(
    query: RecallQuery,
    records: Sequence[StoredMemory],
) -> tuple[RecallCandidate, ...]:
    """只构造存在词项重合的候选；scope 已由调用方（engine）过滤。

    searchable terms = content ∪ summary 的词项——摘要也是检索面
    （kind、偏好 key 这类浓缩信息常在 summary 里）。
    """

    candidates: list[RecallCandidate] = []
    for record in records:
        searchable = frozenset(tokenize(_normalize(record.content))
                               + tokenize(_normalize(record.summary)))
        matched = tuple(sorted(set(query.terms) & searchable))
        if not matched:
            continue
        candidates.append(RecallCandidate(
            query_id=query.query_id,
            record=record,
            searchable_terms=searchable,
            matched_terms=matched,
            captured_at=_parse_timestamp(record.source.captured_at),
        ))
    return tuple(candidates)


def score_recall_candidate(
    query: RecallQuery,
    candidate: RecallCandidate,
    *,
    as_of: datetime,
) -> RecallScoreBreakdown:
    """透明基线：0.85×覆盖率 + 0.15×新近度，完整 breakdown。"""

    coverage = len(candidate.matched_terms) / len(query.terms)
    age_days = (as_of - candidate.captured_at).total_seconds() / 86_400
    # 夹取到 [0, 1]：采集时间在未来（时钟回拨/手写数据）不该让新近度
    # 超过满分——下游 MemoryContextCandidate 的 score 契约是 0..1。
    recency = min(1.0, max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS))
    lexical_contribution = LEXICAL_WEIGHT * coverage
    recency_contribution = RECENCY_WEIGHT * recency
    total = lexical_contribution + recency_contribution
    if not math.isfinite(total) or not 0.0 <= total <= 1.0:
        raise ValueError(f"recall score out of range: {total}")
    return RecallScoreBreakdown(
        matched_terms=candidate.matched_terms,
        query_term_count=len(query.terms),
        lexical_coverage=coverage,
        recency=recency,
        lexical_contribution=lexical_contribution,
        recency_contribution=recency_contribution,
        total=total,
    )


def stable_rank_recall_candidates(
    query: RecallQuery,
    scored: Sequence[tuple[RecallCandidate, RecallScoreBreakdown]],
) -> list[tuple[RecallCandidate, RecallScoreBreakdown]]:
    """显式排序键：total↓ 覆盖率↓ 采集时间↓ id↑——并列不靠运气。"""

    return sorted(
        scored,
        key=lambda item: (
            -item[1].total,
            -item[1].lexical_coverage,
            -item[0].captured_at.timestamp(),
            item[0].record.memory_id,
        ),
    )


# ═══════════════════════════════════════════════════════════════
# 四、引擎：把五个阶段串起来
# ═══════════════════════════════════════════════════════════════

class RecallEngine:
    """语料 → RecallResult。语料用 provider 回调注入：每次召回现读。

    daily log 会增长，静态列表做语料会悄悄过期；回调（lambda:
    recall_corpus(...)）保证每次拿到的是当时的投影。
    """

    def __init__(self, records_provider: Callable[[], Sequence[StoredMemory]]):
        self._records_provider = records_provider

    def recall(
        self,
        text: str,
        *,
        user_scope: str,
        limit: int = 5,
        as_of: str | datetime | None = None,
        kinds: frozenset[MemoryKind] | set[MemoryKind] | None = None,
    ) -> RecallResult:
        """一次完整召回：normalize → 过滤(scope/kind) → candidate
        → score → stable rank → 取前 limit 条成 hits。

        searched/candidate 两个计数分开记：无结果时能判断问题出在
        语料为空（no_records）还是没有词项重合（no_overlap），
        而不是只看到一个空列表。
        """

        query = normalize_recall_query(
            text, user_scope=user_scope, limit=limit, as_of=as_of)
        moment = _parse_timestamp(query.issued_at)

        searched = [
            record for record in self._records_provider()
            if record.user_scope == query.user_scope
            and (kinds is None or record.kind in kinds)
        ]
        candidates = build_recall_candidates(query, searched)
        if not candidates:
            empty_reason = "no_records" if not searched else "no_overlap"
            return RecallResult(
                query=query, hits=(), searched_records=len(searched),
                candidate_records=0, empty_reason=empty_reason)

        scored = [
            (candidate, score_recall_candidate(query, candidate, as_of=moment))
            for candidate in candidates
        ]
        ranked = stable_rank_recall_candidates(query, scored)[: query.limit]
        hits = tuple(
            RecallHit(
                query_id=query.query_id,
                memory_id=candidate.record.memory_id,
                rank=index + 1,
                snippet=candidate.record.summary
                if candidate.record.summary else candidate.record.content,
                scope=RecallScope(
                    user_scope=candidate.record.user_scope,
                    memory_kind=candidate.record.kind),
                provenance=candidate.record.source,
                score_breakdown=breakdown,
            )
            for index, (candidate, breakdown) in enumerate(ranked)
        )
        return RecallResult(
            query=query, hits=hits, searched_records=len(searched),
            candidate_records=len(candidates), empty_reason=None)
