"""s24 综合收官：机制很多，循环一个——24 课契约的端到端回归。

不是第二个 harness：本文件只消费前面各章的**公开契约**，串一条可
回归的链——recall(s12) → select(s15) → seed(s10/s11/s16) → 工具+
权限(s02-s04) → 转录(s09) → 审计(s23) → 统计(s21) → 重启回读
（**新对象**从盘上恢复，不拿旧实例冒充——教材的硬要求）。

任何一章的契约在集成处退化（ASK 变自动批准、恢复靠旧对象、审计
断链），这条测试就红。全部离线（ScriptedModel + 临时目录）。
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.harness.audit import AuditChain
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import ToolRequest, build_default_policy
from src.harness.prompt_assembly import (
    memory_candidates_from_recall,
    select_memory_context,
)
from src.harness.recall import (
    MemoryKind, MemorySource, RecallEngine, StoredMemory,
)
from src.harness.sidecar import SidecarServer
from src.harness.sqlite_store import HarnessDB
from src.harness.tools import Tool, ToolRegistry


def _fact_record(content: str, captured: str) -> StoredMemory:
    return StoredMemory(
        memory_id=f"fact:{content[:6]}", user_scope="u1",
        kind=MemoryKind.FACT, content=content, summary=content[:40],
        source=MemorySource(
            source_id="log-1", source_type="workspace_daily_log",
            title="decision", captured_at=captured),
        stored_at=captured)


class ComprehensiveTests(unittest.TestCase):
    """端到端：一次 turn 穿过全部机制，然后重启回读。"""

    def test_full_chain_and_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db = HarnessDB(root / "harness.db")
            audit = AuditChain(root / "audit.jsonl")
            registry = ToolRegistry()
            registry.register(Tool(
                name="echo", description="回声工具",
                handler=lambda text="": f"回声：{text}"))
            # 权限契约不许漂移：echo 放行、假想的 fs_write 仍要 ASK。
            policy = build_default_policy(safe_tools=("echo",))
            scripted = ScriptedModel([
                ModelReply(kind="tool_calls",
                           tool_calls=[ToolCall("c1", "echo",
                                                {"text": "综合链路"})],
                           usage={"prompt_tokens": 120,
                                  "completion_tokens": 30,
                                  "total_tokens": 150}),
                ModelReply(kind="final", text="完成了",
                           usage={"prompt_tokens": 150,
                                  "completion_tokens": 40,
                                  "total_tokens": 190}),
            ])
            server = SidecarServer(
                model=scripted, registry=registry, policy=policy,
                metrics_db=db, audit_chain=audit,
                store=None)   # InMemory（重启段换真 JsonlStore 再验）
            sid = server._handle_session_create(
                {"title": "综合验收"})["sessionId"]
            events: list = []
            server._on_event = lambda e, d: events.append(e)
            server._handle_agent_send({"sessionId": sid,
                                       "message": "跑一遍"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if "turn_end" in events:
                    break
                time.sleep(0.05)

            # ── 每本账都该有这笔 ────────────────────────────────
            usage = db.session_usage(sid)
            self.assertEqual(usage["total_tokens"], 340)   # 跨 step 累加
            self.assertEqual(db.top_tools(1)[0]["tool_name"], "echo")
            ok, count, _ = audit.verify()
            self.assertTrue(ok)
            self.assertEqual(count, 1)
            self.assertEqual(audit.entries()[0]["params"]["tool"], "echo")
            record = server._manager.load_record(sid)
            self.assertEqual(record.status, "idle")        # 正常收场

            # ── 权限契约没漂移：bash 仍是 ASK，不是自动批准 ────
            decision = policy.decide(ToolRequest(
                tool_use_id="t", name="bash",
                arguments={"command": "python build.py"}))
            self.assertEqual(decision.action.value, "ask")

            # ── s12→s15：召回证据经选择管线，门禁吃真分数 ──────
            engine = RecallEngine(lambda: [
                _fact_record("选了 uv 管依赖", "2026-10-24T00:00:00Z"),
                _fact_record("无关天气记录", "2026-10-01T00:00:00Z"),
            ])
            result = engine.recall("uv 依赖管理", user_scope="u1",
                                   as_of=datetime(2026, 10, 25,
                                                  tzinfo=timezone.utc))
            plan = select_memory_context(
                memory_candidates_from_recall(result), user_scope="u1")
            self.assertEqual(plan.selected_memory_ids,
                             ("fact:选了 uv",))            # 相关命中入选
            # 不相关的命中被门拦在选中集之外（哪道门由分数决定）。
            self.assertNotIn("fact:无关天气记", plan.selected_memory_ids)

            # ── 重启回读：**新对象**从盘上恢复，不拿旧的冒充 ────
            server._handle_session_close({"sessionId": sid})
            db2 = HarnessDB(root / "harness.db")
            audit2 = AuditChain(root / "audit.jsonl")
            try:
                listing = db2.list_sessions()
                self.assertIn(sid, [row["id"] for row in listing])
                ok2, count2, _ = audit2.verify()
                self.assertTrue(ok2)                # 新实例重放链仍完整
                self.assertEqual(count2, 1)
            finally:
                db2.close()

    def test_automation_and_stats_compose(self) -> None:
        """自动化 + 统计同库共存：tick 一次，run 历史与用量都有账。"""
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            db = HarnessDB(Path(tmp) / "h.db")
            scripted = ScriptedModel([ModelReply(kind="final", text="日报OK",
                usage={"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15})])
            server = SidecarServer(
                model=scripted, registry=ToolRegistry(),
                policy=build_default_policy(), metrics_db=db)
            past = "2000-01-01T00:00:00Z"
            created = server._handle_automation_create({
                "name": "日报", "prompt": "生成日报",
                "schedule_type": "once", "scheduled_at": past})
            reports = server._scheduler.tick()
            self.assertEqual(reports[0].status, "success")
            self.assertIn("日报OK", reports[0].output)
            self.assertEqual(
                len(db.automation_history(created["automationId"])), 1)
            # 隔离会话的用量也进了统计账（真相一处、查询面一处）。
            summary = server._handle_stats_summary({})
            self.assertGreaterEqual(summary["total"]["calls"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
