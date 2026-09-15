"""实战文件工具的回归测试（沙箱、限额——全部离线）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.harness import file_tools
from src.harness.file_tools import (
    ALLOWED_ROOT,
    DEFAULT_READ_LINES,
    DIFF_MAX_LINES,
    MAX_LINE_CHARS,
    MAX_READ_BYTES,
    MAX_READ_CHARS,
    MAX_WRITE_CHARS,
    _is_probably_binary,
    _normalize_newlines,
    _resolve_safe,
    edit_file,
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


class ReadFileTests(unittest.TestCase):
    """读工具：cat -n 行号 + offset/limit 分页。

    重写前的旧实现只能从头返回 2000 字符，文件的后 95% 永远读不到
    （NEXT_SESSION.md 40115 字符 → 只返回 2021 字符）。
    """

    @staticmethod
    def _content_lines(rendered: str) -> list[str]:
        """剥掉行号前缀，还原这一页的正文行。

        cat -n 格式是"右对齐行号 + 制表符 + 正文"，所以按第一个制表符劈开；
        末尾的页码小结没有制表符，自然被排除在外。
        """

        return [
            line.split("\t", 1)[1]
            for line in rendered.splitlines()
            if "\t" in line
        ]

    def test_first_line_carries_number_and_tab(self) -> None:
        rendered = read_file("README.md")
        self.assertRegex(rendered.splitlines()[0], r"^\s*1\t# wywd-harness$")

    def test_small_file_is_reported_as_fully_shown(self) -> None:
        # README.md 只有几十行，一次装得下——小结必须说"已全部显示"，
        # 模型据此才知道不必再翻页。
        rendered = read_file("README.md")
        self.assertIn("已全部显示", rendered)
        self.assertNotIn("继续读用 offset=", rendered)

    def test_offset_and_limit_page_through(self) -> None:
        whole = self._content_lines(read_file("README.md", limit=DEFAULT_READ_LINES))
        page = read_file("README.md", offset=3, limit=4)
        # 第 3~6 行的正文，与整篇读时的对应片段一字不差。
        self.assertEqual(self._content_lines(page), whole[2:6])
        # 小结必须给出下一步的 offset——这是"可行动"的落点。
        self.assertIn("继续读用 offset=7", page)

    def test_offset_beyond_end_says_the_valid_range(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            read_file("README.md", offset=99999)
        message = str(ctx.exception)
        self.assertIn("超出文件末尾", message)
        self.assertIn("1..", message)

    def test_rejects_non_positive_params(self) -> None:
        with self.assertRaises(ValueError):
            read_file("README.md", offset=0)
        with self.assertRaises(ValueError):
            read_file("README.md", limit=0)


class BinaryProbeTests(unittest.TestCase):
    """二进制探测：读与搜共用同一个纯函数。

    它原住在 std_tools，2026-09-11 迁到 file_tools——read_file 也要用它，
    而 std_tools 已经 import 了 file_tools，放那边会绕成循环导入。
    """

    def test_detects_nul(self) -> None:
        # PNG 文件的真实头部字节（含 NUL）——旧实现会把它当文本吐出乱码。
        self.assertTrue(_is_probably_binary(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"))
        self.assertTrue(_is_probably_binary(b"\x00"))

    def test_keeps_real_text(self) -> None:
        # UTF-8 中文和 emoji 都不含 NUL，绝不能误判成二进制。
        self.assertFalse(_is_probably_binary("中文文本，含 emoji 🎯".encode("utf-8")))
        self.assertFalse(_is_probably_binary(b"def main():\n    pass\n"))
        self.assertFalse(_is_probably_binary(b""))

    def test_only_probes_head(self) -> None:
        """只探前 4KB：NUL 出现在 4096 字节之后不算（避免为大文件多读几 MB）。"""

        self.assertTrue(_is_probably_binary(b"a" * 4095 + b"\x00"))
        self.assertFalse(_is_probably_binary(b"a" * 4096 + b"\x00"))


class ReadFileGuardTests(unittest.TestCase):
    """目录 / 二进制 / 空文件 / 超长行 / 超大文件——每种都得给出下一步。

    和 WriteFileTests 同一套 mock：把 ALLOWED_ROOT 换到 tmp，不碰真项目。
    """

    def test_directory_is_reported_as_directory_not_permission_denied(self) -> None:
        """旧实现在 Windows 上把"目录"报成 PermissionError，模型会去猜
        怎么提权——错误消息指错了方向，比没有错误消息更坏。"""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                with self.assertRaises(IsADirectoryError) as ctx:
                    read_file("sub")
            self.assertIn("fs_list", str(ctx.exception))

    def test_binary_file_is_rejected_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00IHDR")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                with self.assertRaises(ValueError) as ctx:
                    read_file("pic.png")
            self.assertIn("二进制", str(ctx.exception))

    def test_empty_file_says_so_instead_of_returning_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "empty.txt").write_text("", encoding="utf-8")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                rendered = read_file("empty.txt")
        self.assertIn("空文件", rendered)

    def test_overlong_line_truncated_without_dropping_neighbours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "long.txt").write_text(
                "x" * (MAX_LINE_CHARS + 1000) + "\n第二行\n", encoding="utf-8")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                rendered = read_file("long.txt")
        # 超长行被截断且注明原长——不能悄悄丢掉，否则模型以为行就这么短。
        self.assertIn(f"本行共 {MAX_LINE_CHARS + 1000} 字符，已截断", rendered)
        # 它后面的行照常返回：一行超长不该拖累整页。
        self.assertIn("第二行", rendered)

    def test_oversize_file_is_rejected_with_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "huge.txt").write_text(
                "x" * (MAX_READ_BYTES + 1), encoding="utf-8")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp)):
                with self.assertRaises(ValueError) as ctx:
                    read_file("huge.txt")
            self.assertIn("fs_find", str(ctx.exception))

    def test_single_page_respects_output_char_budget(self) -> None:
        """第四道限额：单次返回的字符总量。

        前三道各自都拦不住"行数 × 单行"的组合——200 行 × 每行 2000 字符
        = 40 万字符。实测传一遍 limit=5000 就能吐出 54580 字符。
        """

        with tempfile.TemporaryDirectory() as tmp:
            # 300 行 × 1000 字符 = 30 万字符，远超 MAX_READ_CHARS。
            (Path(tmp) / "wide.txt").write_text(
                "".join("y" * 1000 + "\n" for _ in range(300)), encoding="utf-8")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp).resolve()):
                rendered = read_file("wide.txt", limit=300)

        body = "\n".join(rendered.splitlines()[:-1])   # 最后一行是页码小结
        self.assertLessEqual(len(body), MAX_READ_CHARS)
        # 必须说明是撞到字符上限——否则模型会以为自己传的 limit 没生效，
        # 然后把 limit 调得更大（结果更糟）。
        self.assertIn("字符上限", rendered)
        self.assertIn("继续读用 offset=", rendered)

    def test_short_line_file_is_unaffected_by_the_budget(self) -> None:
        # 普通文件（短行）在默认 limit 下整篇返回，小结不该提字符上限。
        rendered = read_file("README.md")
        self.assertIn("已全部显示", rendered)
        self.assertNotIn("字符上限", rendered)


class _SandboxedFileTest(unittest.TestCase):
    """把 ALLOWED_ROOT mock 到临时目录的公共基类。

    patch.object 生效的前提是各工具在**调用时**读模块全局 ALLOWED_ROOT
    （它们确实如此），而不是定义时把值焊死。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, name: str, text: str, newline: str = "\n") -> None:
        """按指定换行风格种一个文件（默认 LF，显式 CRLF 由参数给）。"""

        payload = text.replace("\n", newline) if newline != "\n" else text
        (self.root / name).write_bytes(payload.encode("utf-8"))

    def _read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")

    def _read_bytes(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def _write(self, *args, **kwargs) -> str:
        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            return write_file(*args, **kwargs)

    def _edit(self, *args, **kwargs) -> str:
        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            return edit_file(*args, **kwargs)

    def _list(self, *args, **kwargs) -> str:
        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            return list_dir(*args, **kwargs)

    def _temp_leftovers(self) -> list[str]:
        """目录里残留的临时文件（原子写失败时最容易暴露的地方）。"""

        return sorted(
            entry.name for entry in self.root.iterdir()
            if entry.name.endswith(".tmp")
        )


class EditFileTests(_SandboxedFileTest):
    """编辑工具：只传片段 + 逐字唯一匹配 + 可行动的失败文案。

    这是整条链里最关键的一块——没有它，≥2 万字符的文件改不动：覆盖式
    fs_write 改一行也要重写全文，直接撞 MAX_WRITE_CHARS。
    """

    def test_replaces_unique_occurrence(self) -> None:
        self._seed("a.txt", "第一行\n目标行\n第三行\n")
        summary = self._edit("a.txt", "目标行", "改过了")
        self.assertIn("1 处替换", summary)
        self.assertIn("第 2 行", summary)
        self.assertEqual(self._read("a.txt"), "第一行\n改过了\n第三行\n")

    def test_return_value_carries_a_diff_for_the_human_approver(self) -> None:
        """审批闸门弹 ASK 时，人只看得到这份摘要——没有对照就是盲签。"""

        self._seed("a.txt", "旧内容\n")
        summary = self._edit("a.txt", "旧内容", "新内容")
        self.assertIn("- 旧内容", summary)
        self.assertIn("+ 新内容", summary)

    def test_diff_is_capped_for_huge_edits(self) -> None:
        # 一次编辑塞进几百行对照只会撑爆上下文，必须截断并注明还剩多少。
        self._seed("a.txt", "锚点\n")
        old = "\n".join(f"旧 {i}" for i in range(DIFF_MAX_LINES + 5))
        summary = self._edit("a.txt", "锚点", old)
        self.assertIn("还有 5 行", summary)

    def test_missing_old_text_tells_you_what_to_do(self) -> None:
        self._seed("a.txt", "原文\n")
        with self.assertRaises(ValueError) as ctx:
            self._edit("a.txt", "不存在的原文", "x")
        message = str(ctx.exception)
        self.assertIn("找不到", message)
        # 最常见的踩坑是直接把 fs_read 的行号前缀一起复制进来。
        self.assertIn("行号前缀", message)

    def test_ambiguous_old_text_reports_the_count(self) -> None:
        self._seed("a.txt", "dup\ndup\n")
        with self.assertRaises(ValueError) as ctx:
            self._edit("a.txt", "dup", "x")
        message = str(ctx.exception)
        self.assertIn("2 次", message)  # 报出次数，模型才知道要补多少上下文
        self.assertIn("replace_all", message)

    def test_replace_all_updates_every_occurrence(self) -> None:
        self._seed("a.txt", "dup\ndup\ndup\n")
        summary = self._edit("a.txt", "dup", "x", replace_all=True)
        self.assertIn("3 处替换", summary)
        self.assertEqual(self._read("a.txt"), "x\nx\nx\n")

    def test_empty_new_text_deletes_the_fragment(self) -> None:
        self._seed("a.txt", "保留\n删我\n")
        self._edit("a.txt", "删我\n", "")
        self.assertEqual(self._read("a.txt"), "保留\n")

    def test_identical_old_and_new_is_rejected(self) -> None:
        self._seed("a.txt", "原文\n")
        with self.assertRaises(ValueError):
            self._edit("a.txt", "原文", "原文")

    def test_empty_old_text_is_rejected(self) -> None:
        # 空串在文件里处处匹配，等于"随便改"。
        self._seed("a.txt", "原文\n")
        with self.assertRaises(ValueError):
            self._edit("a.txt", "", "x")

    def test_rejects_oversize_new_text(self) -> None:
        self._seed("a.txt", "原文\n")
        with self.assertRaises(ValueError):
            self._edit("a.txt", "原文", "x" * (MAX_WRITE_CHARS + 1))

    def test_blocks_forbidden_parts(self) -> None:
        # 和 fs_write 同一套闸门：界内禁区同样改不了。
        with self.assertRaises(PermissionError):
            self._edit(".env", "A", "B")

    def test_edits_a_file_that_fs_write_would_refuse(self) -> None:
        """本工具的立身之本。

        大于 MAX_WRITE_CHARS 的文件，fs_write 改不动——改一行也要整文件
        重写，参数长度直接超限被拒。fs_edit 只传两个片段，代价与文件
        大小无关，所以照样能改。NEXT_SESSION.md（4 万字符）的真实处境。
        """

        filler = "x" * 200 + "\n"
        body = filler * ((MAX_WRITE_CHARS // 200) + 10)
        self._seed("big.txt", body + "最后一行：待改\n")
        self.assertGreater(len(body), MAX_WRITE_CHARS)

        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            # 对照：走"整文件重写"这条路，参数长度直接超限。
            with self.assertRaises(ValueError):
                write_file("big.txt", body + "最后一行：改好了\n")

        summary = self._edit("big.txt", "最后一行：待改", "最后一行：改好了")
        self.assertIn("1 处替换", summary)
        self.assertIn("最后一行：改好了", self._read("big.txt"))

    def test_normalize_newlines_reports_original_style(self) -> None:
        """纯函数打表：归一化到 \\n，同时把原本的风格带回来。"""

        self.assertEqual(_normalize_newlines("a\r\nb"), ("a\nb", "\r\n"))
        self.assertEqual(_normalize_newlines("a\nb"), ("a\nb", "\n"))
        self.assertEqual(_normalize_newlines("没有换行"), ("没有换行", "\n"))

    def test_edit_preserves_crlf_without_doubling_cr(self) -> None:
        """CRLF 文件编辑后仍是 CRLF，而且 CR 没有翻倍。

        实测过的坑（本仓库 .py 文件全是 CRLF，一改就中）：文本模式写盘会把
        正文里的 \\n 再翻译一次成 os.linesep，而原本的 \\r 原样保留——于是
        b'a\\r\\nb\\r\\n' 写出来变成 b'a\\r\\r\\nb\\r\\r\\n'。
        """

        self._seed("a.txt", "第一行\n目标行\n第三行\n", newline="\r\n")
        self.assertEqual(
            self._read_bytes("a.txt"),
            "第一行\r\n目标行\r\n第三行\r\n".encode("utf-8"),
        )

        self._edit("a.txt", "目标行", "改过了")
        self.assertEqual(
            self._read_bytes("a.txt"),
            "第一行\r\n改过了\r\n第三行\r\n".encode("utf-8"),
        )

    def test_multiline_old_text_matches_a_crlf_file(self) -> None:
        """模型的多行锚点是用 \\n 拼的，CRLF 文件也必须能匹配上——
        不做归一化的话 fs_edit 对本仓库的文件永远"找不到"。"""

        self._seed("a.txt", "alpha\nbeta\ngamma\n", newline="\r\n")
        # 注意：old_text 里是 \n，文件里是 \r\n。
        self._edit("a.txt", "alpha\nbeta", "ALPHA\nBETA")
        self.assertEqual(
            self._read_bytes("a.txt"), "ALPHA\r\nBETA\r\ngamma\r\n".encode("utf-8")
        )

    def test_new_file_is_written_as_lf(self) -> None:
        # 新建文件没有"原有风格"可沿用，一律用 \n。
        self._write("fresh.txt", "一\n二\n")
        self.assertEqual(self._read_bytes("fresh.txt"), "一\n二\n".encode("utf-8"))

    def test_overwrite_preserves_existing_newline_style(self) -> None:
        """覆盖已有文件时沿用它的换行风格，而不是整篇换成 LF——
        否则 git diff 看起来像全文件重写（没动过的行也全变了）。"""

        self._seed("a.txt", "旧\n", newline="\r\n")
        self._write("a.txt", "新一\n新二\n")
        self.assertEqual(
            self._read_bytes("a.txt"), "新一\r\n新二\r\n".encode("utf-8")
        )


class AtomicWriteTests(_SandboxedFileTest):
    """原子落盘：要么是完整的新文件，要么旧文件原封不动。

    为什么值得单独测：非原子写（先截断再写）在"写一半崩溃"时会留下半个
    文件顶着正式名字，原有内容已经被毁了——文件越大这个窗口越宽，而
    fs_edit 正是为改大文件而生的，所以更不能走那条路。
    """

    def test_successful_write_leaves_no_temp_file(self) -> None:
        self._write("a.txt", "内容\n")
        self.assertEqual(self._temp_leftovers(), [])

    def test_successful_edit_leaves_no_temp_file(self) -> None:
        self._seed("a.txt", "原文\n")
        self._edit("a.txt", "原文", "改过")
        self.assertEqual(self._temp_leftovers(), [])

    def test_failed_replace_keeps_original_intact(self) -> None:
        """原子写的核心承诺：写失败 = 什么都没发生。

        mock 掉 os.replace（改名那一步），模拟"数据写完了但改名失败"。
        旧内容必须原封不动，临时文件也必须清干净。
        """

        self._seed("a.txt", "原始内容\n")
        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            with mock.patch("os.replace", side_effect=OSError("模拟改名失败")):
                with self.assertRaises(OSError):
                    write_file("a.txt", "新内容\n")

        self.assertEqual(self._read("a.txt"), "原始内容\n")
        self.assertEqual(self._temp_leftovers(), [])

    def test_failed_edit_keeps_original_intact(self) -> None:
        self._seed("a.txt", "原始内容\n")
        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            with mock.patch("os.replace", side_effect=OSError("模拟改名失败")):
                with self.assertRaises(OSError):
                    edit_file("a.txt", "原始内容", "新内容")

        self.assertEqual(self._read("a.txt"), "原始内容\n")
        self.assertEqual(self._temp_leftovers(), [])


class ErrorMessageTests(_SandboxedFileTest):
    """错误消息必须可行动：不只说"失败了"，还要说下一步干什么。

    Anthropic 的原则——回灌给模型的错误文案质量，直接决定它能不能自愈。
    """

    def test_missing_path_tells_you_how_to_find_it(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            self._list("不存在的目录")
        message = str(ctx.exception)
        self.assertIn("fs_list", message)
        self.assertIn("fs_glob", message)

    def test_file_passed_to_list_suggests_read(self) -> None:
        self._seed("a.txt", "内容\n")
        with self.assertRaises(NotADirectoryError) as ctx:
            self._list("a.txt")
        self.assertIn("fs_read", str(ctx.exception))

    def test_directory_passed_to_write_is_named_as_directory(self) -> None:
        (self.root / "sub").mkdir()
        with self.assertRaises(IsADirectoryError) as ctx:
            self._write("sub", "x")
        self.assertIn("目录不是文件", str(ctx.exception))

    def test_missing_parent_dir_says_it_will_not_create_it(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            self._write("no-such-dir/a.txt", "x")
        message = str(ctx.exception)
        self.assertIn("父目录不存在", message)
        self.assertIn("不会自动创建目录", message)

    def test_oversize_write_points_at_edit(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._write("a.txt", "x" * (MAX_WRITE_CHARS + 1))
        self.assertIn("fs_edit", str(ctx.exception))

    def test_outside_sandbox_message_carries_the_root(self) -> None:
        """沙箱根要报出来——模型知道了边界在哪才可能自己改对路径。"""

        with mock.patch.object(file_tools, "ALLOWED_ROOT", self.root):
            with self.assertRaises(PermissionError) as ctx:
                read_file("../outside.txt")
        message = str(ctx.exception)
        self.assertIn("越出沙箱", message)
        self.assertIn(str(self.root), message)

    def test_forbidden_part_message_names_the_part(self) -> None:
        with self.assertRaises(PermissionError) as ctx:
            read_file(".env")
        message = str(ctx.exception)
        self.assertIn(".env", message)
        self.assertIn("禁区", message)


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
