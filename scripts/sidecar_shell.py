"""s06 编排：真多进程 Sidecar + 终端 UI。

📚 这一课在做什么（承接 src/harness/sidecar.py 的零件）
  sidecar.py 提供了 SidecarServer（agent 宿主 + JSON-RPC 路由）和
  MainProcessClient（壳侧客户端 + 分派环），本文件把它们装进真进程：

    主进程（壳/UI）                子进程（Sidecar/agent）
   ┌──────────────────┐  socketpair  ┌────────────────────┐
   │ MainProcessClient │◄────────────►│ SidecarServer      │
   │ call + 分派环     │  JSON-RPC     │ handle_connection  │
   └──────────────────┘               └────────────────────┘

  Windows spawn 守则（同 s05，务必读）：
    1. mp.Process target 必须是模块级函数（sidecar_process）；
    2. 装配（choose_model/build_registry/build_policy）在子进程内部做；
    3. 启动逻辑全包进 if __name__ == "__main__":；
    4. 干净退出：main 先 call(shutdown) 再 close()（EOF）→ join(timeout=5)
       → 还活着 terminate()。shutdown 单独不生效，必须 close() 触发 EOF。
  socket 跨进程：socket.socketpair() 的一端传进 args，Windows 上官方
  reducer 支持 pickle；父进程 start() 后必须 srv.close() 释放自己的句柄。
"""

import multiprocessing as mp
import socket

from scripts.electron_shell import choose_model
from scripts.toolbox import build_policy, build_registry, with_system
from src.harness.sidecar import MainProcessClient, RPCConnection, SidecarServer


def show_event(data: dict) -> None:
    """把 sidecar 直播过来的事件打印到终端（对齐 electron_shell.show_event）。

    TODO 1（你来填）：
      data 里 "event" 是事件名，"name" 才是工具名（tool_start 自带）。
        - round_start  → print(f"  ⚙️ 第 {data.get('step')} 轮")
        - tool_start   → print(f"  🔧 调用 {data.get('name')} 参数={data.get('arguments')}")
        - tool_end     → print(f"     ↳ {data.get('content')}")
        - model_reply  → print(f"  🤖 模型请求：{data.get('tool_names')}")
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


def sidecar_process(sock: socket.socket) -> None:
    """Sidecar 子进程入口（mp target，必须模块级——spawn 守则①）。

    TODO 2（你来填）：
      1. 装配全在子进程内（守则②）：
           server = SidecarServer(
               model=choose_model(),
               registry=build_registry(),
               policy=build_policy(),
               history_seed=lambda: with_system([]),   # system 常驻起步
           )
      2. server.handle_connection(RPCConnection(sock))   # 接管连接循环
    """
    server = SidecarServer(
        model=choose_model(),
        registry=build_registry(),
        policy=build_policy(),
        history_seed=lambda: with_system([]),   # system 常驻起步
    )
    server.handle_connection(RPCConnection(sock))


def main() -> None:
    """壳主流程：起 sidecar 子进程 → 连接 → 交互 → 干净收尾。

    TODO 3（你来填）：
      1. srv, cli = socket.socketpair()
         proc = mp.Process(target=sidecar_process, args=(srv,))
         proc.start(); srv.close()          # 父进程不再持有这端
      2. client = MainProcessClient(on_event=show_event)
         client.connect(cli)
      3. try:
           pong = client.call("sidecar/ping")["result"]
           print(f"[main] sidecar/ping → {pong['status']}")
           sid = client.call("session/create", {"cwd": ".", "mode": "craft"})
                 ["result"]["sessionId"]
           print 提示：输入问题回车发送，/status /sessions /logs 是特殊命令，q 退出
           while True:                      # 交互循环
               query = input("s06 >> ").strip()
               (EOFError/KeyboardInterrupt → break；空 → continue)
               q/exit/quit → break
               /status  → call("sidecar/status") → 打印 sessions/ringBuffer/handlers
               /sessions→ call("session/list") → 打印每个会话 id/status/cwd
               /logs    → call("sidecar/logs") → 打印 logs
               其他     → r = client.call("agent/send", {"sessionId": sid,
                          "message": query})
                          有 error 打 error，否则打 result["output"]
         finally:
           try: client.call("sidecar/shutdown")
           except Exception: pass           # sidecar 已死也要继续收尾
           client.close()                   # EOF → sidecar 循环 break
           proc.join(timeout=5)
           if proc.is_alive():
               proc.terminate(); proc.join()   # 别留僵尸（守则④）
    """
    # ① 建 socketpair，把一端交给 sidecar 子进程（Windows reducer 支持 pickle）
    srv, cli = socket.socketpair()
    proc = mp.Process(target=sidecar_process, args=(srv,))
    proc.start()
    srv.close()  # 父进程不再持有这端——子进程有自己那份 duplicate

    # ② 壳侧客户端：接上 cli 端，挂上事件直播
    client = MainProcessClient(on_event=show_event)
    client.connect(cli)

    try:
        pong = client.call("sidecar/ping")["result"]
        print(f"[main] sidecar/ping → {pong['status']}")
        sid = client.call("session/create", {"cwd": ".", "mode": "craft"})["result"]["sessionId"]
        print(f"[main] session/create → {sid}")
        print("输入问题回车发送。特殊命令：/status /sessions /logs。q 退出。\n")

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
                r = client.call("sidecar/status")["result"]
                print(f"  sessions: {r['sessions']}")
                print(f"  ringBuffer: {r['ringBufferUsed']}/{r['ringBufferTotal']} bytes")
                print(f"  handlers: {r['handlers']}\n")
                continue

            if query == "/sessions":
                r = client.call("session/list")["result"]
                for s in r["sessions"]:
                    print(f"  {s['id']}  {s['status']}  {s['cwd']}")
                print()
                continue

            if query == "/logs":
                r = client.call("sidecar/logs")["result"]
                logs = r["logs"]
                print(f"{logs[-2000:] if len(logs) > 2000 else logs}\n")
                continue

            r = client.call("agent/send", {"sessionId": sid, "message": query})
            result = r["result"]
            if "error" in result:
                print(f"Error: {result['error']}\n")
            else:
                print(f"{result['output']}\n")
    finally:
        try:
            client.call("sidecar/shutdown")  # 先礼貌通知
        except Exception:
            pass  # sidecar 已死也要继续收尾
        client.close()  # EOF → sidecar 的 recv 返回 None → 循环 break → 进程退出
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join()  # 别留僵尸（守则④）



if __name__ == "__main__":
    # 守则③：spawn 会重跑本模块顶层，启动逻辑必须在这里面。
    main()
