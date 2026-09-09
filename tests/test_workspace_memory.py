"""s10 工作区记忆的离线单测：scope 隔离 / 追加校验 / 蒸馏门槛 / 原子写 / 有界注入。

全 tmp 目录；时间用注入的 recorded_at / as_of 控制（30 天年龄门槛不用真等）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.toolbox import (
    DEFERRED_TOOLS,
    SAFE_TOOLS,
    build_history_seed,
    write_memory_fact,
)
from src.harness.workspace_memory import (
    DistillPolicy,
    MemoryCorruptionError,
    MemoryScopeError,
    WorkspaceMemory,
)

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)   # 距今 8 个月——稳过 30 天线


def _memory(root: Path) -> WorkspaceMemory:
    return WorkspaceMemory(root)


class ScopeTests(unittest.TestCase):
    def test_different_projects_have_different_ids(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.assertNotEqual(_memory(Path(a)).workspace_id,
                                _memory(Path(b)).workspace_id)

    def test_cross_workspace_fact_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            mem_a, mem_b = _memory(Path(a)), _memory(Path(b))
            fact = mem_a.append_daily_log("A 的事实", recorded_at=OLD)
            # 把 A 的日志行手工塞进 B 的目录（模拟拷贝/串线）
            src = mem_a.daily_log_path(OLD.date()).read_text(encoding="utf-8")
            mem_b.daily_dir.mkdir(parents=True, exist_ok=True)
            (mem_b.daily_log_path(OLD.date())).write_text(src, encoding="utf-8")
            with self.assertRaises(MemoryScopeError):
                mem_b.read_all_facts()


class AppendTests(unittest.TestCase):
    def test_validates_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            with self.assertRaises(ValueError):     # 空 content
                mem.append_daily_log("   ", recorded_at=OLD)
            with self.assertRaises(ValueError):     # 超长
                mem.append_daily_log("x" * 2001, recorded_at=OLD)
            with self.assertRaises(ValueError):     # importance 越界
                mem.append_daily_log("ok", importance=9, recorded_at=OLD)
            with self.assertRaises(ValueError):     # 未知 kind
                mem.append_daily_log("ok", kind="gossip", recorded_at=OLD)

    def test_append_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("第一条", kind="decision", importance=5,
                                 recorded_at=OLD)
            mem.append_daily_log("第二条", recorded_at=OLD)
            facts = mem.read_all_facts()
            self.assertEqual(len(facts), 2)
            self.assertEqual(facts[0].content, "第一条")
            self.assertTrue(facts[0].fact_id)
            self.assertTrue(facts[0].recorded_at.endswith("Z"))
            log = mem.daily_log_path(OLD.date())
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 2)

    def test_partial_tail_tolerated_complete_bad_line_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("好事实", recorded_at=OLD)
            log = mem.daily_log_path(OLD.date())
            with open(log, "ab") as f:
                f.write(b'{"fact_id": "xx')          # 半行，无换行
            self.assertEqual(len(mem.read_all_facts()), 1)
            with open(log, "ab") as f:
                f.write("完全的坏行\n".encode("utf-8"))   # 带换行的完整坏行
            with self.assertRaises(MemoryCorruptionError):
                mem.read_all_facts()


class DistillTests(unittest.TestCase):
    def _aged(self, root: Path, content: str, **kw) -> None:
        _memory(root).append_daily_log(content, recorded_at=OLD, **kw)

    def test_age_gate_blocks_fresh_facts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("刚发生", kind="decision", importance=5)
            report = mem.distill()
            self.assertEqual(report.scanned, 0)      # 没过年龄线，门都没进
            self.assertEqual(report.created, 0)

    def test_kind_gate_blocks_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            for _ in range(3):
                mem.append_daily_log("测试通过", kind="outcome",
                                     importance=5, recorded_at=OLD)
            report = mem.distill()
            self.assertEqual(report.created, 0)      # 老且重要且重复也不晋升
            self.assertEqual(report.skipped, 3)

    def test_importance_or_repetition_gate(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("重要决策", kind="decision",
                                 importance=5, recorded_at=OLD)
            mem.append_daily_log("低价值单条", kind="convention",
                                 importance=2, recorded_at=OLD)
            report = mem.distill()
            self.assertEqual(report.created, 1)      # importance>=4 的那条
            self.assertEqual(report.skipped, 1)      # 单条低价值被拦
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("重复出现的约定", kind="convention",
                                 importance=2, recorded_at=OLD)
            mem.append_daily_log("重复出现的约定", kind="convention",
                                 importance=2, recorded_at=OLD)
            report = mem.distill()
            self.assertEqual(report.created, 1)      # 重复 >=2 也够格
            self.assertEqual(report.created + report.skipped, report.scanned)

    def test_distill_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("幂等决策", kind="decision",
                                 importance=5, recorded_at=OLD)
            first = mem.distill()
            second = mem.distill()
            self.assertEqual((first.created, first.updated), (1, 0))
            self.assertEqual((second.created, second.updated), (0, 0))
            self.assertEqual(second.scanned, 0)      # 已处理的证据不再进扫描

    def test_same_normalized_content_merges(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("SQLite 必须 WAL", kind="decision",
                                 importance=2, recorded_at=OLD)
            mem.append_daily_log("sqlite   必须  wal", kind="decision",
                                 importance=2, recorded_at=OLD)   # 大小写+空格
            report = mem.distill()
            self.assertEqual(report.created, 1)      # 一条记忆，不是两条
            entries = mem._load_curated()
            self.assertEqual(entries[0].occurrences, 2)
            self.assertEqual(len(entries[0].evidence_ids), 2)

    def test_distill_writes_both_files_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("架构决策", kind="decision",
                                 importance=5, recorded_at=OLD)
            mem.distill()
            payload = json.loads(mem.curated_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["workspace_id"], mem.workspace_id)
            md = mem.memory_file.read_text(encoding="utf-8")
            self.assertIn("## Decisions", md)
            self.assertIn("(seen 1x; evidence: 1)", md)
            leftovers = [p.name for p in mem.memory_dir.iterdir()
                         if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])           # 原子替换不留残骸


class ContextTests(unittest.TestCase):
    def test_recent_facts_bounded_and_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            self.assertEqual(mem.get_context_for_agent(),
                             "(no workspace memory yet)")
            for i in range(8):
                mem.append_daily_log(f"事实{i}", recorded_at=OLD)
            context = mem.get_context_for_agent()
            self.assertIn("事实7", context)           # 最近 6 条在
            self.assertIn("事实2", context)
            self.assertNotIn("事实1", context)        # 第 7、8 条被预算截掉

    def test_restart_recovers_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            mem = _memory(Path(root))
            mem.append_daily_log("重启后还在", kind="decision",
                                 importance=5, recorded_at=OLD)
            mem.distill()
            restarted = _memory(Path(root))           # 新实例 = 新进程
            self.assertEqual(len(restarted.read_all_facts()), 1)
            self.assertIn("重启后还在", restarted.read_memory_md())


class ToolboxIntegrationTests(unittest.TestCase):
    def test_write_memory_fact_appends_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            result = write_memory_fact("用了 uv 管环境", "decision", 5,
                                       root=Path(root))
            self.assertIn("已记录", result)
            facts = _memory(Path(root)).read_all_facts()
            self.assertEqual(facts[0].kind, "decision")
            self.assertEqual(facts[0].importance, 5)

    def test_history_seed_with_and_without_memory(self) -> None:
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as rich:
            seed = build_history_seed(root=Path(empty))
            self.assertEqual(len(seed), 1)            # 空记忆 = 只有工具目录
            self.assertEqual(seed[0]["role"], "system")

            mem = _memory(Path(rich))
            mem.append_daily_log("起步就有的记忆", kind="decision",
                                 importance=5, recorded_at=OLD)
            seed2 = build_history_seed(root=Path(rich))
            self.assertEqual(len(seed2), 2)           # 第二条 system = 记忆
            self.assertIn("起步就有的记忆", seed2[1]["content"])

    def test_memory_write_is_deferred_and_safe(self) -> None:
        names = {tool.name for tool in DEFERRED_TOOLS}
        self.assertIn("memory_write", names)          # schema 不进基础上下文
        self.assertIn("memory_write", SAFE_TOOLS)     # 只追加日志 → 免审批


if __name__ == "__main__":
    unittest.main()
