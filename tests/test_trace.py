"""轨迹渲染的回归测试（纯函数，全部离线）。"""

import html
import unittest

from src.harness.trace import render_trace


def demo_messages() -> list[dict]:
    """一套覆盖四种消息形态的完整轨迹。"""

    return [
        {"role": "user", "content": "北京天气怎么样？"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "call_1", "name": "get_weather", "arguments": {"city": "北京"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "北京今天下紫色雪花"},
        {"role": "assistant", "content": "北京今天下紫色雪花，注意保暖。"},
    ]


class RenderTraceTests(unittest.TestCase):
    def test_all_roles_are_rendered(self) -> None:
        page = render_trace(demo_messages())

        self.assertIn("北京天气怎么样？", page)
        self.assertIn("get_weather", page)
        # 参数 JSON 在页面里是转义后的形态，期望值用 html.escape 动态构造。
        self.assertIn(html.escape('"city": "北京"'), page)
        self.assertIn("call_1", page)
        self.assertIn("北京今天下紫色雪花，注意保暖。", page)

    def test_html_is_escaped(self) -> None:
        # 两种攻击载荷：脚本标签 + 属性注入的图片标签。
        messages = [
            {"role": "user", "content": "<script>alert(1)</script>"},
            {"role": "assistant", "content": '<img src=x onerror="alert(2)">'},
        ]

        page = render_trace(messages)

        # 转义后：载荷变成无害的可见文本（&lt;...&gt;），必须出现；
        # 原始标签（<script> / <img>）绝不能出现——能执行的是标签，不是词。
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;img", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)


if __name__ == "__main__":
    unittest.main()
