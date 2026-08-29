"""冒烟脚本：真实调用 DeepSeek，亲眼看看真模型的回复长什么样。

练习 09 起配了真工具：问天气 -> 模型真实发起 tool_calls -> 循环执行 ->
模型基于工具结果给出最终回答。

运行前先在 PowerShell 里设置密钥（只对当前窗口有效）：
    $env:DEEPSEEK_API_KEY = "sk-你的key"

然后运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m scripts.smoke_deepseek
"""

from src.harness.agent import run_agent
from src.harness.real_model import RealModel
from src.harness.tools import Tool, ToolRegistry, tool_to_schema


def get_weather(city: str) -> str:
    """演示用工具：查询一个城市的天气。

    故意返回模型不可能自己编出来的数据——最终回答里出现"紫色雪花"，
    就证明工具结果真的参与了回答（真伪判别法）。
    """

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def main() -> None:
    weather_tool = Tool(
        name="get_weather",
        description="查询一个城市今天的天气",
        handler=get_weather,
    )
    registry = ToolRegistry()
    registry.register(weather_tool)

    # 把工具说明书交给真模型：没有这份说明书，它不知道 get_weather 的存在。
    model = RealModel(tools=[tool_to_schema(weather_tool)])

    print("=== 第 1 步：单发 generate ===")
    reply = model.generate(
        [{"role": "user", "content": "用一句话说明什么是 Agent Loop"}]
    )
    print(reply.text)

    print()
    print("=== 第 2 步：run_agent + 真工具（期待真实 tool_calls 往返）===")
    result = run_agent("北京天气怎么样？", model=model, registry=registry)
    print(f"[status={result.status}] {result.output}")


if __name__ == "__main__":
    main()
