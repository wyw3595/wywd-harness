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

import json

from scripts.toolbox import (
    MAX_HISTORY_MESSAGES,
    build_model,
    build_registry,
    build_system_prompt,
)
from src.harness.agent import run_agent
from src.harness.memory import trim_history
from src.harness.trace import show_trace


def _with_system(history: list[dict] | None) -> list[dict]:
    """把系统提示（工具目录）放到会话最前，且幂等——不重复添加。

    对齐 s03 的目录设计：目录是独立工件，常驻模型上下文；历史截断
    可能把 system 切出窗口（窗口比消息少时），这里自动补回；若
    history 第一条已是 system（上一轮的 result.messages 带回来的），
    直接原样返回。谁也不用特判。
    """

    system_message = {"role": "system", "content": build_system_prompt()}
    if not history:
        return [system_message]
    if history[0].get("role") == "system":
        return history
    return [system_message] + history


def _format_messages(messages: list[dict]) -> str:
    """把消息列表渲染成"role 视角全量"文本。

    /msgs 和每次请求前的自动展示共用同一份渲染——只写一处，
    两边看到的永远是同一个格式（避免逻辑漂移）。
    """

    lines: list[str] = []
    for index, message in enumerate(messages):
        role = message["role"]
        lines.append(f"[{index}] role={role}")
        if role == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                lines.append(f"    content: {message['content']}")
            for call in message["tool_calls"]:
                args = json.dumps(call["arguments"], ensure_ascii=False)
                lines.append(f"    tool_call: {call['name']}({call['call_id']}) -> {args}")
        elif role == "tool":
            content = message.get("content", "")
            lines.append(f"    回灌(tool_call_id={message.get('tool_call_id')}): {content}")
        else:
            lines.append(f"    {message.get('content', '')}")
    return "\n".join(lines)


def _format_tools(tools: list[dict] | None) -> str:
    """把 tools 数组渲染成"说明书视角"文本。

    发给模型的请求有两部分：messages（对话）和 tools（工具说明书，
    DeepSeek 请求体顶层的 tools 字段）。展示时两者都要——只看消息
    看不到模型手里握着哪些工具。
    """

    if not tools:
        return "（本会话没有工具说明书）"
    lines: list[str] = []
    for index, tool in enumerate(tools):
        lines.append(f"[{index}] {tool['function']['name']}")
        lines.append(f"    {tool['function']['description']}")
        pretty = json.dumps(tool, ensure_ascii=False, indent=2).replace("\n", "\n    ")
        lines.append(f"    {pretty}")
    return "\n".join(lines)


def main() -> None:
    registry = build_registry()
    model = build_model()

    def on_event(event: str, data: dict) -> None:
        """实时播报循环事件——过程不再黑盒。"""

        if event == "round_start":
            print(f"  ⚙️ 第 {data['step']} 轮")
        elif event == "model_reply":
            if data["kind"] == "tool_calls":
                names = "、".join(data["tool_names"])
                print(f"  🤖 模型请求调用工具：{names}")
            else:
                print("  🤖 模型给出最终回答")
        elif event == "tool_start":
            args = json.dumps(data["arguments"], ensure_ascii=False)
            print(f"  🔧 执行 {data['name']}({args}) ……")
        elif event == "tool_end":
            print(f"     ↳ {data['content']}")

    # 会话记忆：Harness 无状态，记忆归应用层管——就是这个变量。
    history: list[dict] | None = None

    print("和你的 Harness 对话吧！试试：")
    print("  北京天气怎么样？")
    print("  那上海呢？      <- 它能接住\"那\"，因为上一问还在记忆里")
    print("  读一下 README.md，用一句话总结")
    print("  输入 /msgs 查看当前会话发给模型的全部消息（role 视角全量）")
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

        if task == "/msgs":
            # 查看"发给模型的所有东西"：history 里每一轮的完整消息
            # （system 目录 / user 提问 / assistant 的工具调用清单与
            # 最终回答 / tool 的结果回灌）。全量打印，不截断。
            if not history:
                print("还没有对话内容，先聊一句再来 /msgs。")
                continue
            print("── 以下是当前会话发给模型的全部消息 ──")
            print(_format_messages(history))
            print("── 全部消息到此为止 ──")
            continue

        if task == "/clear":
            history = None
            print("记忆已清空")
            continue

        # 成本刹车（练习 19）：调 run_agent 之前先把历史瘦到窗口大小。
        # result.messages 以截断后的历史为前缀，下一问自动延续瘦身的记忆。
        if history:
            history = trim_history(history, MAX_HISTORY_MESSAGES)
        # 目录常驻：system 提示（含延迟工具目录）每次调到最前，幂等。
        history = _with_system(history)
        # 透明化：发出前展示这次"发给模型的所有东西"——请求体由两部分
        # 组成：tools（模型能调用的说明书）+ messages（对话内容）。
        # 与 /msgs 的 messages 渲染共用 _format_messages。
        request_messages = history + [{"role": "user", "content": task}]
        print("─" * 24 + " ⤴ 发给模型的完整请求 " + "─" * 24)
        print("── 工具（tools 参数：模型能调用的说明书）──")
        print(_format_tools(model.tools))
        print()
        print("── 消息（messages 参数：对话内容）──")
        print(_format_messages(request_messages))
        print("─" * 60)
        result = run_agent(
            task,
            model=model,
            registry=registry,
            history=history,
            on_event=on_event,
        )
        history = result.messages
        # 失败不该和成功长得一样（练习 16）；history 照常更新——
        # 用户的问题已入列，下一问还带着上下文。
        # s01 整合：截断（答案被 token 预算掐断）也因为 incomplete 而闪黄——
        # 和失败同理，不能让用户以为这是完整回答。
        if result.status == "failed":
            print(f"⚠️ {result.output}")
        elif result.status == "truncated":
            print(f"⚠️ 助手> {result.output}（回答被截断，内容不完整）\n")
        else:
            print(f"助手> {result.output}\n")
        # 本问账单（练习 18）：usage 是本次运行跨轮累计的 token 用量。
        # 连着追问几句，输入 tokens 会随记忆变大——历史全量重发，记忆=钱。
        print(
            f"（本问 tokens：输入 {result.usage.get('prompt_tokens', 0)}"
            f" / 输出 {result.usage.get('completion_tokens', 0)}）"
        )
        # 交互分割线：一问一答一个段落，别让多轮输出糊成一团。
        print("─" * 60)

    print("再见！")


if __name__ == "__main__":
    main()
