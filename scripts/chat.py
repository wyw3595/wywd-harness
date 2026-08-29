"""交互式命令行：和你的 Harness 真实对话。

这是整个项目的"终端入口"：输入任何问题，DeepSeek 会真的思考、
真的发起工具调用（查天气、做加法、列目录、读文件），循环真的执行
并把结果回灌。

运行前设置密钥（PowerShell，一次性）：
    setx DEEPSEEK_API_KEY "sk-你的key"

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m scripts.chat

输入 /trace 生成可视化轨迹页，/clear 清空记忆，q 退出。
每个问题独立运行（轮内有记忆，问题之间没有）。
"""

from scripts.toolbox import build_model, build_registry
from src.harness.agent import run_agent
from src.harness.trace import show_trace


def main() -> None:
    registry = build_registry()
    model = build_model()

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
