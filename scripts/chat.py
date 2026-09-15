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

工作区（上传目录）：/ws 命令族让模型换个目录干活——
    /ws <路径>         切到一个本地目录（直接引用，不复制）
    /ws-zip <zip 路径>  解压 zip 成新工作区（三道安检）
    /ws-list           看有哪些工作区
    /ws-archive        把当前工作区打包成 zip 带走
    /ws-clean [id]     删掉一个工作区（要人工确认）
    /ws-reset          切回默认（本项目根）
切换会清空会话记忆：旧沙箱里的路径到了新沙箱全都不存在，
留着只会误导模型。
"""

import json
import zipfile
from pathlib import Path

from scripts.toolbox import (
    DEFAULT_WORKSPACE,
    MAX_HISTORY_MESSAGES,
    build_model,
    build_policy_for,
    build_registry_for,
    with_system,
)
from src.harness.agent import run_agent
from src.harness.memory import trim_history
from src.harness.permissions import (
    AuditTrail,
    GovernedToolRunner,
    PermissionDecision,
)
from src.harness.trace import show_trace
from src.harness.workspace import WORKSPACE_BASE, Workspace


def cli_approver(decision: PermissionDecision) -> bool:
    """终端审批员（练习 s04）：把待审请求亮出来，等用户敲 y/n。

    只有 ASK 分支会调到这里——ALLOW/DENY 不惊动人；返回 False 时
    循环回灌"被权限拦截"，模型会换路径或向用户解释。
    """

    print(f"  ⚠️ 需要审批 [{decision.rule_id}] {decision.reason}")
    answer = input("     允许这次工具调用吗？(y/n) ").strip().lower()
    return answer == "y"


def build_runtime(workspace: Workspace) -> tuple:
    """按工作区现做一套运行件：工具注册表 + 权限闸门。

    为什么模型不在其中：工具 schema 只由"名字 + 描述 + 签名"决定，
    换个工作区这三样一样——变的只有 handler 闭包捕获的沙箱根。
    所以 RealModel 的说明书可以复用，重建的只有 registry 和策略
    （策略里的 WorkspaceScope 必须跟着 root 走，这是硬绑的）。
    """

    registry = build_registry_for(workspace)
    runner = GovernedToolRunner(
        policy=build_policy_for(workspace),
        approver=cli_approver,
        registry=registry,
        audit=AuditTrail(),
    )
    return registry, runner


def _print_workspace(workspace: Workspace) -> None:
    """一行说清"模型现在在哪个目录里干活"。"""

    origin = "（默认：本项目根）" if workspace.workspace_id == "default" else ""
    print(f"  当前工作区：{workspace.workspace_id}{origin}")
    print(f"  沙箱根：{workspace.root}")


def handle_workspace_command(
    task: str, current: Workspace, history: list[dict] | None
) -> tuple | None:
    """处理 /ws 命令族；返回 (新工作区 | None, 是否清空历史)，None = 不是命令。

    设计取舍：返回**指令**而不是就地改状态——main() 拿着返回值统一
    重建运行件、决定要不要清记忆。命令解析与状态变更分开，读起来
    才知道"切一次工作区到底动了哪些东西"。

    三条拒绝对项目根（default）的破坏性操作（archive / clean）：
    默认工作区就是本项目，打包带走没意义、删掉等于删项目——拒绝而
    不是"问一下就放行"，因为这种操作没有正确的使用场景。
    """

    if task == "/ws" or task.startswith("/ws "):
        raw = task[len("/ws"):].strip().strip('"').strip("'")
        if not raw:
            _print_workspace(current)
            return (None, False)
        try:
            workspace = Workspace.from_existing_dir(Path(raw))
        except FileNotFoundError as exc:
            print(f"  切换失败：{exc}")
            return (None, False)
        print(f"  已切到本地目录（直接引用，不复制）：{workspace.root}")
        print("  会话记忆已清空——旧沙箱的路径在新沙箱里不存在。")
        return (workspace, True)

    if task.startswith("/ws-zip"):
        raw = task[len("/ws-zip"):].strip().strip('"').strip("'")
        if not raw:
            print("  用法：/ws-zip <zip 文件路径>")
            return (None, False)
        try:
            workspace = Workspace.from_zip(Path(raw))
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            # 安检没过/坏包/文件不存在：三类错误都走同一句人话回执，
            # 调用方不用分辨异常类型（失败即回滚在 from_zip 里做完了）。
            print(f"  上传失败（已回滚，没留下半成品）：{exc}")
            return (None, False)
        print(f"  已解压为工作区 {workspace.workspace_id}")
        print(f"  沙箱根：{workspace.root}")
        return (workspace, True)

    if task == "/ws-list":
        if not WORKSPACE_BASE.is_dir():
            print(f"  还没有任何工作区（{WORKSPACE_BASE} 不存在）——"
                  f"用 /ws-zip <包> 建一个。")
            return (None, False)
        entries = sorted(WORKSPACE_BASE.iterdir())
        if not entries:
            print(f"  {WORKSPACE_BASE} 是空的。")
            return (None, False)
        print(f"  工作区目录：{WORKSPACE_BASE}")
        for entry in entries:
            kind = "目录" if entry.is_dir() else "文件"
            # 剥层的工作区 root 在 <id>/<repo>/——用"包含关系"判当前，
            # 纯相等会把剥层的那种漏掉。
            is_current = (
                entry.resolve() == current.root
                or (entry.is_dir() and current.root.is_relative_to(entry.resolve()))
            )
            print(f"    - {entry.name}  ({kind}){'   ← 当前' if is_current else ''}")
        return (None, False)

    if task.startswith("/ws-archive"):
        if current.workspace_id == "default":
            print("  拒绝：默认工作区就是本项目根，整个项目不需要打包带走。")
            return (None, False)
        raw = task[len("/ws-archive"):].strip().strip('"').strip("'")
        archive_path = current.archive(raw or None)
        print(f"  已归档：{archive_path}")
        return (None, False)

    if task.startswith("/ws-clean"):
        raw = task[len("/ws-clean"):].strip()
        if not raw or raw == "当前":
            target = current
        else:
            target = Workspace(workspace_id=raw,
                               root=(WORKSPACE_BASE / raw).resolve())
        if target.workspace_id == "default":
            print("  拒绝：默认工作区就是本项目根，删它等于删项目。"
                  "先 /ws 切到别的工作区再删。")
            return (None, False)
        if not target.root.is_dir():
            print(f"  没有这个工作区目录：{target.root}")
            return (None, False)
        answer = input(f"     确认删除 {target.root}（含其中全部文件）？(y/n) "
                       ).strip().lower()
        if answer != "y":
            print("  已取消，什么都没删。")
            return (None, False)
        print("  " + target.cleanup(confirm=True))
        if target.workspace_id == current.workspace_id:
            print("  当前工作区已被删除，已切回默认工作区（本项目根）。")
            return (DEFAULT_WORKSPACE, True)
        return (None, False)

    if task == "/ws-reset":
        print(f"  已切回默认工作区（本项目根）：{DEFAULT_WORKSPACE.root}")
        return (DEFAULT_WORKSPACE, True)

    if task == "/ws-help":
        print("  工作区命令：")
        print("    /ws                看当前工作区")
        print("    /ws <路径>         切到一个本地目录（直接引用，不复制）")
        print("    /ws-zip <zip 路径>  解压 zip 成新工作区（三道安检）")
        print("    /ws-list           看有哪些工作区")
        print("    /ws-archive        把当前工作区打包成 zip 带走")
        print("    /ws-clean [id]     删掉一个工作区（要人工确认）")
        print("    /ws-reset          切回默认（本项目根）")
        return (None, False)

    return None


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
    # 工作区（上传目录）：默认 = 本项目根。启动即按它做一套运行件——
    # 工具闭包与权限作用域都绑着这个 root，/ws 切一次就重建一次
    # （见 handle_workspace_command 的返回值处理）。
    workspace = DEFAULT_WORKSPACE
    registry, runner = build_runtime(workspace)
    # 模型放在工作区之外：工具 schema 与 root 无关（名字/描述/签名
    # 都一样），换目录不需要重建说明书。审批员是终端 y/n（web_app
    # 的审批卡是另一个入口另一套 UI，不共用显示代码）。
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
    print("  输入 /ws-help 看工作区命令（让模型换个目录干活）")
    print("  输入 /clear 清空记忆，q 退出。")
    _print_workspace(workspace)
    print()

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

        # 工作区命令族（/ws*）：不是命令则返回 None，落到下面的普通提问。
        # 状态变更有三件，缺一件就是半切：换工作区变量、重建运行件
        # （工具闭包 + 权限作用域要跟着新 root）、清掉旧记忆。
        handled = handle_workspace_command(task, workspace, history)
        if handled is not None:
            new_workspace, clear_history = handled
            if new_workspace is not None:
                workspace = new_workspace
                registry, runner = build_runtime(workspace)
                _print_workspace(workspace)
            if clear_history:
                history = None
                print("  记忆已清空（换了沙箱，旧路径不再有效）。")
            continue

        # 成本刹车（练习 19）：调 run_agent 之前先把历史瘦到窗口大小。
        # result.messages 以截断后的历史为前缀，下一问自动延续瘦身的记忆。
        if history:
            history = trim_history(history, MAX_HISTORY_MESSAGES)
        # 目录常驻：system 提示（含延迟工具目录）每次调到最前，幂等。
        history = with_system(history)
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
            runner=runner,
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
