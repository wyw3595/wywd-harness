"""工具系统的回归测试。"""

import unittest

from src.harness.tools import Tool, ToolRegistry


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
