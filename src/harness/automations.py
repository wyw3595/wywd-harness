"""s22 Automation Scheduler：到点自动跑，不需要人推。

核心机制（教材同款）：
  - **RRULE**（RFC 5545 的教学子集）：FREQ=HOURLY/DAILY/WEEKLY/MONTHLY/
    YEARLY + INTERVAL；once 类不走 RRULE，给 scheduled_at。
  - **tick 循环**：查到期（ACTIVE 且 next_run 空/≤now）→ 隔离会话执行
    → 记 run 历史 → 更新运行时状态 → 算下一次。
  - **软删除**：status='deleted'，行保留（NEVER DELETE FROM）。
  - **run 历史**：每次执行 started/completed/status/output 落库——
    "定时任务直接调模型但不记输入输出，后续无法解释"是误区清单第一条。

本项目的取舍（模块头三句话）：
  - 错过执行**不补跑**：recurring 的下一次从**执行时刻**起算（anchor
    on run time）——停机两天重启后补跑 48 个日报是灾难，跳过才对。
  - runner/clock 全注入：内核不认识 sidecar 也不认识墙钟，测试给假
    时钟与假 runner，离线可测到任意时间点。
  - **自动化过审批闸门的方式与手动一样**：隔离会话用的还是同一套
    registry/policy/approver——需要审批的工具在无人值守时等不到人，
    超时 BLOCKED、如实入 transcript（自动化的权限风险不比手动低，
    不能绕过权限系统——教材误区清单）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from uuid import uuid4

_ALLOWED_FREQ = frozenset({"HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"})


class AutomationError(ValueError):
    """RRULE / 规格校验的人话异常。"""


def parse_rrule(rrule: str) -> dict:
    """FREQ=DAILY;INTERVAL=2 → {"freq": "DAILY", "interval": 2}。

    未知 FREQ / 非法 INTERVAL 构造期就炸——调度规则错一个字，任务
    就在错误的时间跑，宁可早炸。
    """

    parts = {}
    for chunk in str(rrule or "").upper().split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            parts[key.strip()] = value.strip()
    freq = parts.get("FREQ", "")
    if freq not in _ALLOWED_FREQ:
        raise AutomationError(
            f"不认识的 FREQ={freq!r}——可选 {'、'.join(sorted(_ALLOWED_FREQ))}")
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError as error:
        raise AutomationError(f"INTERVAL 不是整数：{parts.get('INTERVAL')!r}") from error
    if interval < 1:
        raise AutomationError("INTERVAL 必须 ≥ 1")
    return {"freq": freq, "interval": interval}


def next_run(rrule: str, after: datetime) -> datetime:
    """下次运行时刻（教材同款简化：MONTHLY=30 天、YEARLY=365 天）。"""

    spec = parse_rrule(rrule)
    step = {
        "HOURLY": timedelta(hours=spec["interval"]),
        "DAILY": timedelta(days=spec["interval"]),
        "WEEKLY": timedelta(weeks=spec["interval"]),
        "MONTHLY": timedelta(days=30 * spec["interval"]),
        "YEARLY": timedelta(days=365 * spec["interval"]),
    }[spec["freq"]]
    return after + step


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class TickReport:
    """一次 tick 的执行报告（日志/测试断言用）。"""

    automation_id: str
    status: str        # success / failed
    output: str
    next_run: Optional[str]


Runner = Callable[[dict], str]
Clock = Callable[[], datetime]


class AutomationScheduler:
    """tick 驱动的调度器：状态全在 SQLite，本类无自己的账本。"""

    def __init__(self, db, runner: Runner,
                 clock: Optional[Clock] = None) -> None:
        self._db = db
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def tick(self) -> list[TickReport]:
        """检查到期并执行。返回本轮执行报告（空表 = 没有到期）。

        once 类跑完转 PAUSED（next_run=None 不再到期）；recurring 的
        下一次从**本轮执行时刻**起算——错过的不补，跳过。
        """

        now = self._clock()
        reports: list[TickReport] = []
        for spec in self._db.due_automations(_iso(now)):
            started = _iso(self._clock())
            run_id = self._db.record_run_start(spec["id"], started)
            status, output = "success", ""
            try:
                output = str(self._runner(spec))
            except Exception as error:      # 失败也是历史，不是异常
                status, output = "failed", f"{type(error).__name__}: {error}"
            self._db.record_run_end(run_id, _iso(self._clock()),
                                    status, output)
            if spec["schedule_type"] == "once":
                self._db.set_automation_status(spec["id"], "PAUSED")
                upcoming = None
            else:
                upcoming = _iso(next_run(spec["rrule"], now))
            self._db.upsert_runtime_state(
                spec["id"], started, upcoming, status)
            reports.append(TickReport(spec["id"], status, output, upcoming))
        return reports


def new_automation_id() -> str:
    return f"auto_{uuid4().hex[:10]}"
