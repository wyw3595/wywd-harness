"""sidecar_panel 聚合逻辑的离线单测：假壳注入，不起 HTTP / 不起 sidecar。

s09 之后所有壳共享同一个证据目录——每个壳的 session/list 都是同一份
全量清单，_collect_sessions 必须按 sid 去重；这些测试钉住该契约。
"""

import copy
import unittest

from scripts import sidecar_panel


class FakeShell:
    """够用的假壳：sessions() 吐罐头清单，session_id 指向当前会话。

    面板四操作（create/resume/close/forget）也给了罐头实现——桥测试
    要从 _run_action 全链路走一遍，不能只测 _notify_bridge。
    """

    def __init__(self, rows: list[dict], sid: str = "",
                 new_sid: str = "sess_0099") -> None:
        self._rows = rows
        self.session_id = sid
        self._new_sid = new_sid

    def sessions(self) -> dict:
        return {"sessions": copy.deepcopy(self._rows)}

    def new_session(self) -> str:
        return self._new_sid

    def resume_session(self, sid: str) -> dict:
        return {"generation": 2}

    def close_session(self, sid: str = "") -> dict:
        return {"status": "ok", "closed": sid or self.session_id}

    def forget_session(self, sid: str = "") -> dict:
        return {"status": "ok"}


class DeadShell:
    """连接已断的壳：sessions() 抛错（侧边栏要的是错误行，不是 500）。"""

    def sessions(self) -> dict:
        raise RuntimeError("connection closed")


def _row(sid: str, status: str = "idle", live: bool = True) -> dict:
    return {"id": sid, "status": status, "live": live, "runtimeGeneration": 1}


class CollectSessionsTests(unittest.TestCase):
    def tearDown(self) -> None:
        # 模块级注册表是共享状态：每个测试清场，别互相渗透
        for tid in list(sidecar_panel._shells):
            sidecar_panel.unregister(tid)

    def test_empty_registry_returns_empty_list(self) -> None:
        self.assertEqual(sidecar_panel._collect_sessions(None), [])

    def test_dedupes_rows_from_shared_store(self) -> None:
        """两个 tab 的壳看到同一份清单 → 侧边栏只显示一份。"""

        rows = [_row("sess_0001"), _row("sess_0002", status="closed", live=False)]
        sidecar_panel.register("tab-a", FakeShell(rows))
        sidecar_panel.register("tab-b", FakeShell(rows))
        collected = sidecar_panel._collect_sessions(None)
        self.assertEqual([r["id"] for r in collected], ["sess_0001", "sess_0002"])

    def test_current_row_wins_dedup(self) -> None:
        """去重时"当前会话"行优先——它带着正确的归属 thread。"""

        rows = [_row("sess_0001")]
        sidecar_panel.register("tab-a", FakeShell(rows))               # 先到（非默认）
        sidecar_panel.register("tab-b", FakeShell(rows, sid="sess_0001"))  # 后到=默认
        collected = sidecar_panel._collect_sessions(None)
        self.assertEqual(len(collected), 1)
        self.assertTrue(collected[0]["current"])
        self.assertEqual(collected[0]["thread"], "tab-b")

    def test_live_aggregated_across_shells(self) -> None:
        """live 是进程本地事实：别页在跑的会话，本页壳看它 live=False。

        聚合后合并行必须恢复 live=True + liveThread 指向持有运行时的 tab
        ——否则别页的活会话会被当成 closed 放出 resume/forget 按钮
        （跨进程双运行时的误伤源头，s07-c 修复的契约）。
        """

        # tab-a（请求者视角）看到 sess_0002 是 closed；tab-b 的运行时里有它
        rows_a = [_row("sess_0001", live=False),
                  _row("sess_0002", status="closed", live=False)]
        rows_b = [_row("sess_0001", live=False),
                  _row("sess_0002", status="idle", live=True)]
        sidecar_panel.register("tab-a", FakeShell(rows_a))
        sidecar_panel.register("tab-b", FakeShell(rows_b, sid="sess_0002"))
        collected = sidecar_panel._collect_sessions("tab-a")
        by_id = {r["id"]: r for r in collected}
        # sess_0001：两边都不活 → closed
        self.assertFalse(by_id["sess_0001"]["live"])
        self.assertEqual(by_id["sess_0001"]["liveThread"], "")
        # sess_0002：本页视角 closed，但 tab-b 的运行时里有它 → 聚合回 live
        self.assertTrue(by_id["sess_0002"]["live"])
        self.assertEqual(by_id["sess_0002"]["liveThread"], "tab-b")
        # 展示行仍是请求者（tab-a）视角的副本——own/current 按它判定
        self.assertTrue(by_id["sess_0002"]["own"])
        self.assertFalse(by_id["sess_0002"]["current"])

    def test_dead_shell_yields_error_row_not_500(self) -> None:
        sidecar_panel.register("tab-dead", DeadShell())
        sidecar_panel.register("tab-ok", FakeShell([_row("sess_0001")]))
        collected = sidecar_panel._collect_sessions(None)
        errors = [r for r in collected if "error" in r]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["thread"], "tab-dead")
        self.assertEqual([r["id"] for r in collected if "id" in r], ["sess_0001"])

    def test_unregister_falls_back_to_previous_default(self) -> None:
        rows = [_row("sess_0001")]
        sidecar_panel.register("tab-a", FakeShell(rows))
        sidecar_panel.register("tab-b", FakeShell(rows))
        self.assertEqual(sidecar_panel._default_thread, "tab-b")
        sidecar_panel.unregister("tab-b")
        self.assertEqual(sidecar_panel._default_thread, "tab-a")
        sidecar_panel.unregister("tab-a")
        self.assertIsNone(sidecar_panel._default_thread)


class BridgeTests(unittest.TestCase):
    """聊天桥契约：resume/create 成功 → 通知对应页面；失败不拖垮操作。

    桥是"点会话，聊天区变成那个会话的对话"的呈现环节（chainlit_app 的
    _make_chat_bridge 负责干活），这里钉住 sidecar_panel 侧的路由契约。
    """

    def tearDown(self) -> None:
        # 模块级注册表是共享状态：每个测试清场，别互相渗透
        for tid in list(sidecar_panel._shells):
            sidecar_panel.unregister(tid)

    def test_resume_and_create_notify_bridge(self) -> None:
        calls = []
        sidecar_panel.register("tab-a", FakeShell([_row("sess_0001")]))
        sidecar_panel.register_bridge(
            "tab-a",
            lambda action, sid, detail="": calls.append((action, sid, detail)))
        self.assertTrue(
            sidecar_panel._run_action("resume", "sess_0002", "tab-a")["ok"])
        self.assertTrue(
            sidecar_panel._run_action("create", "", "tab-a")["ok"])
        self.assertEqual(
            calls, [("resume", "sess_0002", "generation 2"),
                    ("create", "sess_0099", "")])

    def test_bridge_failure_does_not_break_action(self) -> None:
        """桥炸了（页面已关/上下文过期）只是没呈现，操作结果必须完好。"""

        def boom(*args):
            raise RuntimeError("页面已关")

        sidecar_panel.register("tab-a", FakeShell([_row("sess_0001")]))
        sidecar_panel.register_bridge("tab-a", boom)
        result = sidecar_panel._run_action("resume", "sess_0001", "tab-a")
        self.assertTrue(result["ok"])

    def test_bridge_never_crosses_threads(self) -> None:
        """tab-a 的操作绝不落到 tab-b 的桥——重放串台 = 对着错误的嘴说话。"""

        calls = []
        sidecar_panel.register("tab-a", FakeShell([_row("sess_0001")]))
        sidecar_panel.register("tab-b", FakeShell([_row("sess_0001")]))
        sidecar_panel.register_bridge(
            "tab-b", lambda action, sid, detail="": calls.append((action, sid)))
        sidecar_panel._run_action("resume", "sess_0001", "tab-a")
        self.assertEqual(calls, [])

    def test_unregister_removes_bridge(self) -> None:
        sidecar_panel.register("tab-a", FakeShell([_row("sess_0001")]))
        sidecar_panel.register_bridge("tab-a", lambda *a: None)
        sidecar_panel.unregister("tab-a")
        self.assertNotIn("tab-a", sidecar_panel._bridges)


if __name__ == "__main__":
    unittest.main()
