"""reviewer 配置同步脚本的离线测试：一致 / 漂移 / 写回。

只碰临时目录，不读真实 .workbuddy（那是用户本地配置）。
"""

import tempfile
import unittest
from pathlib import Path

from scripts import sync_reviewer_config as sync

REVIEWER = '{"expert_id": "reviewer"}\n'
SKILL = "# code review\n只看 diff，别改代码。\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SyncReviewerConfigTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source = root / ".workbuddy"
        self.target = root / "config" / "reviewer" / ".workbuddy"
        _write(self.source, "experts/reviewer.json", REVIEWER)
        _write(self.source, "skills/code-review/SKILL.md", SKILL)
        # 本地目录里的其它文件不该被同步（记忆/日志/临时脚本）。
        _write(self.source, "memory/MEMORY.md", "private\n")

    def test_consistent_copy_reports_no_drift(self) -> None:
        sync.sync_files(self.source, self.target)
        self.assertEqual(sync.diff_files(self.source, self.target), [])

    def test_check_returns_one_on_content_drift(self) -> None:
        sync.sync_files(self.source, self.target)
        _write(self.source, "experts/reviewer.json", '{"expert_id": "v2"}\n')

        problems = sync.diff_files(self.source, self.target)
        self.assertEqual(len(problems), 1)
        self.assertIn("experts/reviewer.json", problems[0])
        # CLI 契约：漂移必须退出 1（CI 靠这个挡）。
        self.assertEqual(
            sync.main(["--check", "--source", str(self.source),
                       "--target", str(self.target)]), 1)

    def test_check_reports_missing_copy(self) -> None:
        # 副本目录还没建：算漂移，不是崩溃。
        problems = sync.diff_files(self.source, self.target)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("缺少副本" in p for p in problems))
        self.assertEqual(
            sync.main(["--check", "--source", str(self.source),
                       "--target", str(self.target)]), 1)

    def test_write_back_then_check_is_clean(self) -> None:
        sync.sync_files(self.source, self.target)
        _write(self.target, "experts/reviewer.json", "stale\n")
        self.assertTrue(sync.diff_files(self.source, self.target))

        written = sync.sync_files(self.source, self.target)
        self.assertEqual(written, ["experts/reviewer.json"])
        self.assertEqual(sync.diff_files(self.source, self.target), [])
        # 第二次没有变化就不该重复写。
        self.assertEqual(sync.sync_files(self.source, self.target), [])

    def test_only_listed_files_are_synced(self) -> None:
        sync.sync_files(self.source, self.target)
        self.assertFalse((self.target / "memory" / "MEMORY.md").exists())

    def test_missing_local_config_is_exit_two(self) -> None:
        missing = Path(self._tmp.name) / "no-such-config"
        self.assertEqual(
            sync.main(["--check", "--source", str(missing),
                       "--target", str(self.target)]), 2)

    def test_deleted_source_file_is_drift(self) -> None:
        # 目录在、文件被删：这是漂移（不是"没法比"）——副本可能是旧的。
        (self.source / "experts" / "reviewer.json").unlink()
        problems = sync.diff_files(self.source, self.target)
        self.assertTrue(any("缺少源文件" in p for p in problems))
        self.assertEqual(
            sync.main(["--check", "--source", str(self.source),
                       "--target", str(self.target)]), 1)

    def test_check_and_write_are_mutually_exclusive(self) -> None:
        self.assertEqual(sync.main(["--check", "--write"]), 2)
        self.assertEqual(sync.main([]), 2)


if __name__ == "__main__":
    unittest.main()
