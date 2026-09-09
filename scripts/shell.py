"""s06.5 SidecarShell：UI 无关的壳——终端和网页共用一套 sidecar 的编排件。

s06 把 agent 搬进独立 sidecar 子进程，但"起进程 + 连 socket + 建会话 +
干净收尾"这段编排埋在 sidecar_shell.py 的 main() 里，网页 UI 想接 sidecar
只能抄一遍。本文件把这层抽成 SidecarShell 类：

    ┌──────────────────┐        ┌──────────────┐ socketpair  ┌─────────────────┐
    │ UI（终端/网页任意）│──────►│ SidecarShell │◄───────────►│ sidecar 子进程   │
    │ 只调 send/status/ │ 回调  │ (壳：         │ JSON-RPC    │ (SidecarServer) │
    └──────────────────┘        │ MainProcessClient)          └─────────────────┘

  UI 与壳通过两个回调解耦（s05 起的注入哲学）：
    - user_prompt(rule_id, reason) -> bool  审批员（终端 input / 网页按钮）
    - on_event(data: dict) -> None          事件直播（终端 print / 网页步骤卡）
      data["event"] 是事件名，data["name"] 才是工具名（s06 的坑）。

  Windows spawn 守则与 s06 完全一致：
    - mp target 是模块级 _sidecar_process；装配在子进程内做；
    - 干净退出：shutdown → close()（EOF）→ join(5s) → terminate()。
  注意：chainlit 用 console shim 启动，spawn 子进程以 __mp_main__ 重导入，
  shim 的 if __name__ == "__main__" 在子进程为 False，不会重启网页服务——
  但任何 mp target 都必须定义在本文件（模块级），别放 chainlit_app.py。
"""

import multiprocessing as mp
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

# 关键：spawn 子进程会重导入本模块找 target 函数（_sidecar_process），但
# 子进程是"全新解释器"，它在反序列化 process_obj 时就要 import 本模块
# ——此时靠代码内 sys.path 注入是"鸡生蛋"（import 不到就执行不到注入）。
# 所以用环境变量 PYTHONPATH 硬保证：子进程解释器启动时自动把项目根加进
# sys.path，import scripts.shell 才可能成功。这是 chainlit（shim 启动）
# 场景下 spawn 子进程能找到 target 的最后一环。
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_env_paths = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
if _PROJECT_ROOT not in _env_paths:
    os.environ["PYTHONPATH"] = os.pathsep.join([_PROJECT_ROOT] + _env_paths)

from scripts.electron_shell import choose_model
from scripts.toolbox import (
    build_history_seed,
    build_model_router,
    build_policy,
    build_registry,
    with_system,
)
from src.harness.jsonl_store import JsonlSessionStore
from src.harness.sidecar import MainProcessClient, RPCConnection, SidecarServer


def _sidecar_process(sock: socket.socket) -> None:
    """Sidecar 子进程入口（spawn 守则①：必须模块级函数）。装配全在子进程内。

    store 用 JSONL 证据文件（s09）：会话活过进程重启（.sessions/，已进
    .gitignore，测试注 tmp）。model 用路由器（s08）：Router 实现 Model
    协议（generate → craft 槽），sidecar/run_agent 零改动。choose_model()
    保留给 electron_shell（s05 直连架构，不走 sidecar——两个入口两套
    装配，都从 toolbox 出）。

    s10 TODO 9（你来填）：起步历史换成带记忆的种子（一行改动）：
      history_seed=build_history_seed(),
    （替代 lambda: with_system([])——空记忆时 build_history_seed 返回
    的 seed 与它完全一致，行为零变化；有记忆时多一条 system。）
    """
    server = SidecarServer(
        model=build_model_router(),
        registry=build_registry(),
        policy=build_policy(),
        history_seed=lambda: with_system([]),   # system 常驻起步
        store=JsonlSessionStore(root=Path(_PROJECT_ROOT) / ".sessions"),
    )
    server.handle_connection(RPCConnection(sock))


class SidecarShell:
    """起一个 sidecar 子进程、建一个会话，把 RPC 包装成 UI 友好的方法。

    线程约定：所有 RPC 调用过同一把 _rpc_lock——MainProcessClient 是
    单线程的（s06 死锁违约③：双线程同 recv），壳作为唯一咽喉负责串行化。
    """

    def __init__(
        self,
        user_prompt: Optional[Callable[[str, str], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        cwd: str = ".",
        *,
        _client_factory: Any = None,
        _process_factory: Any = None,
    ) -> None:
        self._user_prompt = user_prompt
        self._on_event = on_event
        self._cwd = cwd
        # 测试注入点（默认真实现；单测喂假 client/假 process 绕开 spawn）
        self._client_factory = _client_factory or MainProcessClient
        self._process_factory = _process_factory or mp.Process
        self._client: Optional[MainProcessClient] = None
        self._proc: Any = None
        self._sid: Optional[str] = None
        # 并发串行化（坑 2）：chainlit 不串行化消息，双连发会双线程进 call
        self._rpc_lock = threading.Lock()

    # ── 生命周期 ─────────────────────────────────────────────

    def start(self) -> dict:
        """起 sidecar 子进程并建会话；返回 ping 结果。失败时不留半个壳。"""
        if self._proc is not None:
            raise RuntimeError("SidecarShell 已启动，不要重复 start")
        # 关键：chainlit 加载 app 后会整体重置 sys.path（顶层注入被清掉），
        # 而 spawn 的 preparation data 是 proc.start() 那一刻快照的 sys.path
        # ——必须在 spawn 前一刻把项目根放回去，子进程才能 import scripts.shell
        # 找到 target 函数。这是"spawn 找得到 target"的最后一环。
        sys.path.insert(0, _PROJECT_ROOT)
        srv, cli = socket.socketpair()
        self._proc = self._process_factory(target=_sidecar_process, args=(srv,))
        try:
            self._proc.start()
            srv.close()   # 父进程不再持有这端（子进程有自己那份 duplicate）
            self._client = self._client_factory(
                user_prompt=self._user_prompt, on_event=self._on_event)
            self._client.connect(cli)
            with self._rpc_lock:
                pong = self._client.call("sidecar/ping")["result"]
                sid = self._client.call("session/create",
                    {"cwd": self._cwd, "mode": "craft"})["result"]["sessionId"]
            self._sid = sid
            return pong
        except Exception:
            # 启动失败：清半壳（子进程 + socket）再抛——不留僵尸不留空壳
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join()
            if self._client is not None:
                self._client.close()
            self._proc = None
            self._client = None
            raise

    def stop(self) -> None:
        """干净收尾（幂等）：shutdown → close(EOF) → join(5s) → terminate。

        有在途 call 时跳过礼貌 shutdown（acquire(blocking=False) 拿不到锁），
        直接用 close() 的 EOF 打断它——在途线程的 recv 会抛 OSError →
        ConnectionClosed，诚实失败而不是卡死。
        """
        if self._proc is None:
            return  # 幂等：没起过壳 / 已停过，直接返回
        proc, client = self._proc, self._client
        try:
            if client is not None:
                if self._rpc_lock.acquire(blocking=False):  # 空闲才走礼貌协议
                    try:
                        try:
                            client.call("sidecar/shutdown")
                        except Exception:
                            pass  # sidecar 已死也要继续收尾
                    finally:
                        self._rpc_lock.release()
                client.close()  # EOF 兜底（必须）：让 sidecar 的 recv 返回 None
        finally:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join()  # 别留僵尸
            self._proc = None
            self._client = None
            self._sid = None

    # ── RPC 包装（全部过 _rpc_lock）─────────────────────────

    def send(self, message: str) -> dict:
        """把一句话交给 agent；返回 agent/send 的 result（可能含 "error"）。"""

        with self._rpc_lock:
            return self._client.call("agent/send",
                {"sessionId": self._sid, "message": message})["result"]

    def status(self) -> dict:
        """sidecar 状态：会话数 / RingBuffer 用量 / handler 数。"""

        with self._rpc_lock:
            return self._client.call("sidecar/status")["result"]

    def sessions(self) -> dict:
        """sidecar 里的会话列表。"""

        with self._rpc_lock:
            return self._client.call("session/list")["result"]

    def logs(self) -> str:
        """sidecar 最近日志（走 RPC——真多进程下主进程读不到子进程内存）。"""

        with self._rpc_lock:
            return self._client.call("sidecar/logs")["result"].get("logs", "")

    def tools(self) -> list:
        """sidecar 持有的工具清单（网页欢迎语可渲染，替代硬编码文案）。"""

        with self._rpc_lock:
            return self._client.call("tool/list")["result"].get("tools", [])

    def clear(self) -> str:
        """清记忆（s07-b 升级）：完整编舞 close → forget → create。

        四操作里三个在同一命令里跑一遍：close（释放运行时）→
        forget（删记录）→ create（新身份）。
        """
        with self._rpc_lock:
            if self._sid:
                self._client.call("session/close", {"sessionId": self._sid})
                self._client.call("session/forget", {"sessionId": self._sid})
            sid = self._client.call("session/create",
                {"cwd": self._cwd, "mode": "craft"})["result"]["sessionId"]
        self._sid = sid
        return sid

    def close_session(self, sid: str = "") -> dict:
        """关掉一个会话的运行时（记录保留——之后可 resume / forget）。

        关掉当前会话后 self._sid 保留不动——记录还在，它就是 /resume 的
        靶子；之后的 send 会拿到 sidecar 的诚实报错（closed ≠ 消失）。
        """
        target = sid or self._sid
        with self._rpc_lock:
            return self._client.call("session/close",
                  {"sessionId": target})["result"]

    def resume_session(self, sid: str) -> dict:
        """复活一个 closed 会话（generation+1 的新运行时，历史接着用）。"""
        with self._rpc_lock:
            result = self._client.call("session/resume",
                  {"sessionId": sid})["result"]
        if "error" not in result:
            self._sid = sid
        return result

    def forget_session(self, sid: str = "") -> dict:
        """真删一个会话记录；live 的必须先 close（sidecar 会拒绝）。"""
        target = sid or self._sid
        with self._rpc_lock:
            return self._client.call("session/forget",
                  {"sessionId": target})["result"]

    def new_session(self) -> str:
        """另开一个新会话并切换为当前——旧的只 close（记录保留），不 forget。

        与 clear()（close→forget→create 全清）的区别：新建是"开个新的，
        旧的留档"；旧记录还要不要，留给用户用 /forget 决定。
        """

        with self._rpc_lock:
            if self._sid:
                self._client.call("session/close", {"sessionId": self._sid})
            sid = self._client.call("session/create",
                {"cwd": self._cwd, "mode": "craft"})["result"]["sessionId"]
        self._sid = sid
        return sid

    # ── 查询 ────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._sid or ""

    def is_alive(self) -> bool:
        """子进程还活着吗（网页刷新后 _ensure_shell 靠它决定重建）。"""

        return self._proc is not None and self._proc.is_alive()
