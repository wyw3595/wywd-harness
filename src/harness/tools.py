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
import copy
import inspect
import json
import re
from collections.abc import Iterable

from dataclasses import dataclass
from typing import Any, Callable, Mapping

ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    """一个可被 Harness 调度的工具。"""

    name: str
    description: str
    handler: ToolHandler
    # 默认走反射生成 schema；只有桥接工具（lambda 反射不出 list/dict
    # 类型）才手写参数覆盖——"默认反射、显式覆盖"。
    input_schema: Mapping[str, Any] = None
    defer: bool = False

    def model_schema(self) -> dict:
        return tool_to_schema(self)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        """把参数交给真正的工具函数，并返回工具结果。"""

        return self.handler(*args, **kwargs)

@dataclass(frozen=True)
class ToolMatch:
    """A stable search hit plus the schema that was loaded for this session."""

    name: str
    score: int
    schema: Mapping[str, Any]
    cache_hit: bool

    def to_payload(self) -> dict[str, Any]:
        """Return a model-readable result without exposing the catalog's schema."""

        return {
            "name": self.name,
            "score": self.score,
            "load_state": "cached" if self.cache_hit else "loaded",
            "schema": copy.deepcopy(dict(self.schema)),
        }


@dataclass(frozen=True)
class ToolSearchResult:
    """The complete, inspectable outcome of one ToolSearch call."""

    matches: tuple[ToolMatch, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.matches)

    def render(self) -> str:
        """把发现结果编码成模型能推理的文本（回灌给模型的就是它）。"""

        lines: list[str] = []
        for match in self.matches:
            state = "loaded" if not match.cache_hit else "cached"
            lines.append(f"✓ {match.name} [{state}]")
            lines.append(json.dumps(match.to_payload(), indent=2, ensure_ascii=False))
        lines.extend(f"✗ {name}: 没有这个延迟工具" for name in self.missing)
        return "\n".join(lines) or "没有延迟工具命中查询"


class ToolRegistry:
    """保存工具并根据名称执行工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._loaded_schemas: dict[str, dict[str, Any]] = {}


    def register(self, tool: Tool) -> None:
        """登记一个工具；同名工具不允许覆盖。"""

        if not tool.name:
            raise ValueError("工具名称不能为空")
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

    def model_schemas(self) -> list[dict[str, Any]]:
        """发给 provider 的工具目录：只含即时工具的完整 schema。

        延迟工具的 schema 不在这里出现——它对模型不可见，只能通过
        ToolSearch 发现后才进入会话（s03 的省 token 机制）。
        """

        return [entry.model_schema() for entry in self._tools.values() if not entry.defer]

    def get_deferred_directory(self) -> str:
        return "\n".join(
            f" - {tool.name}: {tool.description}"
            for tool in self._tools.values() if tool.defer
        )

    def get_loaded_names(self) -> tuple[str, ...]:
        """Expose session activation without leaking the mutable cache."""

        return tuple(self._loaded_schemas)

    def get_handler(self, name: str) -> ToolHandler:
        entry =  self._tools.get(name)
        return entry.handler if entry else None

    def is_deferred(self, name: str) -> bool:
        entry = self._tools.get(name)
        return entry.defer if entry else False

    def load_by_name(self, tool_names: Iterable[str]) -> str:
        matches: list[ToolMatch] = []
        missing: list[str] = []
        for name in dict.fromkeys(tool_names):
            entry = self._tools.get(name)
            if entry is None or not entry.defer:
                missing.append(name)
                continue
            matches.append(self._load_match(entry, score=100))
        # ToolSearchResult 是中间产品，交付物是渲染文本——
        # 循环回灌/模型读的都是字符串。
        return ToolSearchResult(tuple(matches), tuple(missing)).render()

    def search(self, queries: Iterable[str], top_k: int = 3) -> str:
        """按用途关键词检索延迟工具，只把 top_k 命中（稳定排序）载入会话。"""

        terms = _search_terms(queries)
        if not terms:
            return "✗ 查询为空，无法检索"

        scored: list[tuple[int, Tool]] = []
        for entry in self._tools.values():
            if not entry.defer:
                continue
            score = _match_score(entry, terms)
            if score:
                scored.append((score, entry))

        if not scored:
            return f"✗ 没有延迟工具命中查询：{' '.join(terms)}"
        # 稳定排序：先按分，平分按名称——同一输入任何构建都加载同一批，
        # trace 才可复现（s03 锁定的契约）。
        scored.sort(key=lambda item: (-item[0], item[1].name))
        matches = [
            self._load_match(entry, score)
            for score, entry in scored[:top_k]
        ]
        return ToolSearchResult(tuple(matches)).render()

    def _load_match(self, entry: Tool, score: int) -> ToolMatch:
        cache_hit = entry.name in self._loaded_schemas
        if cache_hit:
            schema = self._loaded_schemas[entry.name]
        else:
            schema = entry.model_schema()
            # 关键一步：本次发现把 schema 落进会话缓存，下次才 "cached"。
            # 不落这里，cache_hit 永远是 False，loaded/cached 分不出来。
            self._loaded_schemas[entry.name] = schema
        return ToolMatch(entry.name, score, schema, cache_hit)

    def defer_execute(self, tool_name: str, params: dict) -> str:
        """执行一个延迟工具；三项前置检查缺一不可（s03 失败路径）。

        1. 存在且确是延迟工具（即时工具不许借这个入口绕行）；
        2. schema 已在本会话加载（必须先 ToolSearch）；
        3. 参数通过它自己的 schema 校验（复用 s02 执行前校验）。

        三条拒绝各有明确文案——模型要靠文案决定下一步：
        "未知"改查名字，"即时"直接调，"未加载"先搜索。
        """

        tool = self._tools.get(tool_name)
        if tool is None:
            return f"Error: 未知工具 {tool_name!r}"
        if not tool.defer:
            return f"Error: {tool_name!r} 是即时工具，直接调用它"
        if tool_name not in self._loaded_schemas:
            return (
                f"Error: 工具 {tool_name!r} 的 schema 尚未加载，"
                "请先调用 ToolSearch"
            )

        parameters = tool_to_schema(tool)["function"]["parameters"]
        error = validate_arguments(parameters, params)
        if error:
            return f"Error [invalid_arguments]: {error}"

        try:
            return str(tool.execute(**params))
        except Exception as exc:
            return f"Error [execution_error]: {exc}"

    def token_report(self) -> dict[str, int]:
        """估算两种装载策略的工具目录成本，并量化节省。

        len(json) // 4 是估算公式（只用于比较装载策略的相对差异，
        不是 tokenizer 实测）。full = 全量下发，current = 即时 + 延迟
        目录一行，saved 就是延迟加载省的。
        """

        def estimate(text: str) -> int:
            return max(1, len(text) // 4)

        def schema_cost(entry: Tool) -> int:
            return estimate(json.dumps(entry.model_schema(), ensure_ascii=False))

        full = sum(schema_cost(t) for t in self._tools.values())
        immediate = sum(schema_cost(t) for t in self._tools.values() if not t.defer)
        directory = sum(
            estimate(f"{t.name}: {t.description}")
            for t in self._tools.values()
            if t.defer
        )
        current = immediate + directory
        return {
            "full": full,
            "immediate": immediate,
            "directory": directory,
            "current": current,
            "saved": full - current,
        }





    def validate(self, name: str, arguments: dict) -> str | None:
        """执行前校验模型传来的参数；非法返回可行动文案，合法返回 None。

        s02 核心课（练习 22-b）：schema 不只发给模型当说明书，也在本地
        执行前当校验契约——模型"看过" schema 不等于会传对，缺字段、
        类型错、拼错的参数都要在执行前拦住，并回灌一句模型能据此修正
        的文案。校验用的 schema 和 real_model 下发的是同一个
        tool_to_schema 反射出来的——同一份契约，不存在两份会漂移的副本。

        工具不存在时返回 None：让调用方走既有"未知工具"错误路径
        （execute 抛 KeyError，循环捕获回灌），语义保持清晰。
        """

        tool = self._tools.get(name)
        if tool is None:
            return None

        parameters = tool_to_schema(tool)["function"]["parameters"]
        return validate_arguments(parameters, arguments)

def _tokenize(text: str) -> tuple[str, ...]:
    """ASCII 词 + 中文重叠双字（bigram）。

    中英混搜的切词规约：工具名是英文，用途词可能是中文。中文不定长，
    若把连续汉字切成一个整段，查询短词"目录树"撞不进描述长段
    "渲染一棵目录树"（v2 实测）。重叠双字把两端都切成 "目录" "录树"，
    交集判命中就对了。单字不成词，跳过。
    """

    ascii_terms = re.findall(r"[a-z0-9]+", text.lower())
    cjk_bigrams = (
        block[i : i + 2]
        for block in re.findall(r"[\u4e00-\u9fff]+", text)
        for i in range(len(block) - 1)
    )
    return tuple(dict.fromkeys([*ascii_terms, *cjk_bigrams]))


def _search_terms(queries: Iterable[str]) -> tuple[str, ...]:
    """Normalize free text into unique lowercase terms in caller order."""

    terms = (
        term for query in queries for term in _tokenize(query.replace("_", " "))
    )
    return tuple(dict.fromkeys(terms))


def _match_score(entry: Tool, terms: tuple[str, ...]) -> int:
    """Score exact names, name tokens, then description tokens."""

    name = entry.name.lower()
    name_terms = set(name.replace("_", " ").split())
    # 同一套 _tokenize：中文描述词（目录树 → 目录/录树）才能被命中。
    description_terms = set(_tokenize(entry.description))
    score = 0
    if "_".join(terms) == name or " ".join(terms) == name:
        score += 100
    for term in terms:
        if term in name_terms:
            score += 20
        elif term in name:
            score += 10
        if term in description_terms:
            score += 3
    return score

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

    # 手写优先：桥接工具显式给了 input_schema 就别反射了——lambda 的
    # 参数没有类型注解，反射会给兜底 "string"（而 params 明明是 object）。
    # input_schema 直接就是 "parameters" 那一段，不需要再包一层；
    # 两条路径共用下面同一个 return，schema 形状因此保证一致。
    if tool.input_schema is not None:
        parameters = dict(tool.input_schema)
    else:
        sig = inspect.signature(tool.handler)
        # 参数说明（练习 20）：docstring 是唯一来源——getdoc 拿到清洗过
        # 缩进的 docstring，parse_docstring 抽出 {参数名: 说明}；没写说明
        # 的参数不加 description 键（键不存在比空字符串诚实）。
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

        parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def validate_arguments(parameters: dict, raw: object) -> str | None:
    """执行前校验模型传来的参数；非法返回可行动文案，合法返回 None。

    s02 核心课（练习 22-b）：json 里 model 看到的是什么契约，本地执行前
    就按什么校验——缺必填、类型错、多传（拼错）的参数都要在执行前
    拦住，回灌"缺了什么、哪里不对"，而不是让 handler 的 ** 展开把
    模糊的 TypeError 冒出来。parameters 是 tool_to_schema 产出的
    "parameters" 那段（type / properties / required）。
    """

    if not isinstance(raw, dict):
        return "参数必须是对象"

    properties = parameters.get("properties", {})
    required = parameters.get("required", [])

    missing = [name for name in required if name not in raw]
    if missing:
        return "缺少必填参数：" + "、".join(missing)

    # 反射 schema 没有 additionalProperties: false——properties 里印出来的
    # 就是全部合法键，多出来的只能是模型拼错的参数，执行前同样拦下。
    unknown = sorted(set(raw) - set(properties))
    if unknown:
        return "多余参数：" + "、".join(unknown)

    # 类型反向映射：JSON Schema 类型 -> Python 类型集合。
    json_types: dict[str, tuple[type, ...]] = {
        "integer": (int,),
        "number": (int, float),
        "string": (str,),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
    }
    for name, value in raw.items():
        rule = properties.get(name)
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        accepted = json_types.get(expected)
        if not accepted:
            continue
        # s02 同款坑：Python 的 bool 是 int 的子类，isinstance(True, int)
        # 为真——但 JSON 参数里的 true 不该通过 integer / number 校验。
        is_bool_in_number_like = (
            expected in {"integer", "number"} and isinstance(value, bool)
        )
        if not isinstance(value, accepted) or is_bool_in_number_like:
            return f"参数 {name!r} 必须是 {expected}"

    return None
