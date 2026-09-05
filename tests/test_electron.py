"""s05 桌面壳的可测核心：协议面 + ElectronMain 路由/审批 + PreloadBridge 分派环。

全离线：不真起子进程。用假队列（FakeQueue）和"假 send/recv 回调"把两个
类单测锁死——路由逻辑和"队列长什么样"解耦，这正是依赖注入的回报。
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from src.harness.electron import (
    INCOMING_RENDERER,
    OUTGOING_MAIN,
    ElectronMain,
    PreloadBridge,
    PreloadBridgeClosed,
)
from src.harness.models import FakeModel, ModelReply, ScriptedModel, ToolCall
from src.harness.permissions import (
    PermissionAction,
    WorkspaceScope,
    build_default_policy,
)
from src.harness.tools import Tool, ToolRegistry


class FakeQueue:
    """list 底假队列：get 从头弹出（缺省抛超时哨兵），put 只收集不混入。

    real multiprocessing.Queue 的 get(timeout=) 超时会抛 queue.Empty；
    这里用自定义哨兵，语义一样：空队列取不到 = 时间到。
    """

    class _Empty(Exception):
        pass

    def __init__(self, items: list | None = None) -> None:
        self._items = list(items or [])
        self.calls: list[object] = []  # put 的入参，测试断言用

    def put(self, item: object) -> None:
        self.calls.append(item)

    def get(self, timeout: float | None = None) -> object:
        if not self._items:
            raise FakeQueue._Empty()
        return self._items.pop(0)


class ProtocolTests(unittest.TestCase):
    """协议面冻结：类型集合 + 审批消息形状。"""

    def test_ipc_type_sets_frozen(self) -> None:
        """类型集合与预期相等——加协议类型必须改这里，锁住协议面。"""

        self.assertEqual(
            INCOMING_RENDERER,
            {"ping", "session/list", "agent/message", "approval/response"},
        )
        self.assertEqual(
            OUTGOING_MAIN,
            {"pong", "result", "approval/request", "event"},
        )

    def test_approval_request_carries_request_id_rule_reason(self) -> None:
        """approver 发出的审批请求要有票号、规则号、人话理由。"""

        sent: list[dict] = []
        main = ElectronMain(
            model=FakeModel(),
            registry=ToolRegistry(),
            policy=build_default_policy(),
            send=sent.append,
            recv=lambda: None,
        )
        from src.harness.permissions import PermissionDecision, PermissionAction

        fake_dec = PermissionDecision(
            request=None, action=PermissionAction.ASK,  # type: ignore
            rule_id="path.write_ask", reason="写入 notes.txt 需审批",
        )
        main._make_approver()(fake_dec)
        approval = sent[0]["data"]
        self.assertEqual(sent[0]["type"], "approval/request")
        self.assertTrue(approval["request_id"])  # 票号非空
        self.assertEqual(approval["rule_id"], "path.write_ask")
        self.assertIn("notes.txt", approval["reason"])


class ElectronMainTests(unittest.TestCase):
    """主进程路由：ping / 会话簿 / 历史延续 / ASK 审批两条路 / 事件直播。"""

    def _make_tmp_write_registry(self, tmp: str, tracked: dict) -> ToolRegistry:
        """造一个"会触发 write_ask 的写工具"注册表，handler 写 tmp 目录。"""

        registry = ToolRegistry()

        def handler(path: str, text: str) -> str:
            tracked["calls"] += 1
            (Path(tmp) / path).write_text(text, encoding="utf-8")
            return f"已写入 {path}"

        registry.register(Tool(
            name="fs_write", description="写入文件",
            handler=handler,
        ))
        return registry

    def test_ping_routes_pong(self) -> None:
        main = ElectronMain(
            model=FakeModel(),
            registry=ToolRegistry(),
            policy=build_default_policy(),
            send=lambda m: None,
            recv=lambda: None,
        )
        self.assertEqual(
            main.route({"type": "ping"}), {"type": "pong", "data": "main alive"}
        )

    def test_session_list_empty_then_populated(self) -> None:
        main = ElectronMain(
            model=FakeModel(),
            registry=ToolRegistry(),
            policy=build_default_policy(),
            send=lambda m: None,
            recv=lambda: None,
        )
        self.assertEqual(main.route({"type": "session/list"})["data"], [])
        main.route({"type": "agent/message", "data": "任务甲"})
        self.assertEqual(
            main.route({"type": "session/list"})["data"], ["任务甲"]
        )

    def test_agent_message_persists_history(self) -> None:
        """两次提问：第二次模型收到的历史里要有第一次的问答。"""

        scripted = ScriptedModel([
            ModelReply(kind="final", text="甲"),
            ModelReply(kind="final", text="乙"),
        ])
        main = ElectronMain(
            model=scripted,
            registry=ToolRegistry(),
            policy=build_default_policy(),
            send=lambda m: None,
            recv=lambda: None,
        )
        main.route({"type": "agent/message", "data": "任务甲"})
        main.route({"type": "agent/message", "data": "任务乙"})
        second_input = scripted.received_inputs[1]
        self.assertEqual(second_input[0]["role"], "user")
        self.assertIn("任务甲", str(second_input))  # 第一次的问答还在历史里

    def _recv_echo_ticket(self, sent: list[dict], approved: bool) -> Callable:
        """假 recv：回显 approver 刚发出的那张票（回程票机制的真测试）。

        approver 发审批请求会带一个 uuid 票号，测试要"凭同一张票"回执——
        从已发出消息里把票捞回来回显，锁住"只认同一票"的机制。
        """

        def recv() -> dict | None:
            for m in reversed(sent):
                if m["type"] == "approval/request":
                    return {"type": "approval/response", "data": {
                        "request_id": m["data"]["request_id"], "approved": approved,
                    }}
            return None

        return recv

    def test_ask_grants_write_executes(self) -> None:
        """ASK 批准：走 IPC 回执，handler 真的被调，结果不含拦截文案。"""

        with tempfile.TemporaryDirectory() as tmp:
            tracked = {"calls": 0}
            registry = self._make_tmp_write_registry(tmp, tracked)
            policy = build_default_policy(
                scope=WorkspaceScope(Path(tmp)), write_tools=["fs_write"],
            )
            sent: list[dict] = []
            main = ElectronMain(
                model=ScriptedModel([
                    ModelReply(kind="tool_calls", tool_calls=[
                        ToolCall("c1", "fs_write", {"path": "notes.txt", "text": "你好"}),
                    ]),
                    ModelReply(kind="final", text="写好了"),
                ]),
                registry=registry,
                policy=policy,
                send=sent.append,
                recv=self._recv_echo_ticket(sent, approved=True),
            )
            result = main.route({"type": "agent/message", "data": "写文件"})
            self.assertIsInstance(sent[0], dict)
            self.assertEqual(sent[0]["type"], "approval/request")
            self.assertEqual(tracked["calls"], 1)  # 批准后真的执行了
            self.assertEqual(result["data"], "写好了")

    def test_ask_reject_blocks_with_permission_blocked(self) -> None:
        """ASK 拒绝：handler 不碰，历史里的工具消息回灌 permission_blocked。"""

        with tempfile.TemporaryDirectory() as tmp:
            tracked = {"calls": 0}
            registry = self._make_tmp_write_registry(tmp, tracked)
            policy = build_default_policy(
                scope=WorkspaceScope(Path(tmp)), write_tools=["fs_write"],
            )
            sent: list[dict] = []
            main = ElectronMain(
                model=ScriptedModel([
                    ModelReply(kind="tool_calls", tool_calls=[
                        ToolCall("c1", "fs_write", {"path": "notes.txt", "text": "你好"}),
                    ]),
                    ModelReply(kind="final", text="好吧，不写了"),
                ]),
                registry=registry,
                policy=policy,
                send=sent.append,
                recv=self._recv_echo_ticket(sent, approved=False),
            )
            result = main.route({"type": "agent/message", "data": "写文件"})
            self.assertEqual(tracked["calls"], 0)  # 拒绝：不碰 handler
            # 回灌发生在历史里的 tool 消息，不是最终回答里——
            # 模型应"看到"permission_blocked + 规则号，才懂为什么被拦。
            tool_contents = " ".join(
                m.get("content", "") for m in main._history if m["role"] == "tool"
            )
            self.assertIn("permission_blocked", tool_contents)
            self.assertIn("path.write_ask", tool_contents)

    def test_ask_rejected_when_main_shutting_down(self) -> None:
        """审批途中 main 被关停（recv 返回 None）→ 一律拒绝（fail-closed）。"""

        sent: list[dict] = []
        main = ElectronMain(
            model=FakeModel(),
            registry=ToolRegistry(),
            policy=build_default_policy(),
            send=sent.append,
            recv=lambda: None,  # 关停哨兵
        )
        approver = main._make_approver()
        from src.harness.permissions import PermissionDecision, PermissionAction

        fake_dec = PermissionDecision(
            request=None, action=PermissionAction.ASK,  # type: ignore
            rule_id="path.write_ask", reason="x",
        )
        self.assertFalse(approver(fake_dec))

    def test_agent_event_streamed_via_send(self) -> None:
        """on_event 钩子透传：跑 agent 时事件被广播出来（练习 15 的接线）。"""

        registry = ToolRegistry()
        registry.register(Tool(
            name="get_weather", description="天气",
            handler=lambda city: "晴",  # type: ignore
        ))
        events: list[tuple[str, dict]] = []
        main = ElectronMain(
            model=ScriptedModel([
                ModelReply(kind="tool_calls", tool_calls=[
                    ToolCall("c1", "get_weather", {"city": "北京"}),
                ]),
                ModelReply(kind="final", text="北京晴"),
            ]),
            registry=registry,
            policy=build_default_policy(safe_tools=["get_weather"]),
            send=lambda m: None,
            recv=lambda: None,
            on_event=lambda event, data: events.append((event, data)),
        )
        main.route({"type": "agent/message", "data": "天气？"})
        names = [name for name, _ in events]
        self.assertIn("round_start", names)
        self.assertIn("tool_start", names)
        self.assertIn("tool_end", names)


class PreloadBridgeTests(unittest.TestCase):
    """渲染桥：三个公开方法 + 分派环（审批就地消费、直播就地消费）。"""

    def test_send_message_returns_result_data(self) -> None:
        send_q = FakeQueue()
        recv_q = FakeQueue([{"type": "result", "data": "OK"}])
        bridge = PreloadBridge(send_q, recv_q)
        self.assertEqual(bridge.send_message("任务"), "OK")
        self.assertEqual(send_q.calls[0], {"type": "agent/message", "data": "任务"})

    def test_ping_returns_pong(self) -> None:
        send_q = FakeQueue()
        recv_q = FakeQueue([{"type": "pong", "data": "main alive"}])
        bridge = PreloadBridge(send_q, recv_q)
        self.assertEqual(bridge.ping(), "main alive")

    def test_ask_flushed_before_result(self) -> None:
        """分派环核心：审批请求和直播事件混在半路时，就地处理、继续等结果。"""

        send_q = FakeQueue()
        recv_q = FakeQueue([
            {"type": "approval/request", "data": {
                "request_id": "t-1", "rule_id": "path.write_ask", "reason": "写文件",
            }},
            {"type": "event", "data": {"name": "tool_start"}},
            {"type": "result", "data": "写好了"},
        ])
        prompts: list[tuple[str, str]] = []
        events: list[dict] = []
        bridge = PreloadBridge(
            send_q, recv_q,
            user_prompt=lambda rule_id, reason: prompts.append((rule_id, reason)) or True,
            on_event=events.append,
        )
        self.assertEqual(bridge.send_message("写文件"), "写好了")
        # 回执已发出，且带着同一张票 + 批准
        resp = send_q.calls[1]["data"]
        self.assertEqual(send_q.calls[1]["type"], "approval/response")
        self.assertEqual(resp["request_id"], "t-1")
        self.assertTrue(resp["approved"])
        # 审批理由喂给了用户交互，事件被直播消费
        self.assertEqual(prompts, [("path.write_ask", "写文件")])
        self.assertEqual(events, [{"name": "tool_start"}])

    def test_ask_reject_flushes_and_proceeds(self) -> None:
        """拒绝也照常回执（approved=False），分派环不停，仍等到 result。"""

        send_q = FakeQueue()
        recv_q = FakeQueue([
            {"type": "approval/request", "data": {
                "request_id": "t-2", "rule_id": "path.write_ask", "reason": "写文件",
            }},
            {"type": "result", "data": "好吧"},
        ])
        bridge = PreloadBridge(
            send_q, recv_q, user_prompt=lambda rule_id, reason: False,
        )
        self.assertEqual(bridge.send_message("写文件"), "好吧")
        resp = send_q.calls[1]["data"]
        self.assertFalse(resp["approved"])
        self.assertEqual(resp["request_id"], "t-2")

    def test_preload_closed_on_sentinel(self) -> None:
        """None 哨兵：不是"跳过"，是关停——整个渲染层要退出。"""

        send_q = FakeQueue()
        recv_q = FakeQueue([None])
        bridge = PreloadBridge(send_q, recv_q)
        with self.assertRaises(PreloadBridgeClosed):
            bridge.send_message("任务")


if __name__ == "__main__":
    unittest.main()