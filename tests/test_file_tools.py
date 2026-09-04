"""实战文件工具的回归测试（沙箱、限额——全部离线）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.harness import file_tools
from src.harness.file_tools import (
    ALLOWED_ROOT,
    MAX_WRITE_CHARS,
    _resolve_safe,
    list_dir,
    read_file,
    write_file,
)


class ResolveSafeTests(unittest.TestCase):
    def test_allows_paths_inside_root(self) -> None:
        self.assertEqual(_resolve_safe("src"), (ALLOWED_ROOT / "src").resolve())
        self.assertEqual(_resolve_safe("."), ALLOWED_ROOT)

    def test_blocks_path_traversal(self) -> None:
        # 相对路径翻墙
        with self.assertRaises(PermissionError):
            _resolve_safe("../..")
        # 绝对路径指到沙箱外
        with self.assertRaises(PermissionError):
            _resolve_safe("C:/Windows")


class FileToolTests(unittest.TestCase):
    def test_list_dir_lists_entries(self) -> None:
        src_listing = list_dir("src")
        self.assertIn("harness/", src_listing)
        self.assertIn("(目录)", src_listing)

        root_listing = list_dir(".")
        self.assertIn("README.md", root_listing)
        self.assertIn("(文件", root_listing)

    def test_read_file_reads_and_truncates(self) -> None:
        full = read_file("README.md")
        self.assertTrue(full.startswith("# wywd-harness"))

        truncated = read_file("README.md", max_chars=20)
        # 截断保留的正是正文前 20 个字符，并附上截断说明
        self.assertIn("已截断", truncated)
        self.assertTrue(truncated.startswith(full[:20]))
        self.assertLess(len(truncated), len(full))


class DenylistTests(unittest.TestCase):
    """练习 16：沙箱保镖——界内禁区同样进不去。"""

    def test_env_is_forbidden(self) -> None:
        # 检查发生在读取之前，不需要真的创建 .env。
        with self.assertRaises(PermissionError):
            read_file(".env")

    def test_git_is_forbidden(self) -> None:
        # 文件名是 config，撞禁区的是路径中段的 .git。
        with self.assertRaises(PermissionError):
            read_file(".git/config")
        # 禁区对两个工具一视同仁。
        with self.assertRaises(PermissionError):
            list_dir(".git")


class WriteFileTests(unittest.TestCase):
    """练习 s04 的写工具：沙箱/禁区照旧，新增内容限额。

    写测试不能污染真实项目——mock 把 ALLOWED_ROOT 换成临时目录。
    patch.object 生效的前提是 _resolve_safe 在"调用时"读模块全局
    ALLOWED_ROOT（它确实如此），而不是定义时把值焊死。
    """

    def test_writes_file_inside_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                summary = write_file("note.txt", "你好，沙箱")
            self.assertIn("已写入", summary)
            self.assertEqual(
                (Path(tmp) / "note.txt").read_text(encoding="utf-8"),
                "你好，沙箱",
            )

    def test_write_overwrites_existing(self) -> None:
        # 整文件覆盖不是追加：写第二次，读回来的只有第二次的内容。
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                write_file("note.txt", "第一版")
                write_file("note.txt", "第二版")
            self.assertEqual(
                (Path(tmp) / "note.txt").read_text(encoding="utf-8"), "第二版"
            )

    def test_write_blocks_traversal(self) -> None:
        # 越界写在执行层也要被 _resolve_safe 拦住——审批层之外的保底。
        # 检查发生在写之前，不需要真去写那个文件。
        with self.assertRaises(PermissionError):
            write_file("../evil.txt", "x")

    def test_write_blocks_forbidden_parts(self) -> None:
        # .env 是禁区：界内也不许写，和读工具同一套闸门。
        with self.assertRaises(PermissionError):
            write_file(".env", "SECRET=1")

    def test_write_rejects_oversize_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                with self.assertRaises(ValueError):
                    write_file("big.txt", "x" * (MAX_WRITE_CHARS + 1))
            # 被拒的写入不能留下半成品文件。
            self.assertFalse((Path(tmp) / "big.txt").exists())


if __name__ == "__main__":
    unittest.main()
