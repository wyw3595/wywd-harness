"""C 方案 web_app 的单测：直调处理函数 + 假 shell 注入（沿用既有套路）。

scripts/sidecar_panel.py / scripts/shell.py 的测试套路：
  - 领域逻辑（WebApp 方法）与 HTTP 层分开测——直调 app.sessions() 等；
  - 壳用假实现隔离 sidecar（FakeShell），不真 spawn；
  - 历史读用真 JsonlSessionStore（tempfile 目录），不碰真实 .sessions/。
"""

import copy
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.web_app import (
    PUBLIC_DIR,
    ApprovalBoard,
    EventRing,
    WebApp,
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


if __name__ == "__main__":
    unittest.main()