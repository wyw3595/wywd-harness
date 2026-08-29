"""演示：跑一次带工具往返的对话，生成轨迹页并自动用浏览器打开。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m scripts.demo_trace
"""

from src.harness.agent import run_agent
from src.harness.models import ModelReply, ScriptedModel, ToolCall
from src.harness.tools import Tool, ToolRegistry
from src.harness.trace import show_trace


def get_weather(city: str) -> str:
    """演示用工具：查询一个城市的天气。"""

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def main() -> None:
    weather_tool = Tool(
        name="get_weather",
        description="查询一个城市今天的天气",
        handler=get_weather,
    )
    registry = ToolRegistry()
    registry.register(weather_tool)

    scripted = ScriptedModel(
        [
            ModelReply(
                kind="tool_calls",
                tool_calls=[ToolCall("call_1", "get_weather", {"city": "北京"})],
            ),
            ModelReply(kind="final", text="北京今天下紫色雪花，零下 42 度，注意保暖。"),
        ]
    )

    result = run_agent("北京天气怎么样？", model=scripted, registry=registry)

    path = show_trace(result.messages)
    print(f"轨迹页已生成：{path}")


if __name__ == "__main__":
    main()
