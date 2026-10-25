"""s22 自动化调度的离线单元测试：RRULE × tick × 软删除 × 隔离执行。

假时钟 + 假 runner 驱动调度器到任意时间点；sidecar 集成走真实
run_turn 路径（ScriptedModel）。临时目录同 s21 的 Windows 纪律。
"""

import contextlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.harness.automations import (
    AutomationError,
    AutomationScheduler,
    new_automation_id,
    next_run,
    parse_rrule,
)
from src.harness.models import ModelReply, ScriptedModel
from src.harness.permissions import build_default_policy
from src.harness.sidecar import SidecarServer
from src.harness.sqlite_store import HarnessDB
from src.harness.tools import ToolRegistry


@contextlib.contextmanager
def _open_db(tmp):
    db = HarnessDB(Path(tmp) / "h.db")
    try:
        yield db
    finally:
        db.close()


def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)


class RruleTests(unittest.TestCase):

    def test_parse_and_next(self) -> None:
        self.assertEqual(parse_rrule("FREQ=DAILY;INTERVAL=2"),
                         {"freq": "DAILY", "interval": 2})
        base = _at("2026-10-25T08:00:00Z")
        self.assertEqual(next_run("FREQ=HOURLY;INTERVAL=6", base),
                         base + timedelta(hours=6))
        self.assertEqual(next_run("FREQ=DAILY", base), base + timedelta(days=1))
        self.assertEqual(next_run("FREQ=WEEKLY;INTERVAL=2", base),
                         base + timedelta(weeks=2))

    def test_invalid_rules_fail_closed(self) -> None:
        with self.assertRaises(AutomationError):
            parse_rrule("FREQ=MINUTELY")        # 不在白名单
        with self.assertRaises(AutomationError):
            parse_rrule("FREQ=DAILY;INTERVAL=0")
        with self.assertRaises(AutomationError):
            parse_rrule("FREQ=DAILY;INTERVAL=x")


class SchedulerTests(unittest.TestCase):

    def _setup(self, tmp, clock, runner=lambda spec: f"跑了 {spec['name']}"):
        db = HarnessDB(Path(tmp) / "h.db")
        scheduler = AutomationScheduler(db, runner, clock=clock)
        return db, scheduler

    def test_once_runs_once_then_pauses(self) -> None:
        now = {"t": _at("2026-10-25T09:00:00Z")}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, scheduler = self._setup(tmp, lambda: now["t"])
            db.create_automation({"id": "a1", "name": "日报", "prompt": "p",
                                  "schedule_type": "once",
                                  "scheduled_at": "2026-10-25T08:00:00Z"})
            # 按真实 create 路径种初始状态（next_run=scheduled_at）。
            db.upsert_runtime_state("a1", None,
                                    "2026-10-25T08:00:00Z", None)
            reports = scheduler.tick()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].status, "success")
            # once 跑完转 PAUSED：再 tick（时间不变）不再执行。
            self.assertEqual(scheduler.tick(), [])
            # run 历史一条，可审计。
            history = db.automation_history("a1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "success")
            db.close()

    def test_recurring_anchors_on_execution_time(self) -> None:
        # 停机"两天"后恢复：错过的不补——只跑一次，下一次从现在起算。
        now = {"t": _at("2026-10-27T09:00:00Z")}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, scheduler = self._setup(tmp, lambda: now["t"])
            db.create_automation({"id": "a1", "name": "日报", "prompt": "p",
                                  "schedule_type": "recurring",
                                  "rrule": "FREQ=DAILY"})
            db.upsert_runtime_state("a1", None,
                                    "2026-10-25T09:00:00Z", None)
            reports = scheduler.tick()
            self.assertEqual(len(reports), 1)              # 没有补跑 2 次
            self.assertEqual(reports[0].next_run,
                             "2026-10-28T09:00:00Z")       # 从执行时刻起算
            db.close()

    def test_failed_runner_records_history(self) -> None:
        now = {"t": _at("2026-10-25T09:00:00Z")}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, scheduler = self._setup(
                tmp, lambda: now["t"],
                runner=lambda spec: (_ for _ in ()).throw(RuntimeError("炸了")))
            db.create_automation({"id": "a1", "name": "x", "prompt": "p",
                                  "schedule_type": "once",
                                  "scheduled_at": "2026-10-25T08:00:00Z"})
            db.upsert_runtime_state("a1", None,
                                    "2026-10-25T08:00:00Z", None)
            reports = scheduler.tick()
            self.assertEqual(reports[0].status, "failed")
            self.assertIn("炸了", db.automation_history("a1")[0]["output"])
            db.close()

    def test_soft_deleted_and_paused_not_due(self) -> None:
        now = {"t": _at("2026-10-25T09:00:00Z")}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, scheduler = self._setup(tmp, lambda: now["t"])
            db.create_automation({"id": "a1", "name": "x", "prompt": "p",
                                  "schedule_type": "recurring",
                                  "rrule": "FREQ=DAILY"})
            db.set_automation_status("a1", "deleted")       # 软删除
            db.create_automation({"id": "a2", "name": "y", "prompt": "p",
                                  "schedule_type": "recurring",
                                  "rrule": "FREQ=DAILY"})
            db.set_automation_status("a2", "PAUSED")
            self.assertEqual(scheduler.tick(), [])          # 都不到期
            # 软删除的行还在（includeDeleted 看得见）。
            ids = {a["id"] for a in db.list_automations(
                include_deleted=True)}
            self.assertIn("a1", ids)
            db.close()


class SidecarAutomationTests(unittest.TestCase):

    def test_full_rail_create_tick_delete(self) -> None:
        scripted = ScriptedModel([ModelReply(kind="final", text="日报完成")])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            db = HarnessDB(Path(tmp) / "h.db")
            server = SidecarServer(
                model=scripted, registry=ToolRegistry(),
                policy=build_default_policy(), metrics_db=db)
            # 注册一个已到期的 once 任务。
            past = (datetime.now(timezone.utc)
                    - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            created = server._handle_automation_create({
                "name": "日报", "prompt": "生成昨日进展日报",
                "schedule_type": "once", "scheduled_at": past})
            self.assertIn("automationId", created)
            listing = server._handle_automation_list({})
            self.assertEqual(listing["automations"][0]["name"], "日报")
            # 手动 tick：走真实 run_turn（隔离会话 + ScriptedModel）。
            reports = server._scheduler.tick()
            self.assertEqual(reports[0].status, "success")
            self.assertIn("日报完成", reports[0].output)
            self.assertEqual(len(db.automation_history(
                created["automationId"])), 1)
            # once 跑完 PAUSED；软删除后清单不见、行还在。
            self.assertEqual(server._scheduler.tick(), [])
            server._handle_automation_delete(
                {"automationId": created["automationId"]})
            self.assertEqual(server._handle_automation_list(
                {})["automations"], [])
            self.assertEqual(len(server._handle_automation_list(
                {"includeDeleted": True})["automations"]), 1)
            db.close()

    def test_bad_create_params_are_actionable(self) -> None:
        scripted = ScriptedModel([ModelReply(kind="final", text="x")])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            db = HarnessDB(Path(tmp) / "h.db")
            server = SidecarServer(
                model=scripted, registry=ToolRegistry(),
                policy=build_default_policy(), metrics_db=db)
            bad = server._handle_automation_create({
                "name": "x", "prompt": "p", "schedule_type": "recurring",
                "rrule": "FREQ=MINUTELY"})
            self.assertIn("rrule 不合法", bad["error"])
            once = server._handle_automation_create({
                "name": "x", "prompt": "p", "schedule_type": "once"})
            self.assertIn("scheduled_at", once["error"])
            db.close()


if __name__ == "__main__":
    unittest.main()
