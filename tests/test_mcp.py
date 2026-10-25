"""s17 MCP Connectors 的离线单元测试：两道门 × 生命周期 × 真轨道接线。

覆盖三条线：
  - 内核（mcp.py）：grant 契约 / 信任持久化 / 连接器生命周期 /
    connected ∩ grant 可见性 / dispatch 执行期复查；
  - 装配（toolbox）：mcp.json → manager、MCP 工具注册成**真**延迟工具、
    权限 ASK 规则、seed 的连接器目录段；
  - 真轨道端到端：ToolSearch 精确加载 → defer_execute → 服务端被调，
    以及"未加载先拒""撤权后拒"两条 fail-closed 路径。

全部离线：进程内连接器 + 临时目录，不碰网络、不碰真实 mcp.json。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.harness.mcp import (
    NO_MCP_PERMISSIONS,
    InProcessMCPServer,
    MCPConnector,
    MCPPermissionGrant,
    TrustStore,
    ConnectorManager,
    grant_from_config,
    namespaced_tool_name,
)
from src.harness.permissions import PermissionAction, ToolRequest
from src.harness.workspace import Workspace


def _server() -> InProcessMCPServer:
    def echo(arguments: dict) -> str:
        """把文本原样返回（测试连接器）。"""
        return f"回声：{arguments.get('text', '')}"

    def ping(arguments: dict) -> str:
        """探活。"""
        return "pong"

    def secret(arguments: dict) -> str:
        """不在 grant 名单里的工具。"""
        return "不应被调用"

    return InProcessMCPServer({"echo": echo, "ping": ping, "secret": secret})


def _config() -> dict:
    return {
        "mcpServers": {
            "demo": {
                "type": "inprocess",
                "grant": {"tools": ["echo", "ping"], "network": True},
            },
            "other": {
                "type": "inprocess",
                "grant": {"tools": ["ping"], "network": True},
            },
        }
    }


def _manager(tmp: Path, *, config: dict | None = None,
             servers: dict | None = None) -> ConnectorManager:
    """未信任起步的 manager（测试从"用户点 Trust"开始走全流程）。"""

    return ConnectorManager(
        config or _config(),
        servers=servers if servers is not None
        else {"demo": _server(), "other": _server()},
        trust_store=TrustStore(tmp / "trust.json"),
        permission_grant=grant_from_config(config or _config()),
    )


# ═══════════════════════════════════════════════════════════════
# 一、grant 契约：只能收窄，构造期挡住一切"会放宽"的写法
# ═══════════════════════════════════════════════════════════════

class GrantContractTests(unittest.TestCase):

    def test_bare_names_rejected(self) -> None:
        # 裸名会把授权意外放宽到别的连接器的同名工具上——构造期就炸。
        with self.assertRaises(ValueError):
            MCPPermissionGrant(tools={"create_pr"})
        with self.assertRaises(ValueError):
            MCPPermissionGrant(tools="mcp__demo__echo")   # 字符串不是集合
        with self.assertRaises(ValueError):
            MCPPermissionGrant(network="yes")             # type: ignore[arg-type]

    def test_allows_needs_both_network_and_membership(self) -> None:
        listed = MCPPermissionGrant(
            tools=frozenset({"mcp__demo__echo"}), network=True)
        self.assertTrue(listed.allows("mcp__demo__echo"))
        self.assertFalse(listed.allows("mcp__demo__ping"))    # 名单外
        no_network = MCPPermissionGrant(
            tools=frozenset({"mcp__demo__echo"}), network=False)
        self.assertFalse(no_network.allows("mcp__demo__echo"))  # 网络位没开
        self.assertFalse(NO_MCP_PERMISSIONS.allows("mcp__demo__echo"))

    def test_grant_from_config_merges_and_gates_network(self) -> None:
        grant = grant_from_config(_config())
        self.assertEqual(
            grant.tools,
            frozenset({"mcp__demo__echo", "mcp__demo__ping",
                       "mcp__other__ping"}))
        self.assertTrue(grant.network)

        # network 是与门：任何一段没开，整体就没有网络位。
        partial = _config()
        partial["mcpServers"]["other"]["grant"]["network"] = False
        self.assertFalse(grant_from_config(partial).network)

        # 空配置：空 grant + 关网络。
        empty = grant_from_config({})
        self.assertEqual(empty.tools, frozenset())
        self.assertFalse(empty.network)


# ═══════════════════════════════════════════════════════════════
# 二、第一道门：信任（用户手动、持久化、fail-closed）
# ═══════════════════════════════════════════════════════════════

class TrustStoreTests(unittest.TestCase):

    def test_trust_round_trip_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "trust.json"
            store = TrustStore(path)
            self.assertFalse(store.is_trusted("demo"))   # 没文件 = 不信任
            store.trust("demo")
            # 新实例从盘上读：信任是持久的，重启不丢。
            self.assertTrue(TrustStore(path).is_trusted("demo"))
            self.assertFalse(TrustStore(path).is_trusted("other"))

    def test_corrupted_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trust.json"
            path.write_text("{这不是 json", encoding="utf-8")
            self.assertFalse(TrustStore(path).is_trusted("demo"))


# ═══════════════════════════════════════════════════════════════
# 三、连接器生命周期与进程内协议
# ═══════════════════════════════════════════════════════════════

class ConnectorTests(unittest.TestCase):

    def test_untrusted_connector_refuses_to_start(self) -> None:
        connector = MCPConnector(name="demo", config={})
        self.assertFalse(connector.connect(_server()))
        self.assertEqual(connector.status, "disconnected")
        self.assertIn("未信任", connector.unavailable_reason)

    def test_tools_list_failure_falls_back_to_disconnected(self) -> None:
        class ExplodingServer:
            def tools_list(self) -> list[dict]:
                raise RuntimeError("连接器崩了")

            def tools_call(self, name: str, arguments: dict) -> str:
                return ""

        connector = MCPConnector(name="demo", config={})
        connector.trusted = True
        self.assertFalse(connector.connect(ExplodingServer()))
        self.assertEqual(connector.status, "disconnected")
        self.assertIn("tools/list 失败", connector.unavailable_reason)

    def test_discovery_namespaces_immediately(self) -> None:
        connector = MCPConnector(name="demo", config={})
        connector.trusted = True
        self.assertTrue(connector.connect(_server()))
        names = {tool["name"] for tool in connector.tools}
        self.assertEqual(
            names,
            {"mcp__demo__echo", "mcp__demo__ping", "mcp__demo__secret"})
        # 两个连接器的同名工具永不撞名（命名空间化的意义）。
        self.assertEqual(namespaced_tool_name("demo", "ping"),
                         "mcp__demo__ping")
        self.assertNotEqual(namespaced_tool_name("other", "ping"),
                            namespaced_tool_name("demo", "ping"))


class InProcessServerTests(unittest.TestCase):

    def test_protocol_shape(self) -> None:
        server = _server()
        listing = server.tools_list()
        self.assertEqual({tool["name"] for tool in listing},
                         {"echo", "ping", "secret"})
        echo = next(t for t in listing if t["name"] == "echo")
        self.assertIn("inputSchema", echo)
        self.assertEqual(server.tools_call("echo", {"text": "嗨"}), "回声：嗨")
        self.assertIn("不提供工具", server.tools_call("nope", {}))


# ═══════════════════════════════════════════════════════════════
# 四、管理器：可见性 = connected ∩ grant；dispatch 执行期复查
# ═══════════════════════════════════════════════════════════════

class ManagerTests(unittest.TestCase):

    def test_untrusted_connector_is_invisible_until_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            self.assertEqual(manager.visible_tools(), [])   # 未信任 → 不可见
            self.assertTrue(manager.trust_connector("demo"))
            names = {tool["name"] for tool in manager.visible_tools()}
            # connected ∩ grant：secret 被 grant 收窄掉。
            self.assertEqual(names, {"mcp__demo__echo", "mcp__demo__ping"})

    def test_trust_persists_across_manager_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _manager(Path(tmp)).trust_connector("demo")
            # 新 manager（模拟进程重启）：受信连接器自动重连。
            restarted = _manager(Path(tmp))
            self.assertTrue(any(t["name"] == "mcp__demo__echo"
                                for t in restarted.visible_tools()))

    def test_grant_without_network_hides_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.trust_connector("demo")
            manager.set_permission_grant(MCPPermissionGrant(
                tools=frozenset({"mcp__demo__echo"}), network=False))
            self.assertEqual(manager.visible_tools(), [])

    def test_dispatch_rechecks_grant_after_revocation(self) -> None:
        # TOCTOU 防线：加载进工具池之后撤权，旧句柄照样调不动。
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.trust_connector("demo")
            self.assertIn("回声", manager.dispatch(
                "mcp__demo__echo", {"text": "hi"}))
            manager.set_permission_grant(NO_MCP_PERMISSIONS)
            result = manager.dispatch("mcp__demo__echo", {"text": "hi"})
            self.assertIn("permission_denied", result)
            self.assertIn("网络位未开启", result)

    def test_dispatch_denials_are_actionable_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.trust_connector("demo")
            # 未授权的工具：名单外（未知名字也先撞这道门——fail-closed，
            # 不泄露"这名字存在与否"的信息）。
            self.assertIn("grant 名单",
                          manager.dispatch("mcp__demo__secret", {}))
            self.assertIn("grant 名单",
                          manager.dispatch("mcp__nope__echo", {}))
            # 名单里有、归属明确、但连接器不提供它（grant 写了个
            # 不存在的工具名）——文案能指引"查名字"。
            manager.set_permission_grant(MCPPermissionGrant(
                tools=frozenset({"mcp__demo__ghost"}), network=True))
            self.assertIn("不提供工具",
                          manager.dispatch("mcp__demo__ghost", {}))
            # 已授权但连接器没连（trust 了 demo，没 trust other）。
            manager.set_permission_grant(grant_from_config(_config()))
            self.assertIn("未连接", manager.dispatch("mcp__other__ping", {}))

    def test_build_context_lists_granted_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            self.assertIn("没有已连接且已授权", manager.build_context())
            manager.trust_connector("demo")
            context = manager.build_context()
            self.assertIn("<available_deferred_tools>", context)
            self.assertIn("mcp__demo__echo", context)
            self.assertNotIn("mcp__demo__secret", context)
            # 目录里没有 schema——schema 由 ToolSearch 按需加载。
            self.assertNotIn("inputSchema", context)

    def test_status_lines_are_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp))
            manager.trust_connector("demo")
            status = manager.status_lines()
            self.assertIn("demo", status)
            self.assertIn("trusted=✓", status)
            self.assertIn("other", status)


# ═══════════════════════════════════════════════════════════════
# 五、装配接线：mcp.json → manager → 真注册表/真策略/真 seed
# ═══════════════════════════════════════════════════════════════

class ToolboxWiringTests(unittest.TestCase):
    """临时目录建 mcp.json——不碰任何真实工作区。"""

    def _workspace_root(self, tmp: Path) -> Path:
        root = Path(tmp) / "ws"
        root.mkdir()
        (root / ".workbuddy").mkdir()
        (root / ".workbuddy" / "mcp.json").write_text(
            json.dumps(_config()), encoding="utf-8")
        return root

    def test_no_config_means_no_manager(self) -> None:
        from scripts.toolbox import build_mcp_manager

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_mcp_manager(Path(tmp)))
            # 空目录连 .workbuddy 都没有 → 同样 None（零变化路径）。

    def test_stdio_declaration_fails_loudly(self) -> None:
        from scripts.toolbox import build_mcp_manager

        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace_root(tmp)
            config = _config()
            config["mcpServers"]["demo"]["type"] = "stdio"
            (root / ".workbuddy" / "mcp.json").write_text(
                json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_mcp_manager(root)
            self.assertIn("stdio", str(ctx.exception))

    def _wired(self, tmp: Path):
        """root + 已信任连接器 + 注册表 + 策略（真轨道装配）。"""
        from scripts.toolbox import (
            build_mcp_manager, build_policy_for, build_registry_for)

        root = self._workspace_root(tmp)
        manager = build_mcp_manager(
            root, servers={"demo": _server(), "other": _server()})
        manager.trust_connector("demo")
        registry = build_registry_for(
            Workspace(workspace_id="mcp-test", root=root),
            mcp_manager=manager)
        policy = build_policy_for(
            Workspace(workspace_id="mcp-test", root=root),
            mcp_manager=manager)
        return manager, registry, policy

    def test_mcp_tools_registered_as_real_deferred_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, registry, _ = self._wired(tmp)
            # 延迟目录里有；即时 schema 目录里没有——不常驻是关键。
            self.assertIn("mcp__demo__echo", registry.get_deferred_directory())
            schemas = {schema["function"]["name"]
                       for schema in registry.model_schemas()}
            self.assertNotIn("mcp__demo__echo", schemas)
            # secret 不在 grant 里：根本不注册。
            self.assertNotIn("mcp__demo__secret", registry.get_deferred_directory())

    def test_policy_asks_for_granted_and_denies_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, policy = self._wired(tmp)
            decision = policy.decide(ToolRequest(
                tool_use_id="t1", name="mcp__demo__echo", arguments={}))
            self.assertIs(decision.action, PermissionAction.ASK)
            self.assertEqual(decision.rule_id, "mcp.requires_approval")
            ungranted = policy.decide(ToolRequest(
                tool_use_id="t2", name="mcp__demo__secret", arguments={}))
            self.assertIs(ungranted.action, PermissionAction.DENY)
            # 内置工具的治理路径不受影响（新规则在前也不抢它们的道）。
            builtin = policy.decide(ToolRequest(
                tool_use_id="t3", name="fs_read", arguments={}))
            self.assertIs(builtin.action, PermissionAction.ALLOW)

    def test_full_rail_toolsearch_then_defer_execute(self) -> None:
        # 端到端：ToolSearch 精确名加载 → defer_execute → 服务端被调。
        with tempfile.TemporaryDirectory() as tmp:
            _, registry, _ = self._wired(tmp)
            loaded = registry.load_by_name(["mcp__demo__echo"])
            self.assertIn("mcp__demo__echo", loaded)
            result = registry.defer_execute(
                "mcp__demo__echo", {"text": "全链路"})
            self.assertIn("回声：全链路", result)

    def test_direct_call_without_toolsearch_is_refused(self) -> None:
        # 幻觉直呼名字（绕过 ToolSearch）被"先 ToolSearch"拦下。
        with tempfile.TemporaryDirectory() as tmp:
            _, registry, _ = self._wired(tmp)
            result = registry.defer_execute(
                "mcp__demo__echo", {"text": "直呼"})
            self.assertIn("尚未加载", result)
            self.assertIn("ToolSearch", result)

    def test_revoked_grant_blocks_even_after_schema_loaded(self) -> None:
        # handler 的执行期复查：schema 已加载、权限层也过了，
        # grant 撤了照样拒——TOCTOU 防线的第二检。
        with tempfile.TemporaryDirectory() as tmp:
            manager, registry, _ = self._wired(tmp)
            registry.load_by_name(["mcp__demo__echo"])
            manager.set_permission_grant(NO_MCP_PERMISSIONS)
            result = registry.defer_execute(
                "mcp__demo__echo", {"text": "晚了"})
            self.assertIn("授权已被撤销", result)

    def test_seed_carries_connector_directory_segment(self) -> None:
        from scripts.toolbox import plan_history_seed

        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace_root(tmp)
            manager = _manager(root)      # 直接用内核装配（root 即语料根）
            manager.trust_connector("demo")
            plan, contents = plan_history_seed(
                root=root, mcp_manager=manager)
            self.assertIn("connector_status", plan.included_names)
            self.assertIn("mcp__demo__echo", contents["connector_status"])

            # 没有连接器：整段 inactive——seed 形状零变化。
            plan_bare, _ = plan_history_seed(root=root)
            self.assertNotIn("connector_status", plan_bare.included_names)


if __name__ == "__main__":
    unittest.main()
