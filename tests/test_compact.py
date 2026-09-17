"""上下文压缩（s14）的回归测试：四层各管什么 + 保配对 + 绝不改调用方。

用的消息格式是本项目的方言（OpenAI 风格：assistant 带 tool_calls、
tool 消息带 tool_call_id），不是教材的 Anthropic content blocks——
L2 因此靠"从 tool_calls 反查参数"来认文件，而不是给 block 打标签。
"""

import copy
import unittest

from src.harness.compact import (
    DurableContextState,
    DurableFact,
    PendingItem,
    compact,
    dedup_file_reads,
    estimate_tokens,
    prune_old_messages,
    render_durable_context,
    summarize_history,
    truncate_tool_results,
)

SYSTEM = {"role": "system", "content": "工具目录"}


def call_message(call_id: str, name: str, arguments: dict) -> dict:
    """一条 assistant 的工具调用消息。"""

    return {"role": "assistant", "content": "",
            "tool_calls": [{"call_id": call_id, "name": name,
                            "arguments": arguments}]}


def result_message(call_id: str, content: str) -> dict:
    """一条 tool 结果消息。"""

    return {"role": "tool", "tool_call_id": call_id, "content": content}


class EstimateTokensTests(unittest.TestCase):
    """估算：只要单调、相对准就够了——它决定"要不要压"，不管账。"""

    def test_counts_content_by_chars_per_token(self) -> None:
        self.assertEqual(estimate_tokens([{"role": "user", "content": "x" * 40}]), 10)

    def test_counts_tool_call_arguments_too(self) -> None:
        """参数也是上下文的一部分——模型看得见，账单里有它。"""

        bare = [call_message("c1", "fs_read", {})]
        with_args = [call_message("c1", "fs_read", {"path": "a" * 400})]

        self.assertGreater(estimate_tokens(with_args), estimate_tokens(bare))

    def test_empty_is_zero(self) -> None:
        self.assertEqual(estimate_tokens([]), 0)


class LayerOneTests(unittest.TestCase):
    """L1：截断超大工具结果——最便宜的一层，不丢消息只丢噪声。"""

    def test_truncates_and_says_so(self) -> None:
        """必须**注明**被截断：模型得知道"这不是全部"才会换更精确的命令。"""

        messages = [result_message("c1", "x" * 400)]

        changed, saved = truncate_tool_results(messages, max_tokens=10)

        self.assertEqual(len(changed), 1)              # 消息还在，只缩了内容
        self.assertIn("已截断", changed[0]["content"])
        self.assertIn("原始 400 字符", changed[0]["content"])
        self.assertEqual(len(changed[0]["content"][:40]), 40)   # 保留前 10 token
        self.assertEqual(saved, 100 - 10)

    def test_leaves_small_results_alone(self) -> None:
        messages = [result_message("c1", "短输出")]
        changed, saved = truncate_tool_results(messages, max_tokens=10)
        self.assertEqual(changed[0]["content"], "短输出")
        self.assertEqual(saved, 0)

    def test_ignores_non_tool_messages(self) -> None:
        """只碰 tool 消息——用户说的话、模型的回答都不是可截断的噪声。"""

        messages = [{"role": "user", "content": "x" * 400}]
        changed, saved = truncate_tool_results(messages, max_tokens=10)
        self.assertEqual(changed[0]["content"], "x" * 400)
        self.assertEqual(saved, 0)


class LayerTwoTests(unittest.TestCase):
    """L2：同一文件读多次，只留最新——但**只换正文，不删消息**。"""

    def _two_reads(self) -> list[dict]:
        return [
            SYSTEM,
            {"role": "user", "content": "看看 a.md"},
            call_message("c1", "fs_read", {"path": "a.md"}),
            result_message("c1", "第一次读到的内容"),
            {"role": "user", "content": "再看看"},
            call_message("c2", "fs_read", {"path": "a.md"}),
            result_message("c2", "第二次读到的内容"),
        ]

    def test_keeps_only_the_latest_read(self) -> None:
        messages = self._two_reads()

        changed, saved = dedup_file_reads(messages)

        self.assertIn("第二次读到的内容", changed[6]["content"])
        self.assertNotIn("第一次读到的内容", changed[3]["content"])
        self.assertIn("已省略", changed[3]["content"])
        self.assertGreater(saved, 0)

    def test_message_count_is_unchanged(self) -> None:
        """**绝不删消息**：删掉 tool 消息会让上方那条 tool_calls 变成孤儿调用，
        OpenAI 兼容协议会直接报错。缩的只能是内容。"""

        messages = self._two_reads()

        changed, _ = dedup_file_reads(messages)

        self.assertEqual(len(changed), len(messages))
        self.assertEqual([m["role"] for m in changed], [m["role"] for m in messages])

    def test_different_paths_are_untouched(self) -> None:
        messages = [
            call_message("c1", "fs_read", {"path": "a.md"}),
            result_message("c1", "甲"),
            call_message("c2", "fs_read", {"path": "b.md"}),
            result_message("c2", "乙"),
        ]

        changed, saved = dedup_file_reads(messages)

        self.assertEqual(changed[1]["content"], "甲")
        self.assertEqual(changed[3]["content"], "乙")
        self.assertEqual(saved, 0)

    def test_write_tools_are_never_deduped(self) -> None:
        """写操作不去重：两次写入的**结果可能不同**，那是"我做过什么"的证据。"""

        messages = [
            call_message("c1", "fs_write", {"path": "a.md", "text": "1"}),
            result_message("c1", "已写入"),
            call_message("c2", "fs_write", {"path": "a.md", "text": "2"}),
            result_message("c2", "已写入"),
        ]

        changed, saved = dedup_file_reads(messages)

        self.assertEqual(changed[1]["content"], "已写入")
        self.assertEqual(saved, 0)


class LayerThreeTests(unittest.TestCase):
    """L3：修剪旧消息——保首条 + 最近 N 条，且**不留孤儿 tool 消息**。"""

    def test_keeps_first_and_recent(self) -> None:
        messages = [SYSTEM]
        messages.append({"role": "user", "content": "最初的目标"})
        for index in range(10):
            messages.append({"role": "user", "content": f"第 {index} 句"})

        changed, saved = prune_old_messages(messages, keep=3)

        self.assertEqual(changed[0], SYSTEM)
        self.assertEqual(changed[1]["content"], "最初的目标")
        self.assertEqual(len(changed), 1 + 1 + 3)
        self.assertGreater(saved, 0)

    def test_strips_orphan_tool_messages_at_the_cut(self) -> None:
        """切口正好落在 assistant(tool_calls) 之后时，那条 tool 消息没有调用方
        ——必须一路剥掉，否则协议报错。"""

        messages = [
            {"role": "user", "content": "开头"},
            call_message("c1", "fs_read", {"path": "a.md"}),
            result_message("c1", "内容"),
            call_message("c2", "fs_read", {"path": "b.md"}),
            result_message("c2", "内容"),
            {"role": "user", "content": "结尾"},
        ]

        # keep=4 会让尾巴从 c1 的 tool 结果开始（它的调用方已被切走）→ 必须剥掉
        changed, _ = prune_old_messages(messages, keep=4)

        self.assertNotEqual(changed[1].get("role"), "tool")

    def test_short_history_is_untouched(self) -> None:
        messages = [{"role": "user", "content": "就一句"}]
        changed, saved = prune_old_messages(messages, keep=10)
        self.assertEqual(changed, messages)
        self.assertEqual(saved, 0)


class LayerFourTests(unittest.TestCase):
    """L4：生成式摘要——最贵的一层，也是唯一会额外花钱的。"""

    def _history(self, extra: int = 8) -> list[dict]:
        messages = [SYSTEM, {"role": "user", "content": "最初的目标"}]
        for index in range(extra):
            messages.append({"role": "user", "content": f"第 {index} 句" * 20})
        return messages

    def test_summary_merges_into_the_first_system(self) -> None:
        """摘要并进已有的第一条 system，不新插中间位置的 system。

        "system 只在开头"是比"允许中间插"更保守的约定，并进去零风险。
        """

        changed, saved = summarize_history(
            self._history(), summarizer=lambda old: "要点一；要点二", keep=2)

        self.assertEqual(changed[0]["role"], "system")
        self.assertIn("工具目录", changed[0]["content"])      # 原有的还在
        self.assertIn("要点一", changed[0]["content"])
        self.assertIn("细节可能有损", changed[0]["content"])
        self.assertGreater(saved, 0)

    def test_recent_messages_survive_verbatim(self) -> None:
        """最近几条原样保留——那是"刚才发生了什么"，agent 最需要的部分。"""

        changed, _ = summarize_history(
            self._history(), summarizer=lambda old: "摘要", keep=2)

        self.assertIn("第 7 句", changed[-1]["content"])

    def test_failed_summary_leaves_history_alone(self) -> None:
        """摘要失败（返回 None / 空串）→ 整层放弃。

        用一段空摘要换掉整段历史，比不压缩严重得多——那才是真的失忆。
        """

        original = self._history()

        for bad in (lambda old: None, lambda old: "", lambda old: "   "):
            changed, saved = summarize_history(original, summarizer=bad, keep=2)
            self.assertEqual(changed, original, "失败时历史必须原样")
            self.assertEqual(saved, 0)

    def test_no_summarizer_means_skip(self) -> None:
        original = self._history()
        changed, saved = summarize_history(original, summarizer=None, keep=2)
        self.assertEqual(changed, original)
        self.assertEqual(saved, 0)


class PipelineTests(unittest.TestCase):
    """管线：预算内不动、超了从轻到重、**绝不改调用方的列表**。"""

    def _big_history(self) -> list[dict]:
        messages = [SYSTEM, {"role": "user", "content": "目标"}]
        for index in range(6):
            messages.append(call_message(f"c{index}", "fs_read", {"path": "a.md"}))
            messages.append(result_message(f"c{index}", "内容" * 500))
        return messages

    def test_under_budget_is_a_no_op_copy(self) -> None:
        """预算内：原样返回**副本**，报告里一层都没跑。"""

        messages = [{"role": "user", "content": "短"}]

        view, report = compact(messages, budget_tokens=1000)

        self.assertEqual(view, messages)
        self.assertFalse(report.changed)
        self.assertIsNot(view, messages)

    def test_caller_history_is_never_mutated(self) -> None:
        """**最重要的一条**：入口深拷贝。

        调用方给的往往是会话 store 里那份历史。就地改会污染 transcript 证据；
        更糟的是下一轮会在"已被压过的历史"上继续压，越压越短直到失忆。
        """

        messages = self._big_history()
        snapshot = copy.deepcopy(messages)

        compact(messages, budget_tokens=100, summarizer=lambda old: "摘要")

        self.assertEqual(messages, snapshot, "调用方的列表必须一个字都没变")

    def test_runs_layers_from_cheap_to_expensive(self) -> None:
        """超预算 → 按 L1→L2→L3 的顺序跑，且报告记了每层省多少。"""

        messages = self._big_history()

        view, report = compact(messages, budget_tokens=100,
                               summarizer=lambda old: "摘要",
                               max_tool_tokens=10)

        self.assertTrue(report.changed)
        self.assertLess(report.after_tokens, report.before_tokens)
        self.assertEqual(report.layers[0], "L1 截断超大工具结果")
        self.assertGreater(len(report.steps), 0)
        self.assertLessEqual(len(view), len(messages))

    def test_report_renders_a_readable_line(self) -> None:
        """账本要能直接显示给人看（压缩是有损的，说不清就没人敢信）。"""

        messages = self._big_history()
        _, report = compact(messages, budget_tokens=100, max_tool_tokens=10)

        line = report.render()
        self.assertIn("→", line)
        self.assertIn("L1", line)

    def test_no_op_report_says_so(self) -> None:
        _, report = compact([{"role": "user", "content": "短"}], budget_tokens=1000)
        self.assertIn("未压缩", report.render())


class DurableStateTests(unittest.TestCase):
    """无损旁路：结构校验 + 渲染。它永不参与压缩。"""

    def _fact(self, **overrides) -> DurableFact:
        fields = {
            "fact_id": "f1",
            "content": "存储层用 SQLite WAL 模式",
            "source_pointer": "transcript:sess_1#e12",
            "last_confirmed_at": "2026-09-17T15:30:00+08:00",
        }
        fields.update(overrides)
        return DurableFact(**fields)

    def test_renders_facts_with_provenance(self) -> None:
        """每条都要能追溯回来源——不然"已确认"三个字没有任何分量。"""

        state = DurableContextState(facts=(self._fact(),))

        text = render_durable_context(state)

        self.assertIn("SQLite WAL", text)
        self.assertIn("transcript:sess_1#e12", text)
        self.assertIn("2026-09-17T15:30:00+08:00", text)
        self.assertIn("不参与压缩", text)

    def test_renders_pending_items(self) -> None:
        state = DurableContextState(pending_items=(PendingItem(
            item_id="p1", description="还没跑全量测试",
            source_pointer="artifact:tool_result_003",
            last_confirmed_at="2026-09-17T15:31:00+08:00",
        ),))

        text = render_durable_context(state)

        self.assertIn("未决事项", text)
        self.assertIn("还没跑全量测试", text)

    def test_empty_state_renders_nothing(self) -> None:
        """空状态返回空串——调用方据此决定加不加，不用特判。"""

        self.assertEqual(render_durable_context(DurableContextState()), "")
        self.assertEqual(render_durable_context(None), "")

    def test_rejects_empty_id_and_source(self) -> None:
        for bad in ({"fact_id": ""}, {"fact_id": "  "},
                    {"source_pointer": ""}, {"content": "  "}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._fact(**bad)

    def test_rejects_timestamp_without_timezone(self) -> None:
        """没有时区的时间在跨时区协作里是歧义的——同一个串能指两个时刻。"""

        with self.assertRaises(ValueError):
            self._fact(last_confirmed_at="2026-09-17 15:30")

    def test_accepts_z_suffix(self) -> None:
        self.assertEqual(
            self._fact(last_confirmed_at="2026-09-17T07:30:00Z").fact_id, "f1")

    def test_rejects_duplicate_ids(self) -> None:
        with self.assertRaises(ValueError):
            DurableContextState(facts=(self._fact(), self._fact()))

    def test_is_frozen(self) -> None:
        """frozen：压缩流程只能读它，想改就得构造新的——那个动作一眼可见。"""

        fact = self._fact()
        with self.assertRaises(Exception):
            fact.content = "被改写了"      # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
