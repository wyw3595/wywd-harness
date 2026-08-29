"""real_model 离线部分（schema 生成与 wire 翻译）的测试。

只测不发网络请求的纯函数——真模型本体只能靠冒烟脚本验证。
"""

import json
import unittest

from src.harness.real_model import _to_wire_messages
from src.harness.tools import Tool, tool_to_schema


def get_weather(city: str) -> str:
    """测试用工具：查询一个城市的天气。"""

    return f"{city}今天晴，25 度"


class ToolToSchemaTests(unittest.TestCase):
    def test_schema_reflects_handler_signature(self) -> None:
        schema = tool_to_schema(Tool("get_weather", "查询城市天气", get_weather))

        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "get_weather")
        self.assertEqual(schema["function"]["description"], "查询城市天气")
        self.assertEqual(
            schema["function"]["parameters"]["properties"],
            {"city": {"type": "string"}},
        )
        self.assertEqual(schema["function"]["parameters"]["required"], ["city"])


class ToWireMessagesTests(unittest.TestCase):
    def test_assistant_tool_calls_are_translated(self) -> None:
        dialect = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "call_1", "name": "add", "arguments": {"a": 2, "b": 3}}
            ],
        }

        wire = _to_wire_messages([dialect])

        self.assertIsNone(wire[0]["content"])
        call = wire[0]["tool_calls"][0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "add")
        # 用 json.loads 往返对比，不和空格、键序较劲。
        self.assertEqual(
            json.loads(call["function"]["arguments"]), {"a": 2, "b": 3}
        )

    def test_other_messages_pass_through(self) -> None:
        messages = [
            {"role": "user", "content": "北京天气怎么样？"},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴，25 度"},
        ]

        wire = _to_wire_messages(messages)

        self.assertEqual(len(wire), 2)
        self.assertEqual(wire[0], messages[0])
        self.assertEqual(wire[1], messages[1])


if __name__ == "__main__":
    unittest.main()
