"""s05 桌面壳：IPC 协议 + ElectronMain(主进程) + PreloadBridge(渲染桥)。

📚 这一课在做什么
  用 Python 的 multiprocessing 模拟 Electron 的"三进程隔离"：
    ┌────────────┐     两条队列      ┌─────────────┐
    │ Renderer   │◄─── IPC ────►     │ Main        │
    │ (终端 UI)  │    (Queue)        │ (agent+闸门) │
    └────────────┘                   └─────────────┘
          ▲                                ▲
          └── 只能走 PreloadBridge 这个受控 API

  机制三件套（写之前必读，代码注释里会反复引用）：
  1. 收信分派环（renderer 侧）：send_message 阻塞等"结果"的途中，队列里
     会混入 approval/request(要审批) 和 event(直播)。拿到非答案消息必须
     就地处理(弹审批/打印)再 continue，绝不把它当结果返回。
  2. 回程票（main 侧 approve）：approval/request 带 request_id(一张票)，
     approval/response 必须亮同一张票 approver 才认账——agent 一轮可能
     连发多个审批，凭票兑付才不会拿错。
  3. Main 是单线程：approver 的 recv() 是"顺序嵌套"接管（主 while 正
     卡在 route 里跑 run_agent），不是并发抢队列，这个边界要写进注释。
"""

import uuid
from typing import Any, Callable, Optional

from src.harness.agent import run_agent
from src.harness.permissions import Approver, AuditTrail, GovernedToolRunner

# ----------------------------------------------------------------------
# IPC 消息协议（type 是消息的第一字段，方向以"谁发出"命名）
# ----------------------------------------------------------------------

# renderer → main：这些 type 会被 main 的 route 分派；None 是关停哨兵。
INCOMING_RENDERER: frozenset[str] = frozenset({
    "ping",
    "session/list",
    "agent/message",      # data = 用户任务文本
    "approval/response",  # data = {request_id, approved}（回程票）
})

# main → renderer：这些 type 会被 renderer 的收信分派环就地消费。
OUTGOING_MAIN: frozenset[str] = frozenset({
    "pong",               # data = "main alive"
    "result",             # data = 终结回复（最终回答 / 会话列表）
    "approval/request",   # data = {request_id, rule_id, reason}
    "event",              # data = {name, ...}（on_event 直播）
})


class PreloadBridgeClosed(Exception):
    """renderer 收到 None 哨兵：main 已关停，整个渲染层退出。

    它是"全局收尾"信号，和"这一条不是我要的结果"（continue 继续等）
    是两回事——分派环看到 None 不是跳过，是抬走。
    """


# ----------------------------------------------------------------------
# ElectronMain：主进程的路由与治理（可注入 send/recv 回调，可单测）
# ----------------------------------------------------------------------

class ElectronMain:
    """主进程大脑：收 IPC → 路由 → 跑 agent（过权限闸门）→ 回结果。

    全部依赖构造器注入（和 GovernedToolRunner 同一哲学，不 import 具体
    装配层）：
      model    —— Model 协议：FakeModel / ScriptedModel / RealModel
      registry —— ToolRegistry：run_agent 校验参数、闸门放行后执行工具
      policy   —— PermissionPolicy：toolbox.build_policy() 产出
      send     —— Callable[[dict], None]：把消息投递给 renderer（main→renderer）
      recv     —— Callable[[], dict | None]：从 renderer 取一条（renderer→main）
      on_event —— 可选：跑 agent 时广播事件（练习 15 的钩子透传）
      history  —— 可选：会话记忆起步（练习 10：Harness 无状态，记忆归应用层）

    多重审批不串账的关键（回程票）见 _make_approver。
    """

    def __init__(
        self,
        model: Any,
        registry: Any,
        policy: Any,
        send: Callable[[dict], None],
        recv: Callable[[], Optional[dict]],
        on_event: Optional[Callable[[str, dict], None]] = None,
        history: Optional[list[dict]] = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._send = send
        self._recv = recv
        self._on_event = on_event
        self._history = history
        # 会话簿：任务文本 -> 最近回复（session/list 的返回值）。
        self._sessions: dict[str, str] = {}
        # 直接复用 s04 的闸门；approver 是"双队列回程票"闭包（见 _make_approver）。
        self._runner = GovernedToolRunner(
            policy=policy,
            approver=self._make_approver(),
            registry=registry,
            audit=AuditTrail(),
        )

    def route(self, msg: dict) -> Optional[dict]:
        """主路由：按 type 分派，返回"对本次请求的终结回复"dict 或 None。

        TODO 1（你来填）：
          - "ping"        → {"type": "pong", "data": "main alive"}
          - "session/list"→ {"type": "result", "data": list(self._sessions.keys())}
          - "agent/message"→ 交给自己写的 _handle_agent_message(msg["data"])
          - "approval/response" → 返回 None（正常不会到主路由——
            它由 approver 的回程票循环直接消费，防御性忽略即可）
          - 其他任何 type → {"type": "result", "data": f"未知消息类型 {type!r}"}
            不炸、给可调试文案，和 default.deny 的 fail-closed 同理。
        """
        match msg["type"]:
            case "ping": return {"type": "pong", "data": "main alive"}
            case "session/list": return {"type": "result", "data": list(self._sessions.keys())}
            case "agent/message": return self._handle_agent_message(msg["data"])
            case "approval/response": return None
            case _type: return {"type": "result", "data": f"未知消息类型 {msg['type']!r}"}


    def _handle_agent_message(self, text: str) -> dict:
        """跑一轮 run_agent，延续会话记忆，返回 {"type":"result","data":...}。

        TODO 2（你来填）：
          result = run_agent(
              text, model=self._model, registry=self._registry,
              history=self._history, on_event=self._on_event,
              runner=self._runner,
          )
          然后：
          - self._history = result.messages      # 会话延续（练习 10）
          - self._sessions[text] = result.output # 记账，session/list 用
          - 返回 result：status == "completed" 时 data 就是 output；
            否则（failed / max_steps / truncated）给 f"[{status}] {output}"
            一样的"失败是结果不是异常"诚实（练习 16）
        """
        result = run_agent(
            text, model=self._model, registry=self._registry,
            history=self._history, on_event=self._on_event,
            runner=self._runner,
        )
        self._history = result.messages
        self._sessions[text] = result.output
        # "失败是结果不是异常"（练习 16）：non-completed 也如实带上前缀，
        # 别让 failed/max_steps/truncated 看起来像正常完成。
        if result.status != "completed":
            return {"type": "result", "data": f"[{result.status}] {result.output}"}
        return {"type": "result", "data": result.output}


    def _make_approver(self) -> Approver:
        """审批闭包（回程票是核心）：发审批到 renderer，等"同一张票"的回执。

        TODO 3（你来填）：
          def approver(decision) -> bool:
              ticket = str(uuid4())          # 开一张票
              self._send({"type": "approval/request", "data": {
                  "request_id": ticket,
                  "rule_id": decision.rule_id,
                  "reason": decision.reason,
              }})
              while True:                    # 回程票循环
                  msg = self._recv()         # 顺序嵌套接管（见模块 docstring ③）
                  if msg is None:            # main 已关停 → 视为拒绝
                      return False
                  data = msg.get("data") or {}
                  if (msg.get("type") == "approval/response"
                          and data.get("request_id") == ticket):
                      return bool(data.get("approved"))  # 只兑自己这张票
          返回这个 approver（Approver = Callable[[PermissionDecision], bool]）
        """
        ...
        def approver(decision) -> bool:
            ticket = str(uuid.uuid4())
            self._send({"type": "approval/request", "data": {
                "request_id": ticket,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
            }})
            while True:
                msg = self._recv()
                if msg is None:
                    return False
                data = msg.get("data") or {}
                if (msg.get("type") == "approval/response"
                        and data.get("request_id") == ticket):
                    return bool(data.get("approved"))
        return approver


# ----------------------------------------------------------------------
# PreloadBridge：渲染器的唯一通道（contextBridge 的教学版）
# ----------------------------------------------------------------------

def _terminal_prompt(rule_id: str, reason: str) -> bool:
    """默认审批交互：终端弹 y/n（对齐 chat.py 的 cli_approver 心智）。"""

    print(f"  ⚠️ 需要审批 [{rule_id}] {reason}")
    answer = input("     允许这次工具调用吗？(y/n) ").strip().lower()
    return answer == "y"


class PreloadBridge:
    """渲染器的受控 API——它只有这一条路接触 main，碰不到文件系统/LLM。

    物理上它就是两个队列的薄封装：send_queue 是"发往 main"，recv_queue
    是"从 main 收"。真实 Electron 里 preload 脚本用 contextBridge 暴露
    安全的 API 给页面；这里我们用这个类暴露安全方法给终端 UI。
    """

    def __init__(
        self,
        send_queue: Any,
        recv_queue: Any,
        user_prompt: Optional[Callable[[str, str], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._send = send_queue  # renderer→main
        self._recv = recv_queue  # main→renderer
        # 可注入：测试塞假 y/n；默认走 _terminal_prompt。
        self._user_prompt = user_prompt or _terminal_prompt
        self._on_event = on_event

    def _dispatch(self, timeout: float | None = None) -> Any:
        """收信分派环（本文件灵魂，务必先读模块 docstring 的机制①）。

        timeout 传给队列 get：ping 用它做"5 秒假死检测"；send_message 不传
        （真实 agent 耗时无上限，教学取舍）。

        注意：把"这条消息怎么消费"全部塞在这个环里——
          - None          → raise PreloadBridgeClosed()（关停哨兵，抬走）
          - "result" / "pong" → return msg["data"]（这才是我这次请求的答案）
          - "approval/request" → data = msg["data"]；
                ok = self._user_prompt(data["rule_id"], data["reason"])
                再把回执发回：self._send.put({"type": "approval/response",
                    "data": {"request_id": data["request_id"], "approved": ok}})
                continue（继续等 result，别停）
          - "event"       → 有 on_event 就 on_event(msg["data"])；continue
        未知 type：掉到循环底部再取下一条（不把不认识的消息当结果）。
        """
        while True:
            msg = self._recv.get(timeout=timeout)
            if msg is None:
                raise PreloadBridgeClosed()
            if msg["type"] in ("result", "pong"):
                return msg["data"]
            if msg["type"] == "approval/request":
                data = msg["data"]
                ok = self._user_prompt(data["rule_id"], data["reason"])
                self._send.put({"type": "approval/response",
                    "data": {"request_id": data["request_id"], "approved": ok}})
                continue
            if msg["type"] == "event" and self._on_event:
                self._on_event(msg["data"])
                continue


    def ping(self) -> str:
        """健康检查：5 秒超时，main 假死要报"无响应"而不是卡死。"""

        self._send.put({"type": "ping"})
        return self._dispatch(timeout=5)

    def send_message(self, text: str) -> str:
        """发一条 agent 任务并等最终回答（阻塞；途中审批/直播被分派环消费）。

        TODO 5（你来填）：put 一条 agent/message，再 return self._dispatch()
        """
        self._send.put({"type": "agent/message", "data": text})
        return self._dispatch()

    def list_sessions(self) -> list:
        """列会话（main 的 session/list）。TODO 6（你来填），同 send_message。"""
        self._send.put({"type": "session/list"})
        return self._dispatch()