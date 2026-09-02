"""工具系统的回归测试。"""

import inspect
import unittest

from src.harness.tools import Tool, ToolRegistry, parse_docstring, tool_to_schema


def add(a: int, b: int) -> int:
    """测试用工具：计算两个整数的和。"""

    return a + b


def greet(*, name: str) -> str:
    """测试关键字参数能否传递到工具函数。"""

    return f"你好，{name}"


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(
                name="add",
                description="计算两个整数的和",
                handler=add,
            )
        )

    def test_register_and_get_tool(self) -> None:
        tool = self.registry.get("add")

        self.assertEqual(tool.name, "add")
        self.assertEqual(tool.description, "计算两个整数的和")
        self.assertEqual(self.registry.names(), ["add"])

    def test_execute_tool_with_positional_arguments(self) -> None:
        result = self.registry.execute("add", 2, 3)

        self.assertEqual(result, 5)

    def test_execute_tool_with_keyword_arguments(self) -> None:
        self.registry.register(
            Tool(
                name="greet",
                description="向指定的人打招呼",
                handler=greet,
            )
        )

        result = self.registry.execute("greet", name="小明")

        self.assertEqual(result, "你好，小明")

    def test_duplicate_tool_name_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                Tool(
                    name="add",
                    description="另一个加法工具",
                    handler=add,
                )
            )

    def test_unknown_tool_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.execute("missing", 1, 2)


def documented(a: int, b: str = "x") -> str:
    """带完整 Args 段的测试工具。

    Args:
        a: 被加数，整数。
        b: 后缀文本，默认 "x"。
    """

    return f"{a}{b}"


def undocumented(city: str) -> str:
    """没有 Args 段的测试工具，只有一句话总结。"""

    return city


class ParseDocstringTests(unittest.TestCase):
    """练习 20：docstring 解析——人看的文档和模型看的说明书同源。"""

    def test_parses_args_section(self) -> None:
        result = parse_docstring(inspect.getdoc(documented))

        self.assertEqual(
            result,
            {"a": "被加数，整数。", "b": '后缀文本，默认 "x"。'},
        )

    def test_ignores_prose_outside_args(self) -> None:
        # 没有 Args 段：只有总结的 docstring 解析结果为空。
        self.assertEqual(parse_docstring(inspect.getdoc(undocumented)), {})

        # Args 段在 Returns: 处结束——段外的"说明"不进结果。
        doc = inspect.getdoc(documented) + "\n\nReturns:\n    拼接结果。"
        self.assertEqual(
            parse_docstring(doc),
            {"a": "被加数，整数。", "b": '后缀文本，默认 "x"。'},
        )

    def test_none_docstring_returns_empty(self) -> None:
        self.assertEqual(parse_docstring(None), {})
        self.assertEqual(parse_docstring(""), {})


class DocstringSchemaTests(unittest.TestCase):
    """练习 20：tool_to_schema 集成——docstring 说明进 JSON Schema。"""

    def test_param_descriptions_enter_schema(self) -> None:
        schema = tool_to_schema(Tool("doc", "测试", documented))

        props = schema["function"]["parameters"]["properties"]
        self.assertEqual(props["a"]["description"], "被加数，整数。")
        self.assertEqual(props["b"]["description"], '后缀文本，默认 "x"。')

    def test_undocumented_param_has_no_description(self) -> None:
        schema = tool_to_schema(Tool("undoc", "测试", undocumented))

        props = schema["function"]["parameters"]["properties"]
        self.assertNotIn("description", props["city"])
