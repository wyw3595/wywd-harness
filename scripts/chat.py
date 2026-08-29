"""交互式命令行：和你的 Harness 真实对话。

这是整个项目的"产品入口"：输入任何问题，DeepSeek 会真的思考、
真的发起工具调用（查天气、做加法），循环真的执行并把结果回灌。

运行前设置密钥（PowerShell，只对当前窗口有效）：
    $env:DEEPSEEK_API_KEY = "sk-你的key"

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m scripts.chat

输入 q 退出。每个问题独立运行（轮内有记忆，问题之间没有）。
"""

from src.harness.agent import run_agent
from src.harness.file_tools import list_dir, read_file
from src.harness.real_model import RealModel
from src.harness.tools import Tool, ToolRegistry, tool_to_schema
from src.harness.trace import show_trace


def get_weather(city: str) -> str:
    """查询一个城市今天的天气。"""

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def add(a: int, b: int) -> int:
    """计算两个整数的和。"""

    return a + b


def main() -> None:
    weather_tool = Tool(
        name="get_weather",
        description="查询一个城市今天的天气",
        handler=get_weather,
    )
    add_tool = Tool(
        name="add",
        description="计算两个整数的和",
        handler=add,
    )
    registry = ToolRegistry()
    registry.register(weather_tool)
    registry.register(add_tool)

    file_tool_1 = Tool(name="list_dir",
                       description="列出项目里某个目录的内容（path 是相对项目根的路径，默认 . ）",
                       handler=list_dir)
    file_tool_2 = Tool(name="read_file",
                       description="读取项目里某个文本文件（path 相对项目根；超长自动截断）",
                       handler=read_file)

    registry.register(file_tool_1)
    registry.register(file_tool_2)
    

    model = RealModel(
        tools=[
            tool_to_schema(weather_tool),
            tool_to_schema(add_tool),
            tool_to_schema(file_tool_1),
            tool_to_schema(file_tool_2),
        ]
    )

    # 会话记忆：Harness 无状态，记忆归应用层管——就是这个变量。
    history: list[dict] | None = None

    print("和你的 Harness 对话吧！试试：")
    print("  北京天气怎么样？")
    print("  那上海呢？      <- 它能接住\"那\"，因为上一问还在记忆里")
    print("  读一下 README.md，用一句话总结")
    print("  输入 /trace 把当前会话变成可视化轨迹页（浏览器打开）")
    print("  输入 /clear 清空记忆，q 退出。\n")

    while True:
        try:
            task = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not task:
            continue
        if task.lower() in {"q", "quit", "exit", "退出"}:
            break

        if task == "/trace":
            # 可视化当前会话：history 就是完整的消息轨迹。
            if history:
                path = show_trace(history)
                print(f"轨迹页已生成：{path}")
            else:
                print("还没有对话内容，先聊一句再来 /trace。")
            continue

        if task == "/clear":
            history = None
            print("记忆已清空")
            continue

        result = run_agent(task, model=model, registry=registry, history=history)
        history = result.messages
        print(f"助手> {result.output}\n")

    print("再见！")


if __name__ == "__main__":
    main()
