"""s06 sidecar 的可测核心：RingBuffer / RPC 帧协议 / 领域路由 / agent/send / 分派环。

全离线：用 socket.socketpair() + threading 驱动 SidecarServer 的连接循环，
测试线程扮演 main 客户端——不真起 mp.Process（进程编排留到 scripts 冒烟）。
"""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import WorkspaceScope, build_default_policy
from src.harness.sidecar import (
    ConnectionClosed,
    MainProcessClient,
    RPCConnection,
    SidecarServer,
)
from src.harness.tools import Tool, ToolRegistry


class RingBufferTests(unittest.TestCase):
    """有界环形缓冲：写读往返 / 满了覆盖最旧 / 并发写计数。"""

    def test_write_read_all_roundtrip(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=16)
        buf.write("hello")
        self.assertEqual(buf.read_all(), "hello")
        self.assertEqual(buf.used, 5)
        self.assertFalse(buf.is_full)

    def test_overwrite_oldest_when_full(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=4)
        buf.write("abcdef")  # 6 字节 > 4：最旧 2 字节被挤掉
        self.assertEqual(buf.read_all(), "cdef")  # 旧→新顺序
        self.assertTrue(buf.is_full)
        self.assertEqual(buf.used, 4)

    def test_used_counts_written_until_full(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=8)
        buf.write("ab")
        self.assertEqual(buf.used, 2)

    def test_write_accepts_str_and_bytes(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=16)
        buf.write("ab")
        buf.write(b"cd")
        self.assertEqual(buf.read_all(), "abcd")

    def test_unicode_survives(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=64)
        buf.write("审批通过")
        self.assertEqual(buf.read_all(), "审批通过")

    def test_concurrent_write_total_written(self) -> None:
        from src.harness.sidecar import RingBuffer

        buf = RingBuffer(size=1024)
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for _ in range(100):
                    buf.write("x")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(buf.total_written, 200)  # Lock 保护计数


class RPCConnectionTests(unittest.TestCase):
    """帧协议：JSON+\n framing / 断开返 None / 半帧累积 / 多消息分条。"""

    def _pair(self) -> tuple[RPCConnection, RPCConnection]:
        srv, cli = socket.socketpair()
        return RPCConnection(srv), RPCConnection(cli)

    def test_framing_roundtrip(self) -> None:
        a, b = self._pair()
        a.send_message({"jsonrpc": "2.0", "method": "ping", "id": 1})
        self.assertEqual(
            b.recv_message(),
            {"jsonrpc": "2.0", "method": "ping", "id": 1},
        )
        a.close(); b.close()

    def test_recv_returns_none_on_disconnect(self) -> None:
        a, b = self._pair()
        b.close()  # 对端正常关闭 → EOF
        self.assertIsNone(a.recv_message())
        a.close()

    def test_partial_chunk_accumulates(self) -> None:
        a, b = self._pair()
        half = '{"jsonrpc":"2.0","method":"ping","id":1}\n'.encode("utf-8")
        a.sock.sendall(half[:10])
        a.sock.sendall(half[10:])  # 分两次，验证残帧累积
        self.assertEqual(b.recv_message()["method"], "ping")
        a.close(); b.close()

    def test_two_messages_one_chunk(self) -> None:
        a, b = self._pair()
        a.send_message({"jsonrpc": "2.0", "method": "ping", "id": 1})
        a.send_message({"jsonrpc": "2.0", "method": "ping", "id": 2})
        self.assertEqual(b.recv_message()["id"], 1)
        self.assertEqual(b.recv_message()["id"], 2)
        a.close(); b.close()


class SidecarServerTests(unittest.TestCase):
    """领域路由 + 会话生命周期（s07-b：close/resume/forget）+ agent/send。

    会话语义已换代：close 释放运行时但留记录（幂等）、resume 换代重建、
    forget 才真删——测试全部按 SessionManager 的新契约写（s07-b 红灯）。
    """

    def _make_registry(self, tmp: str, tracked: dict) -> ToolRegistry:
        """带"写工具"的注册表（handler 必须带类型注解——s04 教训）。"""

        registry = ToolRegistry()

        def handler(path: str, text: str) -> str:
            tracked["calls"] += 1
            (Path(tmp) / path).write_text(text, encoding="utf-8")
            return f"已写入 {path}"

        registry.register(Tool(name="fs_write", description="写入文件", handler=handler))
        return registry

    def _spawn(
        self,
        server: SidecarServer,
    ) -> tuple[RPCConnection, threading.Thread]:
        """socketpair + 线程跑 handle_connection；返回 main 侧连接。"""

        srv, cli = socket.socketpair()
        thread = threading.Thread(
            target=server.handle_connection, args=(RPCConnection(srv),),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)  # 让 handler 线程就绪
        return RPCConnection(cli), thread

    def _request(self, conn: RPCConnection, method: str, params: dict | None = None) -> dict:
        """发请求并等它的 result 响应；途中 event 通知就地消费后继续。

        agent/send 处理时会广播 event（round_start/tool_start...），若只
        recv 一条会拿到 event 而不是 result——这正是死锁违约④的活教材：
        "不消费 event，event 会挡住 result"。
        """

        conn.send_message({"jsonrpc": "2.0", "method": method,
                           "params": params or {}, "id": 1})
        while True:
            msg = conn.recv_message()
            if "method" in msg:   # event 等通知：消费后继续等 result
                continue
            return msg

    def test_ping_returns_status_uptime(self) -> None:
        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        resp = self._request(conn, "sidecar/ping")
        self.assertIn("status", resp["result"])
        self.assertIn("uptime", resp["result"])
        conn.close()

    def test_unknown_method_returns_min32601(self) -> None:
        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        resp = self._request(conn, "no/such")
        self.assertEqual(resp["error"]["code"], -32601)
        conn.close()

    def test_session_close_lifecycle(self) -> None:
        """s07-b 新语义：close 释放运行时但记录保留；幂等；未知 id 报错。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        self.assertTrue(sid.startswith("sess_"))
        # close：关运行时，记录还在（区别于老 destroy 的一删全没）
        closed = self._request(conn, "session/close", {"sessionId": sid})["result"]
        self.assertEqual(closed["status"], "ok")
        self.assertTrue(closed["closed"])
        listed = self._request(conn, "session/list")["result"]["sessions"]
        row = next(s for s in listed if s["id"] == sid)
        self.assertEqual(row["status"], "closed")
        self.assertFalse(row["live"])       # 记录在、运行时没了
        # 幂等：二次 close 不报错（老 destroy 二次会 not found）
        again = self._request(conn, "session/close", {"sessionId": sid})["result"]
        self.assertEqual(again["status"], "ok")
        self.assertFalse(again["closed"])
        # 未知 id：诚实报错
        missing = self._request(conn, "session/close", {"sessionId": "nope"})["result"]
        self.assertIn("error", missing)
        conn.close()

    def test_session_create_seeds_system_prompt(self) -> None:
        server = SidecarServer(
            model=None, registry=ToolRegistry(),
            policy=build_default_policy(),
            history_seed=lambda: [{"role": "system", "content": "目录"}],
        )
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create")["result"]["sessionId"]
        # 从 store 存档看（resume 走的就是这条路——两个副本都得有 system）
        record = server._manager.load_record(sid)
        self.assertEqual(record.messages[0]["role"], "system")
        conn.close()

    def test_session_resume_continues_history(self) -> None:
        """close 后 resume：新运行时接着旧 transcript 聊（s07 的招牌语义）。"""

        scripted = ScriptedModel([
            ModelReply(kind="final", text="甲"),
            ModelReply(kind="final", text="乙"),
        ])
        server = SidecarServer(model=scripted, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        self._request(conn, "agent/send", {"sessionId": sid, "message": "任务甲"})
        self._request(conn, "session/close", {"sessionId": sid})
        resumed = self._request(conn, "session/resume", {"sessionId": sid})["result"]
        self.assertEqual(resumed["generation"], 2)   # 换代：第 2 代运行时
        self._request(conn, "agent/send", {"sessionId": sid, "message": "任务乙"})
        second = scripted.received_inputs[1]
        self.assertIn("任务甲", str(second))   # 跨 close/resume 的记忆延续
        conn.close()

    def test_session_resume_live_rejected(self) -> None:
        """live 会话 resume 会被拒——两个执行器写同一段 transcript 是竞争。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        resp = self._request(conn, "session/resume", {"sessionId": sid})["result"]
        self.assertIn("error", resp)
        conn.close()

    def test_session_forget_requires_close_then_deletes(self) -> None:
        """forget：live 拒绝；close 后真删；不存在报错。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        live = self._request(conn, "session/forget", {"sessionId": sid})["result"]
        self.assertIn("error", live)          # live：必须先 close
        self._request(conn, "session/close", {"sessionId": sid})
        forgot = self._request(conn, "session/forget", {"sessionId": sid})["result"]
        self.assertEqual(forgot.get("status"), "ok")
        listed = self._request(conn, "session/list")["result"]["sessions"]
        self.assertFalse(any(s["id"] == sid for s in listed))   # 真消失
        again = self._request(conn, "session/forget", {"sessionId": sid})["result"]
        self.assertIn("error", again)         # 不存在：诚实报错
        conn.close()

    def test_agent_send_closed_session_returns_error(self) -> None:
        """closed 的会话 send 报错——"记录还在"和"运行时活着"是两回事。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        self._request(conn, "session/close", {"sessionId": sid})
        resp = self._request(conn, "agent/send",
                             {"sessionId": sid, "message": "hi"})["result"]
        self.assertIn("error", resp)
        conn.close()

    def test_status_counts_records_not_live(self) -> None:
        """/status 的 sessions 口径 = 记录总数（closed 未 forget 也计入）。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        self._request(conn, "session/close", {"sessionId": sid})
        status = self._request(conn, "sidecar/status")["result"]
        self.assertEqual(status["sessions"], 1)   # 记录还在，只是运行时没了
        conn.close()

    def test_agent_send_unknown_session_returns_error(self) -> None:
        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        resp = self._request(conn, "agent/send",
                             {"sessionId": "nope", "message": "hi"})
        self.assertIn("error", resp["result"])
        conn.close()

    def test_agent_send_persists_history_across_calls(self) -> None:
        """两次 agent/send：第二次模型收到的历史里要有第一次的问答。"""

        scripted = ScriptedModel([
            ModelReply(kind="final", text="甲"),
            ModelReply(kind="final", text="乙"),
        ])
        server = SidecarServer(model=scripted, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create")["result"]["sessionId"]
        self._request(conn, "agent/send", {"sessionId": sid, "message": "任务甲"})
        self._request(conn, "agent/send", {"sessionId": sid, "message": "任务乙"})
        second = scripted.received_inputs[1]
        self.assertIn("任务甲", str(second))  # 第一轮的问答还在历史里
        conn.close()

    def test_agent_send_max_steps_honest(self) -> None:
        """剧本全是 tool_calls：agent 空转到 max_steps，status 诚实返回。"""

        server = SidecarServer(
            model=ScriptedModel([ModelReply(kind="tool_calls", tool_calls=[
                ToolCall("c1", "fs_write", {"path": "x", "text": "y"}),
            ])]),
            registry=self._make_registry(".", {}),
            policy=build_default_policy(),
        )
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create")["result"]["sessionId"]
        resp = self._request(conn, "agent/send", {"sessionId": sid, "message": "循环"})
        self.assertEqual(resp["result"]["status"], "max_steps")
        conn.close()

    def test_shutdown_flag_then_eof_exits(self) -> None:
        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, thread = self._spawn(server)
        resp = self._request(conn, "sidecar/shutdown")
        self.assertEqual(resp["result"]["status"], "shutting down")
        self.assertTrue(server._shutdown)
        conn.close()  # EOF → 循环 break → 线程结束
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


class MainProcessClientTests(unittest.TestCase):
    """壳侧客户端：call 配对 / 分派环消费审批与事件 / 断连诚实失败。"""

    def _run_server(
        self, tmp: str, tracked: dict,
        user_prompt: Callable[[str, str], bool],
    ) -> tuple[SidecarServer, MainProcessClient, threading.Thread]:
        """真起 server 线程 + MainProcessClient 连接，返回三件套。"""

        registry = ToolRegistry()

        def handler(path: str, text: str) -> str:
            tracked["calls"] += 1
            (Path(tmp) / path).write_text(text, encoding="utf-8")
            return "ok"

        registry.register(Tool(name="fs_write", description="写", handler=handler))
        server = SidecarServer(
            model=ScriptedModel([ModelReply(kind="final", text="ok")]),
            registry=registry,
            policy=build_default_policy(
                scope=WorkspaceScope(Path(tmp)), write_tools=["fs_write"]),
        )
        srv, cli = socket.socketpair()
        thread = threading.Thread(
            target=server.handle_connection, args=(RPCConnection(srv),),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        client = MainProcessClient(user_prompt=user_prompt)
        client.connect(cli)
        return server, client, thread

    def test_call_returns_result_with_matching_id(self) -> None:
        server, client, thread = self._run_server(
            ".", {}, lambda rule_id, reason: True)
        resp = client.call("sidecar/ping")
        self.assertEqual(resp["id"], 1)
        self.assertIn("result", resp)
        client.call("sidecar/shutdown")
        client.close(); thread.join(timeout=2)

    def test_dispatch_loop_consumes_approval_before_result(self) -> None:
        """分派环核心：审批通知混在半路时，弹 y/n 回执后继续等 result。"""

        with tempfile.TemporaryDirectory() as tmp:
            tracked = {"calls": 0}
            prompts: list[tuple[str, str]] = []
            server, client, thread = self._run_server(
                tmp, tracked,
                user_prompt=lambda rule_id, reason: prompts.append(
                    (rule_id, reason)) or True,
            )
            sid = client.call("session/create")["result"]["sessionId"]
            server._model = ScriptedModel([
                ModelReply(kind="tool_calls", tool_calls=[
                    ToolCall("c1", "fs_write", {"path": "n.txt", "text": "hi"}),
                ]),
                ModelReply(kind="final", text="写好了"),
            ])
            resp = client.call("agent/send", {"sessionId": sid, "message": "写文件"})
            self.assertEqual(tracked["calls"], 1)
            self.assertEqual(prompts[0][0], "path.write_ask")  # 审批真的弹了
            self.assertIn("写好了", resp["result"]["output"])
            client.call("sidecar/shutdown")
            client.close(); thread.join(timeout=2)

    def test_dispatch_loop_rejects_blocks_handler(self) -> None:
        """拒绝路：分派环回 approved=False，handler 不碰，回灌 permission_blocked。"""

        with tempfile.TemporaryDirectory() as tmp:
            tracked = {"calls": 0}
            server, client, thread = self._run_server(
                tmp, tracked, user_prompt=lambda rule_id, reason: False,
            )
            sid = client.call("session/create")["result"]["sessionId"]
            server._model = ScriptedModel([
                ModelReply(kind="tool_calls", tool_calls=[
                    ToolCall("c1", "fs_write", {"path": "n.txt", "text": "hi"}),
                ]),
                ModelReply(kind="final", text="好吧不写了"),
            ])
            resp = client.call("agent/send", {"sessionId": sid, "message": "写文件"})
            self.assertEqual(tracked["calls"], 0)  # 拒绝：不碰 handler
            # s07-b：transcript 从 store 存档读（server.sessions 裸字典已退役）
            tool_msgs = " ".join(
                m.get("content", "") for m in server._manager.load_record(sid).messages
                if m["role"] == "tool"
            )
            self.assertIn("permission_blocked", tool_msgs)
            self.assertIn("path.write_ask", tool_msgs)
            self.assertEqual(resp["result"]["output"], "好吧不写了")
            client.call("sidecar/shutdown")
            client.close(); thread.join(timeout=2)

    def test_dispatch_loop_consumes_event_notifications(self) -> None:
        """event 通知被 on_event 消费，result 仍正确返回（不被 event 挡住）。"""

        events: list[dict] = []
        server, client, thread = self._run_server(
            ".", {}, lambda rule_id, reason: True)
        client._on_event = events.append
        sid = client.call("session/create")["result"]["sessionId"]
        resp = client.call("agent/send", {"sessionId": sid, "message": "hi"})
        self.assertIn("result", resp)
        self.assertTrue(any(e.get("event") == "round_start" for e in events))
        client.call("sidecar/shutdown")
        client.close(); thread.join(timeout=2)

    def test_call_raises_closed_on_eof(self) -> None:
        """对端关闭后 call 抛 ConnectionClosed——sidecar 崩溃的诚实失败。"""

        server, client, thread = self._run_server(
            ".", {}, lambda rule_id, reason: True)
        client.close()  # 直接断开 → sidecar 线程退出
        thread.join(timeout=2)
        with self.assertRaises(ConnectionClosed):
            client.call("sidecar/ping")


if __name__ == "__main__":
    unittest.main()