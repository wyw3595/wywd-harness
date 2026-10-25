"""s21 SQLite Database 的离线单元测试：WAL × 迁移 × 软删除 × 聚合 × 接线。

核心断言群：
  - PRAGMA 真的开了（WAL 是并发读写的前提，不是装饰）；
  - 迁移可重放（重开库不再应用、版本号入库）；
  - 软删除：行还在磁盘上，默认查询看不见；
  - 外键开着：用量挂在未知会话上当场炸（fail-closed）；
  - 聚合正确：月度前缀过滤、SUM/GROUP BY 排行；
  - sidecar 挂钩点：会话镜像、轮级用量、工具账、forget 软删除、
    stats/summary 查询面——走真实 turn 路径（执行器线程 + 事件广播）。

Windows 注：TemporaryDirectory 清理对未关闭的 SQLite 连接会 WinError 32
（文件被占用）——所有库句柄走 _open_db 上下文强制 close；sidecar 测试
的服务端线程生命周期超出测试控制，追加 ignore_cleanup_errors 兜底
（真正的泄漏由显式 close 的内核测试兜住）。
"""

import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import build_default_policy
from src.harness.sidecar import SidecarServer
from src.harness.sqlite_store import HarnessDB, SCHEMA_VERSION
from src.harness.tools import Tool, ToolRegistry


def _db(tmp: str) -> HarnessDB:
    return HarnessDB(Path(tmp) / "harness.db")


@contextlib.contextmanager
def _open_db(tmp: str):
    """带强制关闭的库句柄——连接不关，Windows 下临时目录清理就炸。"""

    db = _db(tmp)
    try:
        yield db
    finally:
        db.close()


def _session(sid: str, **overrides) -> dict:
    payload = {"id": sid, "cwd": "/w", "title": f"会话 {sid}",
               "status": "idle", "mode": "craft",
               "created_at": "2026-10-25T10:00:00Z",
               "updated_at": "2026-10-25T10:00:00Z"}
    payload.update(overrides)
    return payload


# ═══════════════════════════════════════════════════════════════
# 一、库本身：PRAGMA × 迁移 × 重开
# ═══════════════════════════════════════════════════════════════

class DatabaseTests(unittest.TestCase):

    def test_pragmas_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                mode = db._conn.execute(
                    "PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
                self.assertEqual(db.schema_version(), SCHEMA_VERSION)
                tables = {row["name"] for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("sessions", tables)
                self.assertIn("usage_tracking", tables)
                self.assertIn("tool_usage", tables)
                self.assertIn("migration_meta", tables)
            # 重开：迁移幂等（版本不倒退、不重复应用）。
            with _open_db(tmp) as reopened:
                self.assertEqual(reopened.schema_version(), SCHEMA_VERSION)

    def test_concurrent_writes_are_serialized(self) -> None:
        # 挂钩点在执行器线程/事件广播线程——一把锁串行化写，并发
        # 记账不丢行。
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session("s1"))
                threads = [threading.Thread(
                    target=lambda i=i: db.record_usage(
                        "s1", "craft", input_tokens=i, output_tokens=1,
                        total_tokens=i + 1))
                    for i in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(db.session_usage("s1")["calls"], 8)


# ═══════════════════════════════════════════════════════════════
# 二、会话索引：upsert × 排序 × 软删除
# ═══════════════════════════════════════════════════════════════

class SessionIndexTests(unittest.TestCase):

    def test_upsert_orders_by_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session(
                    "a", updated_at="2026-10-01T00:00:00Z"))
                db.upsert_session(_session(
                    "b", updated_at="2026-10-25T00:00:00Z"))
                db.upsert_session(_session(
                    "c", updated_at="2026-10-10T00:00:00Z"))
                # 再 upsert a：updated_at 变最新 → 排到最前，且行数不涨。
                db.upsert_session(_session(
                    "a", updated_at="2026-10-26T00:00:00Z"))
                listing = db.list_sessions()
                self.assertEqual([row["id"] for row in listing],
                                 ["a", "b", "c"])

    def test_soft_delete_keeps_row_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session("gone"))
                db.soft_delete_session("gone")
                self.assertEqual(db.list_sessions(), [])    # 默认看不见
                included = db.list_sessions(include_deleted=True)
                self.assertEqual(included[0]["id"], "gone")
                self.assertEqual(included[0]["status"], "deleted")
                # 行真的还在表里——软删除不是 DELETE FROM。
                raw = db._conn.execute(
                    "SELECT COUNT(*) FROM sessions").fetchone()[0]
                self.assertEqual(raw, 1)


# ═══════════════════════════════════════════════════════════════
# 三、用量与工具：聚合 × 外键
# ═══════════════════════════════════════════════════════════════

class UsageTests(unittest.TestCase):

    def test_session_usage_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session("s1"))
                db.record_usage("s1", "craft", input_tokens=100,
                                output_tokens=40, total_tokens=140)
                db.record_usage("s1", "craft", input_tokens=50,
                                output_tokens=10, total_tokens=60)
                totals = db.session_usage("s1")
                self.assertEqual(totals["calls"], 2)
                self.assertEqual(totals["input_tokens"], 150)
                self.assertEqual(totals["output_tokens"], 50)
                self.assertEqual(totals["total_tokens"], 200)

    def test_monthly_prefix_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session("s1"))
                db.record_usage("s1", "craft", input_tokens=10,
                                total_tokens=10,
                                created_at="2026-09-30T23:59:59Z")
                db.record_usage("s1", "craft", input_tokens=20,
                                total_tokens=20,
                                created_at="2026-10-01T00:00:01Z")
                self.assertEqual(
                    db.usage_summary("2026-09")["input_tokens"], 10)
                self.assertEqual(
                    db.usage_summary("2026-10")["input_tokens"], 20)
                self.assertEqual(
                    db.usage_summary()["input_tokens"], 30)

    def test_usage_requires_real_session(self) -> None:
        # 外键开着：挂在未知会话上的账当场炸——统计不许有孤儿行。
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.record_usage("ghost", "craft")

    def test_top_tools_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _open_db(tmp) as db:
                db.upsert_session(_session("s1"))
                for _ in range(3):
                    db.record_tool_call("s1", "fs_read")
                for _ in range(2):
                    db.record_tool_call("s1", "bash")
                db.record_tool_call("s1", "calc")
                ranking = db.top_tools(3)
                self.assertEqual(
                    [(row["tool_name"], row["calls"]) for row in ranking],
                    [("fs_read", 3), ("bash", 2), ("calc", 1)])
                # 排行按次数降序；bash 与 fs_read 同为 3 次，平票按
                # 名字升序——排行是确定性的。
                db.record_tool_call("s1", "calc")
                db.record_tool_call("s1", "bash")
                self.assertEqual(
                    [row["tool_name"] for row in db.top_tools(3)],
                    ["bash", "fs_read", "calc"])


# ═══════════════════════════════════════════════════════════════
# 四、sidecar 挂钩点：真实 turn 路径入账
# ═══════════════════════════════════════════════════════════════

class SidecarMetricsTests(unittest.TestCase):
    """服务端线程（执行器/事件）生命周期超出测试控制——临时目录用
    ignore_cleanup_errors 兜 OS 级清理竞态；连接泄漏由内核测试兜住。"""

    def _server(self, tmp, model) -> tuple[SidecarServer, HarnessDB, list]:
        registry = ToolRegistry()
        registry.register(Tool(
            name="echo", description="测试工具",
            handler=lambda text="": f"回声：{text}"))
        db = _db(tmp)
        server = SidecarServer(
            model=model, registry=registry,
            policy=build_default_policy(safe_tools=("echo",)),
            metrics_db=db)
        events: list[dict] = []
        server._on_event = lambda event, data: events.append(
            {"event": event, **data})
        return server, db, events

    def _wait(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)

    def test_turn_records_usage_and_tool_call(self) -> None:
        scripted = ScriptedModel([
            ModelReply(kind="tool_calls",
                       tool_calls=[ToolCall("c1", "echo", {"text": "hi"})],
                       usage={"prompt_tokens": 100, "completion_tokens": 20,
                              "total_tokens": 120}),
            ModelReply(kind="final", text="done",
                       usage={"prompt_tokens": 150, "completion_tokens": 30,
                              "total_tokens": 180}),
        ])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            server, db, events = self._server(tmp, scripted)
            sid = server._handle_session_create({})["sessionId"]
            server._handle_agent_send({"sessionId": sid, "message": "跑"})

            self._wait(lambda: any(e["event"] == "turn_end"
                                   for e in events))
            # 记账在 turn_end 广播之后——轮询等它落库。
            self._wait(lambda: db.session_usage(sid)["calls"] == 1)
            totals = db.session_usage(sid)
            # 轮级汇总是 run_agent 跨 step 累加的 totals：
            # (100+20+120) + (150+30+180) → prompt 250 / total 300。
            self.assertEqual(totals["total_tokens"], 300)
            self.assertEqual(totals["input_tokens"], 250)
            # 工具账：tool_end 事件顺手记了一笔（落库有先后，轮询防空表）。
            self._wait(lambda: db.top_tools(1)
                       and db.top_tools(1)[0]["calls"] == 1)
            self.assertEqual(db.top_tools(1)[0]["tool_name"], "echo")
            # 会话镜像在。
            self.assertEqual(db.list_sessions()[0]["id"], sid)
            db.close()

    def test_forget_soft_deletes_in_index(self) -> None:
        scripted = ScriptedModel(
            [ModelReply(kind="final", text="ok")])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            server, db, _ = self._server(tmp, scripted)
            sid = server._handle_session_create({})["sessionId"]
            self.assertEqual(len(db.list_sessions()), 1)
            server._handle_session_close({"sessionId": sid})
            server._handle_session_forget({"sessionId": sid})
            self.assertEqual(db.list_sessions(), [])    # 查询面消失
            self.assertEqual(                            # 磁盘还在
                db.list_sessions(include_deleted=True)[0]["status"],
                "deleted")
            db.close()

    def test_stats_summary_rpc(self) -> None:
        scripted = ScriptedModel(
            [ModelReply(kind="final", text="ok",
                        usage={"prompt_tokens": 10,
                               "completion_tokens": 5,
                               "total_tokens": 15})])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            server, db, _ = self._server(tmp, scripted)
            sid = server._handle_session_create({})["sessionId"]
            server._handle_agent_send({"sessionId": sid, "message": "跑"})
            self._wait(lambda: db.session_usage(sid)["calls"] == 1)
            summary = server._handle_stats_summary(
                {"month": time.strftime("%Y-%m")})
            self.assertTrue(summary["enabled"])
            self.assertEqual(summary["total"]["total_tokens"], 15)
            self.assertIn("monthly", summary)  # 给了 month 就有月度视图
            db.close()

        # 未启用的装配：明确说"没有"，而不是给一串零。
        bare = SidecarServer(model=None, registry=ToolRegistry(),
                             policy=build_default_policy())
        self.assertEqual(bare._handle_stats_summary({})["enabled"], False)


if __name__ == "__main__":
    unittest.main()
