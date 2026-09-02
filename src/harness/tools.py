"""
============================================================
Harness 练习 02：工具与工具注册表
============================================================

📚 为什么需要它
  模型只能生成文字，不能直接执行 Python 函数、读取文件或访问数据库。
  Tool 把一个可执行函数包装成带有名称和说明的工具；ToolRegistry 像工具
  登记簿，负责根据工具名称找到工具并执行它。

📚 本课核心
  - Callable：表示“可以被调用的对象”，这里用来描述工具函数。
  - *args 和 **kwargs：让注册表可以把不同数量、不同名字的参数继续传给工具。
  - dict[str, Tool]：用字典按名称保存工具，查找速度快，也便于防止重名。
  - raise：遇到重复工具或不存在的工具时，主动抛出明确异常。
  - frozen=True：工具注册后不允许随意修改描述，避免运行中状态被意外改变。

📚 这层的职责
  Tool：描述并执行一个工具。
  ToolRegistry：注册、查找和调度工具。
  后面的 Agent Loop：根据模型的请求，调用 ToolRegistry。

面试要点：注册表把“工具名称”和“具体实现”解耦；新增工具只需要注册，
不需要修改 Harness 的核心循环。这个设计也便于测试和权限控制。

完成标志：运行 tests/test_tools.py，至少通过注册、执行、重名和未知工具测试。
============================================================
"""

import inspect

from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    """一个可被 Harness 调度的工具。"""

    name: str
    description: str
    handler: ToolHandler

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        """把参数交给真正的工具函数，并返回工具结果。"""

        return self.handler(*args, **kwargs)


class ToolRegistry:
    """保存工具并根据名称执行工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """登记一个工具；同名工具不允许覆盖。"""

        if tool.name in self._tools:
            raise ValueError(f"工具已存在：{tool.name}")

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """根据名称找到工具；找不到时抛出 KeyError。"""

        if name not in self._tools:
            raise KeyError(f"工具不存在：{name}")

        return self._tools[name]

    def execute(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """根据名称找到工具，并把参数转交给它。"""

        tool = self.get(tool_name)
        return tool.execute(*args, **kwargs)

    def names(self) -> list[str]:
        """返回当前已经登记的工具名称。"""

        return list(self._tools)


# Python 类型标注 -> JSON Schema 类型的映射表。
# 真实框架（pydantic、各家 SDK）都内置了这张表，这里是最小版。
TYPE_MAP = {int: "integer", float: "number", str: "string", bool: "boolean"}

# Google 风格 docstring 的段名。遇到 Args: 进入参数段；遇到其他段名
# （如 Returns:）说明参数段结束了。真实框架（griffe）支持 Google /
# NumPy / Sphinx 三种风格，我们只手写 Google 简化版——机制相同：
# 找段名、逐行切"名字: 说明"。
SECTION_HEADERS = {"Args:", "Returns:", "Raises:", "Examples:"}


def parse_docstring(doc: str | None) -> dict[str, str]:
    """从 Google 风格 docstring 的 Args 段解析出 {参数名: 说明}。"""

    descriptions: dict[str, str] = {}
    if not doc:
        # 没有 docstring（或空串）就没有说明书——空字典，不炸。
        return descriptions

    in_args = False
    for raw_line in doc.splitlines():
        line = raw_line.strip()
        if line in SECTION_HEADERS:
            # 段名行：遇 Args: 进入参数段，遇 Returns: 等退出——
            # 一行同时处理"进入"和"退出"两种情况。
            in_args = line == "Args:"
            continue
        if not in_args or not line:
            # 段外散文、空行：跳过。
            continue
        head, _, tail = line.partition(":")
        # isidentifier 守卫：冒号前必须是"合法参数名的形状"，挡住
        # "注意:xxx" 式散文行（诚实边界：Python 3 中文也算合法标识符，
        # 守卫不完美，但 Args 段里正常只有参数行，够用）。
        if head.isidentifier() and tail.strip():
            descriptions[head] = tail.strip()

    return descriptions


def tool_to_schema(tool: Tool) -> dict:
    """把一个 Tool 翻译成 wire 格式的 JSON Schema 说明书。

    例子：handler 为 def get_weather(city: str) -> str 的工具，得到：
        {"type": "function",
         "function": {"name": "get_weather",
                      "description": "...",
                      "parameters": {"type": "object",
                                     "properties": {"city": {"type": "string"}},
                                     "required": ["city"]}}}
    """

    sig = inspect.signature(tool.handler)
    # 参数说明（练习 20）：docstring 是唯一来源——getdoc 拿到清洗过缩进
    # 的 docstring，parse_docstring 抽出 {参数名: 说明}；没写说明的参数
    # 不加 description 键（键不存在比空字符串诚实）。
    # 工具级 description 不动：Tool.description 是显式配置，优先。
    param_docs = parse_docstring(inspect.getdoc(tool.handler))

    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        prop = {"type": TYPE_MAP.get(param.annotation, "string")}
        if name in param_docs:
            prop["description"] = param_docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
