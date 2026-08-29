"""实战文件工具的回归测试（沙箱、限额——全部离线）。"""

import unittest

from src.harness.file_tools import ALLOWED_ROOT, _resolve_safe, list_dir, read_file


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


if __name__ == "__main__":
    unittest.main()
