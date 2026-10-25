"""s15 Prompt Assembly 的离线单元测试：选择管线 + 片段规划器 + 装配接线。

三块各自独立可测（教材的职责边界）：
  - select_memory_context：六步顺序、稳定原因码、原子装箱；
  - plan_prompt：required fail-closed、optional 按价值竞争、展示序分离；
  - plan_history_seed / build_history_seed：起步历史由规划器决定顺序与预算。

全部离线：不碰网络、不碰真实 ~/.workbuddy 与项目根的记忆目录
（临时目录 + 独占前缀，同 test_std_tools 的纪律）。
"""

import tempfile
import unittest
from pathlib import Path

from src.harness.prompt_assembly import (
    DEFAULT_MEMORY_BUDGET_CHARS,
    MemoryContextCandidate,
    MemoryContextProvenance,
    MemoryDecisionReason,
    MemoryDecisionStatus,
    MemorySelectionPolicy,
    PreferenceAuthority,
    PromptBudgetError,
    PromptSegment,
    PROMPT_SEPARATOR,
    candidates_from_user_memory,
    estimate_context_tokens,
    plan_prompt,
    render_memory_context,
    render_memory_hit,
    select_memory_context,
)
from src.harness.user_memory import UserMemory

SCOPE = "u1"
CAPTURED = "2026-01-01T00:00:00Z"


def _candidate(
    memory_id: str,
    text: str = "示例记忆",
    *,
    scope: str = SCOPE,
    score: float = 0.9,
    rank: int = 1,
    conflict_key: str | None = None,
    authority: PreferenceAuthority = PreferenceAuthority.USER_DEFAULT,
    captured_at: str = CAPTURED,
) -> MemoryContextCandidate:
    """测试用候选工厂：provenance 给能通过校验的最小合法值。"""

    return MemoryContextCandidate(
        memory_id=memory_id,
        text=text,
        user_scope=scope,
        score=score,
        source_rank=rank,
        provenance=MemoryContextProvenance(
            source_id=f"src-{memory_id}",
            source_type="test",
            title=f"测试 {memory_id}",
            captured_at=captured_at,
        ),
        conflict_key=conflict_key,
        authority=authority,
    )


def _reasons(plan) -> dict[str, MemoryDecisionReason]:
    """memory_id → 原因码，断言决策日志的便捷视图。"""

    return {d.memory_id: d.reason for d in plan.decisions}


# ═══════════════════════════════════════════════════════════════
# 一、契约校验：非法输入在构造期炸掉，不是在决策时静默走样
# ═══════════════════════════════════════════════════════════════

class CandidateContractTests(unittest.TestCase):
    """frozen dataclass 的 __post_init__ 是第一道闸：越界值进不了管线。"""

    def test_empty_fields_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _candidate("  ")                     # 空 id
        with self.assertRaises(ValueError):
            _candidate("m1", text="")            # 空正文
        with self.assertRaises(ValueError):
            _candidate("m1", scope=" ")          # 空作用域
        with self.assertRaises(ValueError):
            _candidate("m1", conflict_key="  ")  # 槽位给了就得非空

    def test_score_and_rank_bounds(self) -> None:
        with self.assertRaises(ValueError):
            _candidate("m1", score=1.5)          # 分数超上界
        with self.assertRaises(ValueError):
            _candidate("m1", score=float("nan"))  # NaN 不是分数
        with self.assertRaises(ValueError):
            _candidate("m1", rank=0)             # 检索排名从 1 开始

    def test_invented_authority_fail_closed(self) -> None:
        # 模型发明 "system_override" 必须被拒——错误信息列出合法层级。
        with self.assertRaises(ValueError) as ctx:
            _candidate("m1", authority="system_override")  # type: ignore[arg-type]
        self.assertIn("current_turn", str(ctx.exception))
        self.assertIn("user_default", str(ctx.exception))

    def test_provenance_validated(self) -> None:
        with self.assertRaises(ValueError):
            MemoryContextProvenance(
                source_id="", source_type="t", title="x",
                captured_at=CAPTURED)
        with self.assertRaises(ValueError):
            MemoryContextProvenance(
                source_id="s", source_type="t", title="x",
                captured_at="not-a-timestamp")
        # 合法时间戳含时区归一：naive 当 UTC，Z 后缀等价 +00:00。
        MemoryContextProvenance(
            source_id="s", source_type="t", title="x",
            captured_at="2026-01-01T00:00:00")

    def test_policy_bounds(self) -> None:
        with self.assertRaises(ValueError):
            MemorySelectionPolicy(min_score=1.2)
        with self.assertRaises(ValueError):
            MemorySelectionPolicy(top_k=-1)
        with self.assertRaises(ValueError):
            MemorySelectionPolicy(max_chars=-1)


class TokenEstimatorTests(unittest.TestCase):
    """教学估算的三条计数规则：拉丁词组 / CJK 单字 / 标点。"""

    def test_counting_rules(self) -> None:
        self.assertEqual(estimate_context_tokens("hello world"), 2)
        self.assertEqual(estimate_context_tokens("你好世界"), 4)
        self.assertEqual(estimate_context_tokens("a, b"), 3)   # a + , + b
        self.assertEqual(estimate_context_tokens("  \n\t "), 0)

    def test_injected_counter_validated(self) -> None:
        # counter 的返回值参与准入决策：负数 / 非整数在任何路径上
        # （门禁决策或装箱尝试）都会先被验出并炸掉，不产生"用坏数
        # 做的决策"。
        with self.assertRaises(ValueError):
            select_memory_context(
                [_candidate("m1")], user_scope=SCOPE,
                token_counter=lambda text: -1)
        with self.assertRaises(ValueError):
            select_memory_context(
                [_candidate("m1")], user_scope=SCOPE,
                token_counter=lambda text: "many")  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════════
# 二、两道门：scope 与置信度先于一切（权限也不能绕过）
# ═══════════════════════════════════════════════════════════════

class SelectionGateTests(unittest.TestCase):

    def test_scope_mismatch_rejected_even_for_top_authority(self) -> None:
        # 跨 scope 候选先拒：排序不能覆盖安全边界——current_turn + 满分
        # 也不行（顺序即语义的第 1 条）。
        plan = select_memory_context(
            [_candidate("m1", scope="someone-else", score=1.0,
                        authority=PreferenceAuthority.CURRENT_TURN)],
            user_scope=SCOPE)
        self.assertEqual(plan.context, "")
        self.assertEqual(
            _reasons(plan)["m1"], MemoryDecisionReason.SCOPE_MISMATCH)

    def test_low_confidence_rejected_even_for_top_authority(self) -> None:
        # 低于 min_score = 默认弃权，不是"排最后也许能用"。
        plan = select_memory_context(
            [_candidate("m1", score=0.2,
                        authority=PreferenceAuthority.CURRENT_TURN)],
            user_scope=SCOPE)
        self.assertEqual(
            _reasons(plan)["m1"], MemoryDecisionReason.LOW_CONFIDENCE)

    def test_min_score_boundary_is_inclusive(self) -> None:
        # 恰好等于 min_score 的候选过门（"<" 才拒）。
        plan = select_memory_context(
            [_candidate("m1", score=MemorySelectionPolicy().min_score)],
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("m1",))


# ═══════════════════════════════════════════════════════════════
# 三、权限 / 去重 / 冲突：裁决发生在预算花出去之前
# ═══════════════════════════════════════════════════════════════

class SelectionOrderTests(unittest.TestCase):

    def test_authority_beats_score_in_conflict(self) -> None:
        # 低层高分不能覆盖高层低分：workspace_override 0.4 赢，
        # user_default 0.99 落 authority_loser，related 指向 winner。
        plan = select_memory_context(
            [
                _candidate("low-but-boss", "用 TypeScript",
                           score=0.4, rank=9,
                           conflict_key="preference:automation-language",
                           authority=PreferenceAuthority.WORKSPACE_OVERRIDE),
                _candidate("high-but-default", "用 Bash",
                           score=0.99, rank=1,
                           conflict_key="preference:automation-language"),
            ],
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("low-but-boss",))
        reasons = _reasons(plan)
        self.assertEqual(reasons["high-but-default"],
                         MemoryDecisionReason.AUTHORITY_LOSER)
        decision = next(d for d in plan.decisions
                        if d.memory_id == "high-but-default")
        self.assertEqual(decision.related_memory_id, "low-but-boss")

    def test_same_authority_conflict_resolved_by_score(self) -> None:
        plan = select_memory_context(
            [
                _candidate("weak", "用 Bash", score=0.5,
                           conflict_key="preference:lang"),
                _candidate("strong", "用 Python", score=0.9,
                           conflict_key="preference:lang"),
            ],
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("strong",))
        self.assertEqual(_reasons(plan)["weak"],
                         MemoryDecisionReason.CONFLICT_LOSER)

    def test_textbook_abc_example(self) -> None:
        """教材的核心反例：同文不同槽都必须活过去重。

        A（本轮指令）与 B（工作区覆盖）正文相同但槽位不同——按正文删 B
        会让槽位 "preference:automation" 的裁决缺席、C 错误入选。正确
        结局：A、B 都过去重；B 在冲突阶段按权限击败 C。
        """
        plan = select_memory_context(
            [
                _candidate("C", "Use Bash", score=0.9,
                           conflict_key="preference:automation"),
                _candidate("B", "Use Python", score=0.6,
                           conflict_key="preference:automation",
                           authority=PreferenceAuthority.WORKSPACE_OVERRIDE),
                _candidate("A", "Use Python", score=0.5,
                           conflict_key="lang-analysis",
                           authority=PreferenceAuthority.CURRENT_TURN),
            ],
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("A", "B"))
        reasons = _reasons(plan)
        self.assertEqual(reasons["C"], MemoryDecisionReason.AUTHORITY_LOSER)
        self.assertNotIn(MemoryDecisionReason.DUPLICATE_CONTENT,
                         reasons.values())

    def test_exact_dedupe_normalized(self) -> None:
        # NFKC + 大小写折叠 + 空白归一后同槽同文 = 精确重复；
        # 排序更强的（score 高）当 winner。
        plan = select_memory_context(
            [
                _candidate("dup", "Use   PYTHON", score=0.5,
                           conflict_key="preference:lang"),
                _candidate("origin", "use python", score=0.9,
                           conflict_key="preference:lang"),
            ],
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("origin",))
        decision = next(d for d in plan.decisions if d.memory_id == "dup")
        self.assertEqual(decision.reason,
                         MemoryDecisionReason.DUPLICATE_CONTENT)
        self.assertEqual(decision.related_memory_id, "origin")

    def test_unkeyed_never_dedupes_against_keyed(self) -> None:
        # None 与任何显式槽位都不同：同文的一条无槽、一条有槽，都活着。
        plan = select_memory_context(
            [
                _candidate("keyed", "Use Python",
                           conflict_key="preference:lang"),
                _candidate("free", "Use Python"),
            ],
            user_scope=SCOPE)
        self.assertEqual(set(plan.selected_memory_ids), {"keyed", "free"})

    def test_conflict_loser_never_backfills(self) -> None:
        # 冲突败者是"被裁决掉的事实"，不是"昂贵的上下文"——预算再宽
        # 也不回填（与超预算长记录的小记录回填形成对照）。
        plan = select_memory_context(
            [
                _candidate("winner", "用 Python", score=0.9,
                           conflict_key="preference:lang"),
                _candidate("loser", "用 Bash", score=0.8,
                           conflict_key="preference:lang"),
            ],
            user_scope=SCOPE,
            policy=MemorySelectionPolicy(max_chars=None, max_tokens=None))
        self.assertEqual(plan.selected_memory_ids, ("winner",))
        self.assertEqual(_reasons(plan)["loser"],
                         MemoryDecisionReason.CONFLICT_LOSER)


# ═══════════════════════════════════════════════════════════════
# 四、top-k 与双预算：原子装箱、超标不停止、小记录可回填
# ═══════════════════════════════════════════════════════════════

class SelectionBudgetTests(unittest.TestCase):

    def test_top_k_reached(self) -> None:
        plan = select_memory_context(
            [_candidate("m1", "甲"), _candidate("m2", "乙")],
            user_scope=SCOPE,
            policy=MemorySelectionPolicy(top_k=1))
        self.assertEqual(plan.selected_memory_ids, ("m1",))
        self.assertEqual(_reasons(plan)["m2"],
                         MemoryDecisionReason.TOP_K_REACHED)

    def test_oversized_rejected_but_smaller_backfills(self) -> None:
        # 长记录超字符预算被拒，装箱不停止——后面的小记录仍能进来。
        big = "长" * 500
        small = "短"
        plan = select_memory_context(
            [_candidate("big", big), _candidate("small", small)],
            user_scope=SCOPE,
            policy=MemorySelectionPolicy(max_chars=400, max_tokens=None))
        self.assertEqual(plan.selected_memory_ids, ("small",))
        reasons = _reasons(plan)
        self.assertEqual(reasons["big"],
                         MemoryDecisionReason.CHAR_BUDGET_EXCEEDED)

    def test_token_budget_with_injected_counter(self) -> None:
        # 每个 hit 记 600 教学token、预算 650：第一条进，第二条超。
        # （数 "memory_hit " 开标签：闭标签 </memory_hit> 里没有空格。）
        plan = select_memory_context(
            [_candidate("m1", "甲"), _candidate("m2", "乙")],
            user_scope=SCOPE,
            policy=MemorySelectionPolicy(max_chars=None, max_tokens=650),
            token_counter=lambda text: text.count("memory_hit ") * 600)
        self.assertEqual(plan.selected_memory_ids, ("m1",))
        self.assertEqual(_reasons(plan)["m2"],
                         MemoryDecisionReason.TOKEN_BUDGET_EXCEEDED)


# ═══════════════════════════════════════════════════════════════
# 五、计划与渲染：空结果是空串、证据属性齐全、日志确定
# ═══════════════════════════════════════════════════════════════

class SelectionPlanTests(unittest.TestCase):

    def test_empty_selection_renders_empty_string(self) -> None:
        # 空标签比没有更糟：浪费 token 且谎称存在记忆证据。
        plan = select_memory_context([], user_scope=SCOPE)
        self.assertEqual(plan.context, "")
        self.assertEqual(plan.used_chars, 0)
        self.assertEqual(plan.selected_memory_ids, ())

    def test_render_carries_provenance_and_escapes(self) -> None:
        candidate = _candidate(
            'weird"id', text="正文 <script> 与 \"引号\"")
        hit = render_memory_hit(candidate)
        self.assertIn('memory_id="weird&quot;id"', hit)
        self.assertIn("&lt;script&gt;", hit)
        self.assertNotIn("<script>", hit)
        self.assertIn('source_id="src-weird&quot;id"', hit)
        self.assertIn(f'score="{candidate.score:.6f}"', hit)

    def test_render_rejects_scope_mixture(self) -> None:
        # 渲染层再拦一次跨 scope：即便有人绕过选择器直接渲染。
        with self.assertRaises(ValueError):
            render_memory_context(
                SCOPE, [_candidate("m1", scope="other")])

    def test_wrapper_cost_is_visible(self) -> None:
        # used_chars 含 <recalled_memory> 包装——装箱成本不隐藏。
        plan = select_memory_context([_candidate("m1")], user_scope=SCOPE)
        self.assertEqual(plan.used_chars, len(plan.context))
        self.assertGreater(plan.used_chars,
                           len(render_memory_hit(_candidate("m1"))))

    def test_decision_log_complete_and_input_order_free(self) -> None:
        # 每条候选恰好一个决策；日志顺序按权威排序，与输入顺序无关。
        forward = [_candidate("m1", "甲", score=0.5),
                   _candidate("m2", "乙", score=0.9)]
        backward = list(reversed(forward))
        plan_a = select_memory_context(forward, user_scope=SCOPE)
        plan_b = select_memory_context(backward, user_scope=SCOPE)
        self.assertEqual([d.memory_id for d in plan_a.decisions],
                         ["m2", "m1"])
        self.assertEqual(plan_a, plan_b)  # frozen + tuple：计划可整体比较
        # 决策里的成本是渲染后的真实尺寸。
        for decision in plan_a.decisions:
            source = next(c for c in forward
                          if c.memory_id == decision.memory_id)
            self.assertEqual(decision.candidate_chars,
                             len(render_memory_hit(source)))

    def test_duplicate_candidate_ids_rejected(self) -> None:
        with self.assertRaises(ValueError):
            select_memory_context(
                [_candidate("m1"), _candidate("m1")], user_scope=SCOPE)

    def test_default_budget_constant_wired(self) -> None:
        # 教材同款默认：3000 字符 / 800 token。改这个数是有意决策，
        # 测试钉住它——悄悄漂移等于悄悄改行为。
        policy = MemorySelectionPolicy()
        self.assertEqual(policy.max_chars, DEFAULT_MEMORY_BUDGET_CHARS)
        self.assertEqual(policy.max_tokens, 800)


# ═══════════════════════════════════════════════════════════════
# 六、候选适配器：UserMemory → 候选（结构适配，不重算）
# ═══════════════════════════════════════════════════════════════

class UserMemoryAdapterTests(unittest.TestCase):
    """真实 UserMemory 落在临时目录——不碰 ~/.workbuddy。"""

    def _memory(self, root: Path) -> UserMemory:
        return UserMemory(root, "u1")

    def test_preferences_become_keyed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory(Path(tmp))
            memory.set_preference("response.language", "Chinese",
                                  updated_at="2026-09-20T08:00:00Z")
            candidates = candidates_from_user_memory(memory)

        self.assertEqual(len(candidates), 1)
        only = candidates[0]
        self.assertEqual(only.memory_id, "pref:response.language")
        self.assertEqual(only.conflict_key, "preference:response.language")
        self.assertIn("response.language", only.text)
        self.assertIn("Chinese", only.text)
        self.assertEqual(only.authority, PreferenceAuthority.USER_DEFAULT)
        self.assertEqual(only.score, 1.0)   # 蒸馏记忆非检索命中
        self.assertEqual(only.user_scope, "u1")
        self.assertEqual(only.provenance.captured_at,
                         "2026-09-20T08:00:00Z")

    def test_profile_becomes_unkeyed_candidates(self) -> None:
        # 资料是恒真事实：没有槽位语义（不带 conflict_key）。
        # 空白值在 UserMemory 写入层就进不了库，适配器无需处理。
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory(Path(tmp))
            memory.update_profile({"timezone": "UTC+8"})
            candidates = candidates_from_user_memory(memory)

        ids = {c.memory_id for c in candidates}
        self.assertIn("profile:timezone", ids)
        profile = next(c for c in candidates
                       if c.memory_id == "profile:timezone")
        self.assertIsNone(profile.conflict_key)
        self.assertIn("UTC+8", profile.text)

    def test_inactive_preference_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory(Path(tmp))
            memory.set_preference(
                "tmp.only", "过期的约定",
                updated_at="2026-01-01T00:00:00Z",
                expires_at="2026-02-01T00:00:00Z")
            candidates = candidates_from_user_memory(
                memory, as_of="2026-03-01T00:00:00Z")

        self.assertEqual(candidates, ())

    def test_trusted_authority_override(self) -> None:
        # 可信调用方（Harness 边界）可显式标注层级；默认仍是 user_default。
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory(Path(tmp))
            memory.set_preference("editor.mode", "vim")
            candidates = candidates_from_user_memory(
                memory,
                authority_by_memory_id={"pref:editor.mode":
                                        "workspace_override"})

        self.assertEqual(candidates[0].authority,
                         PreferenceAuthority.WORKSPACE_OVERRIDE)

    def test_adapter_feeds_pipeline_end_to_end(self) -> None:
        # 同 key 的新旧两份记忆（一个 user_default、一个被可信方标成
        # workspace_override）进管线：高层赢，败者 authority_loser。
        with tempfile.TemporaryDirectory() as tmp:
            memory = self._memory(Path(tmp))
            memory.set_preference("build.tool", "make")
            candidates = candidates_from_user_memory(
                memory,
                authority_by_memory_id={
                    "pref:build.tool": "workspace_override"})
            # 手工再补一条同槽低层候选（模拟 s12 到来前的多源召回）。
            challenger = _candidate("challenger", "用 cmake",
                                    score=1.0, rank=99,
                                    conflict_key="preference:build.tool")
            plan = select_memory_context(
                [*candidates, challenger], user_scope="u1")

        self.assertIn("pref:build.tool", plan.selected_memory_ids)
        self.assertEqual(_reasons(plan)["challenger"],
                         MemoryDecisionReason.AUTHORITY_LOSER)


# ═══════════════════════════════════════════════════════════════
# 七、第二层：片段规划器
# ═══════════════════════════════════════════════════════════════

def _segment(name: str, content: str, *, priority: int = 50,
             budget_priority: int = 50, required: bool = False,
             condition: bool = True) -> PromptSegment:
    return PromptSegment(
        name=name,
        builder=lambda: content if content else None,
        condition=lambda: condition,
        priority=priority,
        budget_priority=budget_priority,
        required=required,
        provenance="test",
    )


class SegmentPlannerTests(unittest.TestCase):

    def test_required_fail_closed(self) -> None:
        # required 装不下 = PromptBudgetError，绝不静默删安全规则。
        with self.assertRaises(PromptBudgetError) as ctx:
            plan_prompt(
                [_segment("base", "A" * 100, required=True)],
                budget_chars=50)
        self.assertIn("base", str(ctx.exception))

    def test_required_reserved_before_optional(self) -> None:
        # required 先拿空间；剩余预算给高价值的 optional。
        plan = plan_prompt(
            [
                _segment("mode", "M" * 60, required=True),
                _segment("memory", "X" * 30, budget_priority=90),
                _segment("conventions", "Y" * 30, budget_priority=10),
            ],
            budget_chars=100)
        self.assertIn("mode", plan.included_names)
        self.assertIn("memory", plan.included_names)
        self.assertEqual(plan.dropped_names, ("conventions",))
        dropped = next(d for d in plan.decisions
                       if d.name == "conventions")
        self.assertIn("budget", dropped.reason)

    def test_display_order_independent_of_budget_order(self) -> None:
        # 展示序（priority）与预算序（budget_priority）是两件事：
        # 高价值的 identity 排最前，required 的 mode 排最后。
        plan = plan_prompt(
            [
                _segment("mode", "MODE", priority=90, required=True),
                _segment("identity", "ID", priority=5, budget_priority=99),
            ],
            budget_chars=1000)
        self.assertTrue(plan.prompt.startswith("ID"))
        self.assertTrue(plan.prompt.endswith("MODE"))
        self.assertEqual(plan.included_names, ("identity", "mode"))

    def test_inactive_segments_logged(self) -> None:
        # condition False 或 builder 空内容 = inactive，不进竞争也不算舍弃。
        plan = plan_prompt(
            [
                _segment("never", "N", condition=False),
                _segment("empty", ""),
                _segment("real", "R"),
            ],
            budget_chars=1000)
        self.assertEqual(plan.included_names, ("real",))
        statuses = {d.name: d.status for d in plan.decisions}
        self.assertEqual(statuses["never"], "inactive")
        self.assertEqual(statuses["empty"], "inactive")

    def test_none_budget_admits_all(self) -> None:
        plan = plan_prompt(
            [_segment("a", "A" * 10_000), _segment("b", "B" * 10_000)],
            budget_chars=None)
        self.assertEqual(plan.included_names, ("a", "b"))
        self.assertEqual(plan.used_chars, len(plan.prompt))

    def test_duplicate_names_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_prompt([_segment("dup", "A"), _segment("dup", "B")])

    def test_separator_costs_counted(self) -> None:
        plan = plan_prompt(
            [_segment("a", "AAAA"), _segment("b", "BBBB")],
            budget_chars=1000)
        self.assertEqual(plan.prompt, f"AAAA{PROMPT_SEPARATOR}BBBB")
        # used_chars 含分隔符——装箱成本不隐藏。
        self.assertEqual(
            plan.used_chars,
            4 + len(PROMPT_SEPARATOR) + 4)


# ═══════════════════════════════════════════════════════════════
# 八、装配接线：起步历史由规划器决定顺序与预算
# ═══════════════════════════════════════════════════════════════

class SeedPlanTests(unittest.TestCase):
    """plan_history_seed / build_history_seed（s15 接线）。

    root 一律指临时目录：真实工作区记忆随时可能被一次 memory_write
    变成非空，测试不依赖会变的外部状态（2026-09-19 踩过的坑）。
    """

    def _rich_root(self, root: Path) -> None:
        from src.harness.workspace_memory import WorkspaceMemory

        memory = WorkspaceMemory(root)
        memory.append_daily_log("起步就有的记忆", kind="decision",
                                importance=5)

    def _skill_root(self, root: Path) -> None:
        skill_dir = root / ".workbuddy" / "skills" / "demo-seed-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "title: demo-seed-skill\n"
            "summary: 演示\n"
            "read_when:\n  - 演示\n"
            "---\n\n正文。\n",
            encoding="utf-8",
        )

    def test_default_budget_keeps_legacy_shape(self) -> None:
        # 默认预算宽裕：三块全进、顺序不变——s15 之前的行为零变化。
        from scripts.toolbox import build_history_seed, build_skill_index

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._rich_root(root)
            self._skill_root(root)
            user_memory = UserMemory(root, "u1")
            user_memory.set_preference("response.language", "Chinese")
            seed = build_history_seed(
                root=root, user_memory=user_memory,
                skill_index=build_skill_index(root))

        self.assertEqual(seed[0]["role"], "system")
        self.assertIn("工具", seed[0]["content"])      # 工具目录永远第一条
        self.assertIn("起步就有的记忆", seed[1]["content"])  # 工作区第二
        self.assertIn("<recalled_memory ", seed[2]["content"])
        self.assertIn("response.language", seed[2]["content"])  # 用户第三
        self.assertIn("demo-seed-skill", seed[3]["content"])   # 技能第四
        self.assertEqual(len(seed), 4)

    def test_empty_memory_adds_no_segment(self) -> None:
        from scripts.toolbox import build_history_seed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = build_history_seed(root=root,
                                      user_memory=UserMemory(root, "u1"))

        self.assertEqual(len(seed), 1)   # 空记忆 = 只有工具目录 system

    def test_tight_budget_drops_lowest_value_segment(self) -> None:
        # 预算收紧到"恰好装下工作区"：技能目录（budget_priority 最低）
        # 被舍弃，工作区（最高）保留——决策日志回答"为什么没放这个片段"。
        # 预算值从第一遍规划的结果里算（确定性），不写死魔法数。
        from scripts.toolbox import build_skill_index, plan_history_seed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._rich_root(root)
            self._skill_root(root)
            _, contents = plan_history_seed(
                root=root, skill_index=build_skill_index(root))
            tight = len(contents["workspace_memory"])
            plan, _ = plan_history_seed(
                root=root, skill_index=build_skill_index(root),
                budget_chars=tight)

        self.assertIn("workspace_memory", plan.included_names)
        self.assertEqual(plan.dropped_names, ("skills_directory",))

    def test_user_memory_passes_through_selection_pipeline(self) -> None:
        # 用户记忆以 <recalled_memory> 的形态进 seed——选择、装箱、
        # 决策日志都发生在片段规划之前（两层预算的两层决策）。
        from scripts.toolbox import plan_history_seed

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_memory = UserMemory(root, "u1")
            user_memory.set_preference("response.language", "Chinese")
            plan, contents = plan_history_seed(
                root=root, user_memory=user_memory)

        self.assertIn("user_memory", plan.included_names)
        self.assertIn("preference:response.language",
                      contents["user_memory"])
        self.assertIn('<recalled_memory user_scope="u1"',
                      contents["user_memory"])


if __name__ == "__main__":
    unittest.main()
