"""s12 召回链的离线单元测试：五段管线 + s15 桥接 + 装配接线。

全部离线：手造语料 + 临时目录，不碰网络、不碰真实记忆目录。
核心断言群：
  - 阶段可分（normalize / candidate / score / rank 各自可直调）；
  - 分数可解释（coverage / recency / 两项贡献逐项对得上）；
  - 排名确定（并列靠显式键破，不靠语料顺序）；
  - s15 的门从此吃真分数（弱命中被 min_score 拒，不再是恒 1.0）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.harness.prompt_assembly import (
    MemoryDecisionReason,
    PreferenceAuthority,
    memory_candidates_from_recall,
    select_memory_context,
)
from src.harness.recall import (
    LEXICAL_WEIGHT,
    RECENCY_WEIGHT,
    MemoryKind,
    MemorySource,
    RecallEngine,
    StoredMemory,
    build_recall_candidates,
    normalize_recall_query,
    score_recall_candidate,
    stable_rank_recall_candidates,
    tokenize,
)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
SCOPE = "u1"


def _record(memory_id: str, content: str, *, summary: str = "",
            kind: MemoryKind = MemoryKind.FACT,
            scope: str = SCOPE,
            captured_at: str = "2026-10-20T00:00:00Z") -> StoredMemory:
    return StoredMemory(
        memory_id=memory_id,
        user_scope=scope,
        kind=kind,
        content=content,
        summary=summary or content[:40],
        source=MemorySource(
            source_id=f"log-{memory_id}",
            source_type="workspace_daily_log",
            title=f"记录 {memory_id}",
            captured_at=captured_at,
        ),
        stored_at=captured_at,
    )


# ═══════════════════════════════════════════════════════════════
# 一、tokenize 与 normalize：唯一的规范化入口
# ═══════════════════════════════════════════════════════════════

class TokenizeTests(unittest.TestCase):

    def test_latin_words_and_cjk_bigrams(self) -> None:
        # 拉丁词组整词一记；连续中文按二元切（单字歧义太大）。
        self.assertEqual(tokenize("layered memory"), ("layered", "memory"))
        self.assertEqual(tokenize("分层记忆"), ("分层", "层记", "记忆"))
        self.assertEqual(tokenize("记"), ("记",))          # 单字 run 记本身
        self.assertEqual(tokenize("sqlite3 wal"), ("sqlite3", "wal"))

    def test_punctuation_is_not_a_term(self) -> None:
        self.assertEqual(tokenize("a, b。"), ("a", "b"))


class NormalizeQueryTests(unittest.TestCase):

    def test_folding_and_stable_terms(self) -> None:
        query = normalize_recall_query(
            "  Layered   MEMORY  ", user_scope=SCOPE, as_of=NOW)
        self.assertEqual(query.text, "Layered MEMORY")     # 可读原文保留
        self.assertEqual(query.normalized_text, "layered memory")
        self.assertEqual(query.terms, ("layered", "memory"))  # 排序去重
        self.assertEqual(query.issued_at, "2026-10-25T12:00:00Z")

    def test_unsearchable_queries_fail_early(self) -> None:
        # 空白 / 纯标点明确失败：空 terms 进管线是"对空集合求覆盖率"。
        with self.assertRaises(ValueError):
            normalize_recall_query("   ", user_scope=SCOPE)
        with self.assertRaises(ValueError):
            normalize_recall_query("？？？!", user_scope=SCOPE)

    def test_limit_bounds(self) -> None:
        with self.assertRaises(ValueError):
            normalize_recall_query("sqlite", user_scope=SCOPE, limit=0)
        with self.assertRaises(ValueError):
            normalize_recall_query("sqlite", user_scope=SCOPE, limit=11)

    def test_query_ids_are_unique(self) -> None:
        a = normalize_recall_query("sqlite", user_scope=SCOPE)
        b = normalize_recall_query("sqlite", user_scope=SCOPE)
        self.assertNotEqual(a.query_id, b.query_id)


# ═══════════════════════════════════════════════════════════════
# 二、契约校验：非法记录在构造期炸掉
# ═══════════════════════════════════════════════════════════════

class RecordContractTests(unittest.TestCase):

    def test_stored_memory_validation(self) -> None:
        with self.assertRaises(ValueError):
            _record("m1", "   ")                    # 空正文
        with self.assertRaises(ValueError):
            MemorySource(source_id="", source_type="t", title="x",
                         captured_at="2026-01-01T00:00:00Z")
        with self.assertRaises(ValueError):
            _record("m1", "内容", captured_at="not-a-time")

    def test_kind_fail_closed(self) -> None:
        # 字符串 kind 合法则归一，拼错列出合法值——静默拼错不存在。
        record = _record("m1", "内容", kind="fact")  # type: ignore[arg-type]
        self.assertIs(record.kind, MemoryKind.FACT)
        with self.assertRaises(ValueError) as ctx:
            _record("m1", "内容", kind="bogus")      # type: ignore[arg-type]
        self.assertIn("fact", str(ctx.exception))


# ═══════════════════════════════════════════════════════════════
# 三、candidate / score / stable rank：三个纯函数
# ═══════════════════════════════════════════════════════════════

class CandidateTests(unittest.TestCase):

    def test_only_lexical_overlap_becomes_candidate(self) -> None:
        query = normalize_recall_query("sqlite wal", user_scope=SCOPE,
                                       as_of=NOW)
        hit = _record("hit", "选了 SQLite WAL 做持久化")
        miss = _record("miss", "今天天气不错")
        candidates = build_recall_candidates(query, [hit, miss])
        self.assertEqual([c.record.memory_id for c in candidates], ["hit"])
        self.assertEqual(candidates[0].matched_terms, ("sqlite", "wal"))

    def test_summary_is_searchable(self) -> None:
        # 摘要也是检索面：正文没有、摘要里有的词项照样命中。
        query = normalize_recall_query("决策", user_scope=SCOPE, as_of=NOW)
        record = _record("m1", "选了 SQLite WAL", summary="决策：持久化方案")
        candidates = build_recall_candidates(query, [record])
        self.assertEqual(candidates[0].matched_terms, ("决策",))


class ScoringTests(unittest.TestCase):

    def _scored(self, query, record, as_of=NOW):
        candidate = build_recall_candidates(query, [record])[0]
        return candidate, score_recall_candidate(
            query, candidate, as_of=as_of)

    def test_breakdown_math(self) -> None:
        # 2 个词项中 1 个 → coverage 0.5；5 天前 → recency 1-5/30。
        query = normalize_recall_query("sqlite wal", user_scope=SCOPE,
                                       as_of=NOW)
        record = _record("m1", "只有 sqlite 提到",
                         captured_at="2026-10-20T12:00:00Z")  # 距 NOW 5 天
        _, breakdown = self._scored(query, record)
        self.assertEqual(breakdown.lexical_coverage, 0.5)
        self.assertAlmostEqual(breakdown.recency, 1 - 5 / 30)
        self.assertAlmostEqual(breakdown.lexical_contribution,
                               LEXICAL_WEIGHT * 0.5)
        self.assertAlmostEqual(breakdown.recency_contribution,
                               RECENCY_WEIGHT * (1 - 5 / 30))
        self.assertAlmostEqual(
            breakdown.total,
            LEXICAL_WEIGHT * 0.5 + RECENCY_WEIGHT * (1 - 5 / 30))

    def test_recency_floor_and_ceiling(self) -> None:
        query = normalize_recall_query("sqlite", user_scope=SCOPE, as_of=NOW)
        # 31 天前：新近度衰减到 0（地板）。
        old = _record("old", "sqlite 决策",
                      captured_at="2026-09-24T00:00:00Z")
        _, old_breakdown = self._scored(query, old)
        self.assertEqual(old_breakdown.recency, 0.0)
        # 采集时间在未来：夹取到 1（天花板），total 不许超界。
        future = _record("future", "sqlite 决策",
                         captured_at="2026-11-01T00:00:00Z")
        _, future_breakdown = self._scored(query, future)
        self.assertEqual(future_breakdown.recency, 1.0)
        self.assertLessEqual(future_breakdown.total, 1.0)


class StableRankTests(unittest.TestCase):

    def test_tie_breaks_are_explicit(self) -> None:
        # total 并列 → coverage；再并列 → 采集时间降序；再并列 → id 升序。
        query = normalize_recall_query("sqlite", user_scope=SCOPE, as_of=NOW)
        records = [
            _record("b", "sqlite 一", captured_at="2026-10-01T00:00:00Z"),
            _record("a", "sqlite 二", captured_at="2026-10-01T00:00:00Z"),
            _record("c", "sqlite 三", captured_at="2026-10-10T00:00:00Z"),
        ]
        candidates = build_recall_candidates(query, records)
        scored = [(c, score_recall_candidate(query, c, as_of=NOW))
                  for c in candidates]
        ranked = stable_rank_recall_candidates(query, scored)
        # c 新（recency 高）第一；a/b 同分同时间 → id 升序 a、b。
        self.assertEqual(
            [item[0].record.memory_id for item in ranked], ["c", "a", "b"])

    def test_corpus_order_is_irrelevant(self) -> None:
        query = normalize_recall_query("sqlite wal 持久化", user_scope=SCOPE,
                                       as_of=NOW)
        records = [
            _record("full", "sqlite wal 持久化"),
            _record("half", "sqlite 持久化"),
            _record("quarter", "wal"),
        ]
        def _rank(order):
            candidates = build_recall_candidates(query, order)
            scored = [(c, score_recall_candidate(query, c, as_of=NOW))
                      for c in candidates]
            return [i[0].record.memory_id
                    for i in stable_rank_recall_candidates(query, scored)]

        self.assertEqual(_rank(records), _rank(list(reversed(records))))


# ═══════════════════════════════════════════════════════════════
# 四、引擎：五个阶段串起来
# ═══════════════════════════════════════════════════════════════

class EngineTests(unittest.TestCase):

    def test_recall_ranks_and_truncates(self) -> None:
        engine = RecallEngine(lambda: [
            _record("weak", "wal 提过一次", captured_at="2026-09-01T00:00:00Z"),
            _record("strong", "sqlite wal 持久化决策"),
        ])
        result = engine.recall("sqlite wal 持久化", user_scope=SCOPE,
                               limit=1, as_of=NOW)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].memory_id, "strong")
        self.assertEqual(result.hits[0].rank, 1)
        self.assertIsNone(result.empty_reason)

    def test_scope_filter_hides_other_users(self) -> None:
        # 跨用户语料对本次查询不可见——scope 防线在引擎里，不在投影里。
        engine = RecallEngine(lambda: [
            _record("mine", "sqlite 决策"),
            _record("theirs", "sqlite 决策", scope="someone-else"),
        ])
        result = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        self.assertEqual([h.memory_id for h in result.hits], ["mine"])
        self.assertEqual(result.searched_records, 1)   # 只数检查过的

    def test_kind_filter(self) -> None:
        engine = RecallEngine(lambda: [
            _record("fact-1", "sqlite 决策"),
            _record("pref-1", "sqlite 偏好", kind=MemoryKind.PREFERENCE),
        ])
        result = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW,
                               kinds={MemoryKind.FACT})
        self.assertEqual([h.memory_id for h in result.hits], ["fact-1"])
        self.assertEqual(result.hits[0].scope.memory_kind, MemoryKind.FACT)

    def test_empty_reasons_distinguish_failure_points(self) -> None:
        empty = RecallEngine(lambda: [])
        result = empty.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        self.assertEqual(result.empty_reason, "no_records")

        engine = RecallEngine(lambda: [_record("m1", "今天天气不错")])
        result = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        self.assertEqual(result.empty_reason, "no_overlap")
        # searched/candidate 分开：知道检查了几条、几条进了评分。
        self.assertEqual((result.searched_records, result.candidate_records),
                         (1, 0))

    def test_provider_is_read_fresh_each_recall(self) -> None:
        # 语料是回调不是快照：daily log 增长在下一次召回立即可见。
        records: list[StoredMemory] = [_record("old", "sqlite 决策")]
        engine = RecallEngine(lambda: records)
        first = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        records.append(_record("new", "sqlite 新决策",
                               captured_at="2026-10-24T00:00:00Z"))
        second = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        self.assertEqual(len(first.hits), 1)
        self.assertEqual([h.memory_id for h in second.hits],
                         ["new", "old"])

    def test_result_is_json_serializable(self) -> None:
        engine = RecallEngine(lambda: [_record("m1", "sqlite 决策")])
        result = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertIn("score_breakdown", payload)
        self.assertIn("contributions", payload)
        self.assertIn("workspace_daily_log", payload)


# ═══════════════════════════════════════════════════════════════
# 五、桥接：RecallResult → s15 候选（复制证据，不重算分数）
# ═══════════════════════════════════════════════════════════════

class BridgeTests(unittest.TestCase):

    def test_fields_copied_not_recomputed(self) -> None:
        engine = RecallEngine(lambda: [_record("m1", "sqlite 决策")])
        result = engine.recall("sqlite", user_scope=SCOPE, as_of=NOW)
        candidates = memory_candidates_from_recall(result)
        self.assertEqual(len(candidates), 1)
        only = candidates[0]
        hit = result.hits[0]
        self.assertEqual(only.memory_id, hit.memory_id)
        self.assertEqual(only.score, hit.score_breakdown.total)  # 照抄
        self.assertEqual(only.source_rank, hit.rank)
        self.assertEqual(only.user_scope, hit.scope.user_scope)
        self.assertEqual(only.provenance.source_id, hit.provenance.source_id)
        self.assertIsNone(only.conflict_key)

    def test_slot_and_authority_annotations(self) -> None:
        engine = RecallEngine(lambda: [_record("m1", "自动化语言：Python")])
        result = engine.recall("自动化 语言", user_scope=SCOPE, as_of=NOW)
        candidates = memory_candidates_from_recall(
            result,
            conflict_keys={"m1": "preference:automation-language"},
            authority_by_memory_id={"m1": "workspace_override"})
        self.assertEqual(candidates[0].conflict_key,
                         "preference:automation-language")
        self.assertEqual(candidates[0].authority,
                         PreferenceAuthority.WORKSPACE_OVERRIDE)

    def test_missing_provenance_rejected(self) -> None:
        class BareHit:  # 结构上缺 provenance 的"坏 hit"
            memory_id = "m1"
            rank = 1
            snippet = "x"
            scope = type("S", (), {"user_scope": SCOPE})()
            score_breakdown = type("B", (), {"total": 0.9})()

        class BareResult:
            hits = (BareHit(),)

        with self.assertRaises(ValueError):
            memory_candidates_from_recall(BareResult())


class S12ToS15EndToEndTests(unittest.TestCase):
    """召回 → 桥接 → 选择：s15 的门从此吃真分数。"""

    def test_weak_hit_rejected_by_min_score(self) -> None:
        # 强命中（全覆盖 + 新）过门；弱命中（1/3 覆盖 + 31 天前
        # → total ≈ 0.283）被 min_score=0.35 拒——恒 1.0 的时代结束。
        engine = RecallEngine(lambda: [
            _record("strong", "sqlite wal 持久化 决策",
                    captured_at="2026-10-24T00:00:00Z"),
            _record("weak", "提过一次 sqlite",
                    captured_at="2026-09-20T00:00:00Z"),
        ])
        result = engine.recall("sqlite wal 持久化", user_scope=SCOPE,
                               as_of=NOW)
        plan = select_memory_context(
            memory_candidates_from_recall(result), user_scope=SCOPE)

        reasons = {d.memory_id: d.reason for d in plan.decisions}
        self.assertIn("strong", plan.selected_memory_ids)
        self.assertEqual(reasons["weak"],
                         MemoryDecisionReason.LOW_CONFIDENCE)

    def test_real_score_orders_same_authority_conflict(self) -> None:
        # 同槽冲突不再靠"谁先来"，靠真分数：覆盖高的赢。
        engine = RecallEngine(lambda: [
            _record("rich", "自动化语言用 Python，工具链齐全",
                    captured_at="2026-10-24T00:00:00Z"),
            _record("poor", "自动化语言待定",
                    captured_at="2026-10-24T00:00:00Z"),
        ])
        result = engine.recall("自动化 语言 python", user_scope=SCOPE,
                               as_of=NOW)
        plan = select_memory_context(
            memory_candidates_from_recall(
                result,
                conflict_keys={"rich": "preference:automation",
                               "poor": "preference:automation"}),
            user_scope=SCOPE)
        self.assertEqual(plan.selected_memory_ids, ("rich",))


# ═══════════════════════════════════════════════════════════════
# 六、装配接线：投影语料 + recall_memory 工具
# ═══════════════════════════════════════════════════════════════

class ToolboxRecallTests(unittest.TestCase):
    """临时目录 + 真实 WorkspaceMemory/UserMemory——不碰真实存储。"""

    def test_corpus_projection_carries_provenance(self) -> None:
        from scripts.toolbox import recall_corpus
        from src.harness.user_memory import UserMemory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from src.harness.workspace_memory import WorkspaceMemory
            WorkspaceMemory(root).append_daily_log(
                "选了 SQLite WAL 做持久化", kind="decision", importance=5,
                recorded_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
            records = recall_corpus(root, UserMemory(root, "u1"))

        facts = [r for r in records if r.kind is MemoryKind.FACT]
        self.assertEqual(len(facts), 1)
        only = facts[0]
        self.assertTrue(only.memory_id.startswith("fact:"))
        self.assertEqual(only.user_scope, "u1")
        self.assertEqual(only.source.source_type, "workspace_daily_log")
        self.assertEqual(only.source.captured_at, "2026-10-24T00:00:00Z")

    def test_recall_memory_tool_returns_structured_json(self) -> None:
        from scripts.toolbox import build_deferred_tools
        from src.harness.workspace_memory import WorkspaceMemory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            WorkspaceMemory(root).append_daily_log(
                "选了 SQLite WAL 做本地持久化", kind="decision", importance=5,
                recorded_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
            tools = {t.name: t for t in build_deferred_tools(root)}
            payload = json.loads(tools["recall_memory"].handler(
                query="sqlite wal 持久化为什么"))

        self.assertEqual(payload["empty_reason"], None)
        self.assertEqual(len(payload["hits"]), 1)
        self.assertIn("SQLite WAL", payload["hits"][0]["snippet"])
        self.assertIn("score_breakdown", payload["hits"][0])

    def test_recall_memory_tool_no_match_is_actionable(self) -> None:
        from scripts.toolbox import build_deferred_tools

        with tempfile.TemporaryDirectory() as tmp:
            tools = {t.name: t for t in build_deferred_tools(Path(tmp))}
            result = tools["recall_memory"].handler(query="量子引力")

        self.assertTrue(result.startswith("没召回到"))
        self.assertIn("no_records", result)

    def test_tool_is_safe_read_only(self) -> None:
        # 免审批名单里有它：纯读投影，不写回任何存储。
        self.assertIn("recall_memory", SAFE_TOOLS_VIEW())


def SAFE_TOOLS_VIEW() -> frozenset:
    from scripts.toolbox import SAFE_TOOLS
    return SAFE_TOOLS


if __name__ == "__main__":
    unittest.main()
