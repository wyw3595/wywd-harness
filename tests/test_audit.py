"""s23 审计链的离线单元测试：哈希链防改 × 锚点防删尾 × 接线。"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.harness.audit import GENESIS, AuditChain, compute_hash
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import build_default_policy
from src.harness.sidecar import SidecarServer
from src.harness.tools import Tool, ToolRegistry


class AuditChainTests(unittest.TestCase):

    def _chain(self, tmp) -> AuditChain:
        return AuditChain(Path(tmp) / "audit.jsonl")

    def test_chain_builds_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chain = self._chain(tmp)
            for i in range(5):
                entry = chain.append("tool_call", {"n": i}, f"out{i}")
                self.assertEqual(entry["seq"], i + 1)
                self.assertNotEqual(entry["prev_hash"], entry["hash"])
            ok, count, _ = chain.verify()
            self.assertTrue(ok)
            self.assertEqual(count, 5)
            # 链尾锚点文件写了条数与尾 hash。
            head = json.loads(
                (Path(tmp) / "audit.head").read_text(encoding="utf-8"))
            self.assertEqual(head["count"], 5)

    def test_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chain = self._chain(tmp)
            chain.append("a", {"x": 1}, "r")
            chain.append("b", {"x": 2}, "r")
            # 改第 1 条的 result：从那条起 hash 全对不上。
            path = Path(tmp) / "audit.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["result"] = "被改过"
            lines[0] = json.dumps(first, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ok, at, reason = AuditChain(path).verify()
            self.assertFalse(ok)
            self.assertEqual(at, 1)                  # 指认第 1 条
            self.assertIn("hash", reason)

    def test_truncation_is_detected_by_head_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chain = self._chain(tmp)
            chain.append("a", {}, "1")
            chain.append("b", {}, "2")
            path = Path(tmp) / "audit.jsonl"
            # 删掉末尾一条：链本身仍合法（前缀也是合法链）……
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n", encoding="utf-8")
            # ……但锚点还记着 2 条——交叉比对当场抓住截断。
            ok, count, reason = AuditChain(path).verify()
            self.assertFalse(ok)
            self.assertIn("截断", reason)

    def test_canonical_determinism(self) -> None:
        # 同一份数据任何序列化顺序 → 同一个 hash（canonical 键排序）。
        a = compute_hash({"x": 1, "y": 2}, GENESIS)
        b = compute_hash({"y": 2, "x": 1}, GENESIS)
        self.assertEqual(a, b)
        self.assertNotEqual(a, compute_hash({"x": 1, "y": 3}, GENESIS))

    def test_append_after_reopen_continues_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._chain(tmp).append("a", {}, "1")
            # 新实例从盘上重放拿链尾——append 无缝续链。
            second = self._chain(tmp)
            entry = second.append("b", {}, "2")
            self.assertEqual(entry["seq"], 2)
            self.assertEqual(entry["prev_hash"],
                             self._chain(tmp).entries()[0]["hash"])
            ok, count, _ = second.verify()
            self.assertTrue(ok)
            self.assertEqual(count, 2)

    def test_concurrent_appends_keep_chain_valid(self) -> None:
        """reviewer 评测抓到的 blocker：并发 tool_end 同时 append。

        sidecar 用 ThreadPoolExecutor 并行跑会话；两个线程若都读到同一个
        (seq, prev)，会写出重复 seq + 分叉 prev_hash，链永久断。锁住
        「读链尾→算 hash→写日志→写锚点」后，40 条并发追加必须连续。
        """

        with tempfile.TemporaryDirectory() as tmp:
            chain = self._chain(tmp)
            barrier = threading.Barrier(4)

            def worker() -> None:
                barrier.wait()
                for i in range(10):
                    chain.append("tool_call",
                                 {"thread": threading.get_ident(), "i": i}, "r")

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            entries = chain.entries()
            self.assertEqual([e["seq"] for e in entries], list(range(1, 41)))
            ok, count, reason = chain.verify()
            self.assertTrue(ok, reason)
            self.assertEqual(count, 40)

    def test_head_written_atomically(self) -> None:
        """锚点原子写：正常路径不留临时文件，坏 JSON 只报告不抛异常。"""

        with tempfile.TemporaryDirectory() as tmp:
            chain = self._chain(tmp)
            chain.append("a", {}, "1")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

            (Path(tmp) / "audit.head").write_text('{"count": 1',
                                                  encoding="utf-8")
            ok, count, reason = chain.verify()
            self.assertFalse(ok)
            self.assertEqual(count, 1)
            self.assertIn("锚点", reason)


class SidecarAuditTests(unittest.TestCase):

    def test_tool_calls_land_in_audit_chain(self) -> None:
        scripted = ScriptedModel([
            ModelReply(kind="tool_calls",
                       tool_calls=[ToolCall("c1", "echo", {"text": "hi"})]),
            ModelReply(kind="final", text="done"),
        ])
        with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True) as tmp:
            chain = AuditChain(Path(tmp) / "audit.jsonl")
            registry = ToolRegistry()
            registry.register(Tool(name="echo", description="t",
                                   handler=lambda text="": f"回声：{text}"))
            server = SidecarServer(
                model=scripted, registry=registry,
                policy=build_default_policy(safe_tools=("echo",)),
                audit_chain=chain)
            sid = server._handle_session_create({})["sessionId"]
            events: list = []
            server._on_event = lambda e, d: events.append(e)
            server._handle_agent_send({"sessionId": sid, "message": "跑"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if "turn_end" in events:
                    break
                time.sleep(0.05)
            entries = chain.entries()
            self.assertEqual(len(entries), 1)         # 一次工具调用一条痕
            self.assertEqual(entries[0]["action"], "tool_call")
            self.assertEqual(entries[0]["params"]["tool"], "echo")
            self.assertEqual(entries[0]["params"]["session_id"], sid)
            self.assertIn("回声", entries[0]["result"])
            ok, _, _ = chain.verify()
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
