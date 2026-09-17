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
import zipfile
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
    DEFAULT_WORKSPACE,
    build_history_seed,
    build_model_router,
    build_policy,
    build_policy_for,
    build_registry,
    build_registry_for,
    resolve_max_agent_steps,
)
from src.harness.jsonl_store import JsonlSessionStore
from src.harness.sidecar import MainProcessClient, RPCConnection, SidecarServer
from src.harness.workspace import Workspace


def _workspace_runtime(params: dict) -> tuple:
    """【装配层】按请求现做一套工作区运行件——喂给 sidecar 的 workspace/set。

    kind=dir：直接引用本机目录（不复制，改的就是原目录）；
    kind=zip：解压上传（zip 的三道安检在 Workspace.from_zip 里）。

    注意这里只处理**工作区规格**，不处理复位："default" 由 sidecar 自己
    还原启动态（见 SidecarServer._handle_workspace_set），不会走到这儿——
    复位不需要坐标，也就不需要工厂。直调本函数传 "default" 会按未知类型拒绝。

    失败一律翻成 ValueError——这是 RPC 层的唯一错误通道（与 session
    四操作的约定一致）。所以 zipfile 的库异常、路径不存在的 OSError
    都在这里就地翻译成人话，harness 那边只认 ValueError/OSError。

    返回 (registry, policy, info)：info 是给 UI 看的不透明字典——
    harness 只原样转交，怎么用是前端的事（它不认识 Workspace）。
    """

    kind = str(params.get("kind") or "dir")
    raw = str(params.get("path") or "").strip()
    if not raw:
        raise ValueError("没给路径——请指定一个本机目录或 zip 文件")

    if kind == "zip":
        try:
            workspace = Workspace.from_zip(Path(raw))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"不是有效的 zip（或已损坏）：{raw}（{exc}）") from exc
        except OSError as exc:
            raise ValueError(f"压缩包读不了：{raw}（{exc}）") from exc
    elif kind == "dir":
        try:
            workspace = Workspace.from_existing_dir(Path(raw))
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc
    else:
        raise ValueError(f"不认识的工作区类型 {kind!r}——只支持 dir / zip")

    info = {
        "kind": kind,
        "id": workspace.workspace_id,
        "root": str(workspace.root),
        "source": raw,
    }
    return build_registry_for(workspace), build_policy_for(workspace), info


def _idle_reap_seconds() -> float:
    """空闲回收阈值（秒）。`WYWD_IDLE_REAP_SECONDS` 没设 / 非正数 = 不回收。

    **为什么默认关**：回收会把"任何时候发消息都能用"变成"可能要先复活一次"，
    那是手感变化，该由用户明确打开，而不是替所有人改掉默认。打开之后
    前端点一下会话就自动 resume，用户视角只是第一次慢一点。
    解析失败按关处理并喊一声——静默吞掉错配置比报错更难查。
    """
    raw = (os.environ.get("WYWD_IDLE_REAP_SECONDS") or "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        print(f"[sidecar] WYWD_IDLE_REAP_SECONDS 不是数字（{raw!r}），按不回收处理")
        return 0.0
    return value if value > 0 else 0.0


def _sidecar_process(sock: socket.socket) -> None:
    """Sidecar 子进程入口（spawn 守则①：必须模块级函数）。装配全在子进程内。

    store 用 JSONL 证据文件（s09）：会话活过进程重启（.sessions/，已进
    .gitignore，测试注 tmp）。model 用路由器（s08）：Router 实现 Model
    协议（generate → craft 槽），sidecar/run_agent 零改动。choose_model()
    保留给 electron_shell（s05 直连架构，不走 sidecar——两个入口两套
    装配，都从 toolbox 出）。

    s10：起步历史用工具箱的 build_history_seed()——工具目录 system 打底，
    有工作区记忆时再补一条 system（记忆放第二条，FakeModel 只读第一条的
    老怪癖不受影响）。**空记忆时它和 with_system([]) 返回完全一样**，
    所以 s06.5 的行为零变化。

    s11：runtime_builder=_workspace_runtime——把"换沙箱"的能力交给 sidecar
    （workspace/set 路由）。攒在子进程里做而不是主进程：registry/policy
    活在这一侧，边界换在哪边就地换哪边，别隔着 socket 搬工具闭包。
    initial_workspace 顺手把"默认根是谁"告诉它——复位后 UI 要显示路径，
    registry 认不出路径（见 SidecarServer 构造器注释）。

    s12：idle_timeout 交给 SidecarServer 起后台回收线程（默认关，
    见 _idle_reap_seconds）。

    2026-09-16：max_steps 显式传进去（环境变量 WYWD_MAX_STEPS，默认 30）。
    以前不传，于是 run_agent 的签名默认值 5 就成了线上真实上限——一个需要
    6 轮的任务必然以 max_steps 收场。同时 history_seed 带上步数预算，
    让上限成为"看得见的预算"（模型据此规划、接近时收尾），而不是撞墙才知道。
    """
    max_steps = resolve_max_agent_steps()
    server = SidecarServer(
        model=build_model_router(),
        registry=build_registry(),
        policy=build_policy(),
        # s10：工具目录 + 工作区记忆（空记忆时与旧行为一致）。
        history_seed=lambda: build_history_seed(max_steps=max_steps),
        store=JsonlSessionStore(root=Path(_PROJECT_ROOT) / ".sessions"),
        runtime_builder=_workspace_runtime,   # s11：上传目录 / 换沙箱
        initial_workspace={"kind": "default", "root": str(DEFAULT_WORKSPACE.root)},
        idle_timeout=_idle_reap_seconds(),    # s12：空闲回收（默认关）
        max_steps=max_steps,                  # 单轮步数上限（默认 30）
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

    def start(self, create_session: bool = True) -> dict:
        """起 sidecar 子进程；create_session=True 时顺手建首个会话。

        返回 ping 结果。失败时不留半个壳（子进程 + socket 一起收）。

        **为什么要 create_session=False（懒建）**：网页入口每次重启都会
        spawn 一个新 sidecar。如果启动就建会话，前端恢复的却是 localStorage
        里记着的**更早那个** sid，这个启动会话就永远没人打开——于是每重启
        一次就在 .sessions/ 里多一条只有 system 消息的「未命名会话」，几轮
        下来侧边栏全是这种残留（现场：8 条会话有 6 条是启动残留）。
        web_app 传 False，把"建会话"推迟到第一次发消息 / 点 ＋；终端与演示
        入口保持默认 True（启动即有 sid 可用）。
        """
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
                # 走 _create_session_remote 而不是手写 ["result"]["sessionId"]：
                # 它已经把 error 响应翻译成 RuntimeError 人话，和 new_session
                # 同一个出口（原来这里是唯一手写取字段的地方）。
                self._sid = self._create_session_remote() if create_session else None
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

    @staticmethod
    def _result(resp: dict) -> dict:
        """把一次 RPC 响应翻译成 result dict：成功取 result，error 翻译成人话。

        sidecar 内部异常走 handle_connection 兜底，返回结构是
        {"error": {...}} 而不是 {"result": ...}——直接 `["result"]` 会
        KeyError 穿透到 HTTP handler，连接被空响应关掉，浏览器只看到
        "Failed to fetch"（前端冒烟现场：forget 撞上删除失败就这样）。

        send_to / messages 各自写过一遍这个分支，四个会话操作漏了；
        现在收成一处，谁新增 RPC 包装都从这里走。
        """

        if "result" in resp:
            return resp["result"]
        error = resp.get("error") or {}
        return {"error": error.get("message", "sidecar 内部错误")}

    def send_to(self, session_id: str, message: str) -> dict:
        """把一句话交给**指定**会话的 agent（显式 sid）。

        web_app（C 方案）用：单壳单 sidecar，多 tab 各看各的会话，
        发消息必须带 sid，不能依赖壳的"当前会话指针"。返回
        agent/send 的 result（可能含 "error"）。RPC 层错误由
        _result 翻译成人话，不抛洞。
        """

        with self._rpc_lock:
            return self._result(self._client.call("agent/send",
                {"sessionId": session_id, "message": message}))

    def send(self, message: str) -> dict:
        """发给自己当前会话（懒建：还没有会话就现开一个）。

        ensure_session 建会话失败时抛 RuntimeError，但本文件的一贯口径是
        "RPC 层错误翻译成人话、不抛洞"（见 _result），所以这里就地接住转成
        {"error": …}，调用方拿到的形状和 send_to 一致。
        """

        try:
            sid = self.ensure_session()
        except RuntimeError as exc:
            return {"error": str(exc)}
        return self.send_to(sid, message)

    def status(self) -> dict:
        """sidecar 状态：会话数 / RingBuffer 用量 / handler 数。"""

        with self._rpc_lock:
            return self._result(self._client.call("sidecar/status"))

    def sessions(self) -> dict:
        """sidecar 里的会话列表。"""

        with self._rpc_lock:
            return self._result(self._client.call("session/list"))

    def messages(self, sid: str = "") -> dict:
        """读一个会话的完整消息历史（历史重放/审计用；closed 也能读）。

        sid 为空 = 当前会话；id 不存在时 sidecar 回 {"error": 人话}。
        """

        target = sid or self._sid or ""
        with self._rpc_lock:
            return self._result(self._client.call("session/messages",
                {"sessionId": target}))

    def logs(self) -> str:
        """sidecar 最近日志（走 RPC——真多进程下主进程读不到子进程内存）。

        签名是 str，装不下 {"error": …}，所以翻译成一行可读的说明返回：
        比回空串诚实（空串会让人以为 sidecar 从没输出过东西）。
        """

        with self._rpc_lock:
            data = self._result(self._client.call("sidecar/logs"))
        if "error" in data:
            return f"[取不到 sidecar 日志] {data['error']}"
        return data.get("logs", "")

    def tools(self) -> list:
        """sidecar 持有的工具清单（网页欢迎语可渲染，替代硬编码文案）。"""

        with self._rpc_lock:
            data = self._result(self._client.call("tool/list"))
        # 取不到就给空清单：工具清单是展示性的，不该因为一次 RPC 抖动炸掉页面
        return data.get("tools", []) if "error" not in data else []

    # ── 工作区（s11：上传目录）──────────────────────────────

    def set_workspace(self, kind: str, path: str) -> dict:
        """让 sidecar 换工作区：kind="dir" 引用本机目录 / "zip" 解压上传 /
        "default" 复位回启动态（此时 path 忽略，可传空串）。

        返回 {"status": "ok", "workspace": {...}} 或 {"error": 人话}。
        换的是**边界**（registry + policy + runner 三层连带，sidecar 那边
        一次做完），已建会话的历史不动——所以调用方（UI）该提示用户
        "建议新建会话再聊"：旧历史里的路径在新沙箱里可能不存在。
        """

        with self._rpc_lock:
            resp = self._client.call("workspace/set",
                                     {"kind": kind, "path": path})
            if "result" in resp:
                return resp["result"]
            error = resp.get("error") or {}
            return {"error": error.get("message", "sidecar 内部错误")}

    def workspace(self) -> dict:
        """读当前工作区（UI 渲染"模型在哪个目录里干活"）。"""

        with self._rpc_lock:
            return self._client.call("workspace/get")["result"]

    def clear(self) -> str:
        """清记忆（s07-b 升级）：完整编舞 close → forget → create。

        四操作里三个在同一命令里跑一遍：close（释放运行时）→
        forget（删记录）→ create（新身份）。
        """
        with self._rpc_lock:
            if self._sid:
                self._client.call("session/close", {"sessionId": self._sid})
                self._client.call("session/forget", {"sessionId": self._sid})
            sid = self._create_session_remote()
        self._sid = sid
        return sid

    def close_session(self, sid: str = "") -> dict:
        """关掉一个会话的运行时（记录保留——之后可 resume / forget）。

        关掉当前会话后 self._sid 保留不动——记录还在，它就是 /resume 的
        靶子；之后的 send 会拿到 sidecar 的诚实报错（closed ≠ 消失）。
        """
        target = sid or self._sid
        with self._rpc_lock:
            return self._result(self._client.call("session/close",
                  {"sessionId": target}))

    def resume_session(self, sid: str) -> dict:
        """复活一个 closed 会话（generation+1 的新运行时，历史接着用）。"""
        with self._rpc_lock:
            result = self._result(self._client.call("session/resume",
                  {"sessionId": sid}))
        if "error" not in result:
            self._sid = sid
        return result

    def forget_session(self, sid: str = "") -> dict:
        """真删一个会话记录；live 的必须先 close（sidecar 会拒绝）。"""
        target = sid or self._sid
        with self._rpc_lock:
            return self._result(self._client.call("session/forget",
                  {"sessionId": target}))

    def _create_session_remote(self) -> str:
        """让 sidecar 建个会话并取回 id。

        失败抛 RuntimeError（带上 sidecar 的人话）而不是让 KeyError 穿透：
        `["result"]["sessionId"]` 在 error 响应上会炸穿 HTTP handler，
        浏览器只看到 "Failed to fetch"。调用方（WebApp.action）负责把它
        翻译成 {"ok": False, "detail": …} 摆到用户面前。
        """

        data = self._result(self._client.call("session/create",
            {"cwd": self._cwd, "mode": "craft"}))
        if "error" in data or not data.get("sessionId"):
            raise RuntimeError(data.get("error") or "sidecar 没给出新会话 id")
        return data["sessionId"]

    def ensure_session(self) -> str:
        """懒建：有当前会话就返回它，没有就现建一个并接管为当前。

        create_session=False 起的壳（web_app）第一次真正需要会话时调它——
        把"建会话"这个动作从启动时刻推迟到"要用"的时刻。幂等：已经有了就
        是纯读取，不碰锁。
        """

        if self._sid:
            return self._sid
        with self._rpc_lock:
            sid = self._create_session_remote()
        self._sid = sid
        return sid

    def new_session(self) -> str:
        """另开一个新会话并切换为当前——旧的只 close（记录保留），不 forget。

        与 clear()（close→forget→create 全清）的区别：新建是"开个新的，
        旧的留档"；旧记录还要不要，留给用户用 /forget 决定。
        与 ensure_session() 的区别：这个**永远**开新的，那个只在没有时才开。
        """

        with self._rpc_lock:
            if self._sid:
                self._client.call("session/close", {"sessionId": self._sid})
            sid = self._create_session_remote()
        self._sid = sid
        return sid

    # ── 查询 ────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._sid or ""

    def is_alive(self) -> bool:
        """子进程还活着吗（网页刷新后 _ensure_shell 靠它决定重建）。"""

        return self._proc is not None and self._proc.is_alive()
