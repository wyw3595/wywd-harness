"""共享工具箱：终端入口（chat.py）和网页入口（chainlit_app.py）用同一套配置。

新增工具只改这一个文件，两个入口同时生效。
"""

from src.harness.file_tools import list_dir, read_file
from src.harness.real_model import RealModel
from src.harness.tools import Tool, ToolRegistry, tool_to_schema

# 历史窗口大小（练习 19）：按"条数"计（一条 = 一条消息，一轮工具往返
# 约占 3 条）。数字越小越省钱、记忆越短——这是取舍题，不是优化题。
# 记忆策略归应用层（练习 10 铁律），harness 保持中立；两个入口共用。
MAX_HISTORY_MESSAGES = 20


def get_weather(city: str) -> str:
    """查询一个城市今天的天气。"""

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def add(a: int, b: int) -> int:
    """计算两个整数的和。"""

    return a + b


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
]


def build_registry() -> ToolRegistry:
    """注册全部工具的注册表。"""

    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        registry.register(tool)
    return registry


def build_model() -> RealModel:
    """带全部工具说明书的真实模型。"""

    return RealModel(tools=[tool_to_schema(tool) for tool in ALL_TOOLS])
