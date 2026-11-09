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

    def test_staged_diff_only_includes_staged(self) -> None:
        (self.repo / "b.txt").write_text("staged\n", encoding="utf-8")
        _git(self.repo, "add", "b.txt")
        (self.repo / "a.txt").write_text("unstaged\n", encoding="utf-8")

        _, body, _ = review.collect_staged_diff(self.repo)

        self.assertIn("+staged", body)
        self.assertNotIn("+unstaged", body)   # 没 add 的不能混进本次提交

    def test_worktree_diff_includes_unstaged_and_untracked_listing(self) -> None:
        (self.repo / "a.txt").write_text("worktree\n", encoding="utf-8")
        (self.repo / "c.txt").write_text("untracked\n", encoding="utf-8")

        stat, body, _ = review.collect_worktree_diff(self.repo)

        self.assertIn("+worktree", body)
        self.assertIn("-new", body)
        self.assertIn("c.txt", stat)          # 未跟踪文件给清单
        self.assertIn("未跟踪", body)

    def test_severity_gate_levels(self) -> None:
        output = "[blocker] a:1 — x\n[minor] b:2 — y\n"
        self.assertTrue(review.severity_at_least(output, "blocker"))
        self.assertTrue(review.severity_at_least(output, "major"))
        self.assertFalse(review.severity_at_least("[minor] b:2 — y", "major"))
        self.assertFalse(review.severity_at_least("没有发现", "blocker"))

    def test_severity_gate_tolerates_markdown(self) -> None:
        """模型实际会加粗/列点：`**[blocker]` 必须真的挡住（真机踩过）。"""

        cases = (
            ("**[blocker] a:1 — x**", "blocker"),
            ("- [major] b:2 — y", "major"),
            ("1. [minor] c:3 — z", "minor"),
            ("## [BLOCKER] d:4 — w", "blocker"),
        )
        for line, threshold in cases:
            with self.subTest(line=line):
                self.assertTrue(review.severity_at_least(line, threshold))

    def test_severity_gate_ignores_inline_mentions(self) -> None:
        # 正文里复述级别词不是一条发现——闸门只看行首的标记。
        self.assertFalse(review.severity_at_least(
            "这条说明里提到 [blocker] 这个词，但没有发现。", "blocker"))

    def test_task_headers_follow_scope(self) -> None:
        self.assertIn("未提交", review.build_task("a", "b", "s", "d",
                                                 scope="worktree"))
        self.assertIn("已暂存", review.build_task("a", "b", "s", "d",
                                                 scope="staged"))
        self.assertIn("a..b", review.build_task("a", "b", "s", "d"))


class CiConfigTests(unittest.TestCase):
    """CI 没有本地 .workbuddy：config/reviewer 那份必须能直接加载。"""

    def test_tracked_reviewer_config_is_loadable(self) -> None:
        root = Path(__file__).resolve().parents[1] / "config" / "reviewer"
        package = review.ExpertIndex(root).load("reviewer")
        self.assertEqual(package.expert_id, "reviewer")
        self.assertTrue(package.tools_config.disabled)
        body = review.build_skill_index(root).load("code-review")
        self.assertIn("评审", body)

    def test_ci_workflow_has_review_job(self) -> None:
        text = (Path(__file__).resolve().parents[1]
                / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("review:", text)
        self.assertIn("--config-dir config/reviewer", text)
        self.assertIn("--fail-on blocker", text)
        self.assertIn("DEEPSEEK_API_KEY", text)


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
        # 2026-11-08 评测后加的集成检查：调用方/邻居、契约两端、落盘目录。
        self.assertIn("集成检查", prompt)
        self.assertIn("调用方", prompt)
        self.assertIn("契约两端", prompt)
        self.assertIn("调用方或邻居文件", prompt)
        # UI 改动必须同时检查实时路径与历史回放路径。
        self.assertIn("历史回放", prompt)

    def test_task_orders_integration_check_first(self) -> None:
        task = review.build_task("main", "HEAD", "x", "+y")
        self.assertIn("集成检查", task)
        self.assertLess(task.index("集成检查"), task.index("每条发现"))


if __name__ == "__main__":
    unittest.main()
