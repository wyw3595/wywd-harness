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

from src.harness.file_tools import list_dir, read_file
from src.harness.real_model import RealModel
from src.harness.std_tools import calc, find_text, tree_dir
from src.harness.tools import Tool, ToolRegistry

# 历史窗口大小（练习 19）：按"条数"计（一条 = 一条消息，一轮工具往返
# 约占 3 条）。数字越小越省钱、记忆越短——这是取舍题，不是优化题。
# 记忆策略归应用层（练习 10 铁律），harness 保持中立；两个入口共用。
MAX_HISTORY_MESSAGES = 20


def get_weather(city: str) -> str:
    """查询一个城市今天的天气。

    Args:
        city: 城市名，中文或拼音均可，如 "北京" 或 "beijing"。
    """

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def add(a: int, b: int) -> int:
    """计算两个整数的和。

    Args:
        a: 第一个加数，整数。
        b: 第二个加数，整数。
    """

    return a + b


# 即时工具：高频、短 schema，直接全量进模型上下文。
ALL_TOOLS: list[Tool] = [
    Tool(
        name="get_weather",
        description="查询一个城市今天的天气",
        handler=get_weather,
    ),
    Tool(
        name="add",
        description="计算两个整数的和",
        handler=add,
    ),
    Tool(
        name="list_dir",
        description="列出项目里某个目录的内容（path 是相对项目根的路径，默认 . ）",
        handler=list_dir,
    ),
    Tool(
        name="read_file",
        description="读取项目里某个文本文件（path 相对项目根；超长自动截断）",
        handler=read_file,
    ),
    Tool(
        name="calc",
        description="安全计算数学表达式，如 (1 + 2) * 3 或 sqrt(16) * 2",
        handler=calc,
    ),
    Tool(
        name="find_text",
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
]


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