"""trim_history 的回归测试（窗口、配对安全——全部离线）。"""

import unittest

from src.harness.memory import trim_history


class TrimHistoryTests(unittest.TestCase):
    def test_short_history_returned_as_copy(self) -> None:
        history = [{"role": "user", "content": "u1"}]

        trimmed = trim_history(history, 5)

        self.assertEqual(trimmed, history)
        # 副本：改动返回值不许影响调用方的原列表。
        trimmed.append({"role": "user", "content": "extra"})
        self.assertEqual(len(history), 1)

    def test_trims_to_keep_latest(self) -> None:
        history = [{"role": "user", "content": f"u{i}"} for i in range(6)]

        trimmed = trim_history(history, 4)

        self.assertEqual(
            [m["content"] for m in trimmed], ["u2", "u3", "u4", "u5"]
        )

    def test_drops_orphan_tool_messages(self) -> None:
        history = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "", "tool_calls": [{"call_id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "", "tool_calls": [{"call_id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "assistant", "content": "final"},
            {"role": "user", "content": "u3"},
        ]

        # 窗口 3 条 = [tool(c2), assistant(final), u3]——开头是孤儿 tool
        # （它的调用方 assistant(c2) 已在切口之外），丢弃后剩 2 条。
        trimmed = trim_history(history, 3)

        self.assertEqual([m["role"] for m in trimmed], ["assistant", "user"])
        self.assertEqual(trimmed[0]["content"], "final")

    def test_keeps_tool_pair_intact(self) -> None:
        history = [
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "", "tool_calls": [{"call_id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "user", "content": "u3"},
        ]

        # 窗口正好落在 [assistant(tc), tool, u3]——配对完整保留。
        trimmed = trim_history(history, 3)

        self.assertEqual(
            [m["role"] for m in trimmed], ["assistant", "tool", "user"]
        )
        self.assertEqual(trimmed[0]["tool_calls"][0]["call_id"], "c1")
        self.assertEqual(trimmed[1]["tool_call_id"], "c1")


if __name__ == "__main__":
    unittest.main()
