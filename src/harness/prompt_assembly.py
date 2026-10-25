"""s15 Prompt Assembly：从候选到预算内上下文——先选择，再装箱，最后才组装。

============================================================
📚 为什么需要它（承接 s10/s11 的记忆 + s16 的技能目录）
============================================================

有了记忆，下一步的诱惑是"全都塞进 system"。但"记得"不等于"该进
Prompt"：候选里可能有别的用户作用域的数据、低置信度的噪声、同一事实
槽位里的新旧冲突、排名靠后的长记录……全塞进去的代价是预算——上下文
窗口是稀缺资源，安全规则和工具说明才是不可让位的东西。

所以这一课把"进不进 Prompt"做成**两层各自可解释的决策**：

  第一层：记忆候选选择管线（select_memory_context）
      scope 门 → 置信度门 → 权限层级排序 → 精确去重
      → 显式冲突裁决 → top-k / 双预算装箱
      每条候选落一个 selected/rejected 决策 + 稳定原因码。

  第二层：系统提示片段规划器（plan_prompt）
      required 片段先预留预算（装不下就 fail-closed 抛错，
      绝不静默删安全规则）；optional 片段按 budget_priority
      竞争剩余空间；最后按 priority（展示顺序）渲染。

两份决策日志**不能合并**：一个回答"为什么没选这条记忆"，另一个回答
"为什么最终没放这个片段"。

============================================================
📚 顺序为什么不能交换（本课最重要的一张表）
============================================================

  1. scope 门：跨用户候选**先以 scope_mismatch 拒绝**——排序不能覆盖
     安全边界，分数再高也一样；
  2. 置信度门：低于 min_score = 默认弃权（abstain），不是"排最后也许
     能用"——预算宽松时把噪声放回来比少一条记忆更糟；
  3. 权限层级（current_turn > workspace_override > user_default）：
     低层候选即使分数更高、更新、排名更靠前，也不能覆盖高层候选；
     权限**不能绕过前两道门**（跨 scope / 低分的 current_turn 照样拒）；
  4. 精确去重：槽位 + 正文都相同才算重复；正文相同、槽位不同**都要
     留下**（它们携带不同的事实约束）；
  5. 冲突裁决：同一槽位只留一个 winner；败者**不回填**——它表达的是
     已被裁决掉的事实，不只是"昂贵的上下文"；
  6. top-k / 预算装箱：原子块不做中间截断；超大的高分记录被拒绝后，
     **较小的后续记录仍可填入**剩余预算（与冲突败者不回填形成对照）。

============================================================
📚 与教材（learn-workbuddy/s15）的差异——都是刻意的
============================================================

  - 教材的输入是 s12 云端记忆的 RecallResult（query → hits → score）。
    本项目没有 s12，召回源换成**自己的两层记忆**：
      · 用户记忆（偏好 + 资料）经 candidates_from_user_memory 进管线
        ——偏好的语义 key 天然就是事实槽位（conflict_key）；
      · 工作区记忆**不进这条管线**：它在 s10 已经过 harness 控制的
        蒸馏闸门（年龄/类型/重要度），"选择"发生在那一层；这里作为
        一个原子片段参与第二层预算。
  - 分数恒 1.0：这些候选不是检索命中，是蒸馏/人工确认过的记录——
    min_score 门对它们天然放行，真正起作用的约束是 top-k 和预算。
    s12 落地后真分数从那条链来，这里的契约零改动。
  - 权限三层里，本项目现在只有两个生产者（偏好 = user_default，
    可信调用方可标 workspace_override / current_turn）；机制按教材
    全量实现，缺的生产者等 s18（专家）/s12（召回）补。
  - 教材的 adapter 用结构化属性（getattr）而不 import s12——本项目
    的 candidates_from_user_memory 同样只做结构适配，不 import
    user_memory（依赖方向：谁都 import 协议，协议不认识任何人）。

完成标志：tests/test_prompt_assembly.py 全部通过。
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping, Sequence

# 教材同款默认预算：记忆片段 3000 字符 / 800 教学token。装配层
# （toolbox）可以整体收紧或放宽，但"字符和 token 是两条独立约束"
# 这一点不动——离线回归吃字符，接了真 tokenizer 才吃 token。
DEFAULT_MEMORY_BUDGET_CHARS = 3_000
DEFAULT_MEMORY_BUDGET_TOKENS = 800


# ═══════════════════════════════════════════════════════════════
# 一、决策词汇表：状态 / 原因码 / 权限层级
# ═══════════════════════════════════════════════════════════════

class MemoryDecisionStatus(str, Enum):
    """一条候选的终局：进了渲染出来的上下文，还是没进。"""

    SELECTED = "selected"
    REJECTED = "rejected"


class MemoryDecisionReason(str, Enum):
    """稳定原因码——给 trace、测试和离线评估看，不是给人看的散文。

    Enum 而非裸字符串：拼错直接 AttributeError，静默拼错不存在。
    """

    SELECTED = "selected"
    SCOPE_MISMATCH = "scope_mismatch"
    LOW_CONFIDENCE = "low_confidence"
    DUPLICATE_CONTENT = "duplicate_content"
    AUTHORITY_LOSER = "authority_loser"
    CONFLICT_LOSER = "conflict_loser"
    TOP_K_REACHED = "top_k_reached"
    CHAR_BUDGET_EXCEEDED = "char_budget_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


class PreferenceAuthority(str, Enum):
    """谁供给的偏好，层级由可信的 Harness 边界标注——模型不能发明。

    刻意**不含**任何"系统规则"层级：Harness 的安全规则是 required
    的 PromptSegment，任何记忆/偏好候选都不能覆盖它（见第二层）。

    - current_turn：用户对当前任务的明确要求（"这次用 Go 写"）；
    - workspace_override：当前项目的局部约定（"本仓库统一 TypeScript"）；
    - user_default：跨项目复用的个人默认偏好（"我通常偏好 Python"）。
    """

    CURRENT_TURN = "current_turn"
    WORKSPACE_OVERRIDE = "workspace_override"
    USER_DEFAULT = "user_default"


_AUTHORITY_PRIORITY = {
    PreferenceAuthority.CURRENT_TURN: 3,
    PreferenceAuthority.WORKSPACE_OVERRIDE: 2,
    PreferenceAuthority.USER_DEFAULT: 1,
}


def _normalize_authority(value: object) -> PreferenceAuthority:
    """归一化权限字段并拒绝发明的层级（fail-closed）。

    模型提议的候选只有它自己声称的层级可用；"system_override"一类
    的高层发明在这里 ValueError，而不是被默默当默认值。
    """

    if isinstance(value, PreferenceAuthority):
        return value
    try:
        return PreferenceAuthority(str(value).strip())
    except ValueError as error:
        allowed = ", ".join(item.value for item in PreferenceAuthority)
        raise ValueError(f"memory authority must be one of: {allowed}") from error


def _required_text(value: object, *, field_name: str) -> str:
    """必填文本：strip 后非空。空 id / 空正文直接在构造期炸掉。"""

    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def _parse_utc_timestamp(value: str) -> datetime:
    """解析一个 ISO 时间戳，让排序永远不退回字符串比较。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid captured_at timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_memory_text(value: str) -> str:
    """确定性"精确内容键"：NFKC + 大小写折叠 + 空白归一。

    刻意**不声称**语义去重：这里只认"同一个槽位里逐字相同（modulo
    编码/空白/大小写）"；语义级别的合并是上游（embedding cluster）
    的事，且必须继续输出可追踪的 winner/loser 关系。
    """

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


# ═══════════════════════════════════════════════════════════════
# 二、候选 / 策略 / 决策 / 计划：第一层的数据契约
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MemoryContextProvenance:
    """来源证据——从召回投影进 Prompt 的路上不许弄丢的东西。

    决策日志里只记 memory_id 的话，"这条记忆从哪来"就断了：source_id
    （稳定来源标识）、source_type（来源类别）、title（人读的标题）、
    captured_at（采集时刻，参与排序）四样缺一不可。
    """

    source_id: str
    source_type: str
    title: str
    captured_at: str  # UTC ISO

    def __post_init__(self) -> None:
        _required_text(self.source_id, field_name="provenance source_id")
        _required_text(self.source_type, field_name="provenance source_type")
        _required_text(self.title, field_name="provenance title")
        _parse_utc_timestamp(self.captured_at)


@dataclass(frozen=True)
class MemoryContextCandidate:
    """一条查询作用域内的候选。

    conflict_key 刻意要求**显式**：从任意散文里猜矛盾，等于把一次
    不可观察的第二次推理藏进 Prompt 组装的关键路径。类型化的记忆
    生产者（比如本项目的偏好语义 key）直接把竞争同一槽位的记录
    标成同一个 key，S15 只做确定性裁决。
    """

    memory_id: str
    text: str
    user_scope: str
    score: float
    source_rank: int
    provenance: MemoryContextProvenance
    conflict_key: str | None = None
    authority: PreferenceAuthority = PreferenceAuthority.USER_DEFAULT

    def __post_init__(self) -> None:
        _required_text(self.memory_id, field_name="memory_id")
        _required_text(self.text, field_name="memory text")
        _required_text(self.user_scope, field_name="memory user_scope")
        if not math.isfinite(self.score) or not 0.0 <= self.score <= 1.0:
            raise ValueError("memory score must be finite and between 0 and 1")
        if self.source_rank < 1:
            raise ValueError("memory source_rank must be positive")
        if self.conflict_key is not None:
            _required_text(self.conflict_key, field_name="memory conflict_key")
        # frozen dataclass 里归一化字段：绕过 frozen 赋一次"同型值"。
        # 值仍是 PreferenceAuthority，只是把 str/enum 混入归一到 enum。
        object.__setattr__(
            self, "authority", _normalize_authority(self.authority))

    def candidate_order(self) -> tuple[object, ...]:
        """权威排序键：权限降序 → 分数降序 → 检索排名升序
        → 采集时间降序 → id 升序。前四项全平时 id 兜底，
        输入顺序永远不参与决策。"""

        return (
            -_AUTHORITY_PRIORITY[self.authority],
            -self.score,
            self.source_rank,
            -_parse_utc_timestamp(self.provenance.captured_at).timestamp(),
            self.memory_id,
        )


@dataclass(frozen=True)
class MemorySelectionPolicy:
    """两道门 + top-k + 记忆片段自己的预算切片。

    字符和 token 是**两条独立约束**：字符预算离线、确定、可回归；
    token 预算接了目标模型 tokenizer 才有意义（token_counter 注入）。
    None = 该维度不限。
    """

    min_score: float = 0.35
    top_k: int = 5
    max_chars: int | None = DEFAULT_MEMORY_BUDGET_CHARS
    max_tokens: int | None = DEFAULT_MEMORY_BUDGET_TOKENS

    def __post_init__(self) -> None:
        if not math.isfinite(self.min_score) or not 0.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be finite and between 0 and 1")
        if self.top_k < 0:
            raise ValueError("top_k must be non-negative")
        if self.max_chars is not None and self.max_chars < 0:
            raise ValueError("max_chars must be non-negative or None")
        if self.max_tokens is not None and self.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative or None")


@dataclass(frozen=True)
class MemorySelectionDecision:
    """一条候选的终局 + 支撑这个终局的证据。

    candidate_chars/tokens 是**渲染后**的成本（含 XML 包装和 provenance
    属性）——装箱决策必须看到真实成本，不能看"正文多长"。
    related_memory_id：去重/冲突败者指向 winner，审计链不断。
    """

    memory_id: str
    status: MemoryDecisionStatus
    reason: MemoryDecisionReason
    score: float
    source_rank: int
    authority: PreferenceAuthority
    candidate_chars: int
    candidate_tokens: int
    related_memory_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class MemoryContextPlan:
    """渲染好的记忆上下文 + 一份完整、确定性的决策日志。"""

    context: str
    user_scope: str
    policy: MemorySelectionPolicy
    candidate_count: int
    used_chars: int
    used_tokens: int
    decisions: tuple[MemorySelectionDecision, ...]

    @property
    def selected_memory_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.memory_id
            for decision in self.decisions
            if decision.status is MemoryDecisionStatus.SELECTED
        )

    @property
    def rejected_memory_ids(self) -> tuple[str, ...]:
        return tuple(
            decision.memory_id
            for decision in self.decisions
            if decision.status is MemoryDecisionStatus.REJECTED
        )


TokenCounter = Callable[[str], int]


def estimate_context_tokens(text: str) -> int:
    """确定性的教学估算，**不是**任何厂商 tokenizer 的声明。

    拉丁词组一记、单个 CJK 字符一记、标点一记；空白不计。生产适配层
    应注入目标模型的 tokenizer——策略与决策日志对此无感。
    """

    return len(re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]|[^\s]", text))


def _count_tokens(token_counter: TokenCounter, text: str) -> int:
    """校验注入的 tokenizer：它的结果要参与准入决策，先验后用。"""

    count = token_counter(text)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("token_counter must return a non-negative integer")
    return count


# ═══════════════════════════════════════════════════════════════
# 三、渲染：原子块 + 成本可见
# ═══════════════════════════════════════════════════════════════

def render_memory_hit(candidate: MemoryContextCandidate) -> str:
    """渲染一条候选：正文 + 作用域 + provenance，全属性 html 转义。

    一条 hit 是**原子块**：要么整条进（含全部证据属性），要么整条不
    进——切字符串可能切断 provenance、XML 和事实语义。
    """

    provenance = candidate.provenance
    conflict_attr = (
        f' conflict_key="{html.escape(candidate.conflict_key, quote=True)}"'
        if candidate.conflict_key
        else ""
    )
    return (
        f'<memory_hit memory_id="{html.escape(candidate.memory_id, quote=True)}" '
        f'score="{candidate.score:.6f}" source_rank="{candidate.source_rank}" '
        f'authority="{candidate.authority.value}" '
        f'user_scope="{html.escape(candidate.user_scope, quote=True)}" '
        f'source_id="{html.escape(provenance.source_id, quote=True)}" '
        f'source_type="{html.escape(provenance.source_type, quote=True)}" '
        f'captured_at="{html.escape(provenance.captured_at, quote=True)}"'
        f'{conflict_attr}>'
        f'{html.escape(candidate.text)}'
        "</memory_hit>"
    )


def render_memory_context(
    user_scope: str,
    candidates: Sequence[MemoryContextCandidate],
) -> str:
    """渲染选中的候选集合——包装成本（<recalled_memory> 标签）不隐藏。

    没选中任何一条时返回**空串**而不是空的 <recalled_memory>：空标签
    既浪费 token，又让模型误以为存在记忆证据（教材的误区清单）。
    """

    if not candidates:
        return ""
    raw_scope = _required_text(user_scope, field_name="user_scope")
    mismatched = sorted(
        candidate.memory_id
        for candidate in candidates
        if candidate.user_scope != raw_scope
    )
    if mismatched:
        raise ValueError(
            "cannot render candidates from another scope: "
            + ", ".join(mismatched)
        )
    scope = html.escape(raw_scope, quote=True)
    hits = "\n".join(render_memory_hit(candidate) for candidate in candidates)
    return (
        f'<recalled_memory user_scope="{scope}" selected="{len(candidates)}">\n'
        f"{hits}\n"
        "</recalled_memory>"
    )


def _decision(
    candidate: MemoryContextCandidate,
    *,
    status: MemoryDecisionStatus,
    reason: MemoryDecisionReason,
    token_counter: TokenCounter,
    related_memory_id: str | None = None,
    detail: str = "",
) -> MemorySelectionDecision:
    """把"候选 + 终局"落成决策记录：成本按渲染后的真实尺寸计。"""

    rendered = render_memory_hit(candidate)
    return MemorySelectionDecision(
        memory_id=candidate.memory_id,
        status=status,
        reason=reason,
        score=candidate.score,
        source_rank=candidate.source_rank,
        authority=candidate.authority,
        candidate_chars=len(rendered),
        candidate_tokens=_count_tokens(token_counter, rendered),
        related_memory_id=related_memory_id,
        detail=detail,
    )


# ═══════════════════════════════════════════════════════════════
# 四、第一层：选择管线（顺序即语义，六步不可交换）
# ═══════════════════════════════════════════════════════════════

def select_memory_context(
    candidates: Sequence[MemoryContextCandidate],
    *,
    user_scope: str,
    policy: MemorySelectionPolicy | None = None,
    token_counter: TokenCounter = estimate_context_tokens,
) -> MemoryContextPlan:
    """在显式约束下选择并原子装箱召回记忆。

    顺序是刻意的（模块头那张表）：scope 与置信度是**门**，门内先按
    权限层级排序，精确去重与冲突裁决在预算花出去**之前**完成，最后
    top-k 与双预算按同一顺序装箱。超大的记录被拒绝**不停止装箱**——
    较小的后续记录仍可填入；但冲突败者不回填（被裁决掉的事实 ≠
    昂贵的上下文）。每条候选恰好一个决策，顺序与输入无关。
    """

    scope = _required_text(user_scope, field_name="user_scope")
    active_policy = policy or MemorySelectionPolicy()
    candidate_ids = [candidate.memory_id for candidate in candidates]
    duplicates = sorted(
        memory_id
        for memory_id in set(candidate_ids)
        if candidate_ids.count(memory_id) > 1
    )
    if duplicates:
        raise ValueError(
            "duplicate memory candidate ids: " + ", ".join(duplicates))

    ranked = sorted(candidates, key=MemoryContextCandidate.candidate_order)
    decisions: dict[str, MemorySelectionDecision] = {}

    # ── 1+2. 两道门：scope → 置信度。权限不能绕过任何一道。──────
    eligible: list[MemoryContextCandidate] = []
    for candidate in ranked:
        if candidate.user_scope != scope:
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.SCOPE_MISMATCH,
                token_counter=token_counter,
                detail=(f"candidate scope {candidate.user_scope!r} "
                        f"!= query scope {scope!r}"),
            )
        elif candidate.score < active_policy.min_score:
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.LOW_CONFIDENCE,
                token_counter=token_counter,
                detail=(f"score {candidate.score:.6f} is below "
                        f"min_score {active_policy.min_score:.6f}"),
            )
        else:
            eligible.append(candidate)

    # ── 3+4. 权限排序（ranked 已保证）+ 精确去重。
    # 槽位身份跟着去重走：删掉另一槽位的同文记录，可能顺手删掉那个
    # 槽位的权限 winner，让已被裁决的偏好复活（教材 A/B/C 例子）。
    # 无槽位的候选只与同样无槽位的去重；None 与任何显式槽位都不同。
    deduplicated: list[MemoryContextCandidate] = []
    content_winners: dict[tuple[str | None, str], MemoryContextCandidate] = {}
    for candidate in eligible:
        normalized_slot = (
            _normalized_memory_text(candidate.conflict_key)
            if candidate.conflict_key is not None
            else None
        )
        key = (normalized_slot, _normalized_memory_text(candidate.text))
        winner = content_winners.get(key)
        if winner is not None:
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.DUPLICATE_CONTENT,
                token_counter=token_counter,
                related_memory_id=winner.memory_id,
                detail=(f"normalized content duplicates {winner.memory_id} "
                        f"in fact slot {normalized_slot!r}"),
            )
            continue
        content_winners[key] = candidate
        deduplicated.append(candidate)

    # ── 5. 显式冲突：同一槽位只留一个 winner。
    # 跨层级 → authority_loser；同层级 → conflict_loser（此刻顺序仍由
    # 权限排序保证，同层级里先到的是 score/rank 更强的）。
    conflict_free: list[MemoryContextCandidate] = []
    conflict_winners: dict[str, MemoryContextCandidate] = {}
    for candidate in deduplicated:
        if candidate.conflict_key is None:
            conflict_free.append(candidate)
            continue
        normalized_key = _normalized_memory_text(candidate.conflict_key)
        winner = conflict_winners.get(normalized_key)
        if winner is not None:
            authority_lost = (
                _AUTHORITY_PRIORITY[candidate.authority]
                < _AUTHORITY_PRIORITY[winner.authority]
            )
            if authority_lost:
                detail = (f"conflict slot {candidate.conflict_key!r}: "
                          f"{candidate.authority.value} lost to "
                          f"{winner.authority.value} candidate "
                          f"{winner.memory_id}")
                reason = MemoryDecisionReason.AUTHORITY_LOSER
            else:
                detail = (f"conflict slot {candidate.conflict_key!r} "
                          f"won by {winner.memory_id}")
                reason = MemoryDecisionReason.CONFLICT_LOSER
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=reason,
                token_counter=token_counter,
                related_memory_id=winner.memory_id,
                detail=detail,
            )
            continue
        conflict_winners[normalized_key] = candidate
        conflict_free.append(candidate)

    # ── 6. top-k + 双预算装箱：逐条原子尝试，超标拒绝但不停止。───
    selected: list[MemoryContextCandidate] = []
    for candidate in conflict_free:
        if len(selected) >= active_policy.top_k:
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.TOP_K_REACHED,
                token_counter=token_counter,
                detail=f"top_k {active_policy.top_k} already filled",
            )
            continue

        proposed = render_memory_context(scope, [*selected, candidate])
        proposed_chars = len(proposed)
        proposed_tokens = _count_tokens(token_counter, proposed)
        if (active_policy.max_chars is not None
                and proposed_chars > active_policy.max_chars):
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.CHAR_BUDGET_EXCEEDED,
                token_counter=token_counter,
                detail=(f"would use {proposed_chars} chars; "
                        f"budget is {active_policy.max_chars}"),
            )
            continue
        if (active_policy.max_tokens is not None
                and proposed_tokens > active_policy.max_tokens):
            decisions[candidate.memory_id] = _decision(
                candidate,
                status=MemoryDecisionStatus.REJECTED,
                reason=MemoryDecisionReason.TOKEN_BUDGET_EXCEEDED,
                token_counter=token_counter,
                detail=(f"would use {proposed_tokens} tokens; "
                        f"budget is {active_policy.max_tokens}"),
            )
            continue

        selected.append(candidate)
        decisions[candidate.memory_id] = _decision(
            candidate,
            status=MemoryDecisionStatus.SELECTED,
            reason=MemoryDecisionReason.SELECTED,
            token_counter=token_counter,
            detail="passed gates and fits top-k/context budgets",
        )

    context = render_memory_context(scope, selected)
    return MemoryContextPlan(
        context=context,
        user_scope=scope,
        policy=active_policy,
        candidate_count=len(candidates),
        used_chars=len(context),
        used_tokens=_count_tokens(token_counter, context),
        # 决策按 ranked（权威排序）输出——日志顺序同样与输入顺序无关。
        decisions=tuple(
            decisions[candidate.memory_id] for candidate in ranked),
    )


# ═══════════════════════════════════════════════════════════════
# 五、候选适配器：本项目自己的记忆 → 候选（结构适配，零 import）
# ═══════════════════════════════════════════════════════════════

def candidates_from_user_memory(
    user_memory: object,
    *,
    as_of: str | None = None,
    authority_by_memory_id: Mapping[str, PreferenceAuthority | str] | None = None,
) -> tuple[MemoryContextCandidate, ...]:
    """把 UserMemory 的活偏好 + 资料投影成候选——不重算任何东西。

    教材的 memory_candidates_from_recall 适配 s12 的 RecallResult；本
    项目没有 s12，召回源是自己的用户记忆层。适配器走**结构化属性**
    （list_active_preferences / read_profile / user_scope），不 import
    user_memory——S15 保持独立可测，user_memory 演化不牵连这里。

    投影规则：
    - 偏好：key 天然是事实槽位 → conflict_key=f"preference:{key}"，
      authority 默认 user_default；可信调用方可按 memory_id 显式标注
      更高层（比如工作区同 key 约定标 workspace_override）；
    - 资料（profile）：没有槽位语义的恒真事实 → 不带 conflict_key；
    - 分数恒 1.0（蒸馏/人工确认，非检索命中——见模块头"与教材的
      差异"）；source_rank 按（资料 → 偏好按 key 排序）稳定编号；
    - captured_at：偏好用 updated_at（它自己的证据时刻）；资料 JSON
      不带时间戳，用 profile 文件的 mtime——"最后一次被写"的时刻。
    """

    user_scope = _required_text(
        getattr(user_memory, "user_scope", ""), field_name="user_scope")
    overrides = authority_by_memory_id or {}
    converted: list[MemoryContextCandidate] = []

    def _make(memory_id: str, text: str, conflict_key: str | None,
              source_id: str, title: str, captured_at: str
              ) -> MemoryContextCandidate:
        return MemoryContextCandidate(
            memory_id=memory_id,
            text=text,
            user_scope=user_scope,
            score=1.0,
            source_rank=len(converted) + 1,
            provenance=MemoryContextProvenance(
                source_id=source_id,
                source_type="user_memory",
                title=title,
                captured_at=captured_at,
            ),
            conflict_key=conflict_key,
            authority=overrides.get(memory_id, PreferenceAuthority.USER_DEFAULT),
        )

    profile = dict(getattr(user_memory, "read_profile", dict)() or {})
    profile_file = getattr(user_memory, "profile_file", None)
    profile_captured_at = ""
    if profile_file is not None and profile_file.exists():
        stamp = datetime.fromtimestamp(
            profile_file.stat().st_mtime, tz=timezone.utc)
        profile_captured_at = stamp.isoformat()
    elif profile:
        # 结构上读得到资料、却拿不到证据时刻：宁可炸也不编一个时间——
        # provenance 是审计链，"大概齐"的时间戳比缺失更糟。
        raise ValueError(
            "user_memory profile has fields but profile_file is unavailable")
    for name in sorted(profile):
        value = str(profile[name]).strip()
        if not value:
            continue
        converted.append(_make(
            memory_id=f"profile:{name}",
            text=f"{name} = {value}",
            conflict_key=None,
            source_id="profile",
            title=f"个人资料 {name}",
            captured_at=profile_captured_at,
        ))

    for preference in getattr(user_memory, "list_active_preferences",
                              lambda **kw: [])(as_of=as_of):
        converted.append(_make(
            memory_id=f"pref:{preference.key}",
            text=f"{preference.key} = {preference.value}",
            conflict_key=f"preference:{preference.key}",
            source_id=(preference.source_event_id
                       or f"preference:{preference.key}"),
            title=f"偏好 {preference.key}",
            captured_at=preference.updated_at,
        ))

    return tuple(converted)


# ═══════════════════════════════════════════════════════════════
# 五之二、候选适配器：RecallResult → 候选（s12 桥接，结构适配零 import）
# ═══════════════════════════════════════════════════════════════

def memory_candidates_from_recall(
    result: object,
    *,
    conflict_keys: Mapping[str, str] | None = None,
    authority_by_memory_id: Mapping[str, PreferenceAuthority | str] | None = None,
) -> tuple[MemoryContextCandidate, ...]:
    """把 s12 RecallResult 的 hits 投影成候选——**复制证据，不重算分数**。

    教材同款桥（结构化属性访问，不 import recall——两边独立演进）：
    scope / score / rank / provenance 全部照抄：检索证据属于 s12，
    选择决策属于 s15，桥只做形状翻译。score 取 score_breakdown.total
    ——从此 min_score 门与分数排序吃的是真召回分数，不是恒 1.0。

    conflict_keys：类型化生产者可按 memory_id 标注事实槽位（谁竞争
    同一席位由供给方说了算，S15 不从散文里猜）；authority 默认
    user_default，可信调用方可显式标注更高层。
    """

    converted: list[MemoryContextCandidate] = []
    for hit in getattr(result, "hits", ()):  # type: ignore[union-attr]
        memory_id = _required_text(
            getattr(hit, "memory_id", ""), field_name="recall memory_id")
        scope = getattr(hit, "scope", None)
        hit_scope = getattr(scope, "user_scope", None)
        provenance = getattr(hit, "provenance", None)
        if provenance is None:
            raise ValueError(f"recall hit {memory_id} has no provenance")
        breakdown = getattr(hit, "score_breakdown", None)
        score = getattr(breakdown, "total", getattr(hit, "score", None))
        if score is None:
            raise ValueError(f"recall hit {memory_id} has no score")
        converted.append(MemoryContextCandidate(
            memory_id=memory_id,
            text=_required_text(
                getattr(hit, "snippet", ""), field_name="recall snippet"),
            user_scope=_required_text(
                hit_scope, field_name="recall user_scope"),
            score=float(score),
            source_rank=int(getattr(hit, "rank", 0)),
            provenance=MemoryContextProvenance(
                source_id=str(getattr(provenance, "source_id", "")),
                source_type=str(getattr(provenance, "source_type", "")),
                title=str(getattr(provenance, "title", "")),
                captured_at=str(getattr(provenance, "captured_at", "")),
            ),
            conflict_key=(conflict_keys or {}).get(memory_id),
            authority=(authority_by_memory_id or {}).get(
                memory_id, PreferenceAuthority.USER_DEFAULT),
        ))
    return tuple(converted)


# ═══════════════════════════════════════════════════════════════
# 六、第二层：系统提示片段规划器
# ═══════════════════════════════════════════════════════════════

@dataclass
class PromptSegment:
    """系统提示的一个片段。

    - name：调试用标识（同批片段里必须唯一）；
    - builder：返回 str | None；None = 本次不参与；
    - condition：False = inactive（连预算竞争都不进）；
    - priority：**展示顺序**（小 = 靠前），与预算无关；
    - required：预算永远不能删它——安全规则的席位；
    - budget_priority：预算紧张时谁先拿到空间（大 = 先拿）；
    - provenance：来源标签，进决策报告。

    展示顺序与预算价值是**两件事**：工作模式可以排最后，却仍然是
    不可删除的 required 片段。
    """

    name: str
    builder: Callable[[], str | None]
    condition: Callable[[], bool] = field(default=lambda: True)
    priority: int = 50
    required: bool = False
    budget_priority: int = 50
    provenance: str = "runtime"

    def build(self) -> str | None:
        if not self.condition():
            return None
        return self.builder()


PROMPT_SEPARATOR = "\n\n---\n\n"


class PromptBudgetError(ValueError):
    """required 片段装不进预算：宁可炸掉 provider 调用，
    也不把安全规则降级成可选偏好（fail-closed）。"""


@dataclass(frozen=True)
class SegmentDecision:
    """预算规划器做的一个可解释的收录/舍弃决策。"""

    name: str
    priority: int
    required: bool
    budget_priority: int
    provenance: str
    status: str  # included / dropped / inactive
    original_chars: int
    rendered_chars: int
    reason: str


@dataclass(frozen=True)
class PromptPlan:
    """渲染出来的提示 + 产生它的那份决策。"""

    prompt: str
    budget_chars: int | None
    used_chars: int
    decisions: tuple[SegmentDecision, ...]

    @property
    def included_names(self) -> tuple[str, ...]:
        return tuple(
            decision.name for decision in self.decisions
            if decision.status == "included"
        )

    @property
    def dropped_names(self) -> tuple[str, ...]:
        return tuple(
            decision.name for decision in self.decisions
            if decision.status == "dropped"
        )


def _rendered_length(contents: list[str]) -> int:
    """含分隔符的真实装箱成本——不是裸正文长度之和。"""

    if not contents:
        return 0
    return sum(len(content) for content in contents) + (
        (len(contents) - 1) * len(PROMPT_SEPARATOR))


def plan_prompt(
    segments: Sequence[PromptSegment],
    *,
    budget_chars: int | None = 12_000,
) -> PromptPlan:
    """在显式字符预算下构建并选择提示片段。

    required 先收录并检查预算——超了直接 PromptBudgetError（fail-
    closed，不静默删安全规则）；optional 按 budget_priority 降序逐段
    尝试（同分按 priority、name 稳定破平）；选中内容最后按展示
    priority 渲染。片段是**原子的信任/来源块**：超预算整段舍弃，
    绝不切中间——要紧凑投影，片段自己的 builder 先做。
    """

    if budget_chars is not None and budget_chars < 0:
        raise ValueError("budget_chars must be non-negative or None")
    names = [segment.name for segment in segments]
    duplicate_names = sorted(
        {name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(
            "duplicate prompt segment names: " + ", ".join(duplicate_names))

    built: list[tuple[PromptSegment, str]] = []
    for segment in segments:
        content = segment.build()
        if content:
            built.append((segment, content))

    required = [(segment, content)
                for segment, content in built if segment.required]
    optional = [(segment, content)
                for segment, content in built if not segment.required]
    selected: dict[str, tuple[PromptSegment, str]] = {
        segment.name: (segment, content) for segment, content in required
    }

    def _display_key(item: tuple[PromptSegment, str]) -> tuple[int, str]:
        return (item[0].priority, item[0].name)

    required_contents = [
        content for _, content in sorted(required, key=_display_key)]
    required_chars = _rendered_length(required_contents)
    if budget_chars is not None and required_chars > budget_chars:
        required_names = ", ".join(
            segment.name for segment, _ in required)
        raise PromptBudgetError(
            f"required prompt segments need {required_chars} chars, "
            f"but budget is {budget_chars}: {required_names}")

    rejected: dict[str, str] = {}
    for segment, content in sorted(
        optional,
        key=lambda item: (-item[0].budget_priority, item[0].priority,
                          item[0].name),
    ):
        ordered_contents = [
            value for _, value
            in sorted([*selected.values(), (segment, content)],
                      key=_display_key)
        ]
        candidate_chars = _rendered_length(ordered_contents)
        if budget_chars is None or candidate_chars <= budget_chars:
            selected[segment.name] = (segment, content)
        else:
            rejected[segment.name] = (
                f"needs {candidate_chars} chars after higher-value segments; "
                f"budget is {budget_chars}"
            )

    ordered_selected = sorted(selected.values(), key=_display_key)
    prompt = PROMPT_SEPARATOR.join(content for _, content in ordered_selected)

    decisions: list[SegmentDecision] = []
    built_by_identity = {id(segment): content for segment, content in built}
    for segment in sorted(segments, key=lambda item: (item.priority, item.name)):
        built_content = built_by_identity.get(id(segment))
        if built_content is None:
            decisions.append(SegmentDecision(
                name=segment.name, priority=segment.priority,
                required=segment.required,
                budget_priority=segment.budget_priority,
                provenance=segment.provenance, status="inactive",
                original_chars=0, rendered_chars=0,
                reason="condition false or builder returned empty content",
            ))
        elif segment.name in selected:
            decisions.append(SegmentDecision(
                name=segment.name, priority=segment.priority,
                required=segment.required,
                budget_priority=segment.budget_priority,
                provenance=segment.provenance, status="included",
                original_chars=len(built_content),
                rendered_chars=len(built_content),
                reason=("required segment" if segment.required
                        else "fits budget by value order"),
            ))
        else:
            decisions.append(SegmentDecision(
                name=segment.name, priority=segment.priority,
                required=segment.required,
                budget_priority=segment.budget_priority,
                provenance=segment.provenance, status="dropped",
                original_chars=len(built_content), rendered_chars=0,
                reason=rejected[segment.name],
            ))

    return PromptPlan(
        prompt=prompt,
        budget_chars=budget_chars,
        used_chars=len(prompt),
        decisions=tuple(decisions),
    )
