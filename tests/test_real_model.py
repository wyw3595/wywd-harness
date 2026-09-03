"""real_model 离线部分（schema 生成与 wire 翻译）的测试。

只测不发网络请求的纯函数——真模型本体只能靠冒烟脚本验证。
"""

import json
import unittest

from src.harness.real_model import _is_permanent_error, _parse_reply, _to_wire_messages
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

    def test_system_message_passes_through(self) -> None:
        """s03 目录注入：system 消息（含延迟工具目录）由应用层塞进
        history 最前，wire 翻译必须原样放行——OpenAI 兼容协议认
        role="system"（给模型的全局设定）。"""
        system = {"role": "system", "content": "当前可用的延迟工具：…"}

        wire = _to_wire_messages([{**system}, {"role": "user", "content": "hi"}])

        self.assertEqual(wire[0], system)
        self.assertEqual(wire[1]["role"], "user")


class PermanentErrorPolicyTests(unittest.TestCase):
    """练习 16：重试策略打表——不发网络，纯函数直接问。"""

    def test_policy_table(self) -> None:
        # 4xx（除 429）＝重试也没用：key 错、权限错。
        self.assertTrue(_is_permanent_error(401))
        self.assertTrue(_is_permanent_error(403))
        # 429 限流是"歇会儿再来"；5xx 是服务端抖动；200 根本没出错。
        self.assertFalse(_is_permanent_error(429))
        self.assertFalse(_is_permanent_error(500))
        self.assertFalse(_is_permanent_error(200))


class ParseReplyTests(unittest.TestCase):
    """练习 17：wire 解析防御——畸形输入一律 ValueError，不炸穿。"""

    def test_parses_final_and_tool_calls(self) -> None:
        final = _parse_reply(
            {"choices": [{"finish_reason": "stop", "message": {"content": "你好"}}]}
        )
        self.assertEqual(final.kind, "final")
        self.assertEqual(final.text, "你好")

        payload = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "add", "arguments": '{"a": 2}'},
                            }
                        ]
                    },
                }
            ]
        }
        reply = _parse_reply(payload)
        self.assertEqual(reply.kind, "tool_calls")
        self.assertEqual(reply.tool_calls[0].call_id, "call_1")
        self.assertEqual(reply.tool_calls[0].name, "add")
        # arguments 在协议内已是解析好的字典，直接对比内容。
        self.assertEqual(reply.tool_calls[0].arguments, {"a": 2})

    def test_malformed_payloads_raise_value_error(self) -> None:
        # 缺 choices：整体结构就不对。
        with self.assertRaises(ValueError):
            _parse_reply({"foo": 1})

        # 说要用工具，却没给清单。
        no_list = {
            "choices": [
                {"finish_reason": "tool_calls", "message": {"content": None}}
            ]
        }
        with self.assertRaises(ValueError):
            _parse_reply(no_list)

        # 清单是空列表：模型放话要用工具，一条调用都没给——
        # 放行它，agent 循环就会空转到 max_steps 烧钱。
        empty_list = {
            "choices": [
                {"finish_reason": "tool_calls", "message": {"tool_calls": []}}
            ]
        }
        with self.assertRaises(ValueError):
            _parse_reply(empty_list)

        # 参数不是合法 JSON。
        bad_args = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "add", "arguments": "{不是json}"},
                            }
                        ]
                    },
                }
            ]
        }
        with self.assertRaises(ValueError):
            _parse_reply(bad_args)

    def test_length_finish_marks_truncated(self) -> None:
        # s01 整合：finish_reason="length" = 输出被 token 预算掐断，
        # 解析层必须如实标 truncated；"stop" 才是完整结束。
        truncated = _parse_reply(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "我已经回答到这里"},
                    }
                ]
            }
        )
        self.assertEqual(truncated.kind, "final")
        self.assertEqual(truncated.text, "我已经回答到这里")
        self.assertTrue(truncated.truncated)

        complete = _parse_reply(
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "完整回答"}}
                ]
            }
        )
        self.assertFalse(complete.truncated)

    def test_usage_details_are_flattened_to_ints(self) -> None:
        # 真实 DeepSeek 的 usage 混着嵌套明细字典——协议只收"键 -> 整数"。
        # 回归：第一次真实运行在记账处炸出 TypeError（0 + {...}）。
        payload = {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "你好"}}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }

        reply = _parse_reply(payload)

        self.assertEqual(
            reply.usage,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


if __name__ == "__main__":
    unittest.main()
