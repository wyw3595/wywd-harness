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
import socket
import threading
from typing import Any, Callable, Optional

from scripts.electron_shell import choose_model
from scripts.toolbox import build_policy, build_registry, with_system
from src.harness.sidecar import MainProcessClient, RPCConnection, SidecarServer


def _sidecar_process(sock: socket.socket) -> None:
    """Sidecar 子进程入口（spawn 守则①：必须模块级函数）。装配全在子进程内。"""

    server = SidecarServer(
        model=choose_model(),
        registry=build_registry(),
        policy=build_policy(),
        history_seed=lambda: with_system([]),   # system 常驻起步
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
        """起 sidecar 子进程并建会话；返回 ping 结果。失败时不留半个壳。

        TODO 1（你来填）：
          1. 防重复启动：if self._proc is not None: raise RuntimeError(...)
          2. srv, cli = socket.socketpair()
             self._proc = self._process_factory(target=_sidecar_process,
                                                args=(srv,))
          3. try:
                 self._proc.start(); srv.close()   # 父进程不再持有这端
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
                 # 启动失败：清半壳（terminate+join+close）再抛
                 if self._proc.is_alive():
                     self._proc.terminate(); self._proc.join()
                 if self._client is not None:
                     self._client.close()
                 self._proc = None; self._client = None
                 raise
        """
        ...

    def stop(self) -> None:
        """干净收尾（幂等）：shutdown → close(EOF) → join(5s) → terminate。

        TODO 2（你来填）：
          1. if self._proc is None: return        # 幂等
          2. proc, client = self._proc, self._client
          3. if client is not None:
                 if self._rpc_lock.acquire(blocking=False):   # 空闲才礼貌
                     try:
                         try: client.call("sidecar/shutdown")
                         except Exception: pass
                     finally:
                         self._rpc_lock.release()
                 client.close()                   # EOF 兜底（必须，坑 3）
          4. finally: proc.join(timeout=5)
               if proc.is_alive(): proc.terminate(); proc.join()
               self._proc = None; self._client = None; self._sid = None
        """
        ...

    # ── RPC 包装（全部过 _rpc_lock）─────────────────────────

    def send(self, message: str) -> dict:
        """把一句话交给 agent；返回 agent/send 的 result（可能含 "error"）。

        TODO 3（你来填）：with self._rpc_lock:
            return self._client.call("agent/send",
                {"sessionId": self._sid, "message": message})["result"]
        """
        ...

    def status(self) -> dict:
        """TODO 4：call("sidecar/status")["result"]，锁内。"""

        ...

    def sessions(self) -> dict:
        """TODO 5：call("session/list")["result"]，锁内。"""

        ...

    def logs(self) -> str:
        """TODO 6：call("sidecar/logs")["result"].get("logs", "")，锁内。"""

        ...

    def tools(self) -> list:
        """TODO 7：call("tool/list")["result"].get("tools", [])，锁内。"""

        ...

    def clear(self) -> str:
        """清记忆：销毁旧会话再建新会话（/clear 的落点）。返回新 sid。

        TODO 8（你来填）：
          with self._rpc_lock:
              self._client.call("session/destroy", {"sessionId": self._sid})
              sid = self._client.call("session/create",
                  {"cwd": self._cwd, "mode": "craft"})["result"]["sessionId"]
          self._sid = sid
          return sid
        """
        ...

    # ── 查询 ────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._sid or ""

    def is_alive(self) -> bool:
        """子进程还活着吗（网页刷新后 _ensure_shell 靠它决定重建）。"""

        return self._proc is not None and self._proc.is_alive()
