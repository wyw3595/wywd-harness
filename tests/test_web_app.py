"""C 方案 web_app 的单测：直调处理函数 + 假 shell 注入（沿用既有套路）。

scripts/sidecar_panel.py / scripts/shell.py 的测试套路：
  - 领域逻辑（WebApp 方法）与 HTTP 层分开测——直调 app.sessions() 等；
  - 壳用假实现隔离 sidecar（FakeShell），不真 spawn；
  - 历史读用真 JsonlSessionStore（tempfile 目录），不碰真实 .sessions/。
"""

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.web_app import ApprovalBoard, EventRing, WebApp, _static_path
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
        return {"status": "ok", "closed": sid or self.session_id}

    def resume_session(self, sid: str) -> dict:
        self.ops.append(("resume_session", sid))
        return {"sessionId": sid, "generation": 2}

    def forget_session(self, sid: str = "") -> dict:
        self.ops.append(("forget_session", sid))
        return {"status": "ok"}

    def send_to(self, sid: str, message: str) -> dict:
        self.sent.append((sid, message))
        return {"output": f"echo:{message}", "status": "completed"}

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
        self.assertTrue(self.app.action("close", "sess_0001")["ok"])
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

    def test_real_public_file(self) -> None:
        target = _static_path("/app.js")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "app.js")


if __name__ == "__main__":
    unittest.main()