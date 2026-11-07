"""reviewer CLI 的离线测试：diff 采集 / 工具收窄 / 提示词拼装。

不跑模型：run_agent 部分留给真机（scripts/review.py --base …）。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import review
from src.harness.experts import ExpertPackage
from src.harness.tools import Tool, ToolRegistry


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


class CollectDiffTests(unittest.TestCase):
    """git diff 采集是审查的输入口：内容要对、截断要说明。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("old\n", encoding="utf-8")
        (self.repo / "b.txt").write_text("keep\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "init")
        (self.repo / "a.txt").write_text("new\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "change")

    def test_collects_stat_and_patch(self) -> None:
        stat, body, truncated = review.collect_diff(self.repo, "HEAD~1", "HEAD")

        self.assertIn("a.txt", stat)
        self.assertIn("-old", body)
        self.assertIn("+new", body)
        self.assertFalse(truncated)

    def test_path_filter_limits_the_diff(self) -> None:
        _, body, _ = review.collect_diff(self.repo, "HEAD~1", "HEAD", "b.txt")
        self.assertNotIn("a.txt", body)
        self.assertNotIn("+new", body)

    def test_long_diff_is_truncated_with_a_note(self) -> None:
        original = review.MAX_DIFF_CHARS
        review.MAX_DIFF_CHARS = 40
        try:
            _, body, truncated = review.collect_diff(self.repo, "HEAD~1", "HEAD")
        finally:
            review.MAX_DIFF_CHARS = original
        self.assertTrue(truncated)
        self.assertIn("被截断", body)


class FilterRegistryTests(unittest.TestCase):
    """专家包只能少给工具：disabled 必须在注册表上真的摘掉。"""

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for name in ("fs_read", "fs_glob", "fs_write", "bash"):
            registry.register(Tool(
                name=name, description=name, handler=lambda **kwargs: ""))
        return registry

    def test_disabled_tools_are_removed(self) -> None:
        package = ExpertPackage.from_dict({
            "expert_id": "reviewer", "name": "审查员", "system_prompt": "只读",
            "tools_config": {"disabled_tools": ["bash", "fs_write"]},
        })
        filtered, disabled = review.filter_registry(self._registry(), package)

        self.assertEqual(sorted(filtered.names()), ["fs_glob", "fs_read"])
        self.assertEqual(sorted(disabled), ["bash", "fs_write"])

    def test_unknown_disabled_names_are_ignored(self) -> None:
        package = ExpertPackage.from_dict({
            "expert_id": "reviewer", "name": "审查员", "system_prompt": "只读",
            "tools_config": {"disabled_tools": ["no-such-tool"]},
        })
        filtered, disabled = review.filter_registry(self._registry(), package)
        self.assertEqual(len(filtered.names()), 4)
        self.assertEqual(disabled, ())


class PromptTests(unittest.TestCase):
    """提示词只负责"让人和模型都看懂边界"，内容可断言。"""

    def test_task_carries_range_stat_and_diff(self) -> None:
        task = review.build_task("main", "HEAD", "a.txt | 2 +-", "-old\n+new")
        self.assertIn("main..HEAD", task)
        self.assertIn("a.txt | 2 +-", task)
        self.assertIn("+new", task)
        self.assertIn("不要修改任何文件", task)

    def test_system_prompt_carries_specialization_and_skill(self) -> None:
        package = ExpertPackage.from_dict({
            "expert_id": "reviewer", "name": "只读审查员",
            "system_prompt": "只读审查", "guidelines": ["信任边界"],
        })
        prompt = review.build_system_prompt(
            package, "# 评审清单\n先看结构", Path("D:/repo"))
        self.assertIn("只读审查员", prompt)
        self.assertIn("信任边界", prompt)
        self.assertIn("先看结构", prompt)
        self.assertIn("fs_read", prompt)


if __name__ == "__main__":
    unittest.main()
