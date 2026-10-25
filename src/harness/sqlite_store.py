"""s21 SQLite Database：会话要持久，用量要追踪——索引、状态与统计的仓库。

============================================================
📚 为什么需要它（JSONL 与 SQLite 的分工）
============================================================
JSONL 是**证据**：append-only 的事件流，谁都不能改，重放即真相。
但它答不了"这个月花了多少 token""哪个工具用得最多""会话列表按
最近排序"——这类问题要遍历全部文件逐行扫。

SQLite 是**索引、状态与统计**：一张表一个查询面，`SUM`/`GROUP BY`
/`ORDER BY` 都是现成的。两者分工（教材铁律）：

    JSONL 保存事件流（真相，不可变）
    SQLite 保存索引、状态和统计（派生，可重建）

桌面应用选 SQLite 而不是外部数据库服务的理由：一个 .db 文件、
零配置、随应用启动可用。但默认 journal 模式下写会锁全库、读被
阻塞——sidecar 在写用量统计的同时 web 还要读会话列表，所以 WAL：

    PRAGMA journal_mode=WAL      写入先进 -wal 日志，不阻塞读
    PRAGMA synchronous=NORMAL    WAL 下足够安全且更快
    PRAGMA foreign_keys=ON       用量必须挂在真实会话上

============================================================
📚 两条安全纪律（教材同款）
============================================================
1. **软删除**：永远不用 `DELETE FROM`——forget 在本层只把 status
   标成 'deleted'，行还在磁盘上。索引层丢数据没有Undo。
2. **migration_meta**：schema 演进先记账（版本号入库），每个迁移
   一个事务——没有它，"这个库是哪个版本的 schema"只能靠猜。

============================================================
📚 与教材（learn-workbuddy/s21）的差异——都是刻意的
============================================================
- 教材 7 张表；本项目先落 4 张（migration_meta + sessions +
  usage_tracking + tool_usage）——automations 三张表是 s22 的课，
  机制位（迁移框架）先建好，到时候加表就是加一个迁移条目。
- 教材的 sessions/messages 表兼做消息存储；本项目的消息**只在
  JSONL**（证据不搬家），sessions 表是元数据索引（列表查询不重放
  转录）。大文本不进 SQLite——"把所有大文本塞 SQLite 会让数据库
  膨胀且难迁移"正是教材的误区清单第一条。
- cost 列保留（schema 与教材对齐）但当前记 0.0：成本核算在
  router 层（价格表在那边），本层只记 token 原始量——成本可由
  token 后算，反之不行。
- 会话的权威仍是 JSONL + SessionManager；本层是只增不删的投影，
  两个真源不打架的方式是**方向单一**：sidecar 往这写，查询面
  从这读，谁都不从这里回写会话状态。

完成标志：tests/test_sqlite_store.py 全部通过。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 2

# 迁移账本：(版本, 建表 SQL)。每个版本一个事务；已应用的跳过。
# 加表/加列 = 追加条目，不改历史——迁移的可重放性全靠这个纪律。
_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            cwd         TEXT NOT NULL,
            title       TEXT,
            status      TEXT NOT NULL,
            mode        TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage_tracking (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT NOT NULL REFERENCES sessions(id),
            model           TEXT NOT NULL,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            created_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES sessions(id),
            tool_name   TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_session
            ON usage_tracking(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_usage_session
            ON tool_usage(session_id);
    """),
    # s22 自动化调度：定义（软删除）+ 运行时状态 + 执行历史。
    (2, """
        CREATE TABLE IF NOT EXISTS automations (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            schedule_type   TEXT NOT NULL,
            rrule           TEXT,
            scheduled_at    TEXT,
            status          TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS automation_runtime_state (
            automation_id   TEXT PRIMARY KEY REFERENCES automations(id),
            last_run        TEXT,
            next_run        TEXT,
            last_status     TEXT
        );
        CREATE TABLE IF NOT EXISTS automation_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            automation_id   TEXT NOT NULL REFERENCES automations(id),
            started_at      TEXT NOT NULL,
            completed_at    TEXT,
            status          TEXT NOT NULL,
            output          TEXT
        );
    """),
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class HarnessDB:
    """SQLite 索引/统计仓库：WAL、迁移、软删除、聚合查询。

    线程说明：sqlite3 默认 check_same_thread=True；sidecar 的挂钩点
    （turn 执行器线程 / 事件广播）不在主线程——统一开
    check_same_thread=False + 一把锁串行化写（WAL 下读不阻塞，锁
    只保护连接对象的并发使用，量级是每轮几条 insert，无争用之忧）。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # ── 迁移：版本先记账，条目一个事务 ─────────────────────────

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS migration_meta ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self._conn.execute(
            "SELECT value FROM migration_meta WHERE key='schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else 0
        for version, sql in _MIGRATIONS:
            if version <= current:
                continue
            with self._conn:   # 一个迁移一个事务：半途失败整体回滚
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT OR REPLACE INTO migration_meta "
                    "(key, value) VALUES ('schema_version', ?)",
                    (str(version),))

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM migration_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    # ── sessions：元数据索引（软删除）─────────────────────────

    def upsert_session(self, session: dict) -> None:
        """会话元数据入索引（INSERT OR REPLACE：状态变化就重写本行）。"""

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, cwd, title, status, mode, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(session["id"]), str(session["cwd"]),
                 str(session.get("title", "")),
                 str(session.get("status", "")),
                 str(session.get("mode", "")),
                 str(session.get("created_at", _now_iso())),
                 str(session.get("updated_at", _now_iso()))))

    def soft_delete_session(self, session_id: str) -> None:
        """软删除：只标记，行还在磁盘上（永远不用 DELETE FROM）。"""

        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET status='deleted', updated_at=? "
                "WHERE id=?", (_now_iso(), session_id))

    def list_sessions(self, include_deleted: bool = False) -> list[dict]:
        """会话索引视图，按最近更新排序——不重放任何 JSONL。"""

        query = ("SELECT id, cwd, title, status, mode, updated_at "
                 "FROM sessions")
        if not include_deleted:
            query += " WHERE status != 'deleted'"
        query += " ORDER BY updated_at DESC"
        with self._lock:
            return [dict(row) for row in self._conn.execute(query)]

    # ── usage / tool：只追加，聚合靠 SQL ───────────────────────

    def record_usage(self, session_id: str, model: str,
                     input_tokens: int = 0, output_tokens: int = 0,
                     total_tokens: int = 0, cost: float = 0.0,
                     created_at: Optional[str] = None) -> None:
        """一轮的 token 用量入账。外键开着：挂在未知会话上当场炸。"""

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_tracking "
                "(session_id, model, input_tokens, output_tokens, "
                " total_tokens, cost, created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, model, int(input_tokens), int(output_tokens),
                 int(total_tokens), float(cost), created_at or _now_iso()))

    def record_tool_call(self, session_id: str, tool_name: str,
                         created_at: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tool_usage (session_id, tool_name, created_at) "
                "VALUES (?,?,?)",
                (session_id, tool_name, created_at or _now_iso()))

    def session_usage(self, session_id: str) -> dict:
        """单会话累计：轮数 + token 三项。"""

        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, "
                " SUM(input_tokens) AS input, "
                " SUM(output_tokens) AS output, "
                " SUM(total_tokens) AS total "
                "FROM usage_tracking WHERE session_id=?",
                (session_id,)).fetchone()
        return {
            "calls": row["calls"] or 0,
            "input_tokens": row["input"] or 0,
            "output_tokens": row["output"] or 0,
            "total_tokens": row["total"] or 0,
        }

    def usage_summary(self, month_prefix: Optional[str] = None) -> dict:
        """用量汇总；month_prefix='YYYY-MM' 时按月过滤（created_at 前缀）。"""

        query = ("SELECT COUNT(*) AS calls, SUM(input_tokens) AS input, "
                 "SUM(output_tokens) AS output, SUM(total_tokens) AS total, "
                 "SUM(cost) AS cost FROM usage_tracking")
        params: tuple[Any, ...] = ()
        if month_prefix:
            query += " WHERE created_at LIKE ?"
            params = (f"{month_prefix}%",)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return {
            "calls": row["calls"] or 0,
            "input_tokens": row["input"] or 0,
            "output_tokens": row["output"] or 0,
            "total_tokens": row["total"] or 0,
            "cost": row["cost"] or 0.0,
        }

    def top_tools(self, limit: int = 10) -> list[dict]:
        """工具调用排行（GROUP BY，全库或按会话都由调用方决定加参）。"""

        with self._lock:
            rows = self._conn.execute(
                "SELECT tool_name, COUNT(*) AS calls FROM tool_usage "
                "GROUP BY tool_name ORDER BY calls DESC, tool_name ASC "
                "LIMIT ?", (int(limit),)).fetchall()
        return [{"tool_name": row["tool_name"], "calls": row["calls"]}
                for row in rows]

    # ── s22 自动化：定义（软删除）× 运行时状态 × 执行历史 ───────

    def create_automation(self, auto: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO automations "
                "(id, name, prompt, schedule_type, rrule, scheduled_at, "
                " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (auto["id"], auto["name"], auto["prompt"],
                 auto["schedule_type"], auto.get("rrule"),
                 auto.get("scheduled_at"), auto.get("status", "ACTIVE"),
                 _now_iso(), _now_iso()))

    def list_automations(self, include_deleted: bool = False) -> list[dict]:
        query = ("SELECT a.*, r.last_run, r.next_run, r.last_status "
                 "FROM automations a "
                 "LEFT JOIN automation_runtime_state r "
                 "ON a.id = r.automation_id")
        if not include_deleted:
            query += " WHERE a.status != 'deleted'"
        query += " ORDER BY a.created_at ASC"
        with self._lock:
            return [dict(row) for row in self._conn.execute(query)]

    def set_automation_status(self, auto_id: str, status: str) -> None:
        """状态迁移：ACTIVE/PAUSED 互切、deleted 是软删除（行保留）。"""

        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE automations SET status=?, updated_at=? WHERE id=?",
                (status, _now_iso(), auto_id))

    def due_automations(self, now_iso: str) -> list[dict]:
        """到期的 ACTIVE 自动化：next_run 非空且 ≤ now。

        next_run 在 create 时就填好（once=scheduled_at，recurring=首次
        时刻）；once 跑完置 NULL 且转 PAUSED——next_run 为空的要么跑完
        要么没初始化，都不该到期。错过的不补：到期只跑一次，下一次
        由执行时刻重算（scheduler 的 anchor-on-run 语义）。
        """

        return [a for a in self.list_automations()
                if a["status"] == "ACTIVE"
                and a["next_run"] is not None
                and a["next_run"] <= now_iso]

    def upsert_runtime_state(self, auto_id: str,
                             last_run: str, next_run: Optional[str],
                             last_status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO automation_runtime_state "
                "(automation_id, last_run, next_run, last_status) "
                "VALUES (?,?,?,?)",
                (auto_id, last_run, next_run, last_status))

    def record_run_start(self, auto_id: str, started_at: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO automation_runs "
                "(automation_id, started_at, status) "
                "VALUES (?,?,'running')", (auto_id, started_at))
            return int(cursor.lastrowid)

    def record_run_end(self, run_id: int, completed_at: str,
                       status: str, output: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE automation_runs SET completed_at=?, status=?, "
                "output=? WHERE id=?",
                (completed_at, status, output[:2000], run_id))

    def automation_history(self, auto_id: str,
                           limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, completed_at, status, output "
                "FROM automation_runs WHERE automation_id=? "
                "ORDER BY id DESC LIMIT ?",
                (auto_id, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
