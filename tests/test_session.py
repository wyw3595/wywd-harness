"""s07 session 生命周期的可测核心：Record/Store 分离、状态机、四操作。

全离线：turn_runner 用假剧本（不碰模型、不碰网络）；并发场景用
threading.Thread + Event 精确编排 close 竞态（不靠 sleep 碰运气）。
"""

import json
import threading
import unittest
from pathlib import Path

from src.harness.session import (
    DEFAULT_TITLE,
    MODE_ASK,
    InMemorySessionStore,
    SessionAlreadyRunningError,
    SessionLifecycleError,
    SessionManager,
    SessionNotFoundError,
    SessionProcess,
    SessionRecord,
    SessionState,
)

# create/resume 的 cwd 校验要求"真实存在的目录"——tests/ 目录本身最省事
TESTS_CWD = str(Path(__file__).resolve().parent)


class SwitchableRunner:
    """可切换的假 turn_runner：fail=True 就爆炸，否则追加两条消息返回。

    签名 = src.harness.session.TurnRunner：
    (message, history) -> (output, 完整的新 messages)
    """

    def __init__(self, reply: str = "好的") -> None:
        self.reply = reply
        self.fail = False
        self.calls: list[tuple[str, int]] = []

    def __call__(self, message: str, history: list[dict]):
        self.calls.append((message, len(history)))
        if self.fail:
            raise RuntimeError("provider 爆炸")
        return self.reply, history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": self.reply},
        ]


def make_manager(store=None, runner=None):
    """造一套 (manager, store, runner)，测试各取所需。"""

    runner = runner or SwitchableRunner()
    store = store or InMemorySessionStore()
    return SessionManager(turn_runner=runner, store=store), store, runner


class SessionStateTests(unittest.TestCase):
    """str 混血 Enum 的行为锁死：枚举成员就是字符串。"""

    def test_str_mixin_equality_and_json(self) -> None:
        self.assertEqual(SessionState.IDLE, "idle")
        self.assertTrue(SessionState.CLOSED == "closed")
        # json.dumps 直接出字符串，不用 .value——进 Record/Store/JSON 零转换
        self.assertEqual(
            json.dumps({"status": SessionState.IDLE}), '{"status": "idle"}'
        )


class SessionRecordTests(unittest.TestCase):
    """Record 是"恢复事实"：不允许混进任何运行时资源。"""

    def test_record_holds_no_runtime_resources(self) -> None:
        record = SessionRecord(id="sess_0001", cwd=TESTS_CWD)
        forbidden = ("lock", "thread", "server", "port", "event", "runner",
                     "client", "socket", "process")
        for word in forbidden:
            self.assertFalse(
                any(word in name for name in vars(record)),
                f"Record 不该持有运行时资源字段: {word}",
            )

    def test_summary_is_ui_safe(self) -> None:
        record = SessionRecord(
            id="sess_0001", cwd=TESTS_CWD,
            messages=[{"role": "user", "content": "hi"}],
        )
        summary = record.summary()
        self.assertEqual(
            set(summary.keys()),
            {"id", "cwd", "title", "status", "mode", "runtimeGeneration",
             "messages", "lastError"},
        )
        self.assertEqual(summary["messages"], 1)       # 只有条数，不是列表本身
        self.assertEqual(summary["status"], "creating")
        self.assertEqual(summary["runtimeGeneration"], 1)


class InMemorySessionStoreTests(unittest.TestCase):
    """存取边界 deepcopy：调用方改自己那份，store 不受渗透。"""

    def test_create_load_roundtrip_defensive_copy(self) -> None:
        store = InMemorySessionStore()
        record = SessionRecord(id="sess_0001", cwd=TESTS_CWD)
        store.create(record)

        record.status = "closed"                        # 改原件 → store 不动
        self.assertEqual(store.load("sess_0001").status, "creating")

        loaded = store.load("sess_0001")
        loaded.messages.append({"role": "user", "content": "渗透测试"})
        self.assertEqual(store.load("sess_0001").messages, [])  # 改副本也不动

    def test_create_duplicate_and_missing(self) -> None:
        store = InMemorySessionStore()
        store.create(SessionRecord(id="sess_0001", cwd=TESTS_CWD))
        with self.assertRaises(SessionLifecycleError):
            store.create(SessionRecord(id="sess_0001", cwd=TESTS_CWD))
        with self.assertRaises(SessionNotFoundError):
            store.load("sess_9999")
        with self.assertRaises(SessionNotFoundError):
            store.save(SessionRecord(id="sess_9999", cwd=TESTS_CWD))

    def test_save_list_delete(self) -> None:
        store = InMemorySessionStore()
        record = SessionRecord(id="sess_0001", cwd=TESTS_CWD)
        store.create(record)
        record.status = "idle"
        store.save(record)
        self.assertEqual(store.load("sess_0001").status, "idle")

        listed = store.list()
        self.assertEqual(len(listed), 1)
        listed[0].status = "closed"                     # list 也是副本
        self.assertEqual(store.load("sess_0001").status, "idle")

        self.assertTrue(store.delete("sess_0001"))
        self.assertFalse(store.delete("sess_0001"))     # 再删一次：False 不炸


class SessionProcessTests(unittest.TestCase):
    """一代运行时：start / run_turn / abort / close 的状态机行为。"""

    def setUp(self) -> None:
        self.sink = InMemorySessionStore()               # 借它当回写收件箱
        self.record = SessionRecord(id="sess_0001", cwd=TESTS_CWD)
        self.sink.create(self.record)
        self.runner = SwitchableRunner()
        self.proc = SessionProcess(self.record, self.sink.save, self.runner)

    def test_start_publishes_idle(self) -> None:
        self.assertEqual(self.proc.status, "creating")
        self.proc.start()
        self.assertEqual(self.proc.status, "idle")
        self.assertEqual(self.sink.load("sess_0001").status, "idle")

    def test_start_twice_rejected(self) -> None:
        self.proc.start()
        with self.assertRaises(SessionLifecycleError):
            self.proc.start()

    def test_turn_roundtrip_back_to_idle(self) -> None:
        self.proc.start()
        output = self.proc.run_turn("你好")
        self.assertEqual(output, "好的")
        self.assertEqual(self.proc.status, "idle")
        self.assertEqual(len(self.proc.messages), 2)     # user + assistant
        self.assertIsNone(self.proc.record.last_error)
        self.assertEqual(self.sink.load("sess_0001").messages, self.proc.messages)

    def test_first_user_message_becomes_title(self) -> None:
        # 第一句话顶掉默认标题（侧边栏列表的辨识度来源）：超长截 30 字、
        # 只认第一句、随 RUNNING 迁移一起落盘；显式起过名的不碰。
        self.proc.start()
        self.proc.run_turn("一" * 40)                    # 超长 → 截 30 字
        self.assertEqual(self.proc.record.title, "一" * 30)
        self.assertEqual(self.sink.load("sess_0001").title, "一" * 30)
        self.proc.run_turn("第二句不该改标题")
        self.assertEqual(self.proc.record.title, "一" * 30)  # 只认第一句
        named = SessionRecord(id="sess_0002", cwd=TESTS_CWD, title="季度复盘")
        self.sink.create(named)
        proc2 = SessionProcess(named, self.sink.save, self.runner)
        proc2.start()
        proc2.run_turn("随便问点啥")
        self.assertEqual(named.title, "季度复盘")        # 显式命名不覆盖
        self.assertEqual(SessionRecord(id="x", cwd=TESTS_CWD).title,
                         DEFAULT_TITLE)                  # 默认值本身没变

    def test_status_is_running_during_turn(self) -> None:
        seen = {}
        proc = self.proc

        def probe(message, history):                     # 闭包晚绑定：调用时 proc 已存在
            seen["status"] = proc.status
            return "ok", history + [{"role": "assistant", "content": "ok"}]

        proc._turn_runner = probe
        proc.start()
        proc.run_turn("hi")
        self.assertEqual(seen["status"], "running")

    def test_concurrent_turn_rejected_not_queued(self) -> None:
        started, release = threading.Event(), threading.Event()

        def blocker(message, history):
            started.set()
            release.wait(timeout=5)
            return "慢答", history + [{"role": "assistant", "content": "慢答"}]

        self.proc._turn_runner = blocker
        self.proc.start()
        thread = threading.Thread(target=self.proc.run_turn, args=("第一条",))
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        with self.assertRaises(SessionLifecycleError):   # 第二个 turn 被拒，不排队
            self.proc.run_turn("第二条")
        release.set()
        thread.join(timeout=5)
        self.assertEqual(self.proc.status, "idle")       # 第一个 turn 正常收尾

    def test_turn_rejected_when_not_idle_and_lock_released(self) -> None:
        # creating 状态直接发问 → 拒；连拒两次（证明 turn_lock 在失败路径也释放了）
        with self.assertRaises(SessionLifecycleError):
            self.proc.run_turn("还没 start")
        with self.assertRaises(SessionLifecycleError):
            self.proc.run_turn("还没 start 第二次")

    def test_failure_marks_error_with_last_error(self) -> None:
        self.runner.fail = True
        self.proc.start()
        with self.assertRaises(RuntimeError):
            self.proc.run_turn("你好")
        self.assertEqual(self.proc.status, "error")
        self.assertIn("provider 爆炸", self.proc.record.last_error)
        # error 态可以 close（error → closing/closed 合法）
        self.proc.close()
        self.assertEqual(self.proc.status, "closed")

    def test_close_keeps_transcript_and_is_idempotent(self) -> None:
        self.proc.start()
        self.proc.run_turn("你好")
        self.proc.close()
        self.assertEqual(self.proc.status, "closed")
        self.assertEqual(len(self.proc.messages), 2)     # transcript 留着
        self.proc.close()                                # 幂等：第二次不炸
        self.assertEqual(self.proc.status, "closed")
        self.assertEqual(len(self.proc.messages), 2)

    def test_abort_sets_signal(self) -> None:
        self.assertFalse(self.proc._abort_requested.is_set())
        self.proc.abort()
        self.assertTrue(self.proc._abort_requested.is_set())


class LateCommitRaceTests(unittest.TestCase):
    """本课最精妙的竞态：close 完成后晚到的 turn 结果不能改写记录。"""

    def test_late_result_cannot_overwrite_closed(self) -> None:
        store = InMemorySessionStore()
        record = SessionRecord(id="sess_0001", cwd=TESTS_CWD)
        store.create(record)
        started, release = threading.Event(), threading.Event()

        def blocker(message, history):
            started.set()
            release.wait(timeout=5)                      # turn 卡在这里
            return "迟到的话", history + [
                {"role": "assistant", "content": "迟到的话"},
            ]

        proc = SessionProcess(record, store.save, blocker)
        proc.start()

        outcome: dict = {}

        def target() -> None:
            try:
                outcome["output"] = proc.run_turn("你好")
            except Exception as exc:                     # 测试要接住一切异常
                outcome["error"] = exc

        thread = threading.Thread(target=target)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        proc.close()                                     # turn 还卡着，运行时已关
        release.set()
        thread.join(timeout=5)

        self.assertIsInstance(outcome.get("error"), SessionLifecycleError)
        self.assertEqual(proc.status, "closed")          # 关了就是关了
        self.assertEqual(proc.record.messages, [])       # 迟到结果没落盘
        self.assertIsNone(proc.record.last_error)        # close 竞态不算错误


class SessionManagerTests(unittest.TestCase):
    """控制面四操作 + 计数器续号 + 共享 store 的 Manager 换代。"""

    def test_create_returns_sequential_ids_and_starts_runtime(self) -> None:
        manager, _, _ = make_manager()
        sid1 = manager.create_session(TESTS_CWD)
        sid2 = manager.create_session(TESTS_CWD, mode=MODE_ASK)
        self.assertEqual(sid1, "sess_0001")
        self.assertEqual(sid2, "sess_0002")
        self.assertEqual(manager.load_record(sid2).mode, "ask")
        self.assertEqual(manager.load_record(sid1).status, "idle")
        self.assertTrue(manager.get_session(sid1) is not None)   # live
        self.assertTrue(manager.list_sessions()[0]["live"])

    def test_counter_continues_across_managers(self) -> None:
        manager1, store, _ = make_manager()
        manager1.create_session(TESTS_CWD)
        manager1.create_session(TESTS_CWD)
        manager2, _, _ = make_manager(store=store)       # 新 Manager 读同一 store
        self.assertEqual(manager2.create_session(TESTS_CWD), "sess_0003")

    def test_turn_via_manager_runtime(self) -> None:
        manager, _, runner = make_manager()
        sid = manager.create_session(TESTS_CWD)
        runtime = manager.get_session(sid)
        output = runtime.run_turn("hi")
        self.assertEqual(output, "好的")
        self.assertEqual(runner.calls, [("hi", 0)])      # 冷启动：空历史起步

    def test_close_then_resume_new_generation(self) -> None:
        manager, _, _ = make_manager()
        sid = manager.create_session(TESTS_CWD)
        manager.get_session(sid).run_turn("第一问")
        self.assertTrue(manager.close_session(sid))
        record = manager.load_record(sid)
        self.assertEqual(record.status, "closed")
        self.assertEqual(len(record.messages), 2)        # transcript 留着

        self.assertEqual(manager.resume_session(sid), sid)
        runtime = manager.get_session(sid)
        self.assertEqual(runtime.record.runtime_generation, 2)   # 新一代
        self.assertEqual(runtime.status, "idle")
        self.assertEqual(len(runtime.messages), 2)       # 历史跟过来了

    def test_resume_rejects_when_already_live(self) -> None:
        manager, _, _ = make_manager()
        sid = manager.create_session(TESTS_CWD)
        with self.assertRaises(SessionAlreadyRunningError):
            manager.resume_session(sid)

    def test_manager_replacement_shared_store_resume(self) -> None:
        """教材的核心证明：v1 关停后，v2 从共享 store 接着恢复。"""
        manager1, store, _ = make_manager()
        sid = manager1.create_session(TESTS_CWD)
        manager1.get_session(sid).run_turn("第一问")
        manager1.shutdown_all()                          # 只关 runtime
        self.assertEqual(manager1.load_record(sid).status, "closed")

        manager2, _, _ = make_manager(store=store)
        manager2.resume_session(sid)
        runtime = manager2.get_session(sid)
        self.assertEqual(runtime.record.runtime_generation, 2)
        self.assertEqual(len(runtime.messages), 2)

    def test_shutdown_all_keeps_all_records(self) -> None:
        manager, store, _ = make_manager()
        manager.create_session(TESTS_CWD)
        manager.create_session(TESTS_CWD)
        manager.shutdown_all()
        self.assertEqual(len(store.list()), 2)           # 记录全在
        for record in store.list():
            self.assertEqual(record.status, "closed")
        self.assertFalse(manager.list_sessions()[0]["live"])

    def test_close_session_idempotent_returns_false_second_time(self) -> None:
        manager, _, _ = make_manager()
        sid = manager.create_session(TESTS_CWD)
        self.assertTrue(manager.close_session(sid))
        self.assertFalse(manager.close_session(sid))     # 已关：False 不炸
        manager.close_session(sid)                       # 第三次也行
        self.assertEqual(manager.load_record(sid).status, "closed")

    def test_forget_requires_close_then_deletes(self) -> None:
        manager, store, _ = make_manager()
        sid = manager.create_session(TESTS_CWD)
        with self.assertRaises(SessionLifecycleError):   # live 不许 forget
            manager.forget_session(sid)
        manager.close_session(sid)
        self.assertTrue(manager.forget_session(sid))
        self.assertEqual(store.list(), [])
        self.assertFalse(manager.forget_session(sid))    # 再 forget：False
        with self.assertRaises(SessionNotFoundError):
            manager.load_record(sid)

    def test_stale_running_zombie_can_close_then_resume(self) -> None:
        """崩溃现场：记录标着 running，进程早没了——running 不是存活证明。"""
        store = InMemorySessionStore()
        zombie = SessionRecord(id="sess_0009", cwd=TESTS_CWD)
        zombie.status = SessionState.RUNNING             # 模拟崩溃残留
        store.create(zombie)

        manager, _, _ = make_manager(store=store)
        self.assertFalse(manager.close_session("sess_0009"))   # 没有 live 可关
        self.assertEqual(manager.load_record("sess_0009").status, "closed")
        manager.resume_session("sess_0009")              # 僵尸复活成新一代
        self.assertEqual(
            manager.get_session("sess_0009").record.runtime_generation, 2)
        self.assertEqual(manager.create_session(TESTS_CWD), "sess_0010")  # 续号

    def test_error_session_close_then_resume_clears_last_error(self) -> None:
        manager, _, runner = make_manager()
        sid = manager.create_session(TESTS_CWD)
        runner.fail = True
        with self.assertRaises(RuntimeError):
            manager.get_session(sid).run_turn("你好")
        self.assertEqual(manager.load_record(sid).status, "error")

        manager.close_session(sid)
        runner.fail = False
        manager.resume_session(sid)
        record = manager.load_record(sid)
        self.assertIsNone(record.last_error)             # 新一代不带旧伤
        output = manager.get_session(sid).run_turn("再来")
        self.assertEqual(output, "好的")

    def test_create_validates_cwd_and_mode(self) -> None:
        manager, _, _ = make_manager()
        with self.assertRaises(OSError):                 # resolve(strict=True)：不存在即抛
            manager.create_session(str(Path(TESTS_CWD) / "no_such_dir_xyz"))
        with self.assertRaises(ValueError):
            manager.create_session(TESTS_CWD, mode="party")


if __name__ == "__main__":
    unittest.main()
