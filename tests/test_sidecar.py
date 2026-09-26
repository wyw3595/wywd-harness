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

from src.harness.jsonl_store import JsonlSessionStore
from src.harness.models import FakeModel, ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import WorkspaceScope, build_default_policy
from src.harness.session import SessionRecord
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

    def test_forget_live_error_is_actionable_human_text(self) -> None:
        """删 live 会话的报错要能直接给用户看：带 id + 说出下一步。

        这条错误一路透传到界面弹窗，原文是英文（"close session X before
        forgetting its record"）——用户看不懂就只会觉得"删不掉"。
        """

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create", {"cwd": "."})["result"]["sessionId"]
        error = self._request(conn, "session/forget",
                              {"sessionId": sid})["result"]["error"]
        self.assertIn(sid, error)        # 说清是哪一个
        self.assertIn("先关闭", error)   # 说清下一步
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

    def test_reconciles_zombie_records_on_start(self) -> None:
        """s09 启动清账：证据里"活着"的僵尸记录，新 sidecar 起来就抹成 closed。"""

        with tempfile.TemporaryDirectory() as root:
            # 预先埋一个"崩溃现场"：记录声称 running，但没有 runtime
            store = JsonlSessionStore(root)
            store.create(SessionRecord(id="sess_0001", cwd=".", status="running"))
            server = SidecarServer(model=None, registry=ToolRegistry(),
                                   policy=build_default_policy(),
                                   store=JsonlSessionStore(root))
            record = server._manager.store.load("sess_0001")
            self.assertEqual(record.status, "closed")   # 清账落地

    def test_sessions_survive_sidecar_restart(self) -> None:
        """s09 招牌：sidecar"崩溃"重启后，会话清单还在、resume 能接着聊。"""

        with tempfile.TemporaryDirectory() as root:
            # 第一代 sidecar：建会话、聊一轮、连接直接断（不 shutdown = 崩溃）
            scripted1 = ScriptedModel([ModelReply(kind="final", text="甲")])
            server1 = SidecarServer(model=scripted1, registry=ToolRegistry(),
                                    policy=build_default_policy(),
                                    store=JsonlSessionStore(root))
            conn1, _ = self._spawn(server1)
            sid = self._request(conn1, "session/create", {"cwd": "."})["result"]["sessionId"]
            self._request(conn1, "agent/send", {"sessionId": sid, "message": "任务甲"})
            conn1.close()   # EOF：第一代死亡，证据文件留在磁盘

            # 第二代 sidecar：同目录新 store，重启后一切从证据重建
            scripted2 = ScriptedModel([ModelReply(kind="final", text="乙")])
            server2 = SidecarServer(model=scripted2, registry=ToolRegistry(),
                                    policy=build_default_policy(),
                                    store=JsonlSessionStore(root))
            conn2, _ = self._spawn(server2)
            listed = self._request(conn2, "session/list")["result"]["sessions"]
            self.assertEqual([s["id"] for s in listed], [sid])
            self.assertEqual(listed[0]["status"], "closed")   # 启动清账抹过
            self.assertFalse(listed[0]["live"])               # 记录在，运行时没了
            resumed = self._request(conn2, "session/resume", {"sessionId": sid})["result"]
            self.assertEqual(resumed["generation"], 2)        # 换代重建
            self._request(conn2, "agent/send", {"sessionId": sid, "message": "任务乙"})
            second = scripted2.received_inputs[0]
            self.assertIn("任务甲", str(second))   # 跨进程死亡的记忆延续
            conn2.close()

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

    def test_agent_send_reports_per_turn_usage(self) -> None:
        """每轮 token 账：usage 只描述**这一轮**，不是累计值。

        接缝记账的第二笔（第一笔是 status，s07-b）。TurnRunner 协议只回
        (output, messages)，装不下 usage——老办法照用：在闭包里顺手记一笔，
        再逐轮返回。
        """

        scripted = ScriptedModel([
            ModelReply(kind="final", text="甲",
                       usage={"prompt_tokens": 100, "completion_tokens": 20}),
            ModelReply(kind="final", text="乙",
                       usage={"prompt_tokens": 300, "completion_tokens": 40}),
        ])
        server = SidecarServer(model=scripted, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create")["result"]["sessionId"]

        first = self._request(conn, "agent/send",
                              {"sessionId": sid, "message": "甲"})["result"]
        second = self._request(conn, "agent/send",
                               {"sessionId": sid, "message": "乙"})["result"]
        self.assertEqual(first["usage"],
                         {"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(second["usage"],
                         {"prompt_tokens": 300, "completion_tokens": 40})
        conn.close()

    def test_agent_send_usage_empty_for_offline_model(self) -> None:
        """离线模型不产生用量：诚实回空字典，不编造 0。"""

        server = SidecarServer(model=FakeModel(), registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._request(conn, "session/create")["result"]["sessionId"]
        result = self._request(conn, "agent/send",
                               {"sessionId": sid, "message": "你好"})["result"]
        self.assertEqual(result["usage"], {})
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

    # ── 工作区（s11）：换沙箱的 RPC 契约 ──────────────────────

    def _workspace_builder(self, seen: list) -> Callable[[dict], tuple]:
        """假 runtime_builder：记下收到的参数，返回一套**可辨认**的新
        registry/policy（好断言"真换了"）。"""

        def builder(params: dict):
            seen.append(params)
            registry = ToolRegistry()
            registry.register(Tool(
                name="probe", description="探针", handler=lambda: "probe-ok"))
            return (registry, build_default_policy(),
                    {"kind": params.get("kind"), "id": "ws_9",
                     "root": params.get("path")})

        return builder

    def _workspace_server(self, seen: list) -> SidecarServer:
        """带假 runtime_builder 的 server。"""

        return SidecarServer(model=None, registry=ToolRegistry(),
                             policy=build_default_policy(),
                             runtime_builder=self._workspace_builder(seen))

    # ── 工作区（s11 上传目录 → 2026-09-23 改**会话级**）──────────────
    #
    # 契约变化：目录属于**会话**（Record.cwd），不再属于进程。
    # 所以 workspace/set|get 都要带 sessionId，而且**不换** sidecar 级那套
    # registry/policy——换的是"这个会话下次重建运行时用哪个目录"。

    def _new_session(self, conn) -> str:
        return self._request(conn, "session/create", {})["result"]["sessionId"]

    def test_workspace_set_is_per_session(self) -> None:
        """**一个会话一个目录**：给 A 换目录，B 不受影响。

        旧模型是"换掉本进程的 registry"——那样两个会话没法各待一个目录。
        这里断言的正是新模型：cwd 落在**各自的记录**里，进程级那套不被动。
        """

        seen: list = []
        server = self._workspace_server(seen)
        conn, _ = self._spawn(server)

        sid_a = self._new_session(conn)
        sid_b = self._new_session(conn)
        before_b = server._manager.store.load(sid_b).cwd

        # 用真实存在的目录（cwd 会被 resolve(strict=True) 校验）
        with tempfile.TemporaryDirectory() as target:
            resp = self._request(conn, "workspace/set",
                                 {"sessionId": sid_a, "kind": "dir",
                                  "path": target})["result"]

            self.assertEqual(resp["status"], "ok")
            self.assertEqual(resp["workspace"]["id"], "ws_9")
            # 建会话时也会问工厂一次（拿它的 cwd 造运行时），所以看**最后一条**
            self.assertEqual(seen[-1]["path"], target)
            self.assertEqual(seen[-1]["kind"], "dir")
            # A 的目录变了
            self.assertEqual(server._manager.store.load(sid_a).cwd, target)
            # B 一点没动
            self.assertEqual(server._manager.store.load(sid_b).cwd, before_b)
        # 进程级那套没被换（老模型才会动它）
        self.assertNotIn("probe", server._registry.names())
        conn.close()

    def test_workspace_dirs_do_not_leak_between_sessions(self) -> None:
        """两个会话各设一个目录 → 各自运行时用**各自的** registry/沙箱。

        这是"真隔离"的核心：两个 cwd 对应两份 registry，而且互不覆盖。
        """

        seen: list = []
        server = self._workspace_server(seen)
        conn, _ = self._spawn(server)

        sid_a = self._new_session(conn)
        sid_b = self._new_session(conn)
        self._request(conn, "workspace/set",
                      {"sessionId": sid_a, "kind": "dir", "path": "D:/a"})
        self._request(conn, "workspace/set",
                      {"sessionId": sid_b, "kind": "dir", "path": "D:/b"})

        registry_a, policy_a, info_a = server._runtime_for("D:/a")
        registry_b, policy_b, info_b = server._runtime_for("D:/b")

        self.assertIsNot(registry_a, registry_b)          # 两份注册表
        self.assertIsNot(policy_a, policy_b)              # 两份策略
        self.assertEqual(info_a["root"], "D:/a")
        self.assertEqual(info_b["root"], "D:/b")
        # 同一个目录再取，拿到的是缓存里那份（不重复构建）
        again_a, _, _ = server._runtime_for("D:/a")
        self.assertIs(again_a, registry_a)
        conn.close()

    def test_workspace_get_is_per_session(self) -> None:
        seen: list = []
        server = self._workspace_server(seen)
        conn, _ = self._spawn(server)
        sid = self._new_session(conn)

        default = self._request(conn, "workspace/get",
                                {"sessionId": sid})["result"]["workspace"]
        self.assertIn("root", default)          # 新会话总有话可说

        with tempfile.TemporaryDirectory() as target:
            self._request(conn, "workspace/set",
                          {"sessionId": sid, "kind": "dir", "path": target})
            after = self._request(conn, "workspace/get",
                                  {"sessionId": sid})["result"]["workspace"]
            self.assertEqual(after["kind"], "dir")
            self.assertEqual(after["root"], target)
        conn.close()

    def test_workspace_set_translates_builder_error(self) -> None:
        """装配层的 ValueError 翻成人话进 {"error"}——RPC 唯一错误通道。"""

        def builder(params: dict):
            # 只在 zip 上炸——**建会话时**厂会用 dir 问一次（拿会话的 cwd），
            # 那次得让它过，否则连会都建不起来，测的就不是"set 时报错"了。
            if params.get("kind") == "zip":
                raise ValueError("压缩包已损坏或不是 zip：p.zip")
            return (ToolRegistry(), build_default_policy(),
                    {"kind": "dir", "root": params.get("path")})

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy(),
                               runtime_builder=builder)
        conn, _ = self._spawn(server)
        sid = self._new_session(conn)

        result = self._request(conn, "workspace/set",
                               {"sessionId": sid, "kind": "zip",
                                "path": "p.zip"})["result"]

        self.assertIn("error", result)
        self.assertIn("压缩包", result["error"])
        # 失败不动现状：会话的 cwd 还是原来那个（fail-closed 的老规矩）
        self.assertNotEqual(server._manager.store.load(sid).cwd, "p.zip")
        conn.close()

    def test_workspace_set_without_builder_says_so(self) -> None:
        """没注入工厂的 sidecar 如实回绝——不假装成功，也不炸。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._new_session(conn)

        result = self._request(conn, "workspace/set",
                               {"sessionId": sid, "kind": "dir",
                                "path": "D:/proj"})["result"]

        self.assertIn("没接工作区能力", result["error"])
        conn.close()

    def test_workspace_set_needs_a_live_session(self) -> None:
        """不给 sessionId / 会话不存在 → 明确回绝（不假装换了）。"""

        seen: list = []
        server = self._workspace_server(seen)
        conn, _ = self._spawn(server)

        result = self._request(conn, "workspace/set",
                               {"kind": "dir", "path": "D:/proj"})["result"]

        self.assertIn("error", result)
        self.assertIn("不在运行", result["error"])
        self.assertEqual(seen, [])          # 工厂压根没被叫
        conn.close()

    def test_status_carries_startup_workspace(self) -> None:
        """sidecar/status 里的 workspace 是**启动态**（进程级）。

        工作区改成会话级之后，进程层面只剩"我出生在哪个目录"这一个事实——
        某个会话现在在哪儿，得问 workspace/get?sessionId=。
        """

        seen: list = []
        server = SidecarServer(
            model=None, registry=ToolRegistry(), policy=build_default_policy(),
            runtime_builder=self._workspace_builder(seen),
            initial_workspace={"kind": "default", "root": "D:/born-here"})
        conn, _ = self._spawn(server)

        self._request(conn, "sidecar/status")
        status = self._request(conn, "sidecar/status")["result"]
        self.assertEqual(status["workspace"]["root"], "D:/born-here")
        conn.close()

    def test_workspace_set_default_resets_that_session(self) -> None:
        """kind=default 是**这个会话**复位：cwd 回启动态，且不经过工厂。

        为什么强调不经过工厂：复位不需要坐标。让工厂去解析一个"默认路径"
        等于把"默认是什么"外包给装配层，而它本来就只是把启动态交出来的人。
        """

        seen: list = []
        # 用**真实存在的**临时目录：cwd 会被 resume 用 resolve(strict=True) 校验，
        # 假路径会让会话下次打不开（这正是"目录必须存在"这条约束的意义）。
        with tempfile.TemporaryDirectory() as default_root, \
                tempfile.TemporaryDirectory() as other:
            server = SidecarServer(
                model=None, registry=ToolRegistry(),
                policy=build_default_policy(),
                runtime_builder=self._workspace_builder(seen),
                initial_workspace={"kind": "default", "root": default_root})
            conn, _ = self._spawn(server)
            sid = self._new_session(conn)

            self._request(conn, "workspace/set",
                          {"sessionId": sid, "kind": "dir", "path": other})
            self.assertEqual(server._manager.store.load(sid).cwd, other)
            after_switch = len(seen)

            result = self._request(conn, "workspace/set",
                                   {"sessionId": sid, "kind": "default"})["result"]

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["workspace"]["root"], default_root)
            self.assertEqual(len(seen), after_switch)   # 复位没再叫工厂
            self.assertEqual(server._manager.store.load(sid).cwd, default_root)
            conn.close()

    def test_workspace_set_default_works_without_builder(self) -> None:
        """没工厂也能复位——"回启动态"从来不需要工作区能力。"""

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        conn, _ = self._spawn(server)
        sid = self._new_session(conn)

        result = self._request(conn, "workspace/set",
                               {"sessionId": sid, "kind": "default"})["result"]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["workspace"]["kind"], "default")
        conn.close()
    # ── 空闲回收（s12）：后台线程与状态自述 ──────────────────

    def test_idle_timeout_defaults_to_off(self) -> None:
        """默认不回收：不该有线程偷偷跑，status 里也要如实说 0。

        "默认关"是刻意的：回收会把"随时发消息都能用"变成"可能要先复活一次"，
        那是手感变化，得由调用方明确打开。
        """

        server = SidecarServer(model=None, registry=ToolRegistry(),
                               policy=build_default_policy())
        self.assertEqual(server._idle_timeout, 0.0)
        self.assertIsNone(server._reaper)          # 连线程都不该有
        conn, _ = self._spawn(server)
        status = self._request(conn, "sidecar/status")["result"]
        self.assertEqual(status["idleTimeout"], 0.0)
        conn.close()

    def test_reaper_releases_idle_runtime_but_keeps_record(self) -> None:
        """开着回收时：闲置超阈值的运行时被释放，记录还在，resume 能回来。"""

        server = SidecarServer(
            model=None, registry=ToolRegistry(), policy=build_default_policy(),
            store=JsonlSessionStore(root=tempfile.mkdtemp()),
            idle_timeout=0.02, sweep_interval=0.02)
        self.assertIsNotNone(server._reaper)
        conn, _ = self._spawn(server)
        try:
            sid = self._request(conn, "session/create",
                                {"cwd": "."})["result"]["sessionId"]
            # 等回收线程动手：轮询它给出的结果，不睡固定时长猜。
            # 判据要**等到终态**——close() 是 closing → closed 两步落盘，
            # 只等 live 变 false 会在中间态上抓个正着（这里踩过一次）。
            row = {}
            for _ in range(150):
                listed = self._request(conn, "session/list")["result"]["sessions"]
                row = next(s for s in listed if s["id"] == sid)
                if not row["live"] and row["status"] == "closed":
                    break
                time.sleep(0.02)

            self.assertFalse(row["live"])              # 运行时被释放
            self.assertEqual(row["status"], "closed")  # 记录还在，只是关着

            # 点一下就回来（前端对 closed 会话干的就是这件事）。
            # 先停掉回收线程再 resume：阈值只有 0.02 秒，不停的话新运行时立刻
            # 又被收走，断言就变成跟回收赛跑——那不是这条测试要验的东西。
            server.stop_reaper()
            resumed = self._request(conn, "session/resume",
                                    {"sessionId": sid})["result"]
            self.assertEqual(resumed["generation"], 2)  # 换代重建，不是复活旧进程
            again = self._request(conn, "session/list")["result"]["sessions"]
            self.assertTrue(next(s for s in again if s["id"] == sid)["live"])
        finally:
            conn.close()
            server.stop_reaper()           # 幂等；别留线程给下一条测试
            self.assertFalse(server._reaper.is_alive())


class _ScriptedPeer:
    """socketpair 对端的剧本演员（2026-09-26 多路复用测试专用）。

    真 SidecarServer 是单循环顺序处理：回响应的顺序 == 收到请求的顺序，
    造不出"乱序响应"。多路复用的核心行为——响应各回各家、通知不挡结果、
    close 唤醒挂起者——需要一个能精确控制"回什么、什么时候回、什么顺序回"
    的对端。所以这些测试不连真 server：对端线程按剧本收发。
    """

    def __init__(self) -> None:
        srv, cli = socket.socketpair()
        self.conn = RPCConnection(srv)
        self.client_sock = cli        # 交给 MainProcessClient.connect
        self.requests: list[dict] = []
        self.request_seen = threading.Event()   # 每读到一条请求就置位
        self.thread: threading.Thread | None = None

    def start(self, script: list) -> None:
        def run() -> None:
            try:
                for step in script:
                    step(self)
            except OSError:
                pass                  # 客户端 close → 对端断开，正常收场
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def recv_request(self) -> dict:
        msg = self.conn.recv_message()
        self.requests.append(msg)
        self.request_seen.set()
        return msg


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

        # 2026-09-23 界内写放行（path.write_allow）后，fs_write 不再触发
        # 审批。审批通路要一个**仍然会问人**的工具：bash 没有路径约束，
        # 非只读命令永远落 bash.requires_approval（顺序即安全语义）。
        def run_command(command: str) -> str:
            tracked["calls"] += 1
            return "ran"

        registry.register(Tool(name="bash", description="跑命令",
                               handler=run_command))
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
                    ToolCall("c1", "bash", {"command": "touch made-by-agent.txt"}),
                ]),
                ModelReply(kind="final", text="跑完了"),
            ])
            resp = client.call("agent/send", {"sessionId": sid, "message": "跑命令"})
            self.assertEqual(tracked["calls"], 1)
            self.assertEqual(prompts[0][0], "bash.requires_approval")  # 审批真的弹了
            self.assertIn("跑完了", resp["result"]["output"])
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
                    ToolCall("c1", "bash", {"command": "touch made-by-agent.txt"}),
                ]),
                ModelReply(kind="final", text="好吧不跑了"),
            ])
            resp = client.call("agent/send", {"sessionId": sid, "message": "跑命令"})
            self.assertEqual(tracked["calls"], 0)  # 拒绝：不碰 handler
            # s07-b：transcript 从 store 存档读（server.sessions 裸字典已退役）
            tool_msgs = " ".join(
                m.get("content", "") for m in server._manager.load_record(sid).messages
                if m["role"] == "tool"
            )
            self.assertIn("permission_blocked", tool_msgs)
            self.assertIn("bash.requires_approval", tool_msgs)
            self.assertEqual(resp["result"]["output"], "好吧不跑了")
            client.call("sidecar/shutdown")
            client.close(); thread.join(timeout=2)

    def test_dispatch_loop_consumes_event_notifications(self) -> None:
        """event 通知被 on_event 消费，result 仍正确返回（不被 event 挡住）。

        2026-09-26 语义更新：通知改走分派线程，事件到达与 call 返回是
        **异步**的（旧版在单线程分派环里被顺序保证）。前端走 SSE 本来
        就不依赖 call 返回时机，所以异步是纯收益；测试改成限时等到。
        """

        events: list[dict] = []
        server, client, thread = self._run_server(
            ".", {}, lambda rule_id, reason: True)
        client._on_event = events.append
        sid = client.call("session/create")["result"]["sessionId"]
        resp = client.call("agent/send", {"sessionId": sid, "message": "hi"})
        self.assertIn("result", resp)
        deadline = time.time() + 2
        while time.time() < deadline and not any(
                e.get("event") == "round_start" for e in events):
            time.sleep(0.01)
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

    # ── 多路复用（2026-09-26 重构）：用 _ScriptedPeer 钉核心行为 ──

    def test_concurrent_calls_get_their_own_responses(self) -> None:
        """两个线程并发 call，乱序到达的响应各回各家（旧版做不到）。

        旧版单线程分派环下，第二个 call 的响应会被第一个 call 当成自己的
        （或永远等不到）——这是死锁违约③的直接后果，也是壳必须拿
        _rpc_lock 串行化的原因。新版按 id 配对，乱序无所谓。
        """

        peer = _ScriptedPeer()

        def out_of_order(p: _ScriptedPeer) -> None:
            r1 = p.recv_request()
            r2 = p.recv_request()            # 两个请求都到齐
            p.conn.send_message({"jsonrpc": "2.0", "result": "B先回",
                                 "id": r2["id"]})
            p.conn.send_message({"jsonrpc": "2.0", "result": "A后回",
                                 "id": r1["id"]})

        peer.start([out_of_order])
        client = MainProcessClient(on_event=lambda data: None)
        client.connect(peer.client_sock)

        results: dict[str, str] = {}

        def do(key: str, method: str) -> None:
            results[key] = client.call(method)["result"]

        t1 = threading.Thread(target=do, args=("a", "method/a"))
        t2 = threading.Thread(target=do, args=("b", "method/b"))
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(results, {"a": "A后回", "b": "B先回"})
        client.close()

    def test_event_flows_without_call_in_flight(self) -> None:
        """没有 call 在飞，事件照样被消费（旧版：没人 recv = 事件堵在管道里）。"""

        got: list[dict] = []
        peer = _ScriptedPeer()
        peer.start([lambda p: p.conn.send_message(
            {"jsonrpc": "2.0", "method": "event",
             "params": {"event": "tick"}, "id": None})])
        client = MainProcessClient(on_event=got.append)
        client.connect(peer.client_sock)

        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual([g.get("event") for g in got], ["tick"])
        client.close()

    def test_approval_answered_even_without_call_in_flight(self) -> None:
        """审批请求在没有 call 在飞时照样被应答回票（旧版的结构性盲区）。

        旧版审批的消费依赖"此刻有 call 阻塞在 recv"；新版读线程常驻，
        通知随时被消费、回程票随时发得回去——这是第 2 步 turn 后台化的
        前提（turn 在后台跑时，客户端根本没有"阻塞中的 call"）。
        """

        prompts: list[tuple[str, str]] = []
        peer = _ScriptedPeer()

        def challenge(p: _ScriptedPeer) -> None:
            p.conn.send_message({"jsonrpc": "2.0", "method": "approval/request",
                                 "params": {"request_id": "ticket-1",
                                            "rule_id": "bash.requires_approval",
                                            "reason": "跑命令"}, "id": None})
            p.receipt = p.conn.recv_message()   # 等回程票

        peer.start([challenge])
        client = MainProcessClient(
            user_prompt=lambda rid, why:
                prompts.append((rid, why)) or False)
        client.connect(peer.client_sock)

        deadline = time.time() + 2
        while time.time() < deadline and not hasattr(peer, "receipt"):
            time.sleep(0.01)
        self.assertEqual(prompts, [("bash.requires_approval", "跑命令")])
        receipt = getattr(peer, "receipt", None)
        self.assertIsNotNone(receipt, "对端没等到 approval/response")
        self.assertEqual(receipt["params"]["request_id"], "ticket-1")
        self.assertFalse(receipt["params"]["approved"])   # 假审批员回 False
        client.close()

    def test_close_fails_pending_calls(self) -> None:
        """close 时挂着的 call 全部被唤醒并诚实失败——没人该陪收尾挂死。"""

        peer = _ScriptedPeer()
        peer.start([lambda p: p.recv_request()])   # 读走请求，永不回
        client = MainProcessClient()
        client.connect(peer.client_sock)

        outcome: dict = {}

        def caller() -> None:
            try:
                client.call("slow/thing")
            except Exception as exc:               # noqa: BLE001——测试要抓一切
                outcome["exc"] = exc

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        self.assertTrue(peer.request_seen.wait(2), "对端没读到请求")
        time.sleep(0.05)                           # 让 call 真正挂上 future
        client.close()
        t.join(timeout=2)
        self.assertIsInstance(outcome.get("exc"), ConnectionClosed)

    def test_call_timeout_discards_late_response(self) -> None:
        """超时抛 TimeoutError；晚到的响应被丢弃，不污染下一次 call。"""

        peer = _ScriptedPeer()

        def late_then_fast(p: _ScriptedPeer) -> None:
            r1 = p.recv_request()
            time.sleep(0.4)                        # 让第一个 call 超时
            p.conn.send_message({"jsonrpc": "2.0", "result": "太晚了",
                                 "id": r1["id"]})
            r2 = p.recv_request()
            p.conn.send_message({"jsonrpc": "2.0", "result": "及时",
                                 "id": r2["id"]})

        peer.start([late_then_fast])
        client = MainProcessClient()
        client.connect(peer.client_sock)
        with self.assertRaises(TimeoutError):
            client.call("slow/thing", timeout=0.1)
        # 关键：晚到的 r1 响应绝不能被配给 r2（id 配对，不是"下一个响应"）
        self.assertEqual(client.call("fast/thing", timeout=2)["result"], "及时")
        client.close()


class CompactionTranscriptTests(unittest.TestCase):
    """压缩与 append-only 证据的共存：**压缩不能改落盘历史**。

    回归 2026-09-19 那个 P0：`turn_runner` 早先把 `result.messages`（含压缩视图）
    直接回写 Record，而 session.py 的不变量是「record.messages 单调增长、trim
    从不回写 Record」。压缩一旦真出手（L1~L4 任一），回写的前缀就与已落盘的不同
    → `jsonl_store.save` 判"证据被回改" → **第二轮直接卡死**。

    触发条件刻意压得很小（预算 100 + 保留 4 条），否则默认 60K 预算下短会话
    压根不压缩，这个坑测不出来。
    """

    def _build(self, budget: int, max_history: int) -> SidecarServer:
        registry = ToolRegistry()
        registry.register(Tool(name="probe", description="测试用工具",
                               handler=lambda: "填充内容。" * 300))
        model = ScriptedModel([
            ModelReply(kind="tool_calls",
                       tool_calls=[ToolCall("c1", "probe", {})]),
            ModelReply(kind="tool_calls",
                       tool_calls=[ToolCall("c2", "probe", {})]),
            ModelReply(kind="final", text="第一条回答"),
            ModelReply(kind="final", text="第二条回答"),
        ])
        return SidecarServer(
            model=model,
            registry=registry,
            policy=build_default_policy(),
            store=JsonlSessionStore(root=Path(tempfile.mkdtemp())),
            history_seed=lambda: [{"role": "system", "content": "系统提示"}],
            compact_budget=budget,
            max_history=max_history,
        )

    def test_compaction_keeps_the_evidence_append_only(self) -> None:
        """压缩真出手时，落盘的仍是**原始历史**——第二轮照常能发。"""

        server = self._build(budget=100, max_history=4)
        store = server._manager.store
        sid = server._handle_session_create({})["sessionId"]

        first = server._handle_agent_send({"sessionId": sid, "message": "第一问"})
        self.assertEqual(first.get("status"), "completed")
        after_first = store.load(sid).messages

        # 第二轮：压缩会动手（日志里能看到 L3/L4），但**不能碰落盘前缀**
        second = server._handle_agent_send({"sessionId": sid, "message": "第二问"})
        self.assertEqual(second.get("status"), "completed")

        after_second = store.load(sid).messages
        self.assertGreater(len(after_second), len(after_first))
        self.assertEqual(
            after_second[:len(after_first)], after_first,
            "落盘历史被改写了——append-only 不变量被破坏（压缩视图不该进证据）")

    def test_compaction_leaves_transcript_untouched_when_idle(self) -> None:
        """预算充裕时压缩不跑，落盘自然也不受影响（既有行为的回归）。"""

        server = self._build(budget=60_000, max_history=20)
        store = server._manager.store
        sid = server._handle_session_create({})["sessionId"]

        server._handle_agent_send({"sessionId": sid, "message": "第一问"})
        after_first = store.load(sid).messages
        server._handle_agent_send({"sessionId": sid, "message": "第二问"})
        after_second = store.load(sid).messages

        self.assertEqual(after_second[:len(after_first)], after_first)


if __name__ == "__main__":
    unittest.main()