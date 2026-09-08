"""s06 编排（s06.5 后）：终端 UI 接 SidecarShell——壳的编排抽到 shell.py。

    ┌────────────────┐        ┌──────────────┐ socketpair  ┌─────────────────┐
    │ 终端 UI（本文件）│──────►│ SidecarShell │◄───────────►│ sidecar 子进程   │
    │ input/print     │ 回调  │ (壳，shell.py) │ JSON-RPC   │ (SidecarServer) │
    │ show_event      │       └──────────────┘             └─────────────────┘

  本文件只剩"终端展示"（UI 归 UI）：show_event 打印直播 + main 的交互循环。
  起进程 / 连接 / 建会话 / 收尾 全部由 SidecarShell 负责（s06.5 抽的壳，
  终端和网页共用同一套）。附带的红利：/clear 免费获得（shell.clear()）。
"""

from scripts.shell import SidecarShell


def show_event(data: dict) -> None:
    """把 sidecar 直播过来的事件打印到终端（对齐 electron_shell.show_event）。

    data 里 "event" 是事件名，"name" 才是工具名（tool_start 自带）。
    """

    kind = data.get("event")
    if kind == "round_start":
        print(f"  ⚙️ 第 {data.get('step')} 轮")
    elif kind == "tool_start":
        print(f"  🔧 调用 {data.get('name')} 参数={data.get('arguments')}")
    elif kind == "tool_end":
        print(f"     ↳ {data.get('content')}")
    elif kind == "model_reply":
        print(f"  🤖 模型请求：{data.get('tool_names')}")


def main() -> None:
    """壳主流程：起 sidecar（SidecarShell 管）→ 交互 → 干净收尾。"""

    shell = SidecarShell(on_event=show_event)
    try:
        pong = shell.start()
        print(f"[main] sidecar/ping → {pong['status']}")
        print(f"[main] session/create → {shell.session_id}")
        print("输入问题回车发送。特殊命令：/status /sessions /logs /clear "
              "/close /resume <id> /forget <id>。q 退出。\n")

        while True:
            try:
                query = input("s06 >> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not query:
                continue
            if query.lower() in ("q", "exit", "quit"):
                break

            if query == "/status":
                r = shell.status()
                print(f"  sessions: {r['sessions']}")
                print(f"  ringBuffer: {r['ringBufferUsed']}/{r['ringBufferTotal']} bytes")
                print(f"  handlers: {r['handlers']}\n")
                continue

            if query == "/sessions":
                # s07-b：清单多了 live（运行时活着吗）和 gen（第几代）两列
                # ——closed 的会话还在清单里，live=False（记录 ≠ 运行时）。
                for s in shell.sessions()["sessions"]:
                    print(f"  {s['id']}  {s['status']:<7}  "
                          f"live={s['live']}  gen={s['runtimeGeneration']}  "
                          f"{s['cwd']}")
                print()
                continue

            if query == "/logs":
                logs = shell.logs()
                print(f"{logs[-2000:] if len(logs) > 2000 else logs}\n")
                continue

            if query == "/clear":
                print(f"  ↳ 新会话 {shell.clear()}\n")
                continue

            if query == "/close":
                # 关当前会话（不传参=当前）：记录保留，closed ≠ 消失。
                result = shell.close_session()   # {"status","closed"} 或 {"error"}
                if "error" in result:
                    print(f"Error: {result['error']}\n")
                else:
                    print(f"  ↳ 会话 {result.get('closed')} 已关闭（记录保留）\n")
                    print("    /resume <id> 可复活，/clear 可开新会话\n")
                continue   # 别把 "/close" 当普通消息发给 agent

            if query.startswith("/resume"):
                parts = query.split()
                if len(parts) < 2:
                    print('  用法：/resume sess_0001\n')
                    continue
                sid = parts[1]
                result = shell.resume_session(sid)   # 成功 {"sessionId","generation"}；live 被拒
                if "error" in result:
                    print(f"Error: {result['error']}\n")
                else:
                    print(f"  ↳ 已复活 {sid}（generation {result['generation']}）\n")
                continue

            if query.startswith("/forget"):
                parts = query.split()
                if len(parts) < 2:
                    print('  用法：/forget sess_0001\n')
                    continue
                sid = parts[1]
                result = shell.forget_session(sid)   # live 的会被拒绝：先 close 再 forget
                if "error" in result:
                    print(f"Error: {result['error']}\n")
                else:
                    print(f"  ↳ 已遗忘 {sid}\n")
                continue

            result = shell.send(query)
            if "error" in result:
                print(f"Error: {result['error']}\n")
            else:
                print(f"{result['output']}\n")
    finally:
        shell.stop()  # shutdown → close(EOF) → join → terminate，SidecarShell 管


if __name__ == "__main__":
    # 守则③：spawn 会重跑本模块顶层，启动逻辑必须在这里面。
    main()
