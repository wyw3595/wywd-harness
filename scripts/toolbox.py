"""把工具、模型、记忆策略组装成 agent 的运行环境。

新增工具只改这个文件 + 实现模块：
  - 普通工具：写个函数（放 file_tools / std_tools），加进 ALL_TOOLS。
  - 延迟工具：Tool(...) 时 defer=True 放进 DEFERRED_TOOLS——schema 不
    发给模型，等它 ToolSearch 发现后才进会话（s03 省 token）。
  - 桥接工具：ToolSearch / DeferExecuteTool 是 harness 内建协作件，
    handler 用闭包绑 registry，input_schema 必须手写（lambda 反射
    不出 list/dict 类型）。

超 WorkBuddy 的日常工具箱（实现都在 src/harness/std_tools.py）：
  calc —— 安全数学求值（ast 白名单）；
  find_text —— 项目内正则搜索（复用沙箱）；
  tree_dir —— 目录树（低频、长 schema，正好当延迟加载的演示对象）。
"""

from src.harness.file_tools import ALLOWED_ROOT, FORBIDDEN_PARTS, list_dir, read_file, write_file
from src.harness.model_router import ModelRouter, ModelTier
from src.harness.models import FakeModel
from src.harness.permissions import (
    PermissionPolicy,
    WorkspaceScope,
    build_default_policy,
)
from src.harness.real_model import RealModel
from src.harness.std_tools import calc, find_text, now, tree_dir
from src.harness.tools import Tool, ToolRegistry
from src.harness.workspace_memory import FactKind, WorkspaceMemory

import os

# 历史窗口大小（练习 19）：按"条数"计（一条 = 一条消息，一轮工具往返
# 约占 3 条）。数字越小越省钱、记忆越短——这是取舍题，不是优化题。
# 记忆策略归应用层（练习 10 铁律），harness 保持中立；两个入口共用。
MAX_HISTORY_MESSAGES = 20


def get_weather(city: str) -> str:
    """查询一个城市今天的天气。

    Args:
        city: 城市名，中文或拼音均可，如 "北京" 或 "beijing"。

    注意：本工具返回的是**演示用模拟数据**（"下紫色雪花"），不代表真实
    天气——只供冒烟验证工具往返（真实值独有的"紫色雪花"可当真伪判别），
    模型必须知道数据是假的，才不会拿它当真去回答用户。
    """

    return f"{city}今天下紫色雪花，气温零下 42 度。"


# 即时工具：高频、短 schema，直接全量进模型上下文。
ALL_TOOLS: list[Tool] = [
    Tool(
        name="get_weather",
        description="查询一个城市今天的天气（演示用模拟数据，不代表真实天气）",
        handler=get_weather,
    ),
    Tool(
        name="now",
        description="获取当前日期和时间（本地时区），返回格式 YYYY-MM-DD HH:MM",
        handler=now,
    ),
    Tool(
        name="fs_list",
        description="列出项目里某个目录的内容（path 是相对项目根的路径，默认 . ）",
        handler=list_dir,
    ),
    Tool(
        name="fs_read",
        description="读取项目里某个文本文件（path 相对项目根；超长自动截断）",
        handler=read_file,
    ),
    Tool(
        name="fs_write",
        description="写入或覆盖项目里某个文本文件（path 相对项目根，整文件覆盖；"
        "需用户审批后才会真正执行）",
        handler=write_file,
    ),
    Tool(
        name="calc",
        description="安全计算数学表达式，如 (1 + 2) * 3 或 sqrt(16) * 2",
        handler=calc,
    ),
    Tool(
        name="fs_find",
        description="在项目里搜索文件内容（正则；返回 文件:行号:行内容 命中）",
        handler=find_text,
    ),
]

# 延迟工具：低频、长 schema。不发给模型；模型拿到的是目录里的一行
# 摘要，据用途调 ToolSearch 发现后，才在本会话加载完整 schema。
DEFERRED_TOOLS: list[Tool] = [
    Tool(
        name="tree_dir",
        description="渲染一棵目录树，看清项目结构（低频、参数多）",
        handler=tree_dir,
        defer=True,
    ),
    Tool(
        name="memory_write",
        description="往项目工作区记忆追加一条事实（决策/约定/坑/结果）。"
        "只写原始日志，是否晋升长期记忆由 30 天蒸馏策略决定——"
        "不要记寒暄、猜测、密钥或原始工具输出。",
        handler=lambda content, kind="outcome", importance=3:
            write_memory_fact(content, kind, importance),
        defer=True,
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "一句话的项目事实，如'存储层确定用 SQLite WAL 模式'"},
                "kind": {"type": "string",
                         "enum": ["decision", "convention", "pitfall", "outcome"],
                         "description": "事实类型：决策/约定/坑/结果"},
                "importance": {"type": "integer",
                               "description": "重要度 1-5，默认 3；>=4 才可能提前晋升"},
            },
            "required": ["content"],
        },
    ),
]


def write_memory_fact(content: str, kind: str = "outcome",
                      importance: int = 3, root=None) -> str:
    """memory_write 的 handler 本体（root 可注入——测试喂 tmp 目录）。

    TODO 8a（你来填）：
      memory = WorkspaceMemory(root if root is not None else ALLOWED_ROOT)
      fact = memory.append_daily_log(
          content, kind=kind, importance=importance, source="agent")
      return (f"已记录 [{fact.kind}] {fact.content[:60]}"
              f"（{fact.recorded_at[:10]} 日志，等待蒸馏策略裁决）")
    写路径的诚实设计：工具只能**追加**原始事实（要不要记住一辈子，
    蒸馏策略说了算）——"模型说重要就永久保存"是记忆污染的正门。
    """
    raise NotImplementedError("TODO 8a: write_memory_fact")


def build_history_seed(root=None) -> list[dict]:
    """sidecar 会话的起步历史：工具目录 system + 工作区记忆有界视图。

    TODO 8b（你来填）：
      seed = with_system([])
      memory = WorkspaceMemory(root if root is not None else ALLOWED_ROOT)
      context = memory.get_context_for_agent()
      if context and context != "(no workspace memory yet)":
          seed.append({"role": "system", "content": context})
      return seed
    记忆放第二条 system（FakeModel 只读第一条的老怪癖不受影响）；
    每会话开局读一次（不是每 turn——有界视图的教学取舍，边界写进
    NEXT_SESSION）。空记忆不追加消息：seed 与 s06.5 完全一致。
    """
    raise NotImplementedError("TODO 8b: build_history_seed")

# 免审批白名单（练习 s04）：只读 / 沙箱内的工具显式放行。fs_write
# 刻意不在名单里——它由 path.write_ask 规则拦成 ASK，执行前必须人点头。
# 新工具默认 default.deny：必须有人把它加进某条规则才算"有了治理路径"。
# 注意：策略看见的是桥接工具 ToolSearch / DeferExecuteTool 本身，不是
# 延迟工具 tree_dir——延迟加载把执行藏在桥后面，策略管不到穿透后的
# 那一层（已知边界：tree_dir 只读 + 沙箱内，风险可接受）。
SAFE_TOOLS: frozenset[str] = frozenset({
    "get_weather", "now", "fs_list", "fs_read",
    "calc", "fs_find", "ToolSearch", "DeferExecuteTool",
    "memory_write",   # 只追加 .memory/ 原始日志，晋升由蒸馏闸门管（s10）
})

# 读写工具集合（练习 s04 · 去重）：治理语义集中在装配层声明，permissions
# 只提供通用规则框架。新增写工具只改这里一处（加进 WRITE_TOOLS 并确认
# 不在 SAFE_TOOLS 里），build_policy 会把集合喂给 build_default_policy。
READ_TOOLS: frozenset[str] = frozenset({"fs_read", "fs_list"})
WRITE_TOOLS: frozenset[str] = frozenset({"fs_write"})


def build_policy() -> PermissionPolicy:
    """装配本项目工具箱的权限策略：默认规则 + 沙箱作用域 + 白名单。

    scope 用的就是文件工具的 ALLOWED_ROOT——决策层（执行前预判）和
    执行层（handler 里的 _resolve_safe）认同一个沙箱根，两层不说两家话。
    forbidden_parts 同样来自 file_tools 的事实源（.env/.git），决策层
    在 DENY 阶段就拦下禁区，不再等到执行层才炸（s04 补的错位）。
    """

    return build_default_policy(
        scope=WorkspaceScope(ALLOWED_ROOT, forbidden_parts=FORBIDDEN_PARTS),
        safe_tools=sorted(SAFE_TOOLS),
        read_tools=sorted(READ_TOOLS),
        write_tools=sorted(WRITE_TOOLS),
    )


def build_system_prompt() -> str:
    """系统提示：会话开局常驻的"工具目录"（对齐 s03 设计）。

    WorkBuddy 里延迟工具的目录是独立工件：模型在启动时必须看到
    "有哪些延迟工具、各是干什么的"（符号表），却看不到完整 schema
    （那要等 ToolSearch 发现后才按需加载）。目录放 system、由应用层
    注入 history 最前——常驻但便宜；而 ToolSearch 的描述保持干净，
    只管"按名/按词召回"。
    """

    deferred_directory = "\n".join(
        f"  - {tool.name}: {tool.description}" for tool in DEFERRED_TOOLS
    )
    return (
        "你是运行在 wywd-harness 里的 Agent，通过工具完成任务。\n"
        "工具在系统里分为两类：\n"
        "  - 即时工具：直接可用，不需要额外步骤（见你的工具列表）。\n"
        "  - 延迟工具：schema 不在你的工具列表里，想用必须先调 ToolSearch"
        " 按名称或用途搜索、拿到完整 schema，再用 DeferExecuteTool 执行。\n"
        f"当前可用的延迟工具：\n{deferred_directory}"
    )


def with_system(history: list[dict] | None) -> list[dict]:
    """把系统提示（工具目录）放到会话最前，且幂等——不重复添加。

    对齐 s03 的目录设计：目录是独立工件，常驻模型上下文；历史截断
    可能把 system 切出窗口（窗口比消息少时），这里自动补回；若
    history 第一条已是 system（上一轮的 result.messages 带回来的），
    直接原样返回。谁也不用特判。

    chat.py / web_app.py / electron_shell.py 三个入口共用。
    """

    system_message = {"role": "system", "content": build_system_prompt()}
    if not history:
        return [system_message]
    if history[0].get("role") == "system":
        return history
    return [system_message] + history

# 桥接工具的手写 schema（lambda 参数无类型注解，反射会给 string 兜底，
# 而 queries 明明是数组；形状与 tests/test_deferred_tools.py 一致）。
TOOL_SEARCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要检索的工具名称或用途关键词",
        },
        "top_k": {"type": "integer", "description": "最多返回几个命中，默认 3"},
    },
    "required": ["queries"],
}

DEFER_EXECUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "toolName": {"type": "string", "description": "已加载的延迟工具名"},
        "params": {"type": "object", "description": "传给该工具的参数字典"},
    },
    "required": ["toolName", "params"],
}


def build_registry() -> ToolRegistry:
    """注册全部工具（即时 + 延迟 + 两个桥接协作件）。"""

    registry = ToolRegistry()
    for tool in ALL_TOOLS + DEFERRED_TOOLS:
        registry.register(tool)

    # 桥接工具：handler 闭包绑 registry——注册表即边界，循环无需特判。
    # 陷阱：lambda 参数名必须与 input_schema 的键逐字一致（queries/top_k、
    # toolName/params）。校验读的是 schema，execute 做 **kwargs 展开——
    # 名字不一致就是 "unexpected keyword argument"，模型猜死循环。
    # 描述刻意保持干净：目录在 build_system_prompt()（system 常驻），
    # ToolSearch 只负责"按名/按词召回"，两者职责分离（对齐 s03）。

    def search_tools(
            queries: list[str] | None = None, top_k: int | None = None
        ) -> str:
        """ToolSearch 的真实语义：逐词处理，精确优先、模糊兜底。

        模型传查询词不可预测：可能是精确名 ["tree_dir"]、名字混用途
        ["tree_dir 目录树"]、纯用途词 ["目录树"]——三种都必须命中。
        做法：词的精确值等于某个延迟工具名 → 走加载（load_by_name），
        否则按用途模糊搜（search，内部拆词命中描述）。
        注意 top_k 是 schema 里的可选参数，这里必须带默认值——execute
        只展开模型传了的键，可选参数不给默认就是 TypeError。
        """

        parts: list[str] = []
        for query in queries or []:
            if any(tool.name == query for tool in DEFERRED_TOOLS):
                parts.append(registry.load_by_name([query]))
            else:
                parts.append(registry.search([query], top_k=top_k or 3))
        return "\n".join(parts) if parts else "（没有查询词）"

    registry.register(Tool(
        name="ToolSearch",
        description="搜索并加载延迟加载的工具的 schema（按名称或用途），"
        "返回找到的工具名、命中理由与完整 JSON schema。"
        "延迟工具不在普通工具列表里，要用它们必须先调我。",
        handler=search_tools,
        input_schema=TOOL_SEARCH_SCHEMA,
    ))
    registry.register(Tool(
        name="DeferExecuteTool",
        description="执行一个已加载的延迟工具（参数 schema 由 ToolSearch 提供）",
        handler=lambda toolName=None, params=None: registry.defer_execute(
            toolName, params or {}
        ),
        input_schema=DEFER_EXECUTE_SCHEMA,
    ))

    return registry


def build_model() -> RealModel:
    """带工具说明书的真实模型。

    由 registry 的 model_schemas() 出目录——即时工具全量，延迟工具
    用目录摘要代替。schema 生成只发生在 tool_to_schema 一处（单一真源）。
    """

    return RealModel(tools=build_registry().model_schemas())


def build_model_router() -> ModelRouter:
    """三级槽位装配（s08）：标签 → 具体模型的解析表。

    教学矩阵说明：无 key 三槽全 FakeModel（离线可跑），有 key 三槽全
    DeepSeek——同一个模型占三个槽看似没分级，但**槽位机制已经立住**：
    真实分级（lite 换便宜厂商 / craft 换旗舰）只改这张映射表，代码
    一行不动。这正是教材"标签路由"的意义：用户改配置换模型。
    """
    routes = {tier: (build_model() if os.getenv("DEEPSEEK_API_KEY")
                     else FakeModel()) for tier in ModelTier}
    return ModelRouter(routes=routes)