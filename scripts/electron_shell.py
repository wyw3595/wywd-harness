"""s05 桌面壳：三进程编排 + 终端 UI + Windows spawn 守则。

📚 这一课在做什么（承接 src/harness/electron.py 的零件）
  electron.py 提供了 ElectronMain（主进程大脑）和 PreloadBridge（渲染桥），
  但还没人把它们装进"进程"里。本文件就是那个工厂：

     主线程（renderer）                子进程（main）
   ┌──────────────────┐    IPC    ┌─────────────────────┐
   │ PreloadBridge    │◄─────────►│ ElectronMain        │
   │ (终端 input 循环) │  两队列    │ (装配 model+registry │
   └──────────────────┘           │  +policy +闸门)     │
                                  └─────────────────────┘

  renderer 是"用户界面"，但它碰不到 LLM / 文件系统 / bash——
  它只有 PreloadBridge 这一个受控通道（对齐 s05 的沙箱教学点）。

📚 Windows spawn 守则（为什么这么写，务必读）：
  Windows 没有 fork，子进程靠"重新 import 本模块、再执行到 target 函数"
  来启动。所以有四条铁律，违反任何一条都会踩坑：
  1. mp.Process(target=...) 必须是模块级函数——类方法/闭包找不到；
  2. 装配（build_model/build_registry/build_policy）放子进程内部各自做，
     不跨进程传对象（队列里的消息要 pickle，model/registry 不可 pickle）；
  3. 启动逻辑全包进 if __name__ == "__main__":——spawn 会重跑模块顶层，
     重活绝不能放顶层（联网建模型会在每次子进程起来时再执行一遍）；
  4. 干净退出协议：renderer 发 None → main 循环 break → finally 里幂等
     再 put(None) + join(timeout=5) + 仍活着 terminate()。
"""

import multiprocessing as mp
import os

from scripts.toolbox import build_model, build_policy, build_registry
from src.harness.electron import ElectronMain, PreloadBridge
from src.harness.models import FakeModel


def choose_model():
    """离线可切：设了 DEEPSEEK_API_KEY 走真模型，否则 FakeModel。"""
    if os.getenv("DEEPSEEK_API_KEY"):
        return build_model()
    else:
        return FakeModel()


def main_process(renderer_to_main: mp.Queue, main_to_renderer: mp.Queue) -> None:
    """Electron Main 进程：装配自己的零件，然后 while 收消息路由。

    send/recv 两个注入点让 ElectronMain 只认识"发/收"两个回调，进程
    循环只负责"取一条 → 路由 → 回一条"；approver 的审批回执也是从
    renderer_to_main.get 里"嵌套接管"取到的（单线程，非并发）。
    """
    model = choose_model()
    registry = build_registry()
    policy = build_policy()
    main = ElectronMain(
        model, registry, policy,
        send=lambda m: main_to_renderer.put(m),   # 唯一"发给 renderer"的出口
        recv=renderer_to_main.get,                # 唯一"收 renderer 消息"的入口
        # 事件直播（练习 15 的钩子穿透 IPC）：run_agent 广播的事件，打包成
        # event 消息发到 renderer。注意事件名放 "event" 键——tool_start 的
        # data 里本来就有 name（工具名），用 name 存事件名会被覆盖。
        on_event=lambda event, data: main_to_renderer.put(
            {"type": "event", "data": {"event": event, **data}},
        ),
    )
    while True:
        msg = renderer_to_main.get()
        if msg is None: break               # 关停哨兵（守则④）
        outgoing = main.route(msg)
        if outgoing is not None:
            main_to_renderer.put(outgoing)


def renderer_process(send_queue: mp.Queue, recv_queue: mp.Queue) -> None:
    """Electron Renderer（主线程 UI）：只能走 PreloadBridge，碰不到别的东西。"""
    def show_event(data: dict) -> None:
        """把 main 直播过来的事件打印到终端（对齐 chat.py 的 on_event 风格）。

        data 里 "event" 是事件名（main 打包时放的键）；"name" 才是工具名
        （tool_start 自带）。分不清这两者就会拿错字段——见 main_process
        里那条注释。
        """

        kind = data.get("event")
        if kind == "round_start":
            print(f"  ⚙️ 第 {data.get('step')} 轮")
        elif kind == "model_reply":
            print(f"  🤖 模型请求：{data.get('tool_names')}")
        elif kind == "tool_start":
            print(f"  🔧 调用 {data.get('name')} 参数={data.get('arguments')}")
        elif kind == "tool_end":
            print(f"     ↳ {data.get('content')}")

    bridge = PreloadBridge(
        send_queue, recv_queue,
        on_event=show_event,   # 分派环遇到 event 消息会调这里（electron.py）
    )
    try:
        pong = bridge.ping()
    except Exception as e:
        print(f"Main 无响应: {e}")
        return
    print(f"[renderer] Main process: {pong}")
    print("s05 桌面壳（三进程模拟）。输入问题回车发送，q 退出。\n")
    while True:
        try:
            query = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in {"q", "quit", "exit", "退出"}:
            break
        result = bridge.send_message(query)
        print(result)
    send_queue.put(None)


def main() -> None:
    """启动与清理：两条队列 + 一个子进程 + 主线程跑 renderer。"""
    renderer_to_main = mp.Queue()
    main_to_renderer = mp.Queue()
    proc = mp.Process(target=main_process, args=(renderer_to_main, main_to_renderer))
    proc.start()
    try:
        renderer_process(renderer_to_main, main_to_renderer)
    finally:
        renderer_to_main.put(None)
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate(); proc.join()


if __name__ == "__main__":
    # 守则③：spawn 会重跑本模块顶层，启动逻辑必须在这里面。
    main()
