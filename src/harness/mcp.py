"""s17 MCP Connectors：外接工具、标准协议、信任模型。

============================================================
📚 为什么需要它（承接 s16 的技能）
============================================================
技能是 prompt 模板——告诉 agent "怎么做"；读腾讯文档、操作 GitHub PR、
发飞书消息是"能做什么"，prompt 解决不了，得真调外部服务的 API。
把每个服务写死在代码里 = 每加一个服务改一次代码。MCP（Model Context
Protocol）把外部能力标准化成两个方法——`tools/list`（你能做什么）+
`tools/call`（执行一个）——工具开发者与 agent 开发者彻底解耦。

但"外接"带来安全问题：一个恶意连接器能执行任意命令、窃取数据。
两道门缺一不可：

  第一道 **信任（Trust）**：连接器必须由用户手动信任才允许启动进程；
  第二道 **授权（Grant）**：已信任连接器的工具，只有同时出现在**已
  审核的授权名单**里、且网络位打开时，才进入工具池——"信任连接器"
  ≠ "放行它的每个工具"。

生命周期四态（工具可见性）：

    configured ──► disconnected ──► trusted ──► connected
    （配置落盘）  （进程未启动）   （用户点头）  （tools/list 完成，
                                               只可见 grant 交集内的工具）

============================================================
📚 命名空间化与延迟加载
============================================================
- 工具名统一 `mcp__<连接器>__<工具>`——内置工具与其他连接器的工具
  永不撞名；grant 名单用的也是这个全名。
- MCP 工具**不常驻 schema**：40 个连接器 × 每个 10 个工具 = 400 份
  schema 全塞进系统提示就是上下文灾难。目录（名字 + 一句话描述）进
  提示，schema 由 ToolSearch 按需加载——与内置延迟工具同一条轨道。

============================================================
📚 与教材（learn-workbuddy/s17）的差异——都是刻意的
============================================================
1. **不另造 ToolSearch / DeferExecuteTool 模拟**：教材用一对模拟函数
   演示"加载后才能执行"；本项目 s03 起就有**真的**延迟工具轨道
   （ToolRegistry.defer_execute 的三道闸：确是延迟工具、schema 已
   载、参数过校验）。MCP 工具直接注册成真延迟 Tool，骑同一条轨道
   ——机制只保留一套。
2. **transport 分层**：教材的连接器"进程"是模拟；本项目把 MCPServer
   定为协议（tools_list / tools_call），进程内实现给测试与演示，
   stdio 子进程是生产形态——spawn 子进程 + 换行分隔 JSON-RPC 的
   模式本项目 sidecar 已经示范过，接真实连接器时照搬即可（教学
   边界：本课不实现 stdio，配置里声明 type=stdio 会大声报错而不是
   静默假装连上了）。
3. **grant 的来源**：教材是"当前激活 Skill 的授权"；本项目 Skill
   manifest 只能收窄权限（s16），尚无 per-turn 的 skill grant 通道。
   适配为**项目级已审核声明**——mcp.json 里的 grant 段：这个文件
   是用户亲手写进项目的，写下去就是审核。语义不变：grant 只能
   收窄（连接器 advertise 100 个工具，grant 只放行 3 个）。
4. **执行期双检**（fail-closed，教材同款）：权限层决策是一检
   （toolbox 给 MCP 工具配 ASK 规则），工具 handler 在执行瞬间再
   查一次 grant——"先加载、后撤权、仍可执行"的 TOCTOU 缺口不在。

完成标志：tests/test_mcp.py 全部通过。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

MCP_TOOL_PREFIX = "mcp__"


# ═══════════════════════════════════════════════════════════════
# 一、传输协议：MCPServer——连接器进程对 harness 暴露的全部能力
# ═══════════════════════════════════════════════════════════════

class MCPServer(Protocol):
    """一个 MCP 服务端：两个方法就是协议的全部。

    进程内实现（测试/演示）与 stdio 子进程（生产，见 sidecar 同款
    spawn + newline JSON-RPC 模式）对 harness 完全同形——协议与
    实现分离，同 Model 协议一个道理。
    """

    def tools_list(self) -> list[dict]:
        """列出工具：[{name, description, inputSchema}]。"""

    def tools_call(self, name: str, arguments: dict) -> str:
        """执行一个工具，返回结果文本（错误也是文本，回灌给模型）。"""


class InProcessMCPServer:
    """进程内连接器：名字 → 处理函数（dict 进、字符串出）。

    测试与教学用；它实现的就是 MCPServer 协议本身，不是"假的"
    ——真实 stdio 连接器只是把这个类的两个方法换成跨进程 RPC。
    """

    def __init__(self, tools: Mapping[str, Callable[[dict], str]]) -> None:
        if not tools:
            raise ValueError("in-process 连接器至少要有一个工具")
        self._tools = dict(tools)

    def tools_list(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": (getattr(handler, "__doc__", "") or "").strip()
                or f"{name} 工具",
                # 教学形状：声明一个 text 参数——真实连接器给真实 schema。
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
            for name, handler in self._tools.items()
        ]

    def tools_call(self, name: str, arguments: dict) -> str:
        handler = self._tools.get(name)
        if handler is None:
            return f"Error: 连接器不提供工具 {name!r}"
        return str(handler(arguments))


def namespaced_tool_name(connector_name: str, tool_name: str) -> str:
    """mcp__<连接器>__<工具>——命名空间化的唯一真源。"""

    return f"{MCP_TOOL_PREFIX}{connector_name}__{tool_name}"


# ═══════════════════════════════════════════════════════════════
# 二、第二道门：授权（grant 只能收窄，永不放宽）
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MCPPermissionGrant:
    """已审核的外部工具授权：名单 + 网络位，缺一不可。

    信任决定**连接器进程**能不能跑；本 grant 单独决定它的**哪些工具**
    对当前会话可见、可调。名单必须是命名空间化的 mcp__ 全名——
    写裸名（create_pr）会在构造期炸掉：跨连接器同名工具会把授权
    意外放宽到别人的同名工具上。
    """

    tools: frozenset[str] = frozenset()
    network: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.network, bool):
            raise ValueError("MCP grant 的 network 必须是 true/false")
        if isinstance(self.tools, (str, bytes)):
            raise ValueError("MCP grant 的 tools 必须是名字集合")
        normalized = frozenset(self.tools)
        if any(
            not isinstance(tool, str) or not tool.startswith(MCP_TOOL_PREFIX)
            for tool in normalized
        ):
            raise ValueError(
                f"MCP grant 的工具名必须以 {MCP_TOOL_PREFIX} 开头"
                "（命名空间化全名）")
        object.__setattr__(self, "tools", normalized)

    def allows(self, tool_name: str) -> bool:
        """网络位 + 名单命中，两者同时成立才放行。"""

        return self.network and tool_name in self.tools


NO_MCP_PERMISSIONS = MCPPermissionGrant()


def grant_from_config(config: Mapping) -> MCPPermissionGrant:
    """从 mcp.json 的各连接器 grant 段合并出一份总授权。

    每段 {"tools": [裸名...], "network": bool}——这里负责命名空间化；
    network 是**与门**（任何一段没开网络，合并结果就没有网络位）：
    授权只能收窄，合并时不许因为别处开了就把这里也开了。
    """

    names: set[str] = set()
    network = True
    for connector_name, entry in (config.get("mcpServers") or {}).items():
        grant = (entry or {}).get("grant") or {}
        for tool in grant.get("tools") or []:
            names.add(namespaced_tool_name(connector_name, tool))
        network = network and bool(grant.get("network", False))
    if not names:
        network = False  # 空名单开网络没有意义；也给 network=False 的人对齐
    return MCPPermissionGrant(tools=frozenset(names), network=network)


# ═══════════════════════════════════════════════════════════════
# 三、第一道门：信任（用户手动，持久化）
# ═══════════════════════════════════════════════════════════════

class TrustStore:
    """信任状态的持久化：{连接器名: {trusted, timestamp}}。

    项目级（{root}/.workbuddy/connector_trust.json）——工作区就是
    本项目的沙箱边界，信任跟工作区走（教材是用户级，适配见模块头）。
    路径可注入：测试给临时目录，生产给工作区。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 信任文件坏了 = 一个都不信任：fail-closed，宁可让用户重点
            # 一次 Trust，也不能把"读不了"当"全都可信"。
            return {}
        return payload if isinstance(payload, dict) else {}

    def is_trusted(self, name: str) -> bool:
        record = self._load().get(name)
        return bool(isinstance(record, dict) and record.get("trusted"))

    def trust(self, name: str) -> None:
        """用户手动信任一个连接器（唯一的信任入口）。"""

        data = self._load()
        data[name] = {"trusted": True, "timestamp": time.time()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# 四、连接器与生命周期
# ═══════════════════════════════════════════════════════════════

@dataclass
class MCPConnector:
    """单个连接器及其生命周期状态。

    tools 存的是**命名空间化**后的清单（name/description/input_schema
    /_connector/_original_name）——发现即命名空间化，后面所有环节
    （grant 匹配、注册表、权限规则）只见全名。
    """

    name: str
    config: dict
    status: str = "disconnected"   # disconnected → trusted → connected
    tools: list[dict] = field(default_factory=list)
    trusted: bool = False
    unavailable_reason: str = ""   # 配置了却连不上时的人话（不静默）

    def connect(self, server: MCPServer) -> bool:
        """启动连接器并完成 tools/list。未信任的连接器不许启动进程。"""

        if not self.trusted:
            self.unavailable_reason = "未信任——用户点过 Trust 之前不启动"
            return False
        self.status = "connecting"
        try:
            raw_tools = server.tools_list()
        except Exception as error:      # 连接器崩了：回 disconnected，别装连上
            self.status = "disconnected"
            self.unavailable_reason = f"tools/list 失败：{error}"
            return False
        self.tools = [
            {
                "name": namespaced_tool_name(self.name, tool["name"]),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {}),
                "_connector": self.name,
                "_original_name": tool["name"],
            }
            for tool in raw_tools
        ]
        self.status = "connected"
        self.unavailable_reason = ""
        return True

    def disconnect(self) -> None:
        """停进程：状态回 disconnected，工具清单清空。"""

        self.status = "disconnected"
        self.tools = []


# ═══════════════════════════════════════════════════════════════
# 五、管理器：配置解析、信任、发现、交集、调度
# ═══════════════════════════════════════════════════════════════

class ConnectorManager:
    """所有连接器的生命周期 + 授权交集 + 调度入口。

    servers 由装配层**注入**（依赖方向：装配层 import 内核，反之
    不成立）——测试给 InProcessMCPServer，生产将来给 stdio 实现。
    构造时即按信任状态自动重连：信任是持久的（TrustStore），
    进程重启后受信连接器应当自己回来。
    """

    def __init__(
        self,
        config: Mapping,
        *,
        servers: Mapping[str, MCPServer] | None = None,
        trust_store: TrustStore | None = None,
        permission_grant: MCPPermissionGrant | None = None,
    ) -> None:
        self._servers = dict(servers or {})
        self._trust_store = trust_store or TrustStore(
            Path.home() / ".workbuddy" / "connector_trust.json")
        self.permission_grant = permission_grant or NO_MCP_PERMISSIONS
        self.connectors: dict[str, MCPConnector] = {}

        for name, entry in (config.get("mcpServers") or {}).items():
            connector = MCPConnector(name=name, config=dict(entry or {}))
            connector.trusted = self._trust_store.is_trusted(name)
            if connector.trusted:
                connector.status = "trusted"
            self.connectors[name] = connector
            if connector.trusted:
                self._connect(connector)

    def _connect(self, connector: MCPConnector) -> bool:
        server = self._servers.get(connector.name)
        if server is None:
            connector.unavailable_reason = (
                "已信任但没有可用的服务端实现（stdio 连接器留待接入）")
            return False
        return connector.connect(server)

    # ── 信任（用户动作的唯一入口）──────────────────────────────

    def trust_connector(self, name: str) -> bool:
        """用户信任一个连接器：落盘 + 启动 + 发现工具。"""

        connector = self.connectors.get(name)
        if connector is None:
            return False
        self._trust_store.trust(name)
        connector.trusted = True
        connector.status = "trusted"
        return self._connect(connector)

    def set_permission_grant(self, grant: MCPPermissionGrant) -> None:
        """更换授权。可见集合随下一次查询即时收窄——已注册进工具池的
        名单由装配层负责重建；执行期的复查（dispatch）保证旧句柄
        调不动已撤销的工具。"""

        self.permission_grant = grant

    # ── 可见性：connected ∩ grant ──────────────────────────────

    def visible_tools(self) -> list[dict]:
        """对当前会话可见的 MCP 工具（延迟目录的原料）。"""

        visible: list[dict] = []
        for connector in self.connectors.values():
            if connector.status != "connected":
                continue
            for tool in connector.tools:
                if self.permission_grant.allows(tool["name"]):
                    visible.append(tool)
        return visible

    def visible_names(self) -> tuple[str, ...]:
        return tuple(tool["name"] for tool in self.visible_tools())

    # ── 调度：执行期复查（TOCTOU 防线）────────────────────────

    def dispatch(self, namespaced_name: str, arguments: dict) -> str:
        """执行一个 MCP 工具——grant 复查在前，绝不先找后查。

        归属从名字里解析（mcp__<连接器>__<工具>），而不是遍历各连接器
        的工具清单：未连接的连接器清单是空的，按清单找会把"已授权、
        归属明确、只是没连"误报成"未知工具"——错误文案要能指引下一步
        （没连就去连，不认识才去查名字）。

        返回文本（错误也是文本）：延迟工具的结果要回灌给模型，
        异常炸穿只会拖垮整轮 turn（同 defer_execute 的取舍）。
        """

        if not self.permission_grant.allows(namespaced_name):
            reason = (
                "网络位未开启" if not self.permission_grant.network
                else "工具不在已审核的 grant 名单里")
            return (f"Error [permission_denied]: {namespaced_name} "
                    f"不允许调用——{reason}。")
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != MCP_TOOL_PREFIX.rstrip("_"):
            return f"Error: 未知 MCP 工具名 {namespaced_name!r}"
        connector = self.connectors.get(parts[1])
        if connector is None:
            return f"Error: 未知 MCP 工具 {namespaced_name!r}"
        if connector.status != "connected":
            return (f"Error [disconnected]: 连接器 {connector.name!r} "
                    "当前未连接——先信任并连接它")
        tool = next(
            (entry for entry in connector.tools
             if entry["name"] == namespaced_name), None)
        if tool is None:
            return (f"Error: 连接器 {connector.name!r} 不提供工具 "
                    f"{parts[2]!r}")
        server = self._servers.get(connector.name)
        if server is None:
            return (f"Error [disconnected]: 连接器 {connector.name!r} "
                    "没有可用的服务端实现")
        return server.tools_call(tool["_original_name"], arguments)

    # ── 状态呈现 ───────────────────────────────────────────────

    def build_context(self) -> str:
        """系统提示里的连接器目录：名字 + 一句话，无 schema。"""

        lines = ["<available_deferred_tools>"]
        for tool in self.visible_tools():
            lines.append(f"- {tool['name']}: {tool['description']}")
        if len(lines) == 1:
            lines.append("（没有已连接且已授权的 MCP 连接器工具）")
        lines.append("</available_deferred_tools>")
        return "\n".join(lines)

    def status_lines(self) -> str:
        """给人看的连接器状态表（运维/日志用）。"""

        rows = []
        for name, connector in self.connectors.items():
            note = f"（{connector.unavailable_reason}）" \
                if connector.unavailable_reason else ""
            rows.append(
                f"- {name}: status={connector.status} "
                f"trusted={'✓' if connector.trusted else '✗'} "
                f"tools={len(connector.tools)}{note}")
        return "\n".join(rows) if rows else "（mcp.json 里没有配置任何连接器）"
