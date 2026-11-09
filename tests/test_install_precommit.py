"""pre-commit 安装器的离线测试：只写该写的钩子，不碰别人的。"""

import tempfile
import unittest
from pathlib import Path

from scripts import install_precommit


class InstallPrecommitTests(unittest.TestCase):

    def _repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        (repo / ".git" / "hooks").mkdir(parents=True)
        return repo

    def test_installs_staged_gate(self) -> None:
        repo = self._repo()
        hook = install_precommit.install(repo)
        text = hook.read_text(encoding="utf-8")

        self.assertIn("scripts/install_precommit.py", text)
        self.assertIn("--staged", text)
        self.assertIn("--fail-on blocker", text)
        self.assertIn("WYWD_SKIP_REVIEW", text)
        self.assertIn("DEEPSEEK_API_KEY", text)
        self.assertNotIn("__PYTHON__", text)      # 安装时写死解释器

    def test_refuses_to_clobber_existing_hook(self) -> None:
        repo = self._repo()
        install_precommit.install(repo)
        with self.assertRaises(RuntimeError):
            install_precommit.install(repo)
        install_precommit.install(repo, force=True)   # 明确覆盖才行

    def test_uninstall_leaves_foreign_hook(self) -> None:
        repo = self._repo()
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            install_precommit.uninstall(repo)
        self.assertTrue(hook.exists())

    def test_uninstall_own_hook(self) -> None:
        repo = self._repo()
        hook = install_precommit.install(repo)
        self.assertTrue(install_precommit.uninstall(repo))
        self.assertFalse(hook.exists())

    def test_not_a_git_repo(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(RuntimeError):
            install_precommit.install(Path(tmp.name))


if __name__ == "__main__":
    unittest.main()
