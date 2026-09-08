"""s09 JSONL 证据存储的离线单测：SessionTranscript 校验策略 + Store 协议落地。

全部用 tempfile 目录，不碰真实 .sessions/；"重启"用"新开一个 store 实例
指向同一目录"模拟——这正是 JsonlSessionStore 存在的意义。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.harness.jsonl_store import (
    JsonlSessionStore,
    SessionTranscript,
    TranscriptCorruptionError,
    TranscriptValidationError,
)
from src.harness.session import (
    SessionLifecycleError,
    SessionManager,
    SessionNotFoundError,
    SessionRecord,
)


def _record(sid: str = "sess_0001", **kw) -> SessionRecord:
    """造一个测试用 Record；cwd 随便给（校验是 Manager 的事，store 只存）。"""

    base = {"id": sid, "cwd": ".", "mode": "craft", "title": "测试会话"}
    base.update(kw)
    return SessionRecord(**base)


class SessionTranscriptTests(unittest.TestCase):
    """证据文件的三道防线：信封完整 / 保留字拒收 / 损坏策略。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sess_0001.jsonl"

    def _transcript(self) -> SessionTranscript:
        return SessionTranscript(self.path, "sess_0001")

    def test_envelope_fields_complete_and_sequential(self) -> None:
        t = self._transcript()
        t.append({"type": "record", "status": "idle"})
        t.append({"type": "record", "status": "running"})
        events = t.read_events()
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(events[0]["event_id"], "transcript:sess_0001:1")
        self.assertEqual(events[1]["event_id"], "transcript:sess_0001:2")
        self.assertTrue(events[0]["recorded_at"])           # 时间戳在场
        self.assertEqual(events[0]["session_id"], "sess_0001")
        self.assertEqual(events[0]["schema_version"], 1)

    def test_append_rejects_reserved_envelope_fields(self) -> None:
        t = self._transcript()
        with self.assertRaises(TranscriptValidationError):
            t.append({"type": "record", "sequence": 99})    # 信封字段只归 store 管

    def test_partial_tail_ignored_and_flagged(self) -> None:
        """写到一半崩了（无换行的坏尾巴）：放过并报告，不炸。"""

        t = self._transcript()
        t.append({"type": "record", "status": "idle"})
        with open(self.path, "ab") as f:
            f.write(b'{"type":"record","stat')   # 半行，无换行
        events = t.read_events()
        self.assertEqual(len(events), 1)
        self.assertTrue(t.ignored_partial_tail)

    def test_complete_bad_line_raises(self) -> None:
        """带换行的完整坏行 = 不是崩溃现场，是证据损坏：fail-closed。"""

        t = self._transcript()
        t.append({"type": "record", "status": "idle"})
        with open(self.path, "ab") as f:
            f.write(b"definitely not json\n")    # 完整一行 + 换行
        with self.assertRaises(TranscriptCorruptionError):
            t.read_events()

    def test_sequence_gap_raises(self) -> None:
        t = self._transcript()
        t.append({"type": "record", "status": "idle"})
        gap = json.dumps({
            "schema_version": 1, "sequence": 3,          # 跳过了 2
            "recorded_at": "2026-09-08T00:00:00+00:00",
            "session_id": "sess_0001",
            "event_id": "transcript:sess_0001:3",
            "type": "record", "status": "running",
        }) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(gap)
        with self.assertRaises(TranscriptCorruptionError):
            t.read_events()

    def test_foreign_session_id_raises(self) -> None:
        t = self._transcript()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "schema_version": 1, "sequence": 1,
                "recorded_at": "2026-09-08T00:00:00+00:00",
                "session_id": "sess_9999",               # 不是这个文件的主人
                "event_id": "transcript:sess_9999:1",
                "type": "record",
            }) + "\n")
        with self.assertRaises(TranscriptCorruptionError):
            t.read_events()


class JsonlSessionStoreTests(unittest.TestCase):
    """SessionStore 协议的 JSONL 落地：快照进、事件流出、fold 重建。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.store = JsonlSessionStore(self.root)

    def test_create_load_roundtrip(self) -> None:
        record = _record()
        self.store.create(record)
        loaded = self.store.load("sess_0001")
        self.assertEqual(loaded.id, "sess_0001")
        self.assertEqual(loaded.cwd, ".")
        self.assertEqual(loaded.mode, "craft")
        self.assertEqual(loaded.title, "测试会话")
        self.assertEqual(loaded.status, record.status)    # creating（默认值）
        self.assertEqual(loaded.runtime_generation, 1)
        self.assertIsNone(loaded.last_error)
        self.assertEqual(loaded.messages, [])

    def test_save_appends_only_new_messages(self) -> None:
        record = _record()
        self.store.create(record)
        m1 = [{"role": "user", "content": "任务甲"}]
        m2 = m1 + [{"role": "assistant", "content": "回:任务甲"}]
        record.messages = m1
        self.store.save(record)
        record.messages = m2
        self.store.save(record)
        self.assertEqual(self.store.load("sess_0001").messages, m2)
        # 证据只增不改：create(record) + msg + record + msg + record = 5 行
        lines = (Path(self.root) / "sess_0001.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 5)

    def test_fold_takes_latest_record_event(self) -> None:
        """状态迁移在证据里留痕，fold 取最后一条 = 最新状态。"""

        record = _record()
        self.store.create(record)                    # creating
        record.status = "idle"
        self.store.save(record)
        record.status = "running"
        self.store.save(record)
        record.status = "closed"
        self.store.save(record)
        self.assertEqual(self.store.load("sess_0001").status, "closed")
        events = SessionTranscript(
            Path(self.root) / "sess_0001.jsonl", "sess_0001").read_events()
        record_events = [e for e in events if e["type"] == "record"]
        self.assertEqual([e["status"] for e in record_events],
                         ["creating", "idle", "running", "closed"])

    def test_save_rejects_prefix_rewrite(self) -> None:
        """append-only 的执法者：已落盘的消息前缀被改写 → fail-closed。"""

        record = _record()
        self.store.create(record)
        record.messages = [{"role": "user", "content": "任务甲"}]
        self.store.save(record)
        record.messages = [{"role": "user", "content": "被篡改的历史"}]
        with self.assertRaises(TranscriptCorruptionError):
            self.store.save(record)

    def test_create_duplicate_raises(self) -> None:
        self.store.create(_record())
        with self.assertRaises(SessionLifecycleError):
            self.store.create(_record())

    def test_load_and_save_missing_raise(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.store.load("sess_9999")
        with self.assertRaises(SessionNotFoundError):
            self.store.save(_record("sess_9999"))

    def test_persistence_across_instances(self) -> None:
        """"重启"的等价物：新 store 实例指向同一目录，记录原样回来。"""

        record = _record()
        self.store.create(record)
        record.status = "idle"
        record.messages = [{"role": "user", "content": "任务甲"}]
        self.store.save(record)
        restarted = JsonlSessionStore(self.root)     # 新进程、同一磁盘
        loaded = restarted.load("sess_0001")
        self.assertEqual(loaded.status, "idle")
        self.assertEqual(loaded.messages[0]["content"], "任务甲")
        self.assertEqual([r.id for r in restarted.list()], ["sess_0001"])

    def test_list_sorted_and_delete_removes_file(self) -> None:
        self.store.create(_record("sess_0002"))
        self.store.create(_record("sess_0001"))
        self.assertEqual([r.id for r in self.store.list()],
                         ["sess_0001", "sess_0002"])  # sorted：断言稳定
        self.assertTrue(self.store.delete("sess_0001"))
        self.assertFalse(self.store.delete("sess_0001"))  # 幂等：再删 False
        self.assertFalse((Path(self.root) / "sess_0001.jsonl").exists())
        self.assertEqual([r.id for r in self.store.list()], ["sess_0002"])

    def test_unsafe_session_id_rejected(self) -> None:
        """sid 进文件名 = 路径穿越面；store 是协议端口，必须自查。"""

        for evil in ("../evil", "a/b", ".."):
            with self.assertRaises(SessionLifecycleError):
                self.store.load(evil)


class ManagerWithJsonlStoreTests(unittest.TestCase):
    """s09 的招牌语义：Manager 换一代（= 换进程），resume 还能接着聊。"""

    def test_resume_after_store_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def runner(message: str, history: list[dict]):
                messages = history + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": f"回:{message}"},
                ]
                return f"回:{message}", messages

            # 第一代进程：建会话、聊一轮、关掉
            manager1 = SessionManager(runner, JsonlSessionStore(tmp))
            sid = manager1.create_session(cwd=tmp)
            manager1.get_session(sid).run_turn("任务甲")
            manager1.close_session(sid)

            # "重启"：全新 Manager + 全新 store 实例，同一磁盘目录
            manager2 = SessionManager(runner, JsonlSessionStore(tmp))
            manager2.resume_session(sid)
            proc2 = manager2.get_session(sid)
            proc2.run_turn("任务乙")
            contents = [m["content"] for m in proc2.messages]
            self.assertIn("任务甲", contents)   # 跨"进程"的记忆延续
            self.assertIn("回:任务甲", contents)

            # 计数器接续：s07 的换代共享 store 续号，现在跨重启也成立
            self.assertEqual(manager2.create_session(cwd=tmp), "sess_0002")


if __name__ == "__main__":
    unittest.main()
