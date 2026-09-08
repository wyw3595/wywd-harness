"""sidecar_panel 聚合逻辑的离线单测：假壳注入，不起 HTTP / 不起 sidecar。

s09 之后所有壳共享同一个证据目录——每个壳的 session/list 都是同一份
全量清单，_collect_sessions 必须按 sid 去重；这些测试钉住该契约。
"""

import copy
import unittest

from scripts import sidecar_panel


class FakeShell:
    """够用的假壳：sessions() 吐罐头清单，session_id 指向当前会话。"""

    def __init__(self, rows: list[dict], sid: str = "") -> None:
        self._rows = rows
        self.session_id = sid

    def sessions(self) -> dict:
        return {"sessions": copy.deepcopy(self._rows)}


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


if __name__ == "__main__":
    unittest.main()
