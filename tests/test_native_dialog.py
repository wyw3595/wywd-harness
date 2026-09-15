"""原生目录选择框：父进程侧的协议与降级。

真弹框不能进单元测试（会卡住等人点），所以这里把子进程换掉，
只验**父进程这一侧**：怎么解析回话、怎么区分"取消"和"不可用"、
以及什么时候该让调用方降级。

子进程那一侧由 scripts/smoke_native_dialog.py 走真链子验（含真弹框后
按标题找窗口关掉那一招）。
"""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts import native_dialog


def _done(stdout: str = "", stderr: str = "", code: int = 0):
    """假 subprocess.run 的返回值。"""

    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr=stderr)


class AvailableTests(unittest.TestCase):
    """探活：能不能弹，不能要给出人话原因（调用方据此降级）。"""

    def test_reports_available_on_this_machine(self) -> None:
        # 这台机器有可用后端；探活不该弹任何窗口
        can, reason = native_dialog.available()
        self.assertTrue(can, reason)
        self.assertTrue(reason)

    @unittest.skipUnless(sys.platform == "win32", "只在 Windows 走 COM 后端")
    def test_windows_backend_is_the_explorer_style_dialog(self) -> None:
        """Windows 上必须是资源管理器式的那个框（IFileOpenDialog）。

        为什么单测这一条：Tk 的 askdirectory 在 Windows 上只给文件夹树、
        **不显示文件**，用户会以为没打开文件管理器（真实反馈）。这条断言把
        "别退回去用 Tk"钉住。
        """

        can, reason = native_dialog.available()
        self.assertTrue(can)
        self.assertIn("IFileOpenDialog", reason)

    @unittest.skipIf(sys.platform == "win32", "Tk 后端只在非 Windows 上用")
    def test_missing_tkinter_says_so(self) -> None:
        with mock.patch.dict("sys.modules", {"tkinter": None}):
            can, reason = native_dialog.available()
        self.assertFalse(can)
        self.assertIn("tkinter", reason)

    @unittest.skipIf(sys.platform == "win32", "Tk 后端只在非 Windows 上用")
    def test_no_display_environment_says_so(self) -> None:
        """服务跑在没有图形环境的机器上：必须能识别出来，别硬弹。"""

        with mock.patch.dict("os.environ", {}, clear=True):
            can, reason = native_dialog.available()
        self.assertFalse(can)
        self.assertIn("DISPLAY", reason)

    def test_guid_parsing(self) -> None:
        """GUID 字符串解析——写错一位就调不到 COM 对象，而且报错很难懂。

        所以单独验一遍：Data1/2/3 是三个整数，Data4 是 8 个字节（后两段
        各 2 字节要按顺序拼起来，不是当成一个 4 字节整数）。
        """

        GUID, parse = native_dialog._make_guids()
        guid = parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        self.assertEqual(guid.Data1, 0xDC1C5A9C)
        self.assertEqual(guid.Data2, 0xE88A)
        self.assertEqual(guid.Data3, 0x4DDE)
        self.assertEqual(list(guid.Data4),
                         [0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7])

    @unittest.skipUnless(sys.platform == "win32", "GUID 常量只在 Windows 用")
    def test_guids_are_well_formed(self) -> None:
        """三个常量都得是 8-4-4-4-12 的形状（手抄 SDK 头文件最容易抄错）。"""

        for text in (native_dialog._CLSID_FILE_OPEN_DIALOG,
                     native_dialog._IID_IFILE_OPEN_DIALOG,
                     native_dialog._IID_ISHELL_ITEM):
            parts = text.strip("{}").split("-")
            self.assertEqual([len(p) for p in parts],
                             [8, 4, 4, 4, 12], text)


class PickDirectoryProtocolTests(unittest.TestCase):
    """协议：子进程回话怎么读，读不出来算什么。"""

    def test_returns_resolved_path(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:D:/proj\n")) as run:
            picked = native_dialog.pick_directory()
        self.assertEqual(picked, str(Path("D:/proj").resolve()))
        # 一定要用 pythonw（无控制台），否则弹框前先闪一个黑窗
        self.assertIn(Path(run.call_args[0][0][0]).name.lower(),
                      ("pythonw.exe", Path(sys.executable).name.lower()))

    def test_stray_output_is_not_mistaken_for_a_path(self) -> None:
        """Tk/Tcl 自己会往 stdout 吐警告——只有 RESULT: 那行算数。"""

        noisy = ("invalid command name \"after#0\"\n"
                 "Xlib: extension \"RANDR\" missing\n"
                 "RESULT:E:/work\n"
                 "some trailing junk\n")
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done(noisy)):
            self.assertEqual(native_dialog.pick_directory(),
                             str(Path("E:/work").resolve()))

    def test_empty_result_means_cancelled(self) -> None:
        """用户取消：返回 None（不是异常）——取消不是错误。"""

        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:\n")):
            self.assertIsNone(native_dialog.pick_directory())

    def test_unavailable_exit_code_raises_dedicated_error(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done(stderr="没有 tkinter\n", code=2)):
            with self.assertRaises(native_dialog.DialogUnavailable) as ctx:
                native_dialog.pick_directory()
        self.assertIn("tkinter", str(ctx.exception))

    def test_crash_exit_code_raises_dedicated_error(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done(stderr="Traceback...\n", code=1)):
            with self.assertRaises(native_dialog.DialogUnavailable):
                native_dialog.pick_directory()

    def test_no_result_line_at_all_is_unavailable(self) -> None:
        """连回话行都没有 = 机制没按预期工作：降级，别给用户显示一个空路径。"""

        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT_LIKE_BUT_NOT\n")):
            with self.assertRaises(native_dialog.DialogUnavailable):
                native_dialog.pick_directory()

    def test_timeout_is_not_unavailable(self) -> None:
        """超时和"弹不出"是两回事：一个是用户挑太久/卡住，一个是环境不行。

        混在一起会让前端把"卡住"也降级成手输，用户会莫名其妙。
        """

        with mock.patch("scripts.native_dialog.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            with self.assertRaises(TimeoutError):
                native_dialog.pick_directory()

    def test_unavailable_short_circuits_before_spawning(self) -> None:
        """探活说不行的，就别再起子进程了（省一次无用的进程 + 弹窗失败）。"""

        with mock.patch("scripts.native_dialog.available",
                        return_value=(False, "没有图形环境")), \
                mock.patch("scripts.native_dialog.subprocess.run") as run:
            with self.assertRaises(native_dialog.DialogUnavailable):
                native_dialog.pick_directory()
        run.assert_not_called()

    def test_initial_dir_is_passed_through(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:C:/tmp\n")) as run:
            native_dialog.pick_directory(initial="D:/current")
        argv = run.call_args[0][0]
        self.assertIn("D:/current", argv)


class ChildModeTests(unittest.TestCase):
    """子进程入口：参数解析（真弹框在冒烟里验）。"""

    def _child_stdout(self, argv: list[str]) -> str:
        """跑一次子进程的自检模式（不建窗），把它的回话抓回来。"""

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = native_dialog._child(argv)
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_child_parses_initial_dir_after_flags(self) -> None:
        """父进程永远把 --child 放在最前面，所以**不能只认 argv[0]**。

        踩过这个坑：`argv[0] if not argv[0].startswith("--") else ""` 在
        `["--child", "D:/proj", ...]` 下把初始目录判成空串 → 起始目录永远
        不生效 → 框一直开在"文档"。查起来像是 SetFolder 坏了，其实是参数
        根本没传进来。
        """

        target = str(Path("D:/proj").resolve())
        out = self._child_stdout(["--child", "D:/proj", "--selftest"])
        self.assertIn(target, out)

    def test_child_without_initial_dir_falls_back_to_cwd(self) -> None:
        out = self._child_stdout(["--child", "--selftest"])
        self.assertIn(str(Path.cwd().resolve()), out)

    def test_child_ignores_flag_order(self) -> None:
        """开关在前在后都不影响找那个位置参数。"""

        target = str(Path("D:/proj").resolve())
        for argv in (["--child", "--selftest", "D:/proj"],
                     ["--child", "D:/proj", "--selftest"],
                     ["--child", "--auto-ok", "D:/proj", "--selftest"]):
            with self.subTest(argv=argv):
                self.assertIn(target, self._child_stdout(argv))

    def test_flash_arg_passed_to_child(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:\n")) as run:
            native_dialog.pick_directory(flash_ms=800)
        self.assertIn("--flash-ms=800", run.call_args[0][0])

    def test_selftest_arg_passed_to_child(self) -> None:
        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:C:/x\n")) as run:
            native_dialog.pick_directory(selftest=True)
        self.assertIn("--selftest", run.call_args[0][0])

    def test_cancelled_flash_returns_none(self) -> None:
        """闪一下模式里窗口被关掉 = 用户取消 = None（不是错误）。"""

        with mock.patch("scripts.native_dialog.subprocess.run",
                        return_value=_done("RESULT:\n")):
            self.assertIsNone(native_dialog.pick_directory(flash_ms=800))


if __name__ == "__main__":
    unittest.main()
