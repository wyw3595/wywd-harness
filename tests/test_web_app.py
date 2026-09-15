"""C 方案 web_app 的单测：直调处理函数 + 假 shell 注入（沿用既有套路）。

scripts/sidecar_panel.py / scripts/shell.py 的测试套路：
  - 领域逻辑（WebApp 方法）与 HTTP 层分开测——直调 app.sessions() 等；
  - 壳用假实现隔离 sidecar（FakeShell），不真 spawn；
  - 历史读用真 JsonlSessionStore（tempfile 目录），不碰真实 .sessions/。
"""

import base64
import copy
import io
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts import native_dialog
from scripts.web_app import (
    MAX_BODY_BYTES,
    PUBLIC_DIR,
    ApprovalBoard,
    EventRing,
    WebApp,
    _Handler,
    _static_path,
    sse_frame,
)
from src.harness.jsonl_store import JsonlSessionStore
from src.harness.session import SessionRecord


class FakeShell:
    """够用的假壳：罐头清单/操作，send_to 记参数回罐头输出。"""

    def __init__(self, rows: list[dict], sid: str = "sess_0001") -> None:
        self.session_id = sid
        self._rows = rows
        self.sent: list[tuple[str, str]] = []
        self.ops: list[tuple[str, str]] = []
        self.zip_existed: bool | None = None   # set_workspace 那刻 zip 在不在盘上

    def sessions(self) -> dict:
        return {"sessions": copy.deepcopy(self._rows)}

    def new_session(self) -> str:
        self.ops.append(("new_session", ""))
        return "sess_0099"

    def close_session(self, sid: str = "") -> dict:
        self.ops.append(("close_session", sid))
        # closed 必须是布尔（真 sidecar 的契约，test_sidecar 断言过）——
        # 早先这里返回 sid，假实现替真实现圆谎，web_app 把布尔当 sid 拼
        # 进文案的 bug 就一路活到了浏览器上（toast 显示"已关闭 False"）。
        return {"status": "ok", "closed": True}

    def resume_session(self, sid: str) -> dict:
        self.ops.append(("resume_session", sid))
        return {"sessionId": sid, "generation": 2}

    def forget_session(self, sid: str = "") -> dict:
        self.ops.append(("forget_session", sid))
        return {"status": "ok"}

    def send_to(self, sid: str, message: str) -> dict:
        self.sent.append((sid, message))
        return {"output": f"echo:{message}", "status": "completed",
                "usage": {"prompt_tokens": 11, "completion_tokens": 7}}

    def status(self) -> dict:
        return {"sessions": len(self._rows), "handlers": 6,
                "modelCost": [{"tier": "craft", "calls": 1, "cost": 0.0003}]}

    def logs(self) -> str:
        return "[12:00:00] [sidecar] 罐头日志"

    def workspace(self) -> dict:
        return {"workspace": {"kind": "default", "id": "default", "root": "."}}

    def set_workspace(self, kind: str, path: str) -> dict:
        self.ops.append(("set_workspace", kind + ":" + path))
        self.zip_existed = Path(path).is_file()
        if kind == "zip" and not self.zip_existed:
            return {"error": "压缩包不在了"}
        return {"status": "ok",
                "workspace": {"kind": kind, "id": "ws1", "root": path}}

    def stop(self) -> None:
        pass


def _row(sid: str, status: str = "idle", live: bool = True) -> dict:
    return {"id": sid, "cwd": ".", "title": sid, "status": status,
            "mode": "craft", "runtimeGeneration": 1, "messages": 3,
            "lastError": None, "live": live}


class WebAppActionsTests(unittest.TestCase):
    """四操作翻译层：假 shell + 直调 app.action()。"""

    def setUp(self) -> None:
        self.shell = FakeShell([])
        self.app = WebApp(shell=self.shell)

    def test_sessions_passthrough(self) -> None:
        shell = FakeShell([_row("sess_0001"), _row("sess_0002", live=False)])
        app = WebApp(shell=shell)
        rows = app.sessions()["sessions"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["live"])
        self.assertFalse(rows[1]["live"])

    def test_create(self) -> None:
        result = self.app.action("create")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sessionId"], "sess_0099")
        self.assertEqual(self.shell.ops, [("new_session", "")])

    def test_close_resume_forget(self) -> None:
        close = self.app.action("close", "sess_0001")
        self.assertTrue(close["ok"])
        # 文案要点名会话，不能把 result["closed"] 那个布尔吐出来
        self.assertIn("sess_0001", close["detail"])
        self.assertNotIn("True", close["detail"])
        self.assertNotIn("False", close["detail"])
        self.assertTrue(self.app.action("resume", "sess_0001")["ok"])
        self.assertTrue(self.app.action("forget", "sess_0001")["ok"])
        self.assertEqual(self.shell.ops, [
            ("close_session", "sess_0001"),
            ("resume_session", "sess_0001"),
            ("forget_session", "sess_0001"),
        ])

    def test_action_error_from_shell_is_ok_false(self) -> None:
        shell = FakeShell([])

        def bad_resume(sid: str) -> dict:
            return {"error": f"live session {sid}"}
        shell.resume_session = bad_resume
        app = WebApp(shell=shell)
        result = app.action("resume", "sess_0001")
        self.assertFalse(result["ok"])
        self.assertIn("live session", result["detail"])

    def test_row_actions_refuse_empty_sid(self) -> None:
        """空 sid 必须在 web 层被拦住，**不落到壳里**。

        壳的 close/resume/forget 都是 `target = sid or self._sid`：空值会被
        静默当成"当前会话"。前端一旦漏传（真实发生过：按钮上没有 data-sid），
        "删除这一行"就变成"删除当前那个"，而且删完还弹成功——最难查的那种
        坏法。网页是多 tab 的，本层没有"当前会话"这个概念，所以这里硬拒。
        """

        for action in ("close", "resume", "forget"):
            with self.subTest(action=action):
                before = list(self.shell.ops)
                result = self.app.action(action, "")
                self.assertFalse(result["ok"])
                self.assertIn("会话 id", result["detail"])
                self.assertEqual(self.shell.ops, before)   # 没到壳那一步

    def test_row_actions_refuse_whitespace_sid(self) -> None:
        """空白 sid 同样拦——"看着有值但其实是空的"更难发现。"""

        result = self.app.action("forget", "   ")
        self.assertFalse(result["ok"])
        self.assertEqual(self.shell.ops, [])

    def test_create_needs_no_sid(self) -> None:
        """但 create 本来就不需要 sid，别把它一起拦掉。"""

        self.assertTrue(self.app.action("create")["ok"])
        self.assertEqual(self.shell.ops, [("new_session", "")])

    def test_unknown_action(self) -> None:
        result = self.app.action("fly")
        self.assertFalse(result["ok"])
        self.assertIn("未知操作", result["detail"])

    def test_status_passthrough_carries_cost_table(self) -> None:
        """状态直通：成本表就挂在 status 里，前端一个端点全拿到。"""

        status = self.app.status()
        self.assertEqual(status["handlers"], 6)
        self.assertEqual(status["modelCost"][0]["tier"], "craft")

    def test_send_passes_usage_through(self) -> None:
        """每轮 token 账要能穿到前端——它是 sidecar 从接缝读出来的。"""

        result = self.app.send("sess_0001", "你好")
        self.assertEqual(result["usage"]["prompt_tokens"], 11)
        self.assertEqual(result["usage"]["completion_tokens"], 7)

    def test_logs_passthrough(self) -> None:
        self.assertIn("罐头日志", self.app.logs()["logs"])

    def test_create_failure_translated_not_raised(self) -> None:
        """建会话失败 → {"ok": False, 人话}，不是 500 / 空响应。

        ＋ 按钮点了没反应是最糟的失败形态：用户不知道是坏了还是没点到。
        """

        shell = FakeShell([])

        def boom() -> str:
            raise RuntimeError("sidecar 没给出新会话 id")
        shell.new_session = boom
        result = WebApp(shell=shell).action("create")
        self.assertFalse(result["ok"])
        self.assertIn("新建会话失败", result["detail"])
        self.assertIn("sidecar", result["detail"])


def _zip_b64(entries: dict) -> str:
    """造一个内存里的 zip 并 base64 —— 测上传路径用（不碰磁盘）。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class WorkspaceApiTests(unittest.TestCase):
    """工作区 API（s11）：open 转发 / upload 落盘即删 / 错误翻译成人话。

    三个边界各有断言：路径空（不发车）、base64 坏（不发车）、壳报错
    （翻成 ok=False）——错在前端能看见的地方，别让它变成一个静默的
    "什么都没发生"。
    """

    def setUp(self) -> None:
        self.shell = FakeShell([])
        self.app = WebApp(shell=self.shell)

    def test_get_passthrough(self) -> None:
        self.assertIn("workspace", self.app.workspace())

    def test_open_forwards_dir_to_shell(self) -> None:
        """路径两头空白要被 strip 掉——手输路径最容易多带空格。"""

        result = self.app.set_workspace("open", path="  D:/proj  ")
        self.assertTrue(result["ok"])
        self.assertEqual(self.shell.ops, [("set_workspace", "dir:D:/proj")])
        self.assertIn("D:/proj", result["detail"])

    def test_open_without_path_does_not_reach_shell(self) -> None:
        result = self.app.set_workspace("open", path="   ")
        self.assertFalse(result["ok"])
        self.assertEqual(self.shell.ops, [])

    def test_unknown_action(self) -> None:
        result = self.app.set_workspace("fly")
        self.assertFalse(result["ok"])
        self.assertIn("未知的工作区操作", result["detail"])

    def test_shell_error_becomes_ok_false(self) -> None:
        def bad(kind: str, path: str) -> dict:
            return {"error": "不是可用的目录：X:/nope"}
        self.shell.set_workspace = bad
        result = self.app.set_workspace("open", path="X:/nope")
        self.assertFalse(result["ok"])
        self.assertIn("不是可用的目录", result["detail"])

    def test_upload_writes_temp_zip_then_deletes_it(self) -> None:
        """关键时序：**壳被调用的那一刻**包必须在盘上（子进程要读它），
        但 RPC 返回后必须已经删掉（上传包不留痕）。"""

        result = self.app.set_workspace(
            "upload", filename="proj.zip", data=_zip_b64({"a.txt": "hi"}))

        self.assertTrue(result["ok"])
        self.assertTrue(self.shell.zip_existed)
        kind, path = self.shell.ops[-1][1].split(":", 1)
        self.assertEqual(kind, "zip")
        self.assertFalse(Path(path).exists())

    def test_upload_rejects_bad_base64(self) -> None:
        result = self.app.set_workspace("upload", filename="p.zip",
                                        data="这不是 base64!!!")
        self.assertFalse(result["ok"])
        self.assertIn("base64", result["detail"])
        self.assertEqual(self.shell.ops, [])   # 没到壳那一步

    def test_upload_rejects_empty_payload(self) -> None:
        result = self.app.set_workspace("upload", filename="p.zip", data="")
        self.assertFalse(result["ok"])
        self.assertEqual(self.shell.ops, [])

    def test_reset_forwards_default_without_path(self) -> None:
        """复位发的是 kind=default + 空路径：前端本来就不知道项目根在哪，
        也不该假装知道——"默认"由后端定义。"""

        result = self.app.set_workspace("reset")

        self.assertTrue(result["ok"])
        self.assertEqual(self.shell.ops, [("set_workspace", "default:")])

    def test_reset_detail_does_not_dangle(self) -> None:
        """默认工作区可能不报 root（只有 kind）——别拼出"工作区已切到 "。"""

        def bare(kind: str, path: str) -> dict:
            return {"status": "ok", "workspace": {"kind": "default"}}
        self.shell.set_workspace = bare

        result = self.app.set_workspace("reset")

        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "已回到默认工作区")

    def test_switch_without_root_still_has_detail(self) -> None:
        """非 default 但没 root：给通用那句，不留半句。"""

        def bare(kind: str, path: str) -> dict:
            return {"status": "ok", "workspace": {"kind": "dir"}}
        self.shell.set_workspace = bare

        self.assertEqual(self.app.set_workspace("open", path="D:/p")["detail"],
                         "工作区已切换")

    # ── browse：后端弹系统选框（不用手输路径）─────────────────

    def test_browse_switches_to_picked_dir(self) -> None:
        """选中了就切过去——路径由后端拿到，前端一个字都不用填。"""

        with mock.patch("scripts.native_dialog.pick_directory",
                        return_value="D:/picked") as pick:
            result = self.app.set_workspace("browse")

        self.assertTrue(result["ok"])
        self.assertEqual(self.shell.ops, [("set_workspace", "dir:D:/picked")])
        self.assertIn("D:/picked", result["detail"])
        pick.assert_called_once()

    def test_browse_starts_from_current_workspace(self) -> None:
        """起点用当前工作区：已经在一个目录里干活，就别每次都从头翻。

        用真目录——实现会先 is_dir() 检查（给一个不存在的路径当起点，
        系统选框会弹到怪地方），这里要验的就是"真目录会被原样传下去"。
        """

        with tempfile.TemporaryDirectory() as real:
            self.shell.workspace = lambda: {
                "workspace": {"kind": "dir", "root": real}}
            with mock.patch("scripts.native_dialog.pick_directory",
                            return_value="D:/picked") as pick:
                self.app.set_workspace("browse")

        self.assertEqual(pick.call_args.kwargs.get("initial"), real)

    def test_browse_with_vanished_workspace_starts_from_home(self) -> None:
        """当前工作区已经不在了（比如删掉了）：别把死路径当起点传给系统框。"""

        self.shell.workspace = lambda: {
            "workspace": {"kind": "dir", "root": "D:/definitely-not-here"}}
        with mock.patch("scripts.native_dialog.pick_directory",
                        return_value="D:/picked") as pick:
            self.app.set_workspace("browse")

        self.assertEqual(pick.call_args.kwargs.get("initial"), "")

    def test_browse_cancelled_is_not_an_error(self) -> None:
        """取消不是错误：不该弹红字，也不该碰壳（什么都没发生）。"""

        with mock.patch("scripts.native_dialog.pick_directory", return_value=None):
            result = self.app.set_workspace("browse")

        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])       # 前端靠它决定"别弹红字"
        self.assertEqual(self.shell.ops, [])

    def test_browse_unavailable_asks_frontend_to_fall_back(self) -> None:
        """弹不出框是**环境限制**，不是用户的错——要能降级成手输，不能变死路。"""

        with mock.patch("scripts.native_dialog.pick_directory",
                        side_effect=native_dialog.DialogUnavailable("没有图形环境")):
            result = self.app.set_workspace("browse")

        self.assertFalse(result["ok"])
        self.assertEqual(result["fallback"], "manual")
        self.assertIn("没有图形环境", result["detail"])
        self.assertEqual(self.shell.ops, [])

    def test_browse_timeout_is_a_plain_error(self) -> None:
        """超时和"弹不出"要分开：超时不该被降级成手输（用户会莫名其妙）。"""

        with mock.patch("scripts.native_dialog.pick_directory",
                        side_effect=TimeoutError("等太久了")):
            result = self.app.set_workspace("browse")

        self.assertFalse(result["ok"])
        self.assertNotIn("fallback", result)
        self.assertIn("等太久了", result["detail"])


class MessagesReadTests(unittest.TestCase):
    """C 的灵魂：历史直读证据文件，不碰壳、不碰 RPC。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JsonlSessionStore(self.tmp.name)
        self.shell = FakeShell([])
        self.app = WebApp(shell=self.shell, store_root=self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _seed(self, sid: str, messages: list[dict]) -> None:
        record = SessionRecord(id=sid, cwd=self.tmp.name, messages=messages)
        self.store.create(record)

    def test_messages_read_from_evidence_file(self) -> None:
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
        self._seed("sess_0001", msgs)
        result = self.app.messages("sess_0001")
        self.assertEqual(result["messages"], msgs)

    def test_missing_session_yields_error_not_exception(self) -> None:
        result = self.app.messages("sess_does_not_exist")
        self.assertIn("error", result)

    def test_unsafe_id_rejected(self) -> None:
        result = self.app.messages("../../etc/passwd")
        self.assertIn("error", result)

    def test_closed_session_still_readable(self) -> None:
        # closed = 记录还在 store，证据文件在，一样能读
        self._seed("sess_0009", [{"role": "user", "content": "x"}])
        result = self.app.messages("sess_0009")
        self.assertEqual(result["messages"][0]["content"], "x")


class SendTests(unittest.TestCase):
    """发一轮：显式 sid 转发给壳。"""

    def test_send_forwards_sid_and_text(self) -> None:
        shell = FakeShell([])
        app = WebApp(shell=shell)
        result = app.send("sess_0003", "你好")
        self.assertEqual(result["output"], "echo:你好")
        self.assertEqual(shell.sent, [("sess_0003", "你好")])

    def test_send_blank_message_rejected(self) -> None:
        app = WebApp(shell=FakeShell([]))
        self.assertIn("error", app.send("sess_0003", "   "))


class EventRingTests(unittest.TestCase):
    def test_append_after_only_returns_newer(self) -> None:
        ring = EventRing()
        s1 = ring.append("tool_start", {"name": "a"})
        s2 = ring.append("tool_end", {"content": "b"})
        got, latest = ring.after(s1)   # seq > s1：只有第二条
        self.assertEqual([g["seq"] for g in got], [s2])
        self.assertEqual(got[0]["event"], "tool_end")
        self.assertEqual(latest, s2)
        got_all, _ = ring.after(0)
        self.assertEqual([g["seq"] for g in got_all], [s1, s2])

    def test_maxlen_drops_oldest(self) -> None:
        ring = EventRing(maxlen=3)
        for i in range(5):
            ring.append("e", {"i": i})
        got, latest = ring.after(0)
        self.assertEqual([g["i"] for g in got], [2, 3, 4])
        self.assertEqual(latest, 5)


class EventRingWaitTests(unittest.TestCase):
    """wait_for = "没事件就睡、有事件就醒"——SSE 推送的心脏。"""

    def test_returns_immediately_when_events_already_there(self) -> None:
        ring = EventRing()
        seq = ring.append("tool_start", {"name": "now"})
        start = time.monotonic()
        got, latest = ring.wait_for(0, timeout=5)
        self.assertEqual([g["seq"] for g in got], [seq])
        self.assertEqual(latest, seq)
        self.assertLess(time.monotonic() - start, 0.5)   # 根本没睡

    def test_times_out_empty_when_nothing_happens(self) -> None:
        """超时返回空列表。断言写成区间而不是"≥ 0.15 秒"——系统定时器
        精度会让 wait(0.15) 在 0.14999830 秒回来，卡着下限比会被这种
        微秒级误差判失败。真正要钉的是"睡了（不是空转）且按时回来
        （不是死等）"。
        """

        ring = EventRing()
        start = time.monotonic()
        got, latest = ring.wait_for(0, timeout=0.15)
        delta = time.monotonic() - start
        self.assertEqual(got, [])
        self.assertEqual(latest, 0)
        self.assertGreater(delta, 0.05)     # 确实睡下去了，不是立即返回
        self.assertLess(delta, 1.0)         # 在超时附近回来，没有死等

    def test_wakes_up_on_append(self) -> None:
        """另一个线程 append，睡着的 wait_for 要立刻醒——不是等睡满。"""

        ring = EventRing()
        woke = []

        def waiter():
            got, _ = ring.wait_for(0, timeout=5)
            woke.append(got)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)                      # 让 waiter 真的睡下去
        start = time.monotonic()
        ring.append("tool_end", {"content": "x"})
        thread.join(timeout=2)
        self.assertEqual([g["event"] for g in woke[0]], ["tool_end"])
        self.assertLess(time.monotonic() - start, 1.0)

    def test_wake_all_releases_waiters(self) -> None:
        """收尾用：没事件也要把睡着的线程叫起来（不然关服务卡心跳）。"""

        ring = EventRing()
        released = []

        def waiter():
            released.append(ring.wait_for(0, timeout=10)[0])

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        start = time.monotonic()
        ring.wake_all()
        thread.join(timeout=2)
        self.assertEqual(released, [[]])
        self.assertLess(time.monotonic() - start, 1.0)


class SseFramingTests(unittest.TestCase):
    """SSE 帧编码 + 生成器——不碰 HTTP 就能测，所以 HTTP 层只剩搬字节。"""

    def test_frame_carries_id_and_one_data_line(self) -> None:
        frame = sse_frame({"seq": 7, "event": "tool_start", "name": "now"})
        self.assertTrue(frame.startswith("id: 7\n"))
        self.assertTrue(frame.endswith("\n\n"))
        lines = frame.split("\n")
        self.assertEqual(lines[0], "id: 7")            # 断线续传的游标
        self.assertTrue(lines[1].startswith("data: "))
        # data 行绝不能有裸换行：多行内容必须被 JSON 转义进一行
        self.assertEqual(len([line for line in lines if line]), 2)
        payload = json.loads(lines[1][len("data: "):])
        self.assertEqual(payload["event"], "tool_start")
        self.assertEqual(payload["seq"], 7)

    def test_frame_keeps_non_ascii_readable(self) -> None:
        frame = sse_frame({"seq": 1, "event": "tool_end", "content": "紫色雪花"})
        self.assertIn("紫色雪花", frame)               # ensure_ascii=False
        self.assertNotIn("\\u", frame)

    def test_stream_yields_events_then_stops_on_flag(self) -> None:
        """生成器契约：先吐事件，竖 stopping 后下一轮就退，不等心跳睡满。

        用 next() 手动推生成器（而不是 list()）——这样才测得到"竖旗发生
        在两次 yield 之间"这个真实时序：SSE 长连是一帧一帧吐的，退出
        只能在帧与帧之间被观察到。
        """

        app = WebApp(shell=FakeShell([]))
        app.ring.append("tool_start", {"name": "now"})
        app.ring.append("tool_end", {"content": "ok"})

        stream = app.event_stream(0, heartbeat=0.05)
        first = next(stream)
        second = next(stream)
        self.assertIn('"name": "now"', first)
        self.assertTrue(first.startswith("id: 1\n"))
        self.assertTrue(second.startswith("id: 2\n"))

        app.stopping = True
        with self.assertRaises(StopIteration):
            next(stream)

    def test_stream_emits_heartbeat_when_idle(self) -> None:
        app = WebApp(shell=FakeShell([]))
        stream = app.event_stream(0, heartbeat=0.05)
        first = next(stream)
        self.assertEqual(first, ": ping\n\n")   # 静默期先来一发保活

    def test_stream_resumes_from_last_id(self) -> None:
        """断线重连：Last-Event-ID 之后的事件才推，老的绝不重放。"""

        app = WebApp(shell=FakeShell([]))
        app.ring.append("e1", {})
        app.ring.append("e2", {})
        app.ring.append("e3", {})

        stream = app.event_stream(1, heartbeat=0.05)
        heads = [next(stream).split("\n")[0], next(stream).split("\n")[0]]
        self.assertEqual(heads, ["id: 2", "id: 3"])
        app.stopping = True
        with self.assertRaises(StopIteration):
            next(stream)


class ApprovalBoardTests(unittest.TestCase):
    """审批 = Event 栅栏：线程等回执，HTTP 线程 set；超时 fail-closed。"""

    def test_request_respond_roundtrip(self) -> None:
        ring = EventRing()
        board = ApprovalBoard(ring, timeout=5)
        results: list[bool] = []
        t = threading.Thread(
            target=lambda: results.append(board.request("rule_x", "why")))
        t.start()
        ticket = None
        for _ in range(200):          # 等 request 把审批事件进环
            lst, _ = ring.after(0)
            if lst:
                ticket = lst[0]["request_id"]
                break
            time.sleep(0.01)
        self.assertIsNotNone(ticket)
        self.assertEqual(ring.after(0)[0][0]["rule_id"], "rule_x")
        self.assertTrue(board.respond(ticket, True))
        t.join(timeout=2)
        self.assertEqual(results, [True])
        self.assertEqual(board.pending_count(), 0)

    def test_timeout_returns_false_fail_closed(self) -> None:
        board = ApprovalBoard(timeout=0.1)
        self.assertFalse(board.request("rule_x", "谁都不理它"))
        self.assertEqual(board.pending_count(), 0)

    def test_respond_unknown_ticket(self) -> None:
        board = ApprovalBoard(timeout=0.1)
        self.assertFalse(board.respond("no-such-ticket", True))


# ── 以下两类测试未被合流保留（2026-09-15）─────────────────────
# 原 s11 那条线里放的是 EventRingSubscribeTests / _SseStream /
# SseEndpointTests，测的是它的 EventRing 用「订阅队列 + 哨兵」式的
# 实时推流实现（subscribe / unsubscribe / subscriber_count）。
#
# 合流时选了 master 那套 Condition 式实现（wait_for / wake_all），
# 因为 Vue 3 前端与 master 自己的 EventRingTests / EventRingWaitTests /
# SseFramingTests 都是按它写的。两套机制只留一套，所以对应的单测
# 也就只留一份——留着另一份会指向已不存在的 API，永远是红的。
#
# 若将来要换回订阅式实现，找抢救包：
#   D:////React////wywd-harness-salvage-20260915////tests////test_web_app.py/n


class StaticServingTests(unittest.TestCase):
    def test_index_html_exists_and_servable(self) -> None:
        target = _static_path("/")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "index.html")
        self.assertTrue(target.exists())
        self.assertIsNotNone(_static_path("/index.html"))

    def test_directory_traversal_rejected(self) -> None:
        self.assertIsNone(_static_path("/../.git/config"))
        self.assertIsNone(_static_path("/..%2f..%2fsecret"))

    def test_frontend_assets_all_resolve(self) -> None:
        """index.html 里引用的每个静态资源都必须能被静态服务解析。

        s07-c 起前端是 Vue 的免构建布局（/src/*.js + /vendor/vue…js），
        入口文件与静态根之间的链子全靠字符串引用连着。这条测试把那条
        链子钉住：换目录/改文件名后自动跟着走，断链在测试里就现形，
        不用等浏览器 404。
        """

        html = (PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
        # 同时命中 <script src="/src/main.js"> 和 import map 里的
        # "vue": "/vendor/vue.esm-browser.prod.js"
        refs = re.findall(r'"(/[^"\s]+\.(?:js|css))"', html)
        self.assertTrue(refs, "index.html 里没找到 /…js 引用，测试本身失效了")
        for ref in refs:
            target = _static_path(ref)
            self.assertIsNotNone(target, f"{ref} 解析不到 public/ 下的文件")
            self.assertTrue(target.exists(), f"{ref} 指向的文件不存在")


class WorkspaceHttpTests(unittest.TestCase):
    """HTTP 层：/api/workspace 的路由 + 请求体上限是真拦在前面的。

    请求体上限是安全边界（**先读再判 = 把内存交给客户端控制**），
    它的价值就在"读到 body 之前早退"——不真发一次请求测不到这一点。
    """

    def setUp(self) -> None:
        self.shell = FakeShell([])
        self.app = WebApp(shell=self.shell)
        handler = type("_TestHandler", (_Handler,), {"app": self.app})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.app.ring.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _post(self, path: str, payload: dict,
              ui_header: bool = False) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if ui_header:
            headers["X-Wywd-Ui"] = "1"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:      # 4xx 走这条路
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_post_open_reaches_shell(self) -> None:
        status, payload = self._post(
            "/api/workspace", {"action": "open", "path": "D:/proj"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.shell.ops, [("set_workspace", "dir:D:/proj")])

    def test_get_workspace(self) -> None:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/workspace", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertIn("workspace", payload)

    def test_browse_without_ui_header_is_rejected(self) -> None:
        """browse 会在用户桌面上弹系统对话框——不能让任意网页触发它。

        跨站表单发不出自定义头（要发就得先过 CORS 预检，本服务不答预检），
        所以"必须有 X-Wywd-Ui"就足够挡住。缺头时必须在**到达业务逻辑前**被拒，
        否则恶意页面照样能把框弹出来（哪怕读不到响应）。
        """

        with mock.patch("scripts.native_dialog.pick_directory") as pick:
            status, payload = self._post("/api/workspace", {"action": "browse"})

        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        pick.assert_not_called()                   # 关键：根本没去弹框
        self.assertEqual(self.shell.ops, [])

    def test_browse_with_ui_header_reaches_app(self) -> None:
        with mock.patch("scripts.native_dialog.pick_directory",
                        return_value="D:/picked"):
            status, payload = self._post(
                "/api/workspace", {"action": "browse"}, ui_header=True)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.shell.ops, [("set_workspace", "dir:D:/picked")])

    def test_other_actions_do_not_need_ui_header(self) -> None:
        """只有 browse 需要那个头——别的动作别顺手一起拦掉（会白挂）。"""

        status, payload = self._post(
            "/api/workspace", {"action": "reset"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_static_files_are_not_cached(self) -> None:
        """开发期禁缓存：改了前端文件必须立刻生效，否则"后端修好了、
        你还看到旧界面"，白折腾一轮。

        合流（2026-09-15）修：原来取的是 /app.js——那是 s11 那条线的
        vanilla 前端。master 换成 Vue 3 之后 app.js 已被删除，改成
        public/src/ 下的模块文件。断言的行为本身没变（Cache-Control
        = no-store），只是取样文件跟着前端换了。
        """

        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/src/main.js", timeout=5) as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_oversize_body_is_rejected_without_reading_it(self) -> None:
        """只发头部（声称超限）、不发 body——服务端必须当场 413。

        这条测的正是"早退"：若实现是"先读后判"，它会卡在这里等 body，
        客户端等不到响应（超时），测试就会红。
        """

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall((
                "POST /api/workspace HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
            ).encode("ascii"))
            head = sock.recv(65536).decode("utf-8", errors="replace")
        finally:
            sock.close()

        self.assertIn("413", head.split("\r\n")[0])
        self.assertEqual(self.shell.ops, [])       # 没碰到壳


class FakeRevivableShell(FakeShell):
    """能假装死活、并记录 start/stop 的假壳——revive_shell 的靶子。"""

    def __init__(self, alive: bool) -> None:
        super().__init__([])
        self._alive = alive
        self.started: list[bool] = []
        self.stopped = 0

    def is_alive(self) -> bool:
        return self._alive

    def start(self, create_session: bool = True) -> dict:
        self.started.append(create_session)
        self._alive = True
        return {"status": "ok"}

    def stop(self) -> None:
        self.stopped += 1
        self._alive = False


class ShellReviveTests(unittest.TestCase):
    """sidecar 退出后的自愈。

    背景（2026-09-15 现场）：sidecar 一死，所有走 RPC 的端点在 handler 里抛
    ConnectionClosed → 这条请求的连接被直接关掉、一个字节响应都没有 → 浏览器
    看到的是 "Failed to fetch"，用户以为**整个后端没起**。而 web_app 自己还活着，
    所以"重启服务"不该是用户必须做的动作。
    """

    def _app(self, shell, factory) -> WebApp:
        return WebApp(shell=shell, store_root=tempfile.mkdtemp(),
                      shell_factory=factory)

    def test_live_shell_is_left_alone(self) -> None:
        shell = FakeRevivableShell(alive=True)
        made: list = []
        app = self._app(shell, lambda: made.append(1) or shell)

        self.assertFalse(app.revive_shell())
        self.assertIs(app.shell, shell)
        self.assertEqual(made, [])          # 活着就不该造新壳
        self.assertEqual(shell.stopped, 0)

    def test_dead_shell_is_replaced(self) -> None:
        old = FakeRevivableShell(alive=False)
        fresh = FakeRevivableShell(alive=True)
        app = self._app(old, lambda: fresh)

        self.assertTrue(app.revive_shell())
        self.assertIs(app.shell, fresh)
        self.assertEqual(old.stopped, 1)           # 半死的壳被收干净
        self.assertEqual(fresh.started, [False])   # 沿用懒建口径：启动不建会话

    def test_shell_without_probe_is_untouched(self) -> None:
        """没 is_alive 的壳（测试里的 FakeShell 就是）原样放行，行为不变。"""

        shell = FakeShell([])
        app = self._app(shell, lambda: self.fail("不该造新壳"))
        self.assertFalse(app.revive_shell())
        self.assertIs(app.shell, shell)


if __name__ == "__main__":
    unittest.main()