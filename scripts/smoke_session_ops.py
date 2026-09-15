"""冒烟：会话行操作的全链路（真 HTTP + 真 sidecar 子进程）。

为什么单独有这一条：侧边栏三个按钮（关闭/复活/删除）此前**只有
"翻译层单测"**（FakeShell 记下参数）和"sidecar 单测"（RPC 直调），
中间那段"真 HTTP → 真壳 → 真 sidecar"没人走过。于是真实发生的 bug
溜了过去：前端把 sid 读成了空串，壳侧 `target = sid or self._sid` 又把
空串静默当成"当前会话"——点某一行的"删除"，删掉的是当前选中的那个，
删完还弹成功。

覆盖：
  1. 建会话 → 关闭 → 用**显式 sid** 删除：真删掉了那一个
  2. 空 sid / 空白 sid：被 web 层硬拒，且**当前会话没被动**
  3. 删 live 会话：报错是人话（带 id + 说清"先关闭"）
  4. 删不存在的 id：诚实报错，不静默成功

运行：python scripts/smoke_session_ops.py
（离线：不设密钥时 choose_model 给 FakeModel，不起真模型）
"""

import json
import multiprocessing
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.web_app import HOST, WebApp, _Handler, _Server  # noqa: E402

# 换端口：不打扰正在跑的 8765（那是你在用的那个）
PORT = 8792
BASE_URL = f"http://{HOST}:{PORT}"


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def session_ids() -> list[str]:
    return [s["id"] for s in get_json("/api/sessions")["sessions"]]


def run_checks(mine: list[str]) -> None:
    try:
        # ── 1. 建一个 → 关掉 → 用显式 sid 删掉 ────────────────
        created = post_json("/api/sessions", {"action": "create"})
        assert created.get("ok"), created
        target = created["sessionId"]
        mine.append(target)
        print("[1] 新建 →", target)

        closed = post_json("/api/sessions", {"action": "close", "sid": target})
        assert closed.get("ok"), closed
        print("[1] 关闭 →", closed["detail"])

        gone = post_json("/api/sessions", {"action": "forget", "sid": target})
        assert gone.get("ok"), gone
        assert target not in session_ids(), f"{target} 还在清单里"
        mine.remove(target)
        print("[1] 删除成功且真的消失 ✓", gone["detail"])

        # ── 2. 空 sid：硬拒，且当前会话没被动 ─────────────────
        # 这正是用户报的那个 bug 的入口：前端读空 sid → 壳静默退回"当前会话"
        anchor = post_json("/api/sessions", {"action": "create"})["sessionId"]
        mine.append(anchor)
        before = session_ids()

        for label, payload in [
            ("空 sid", {"action": "forget", "sid": ""}),
            ("空白 sid", {"action": "forget", "sid": "   "}),
            ("空 sid 关闭", {"action": "close", "sid": ""}),
            ("空 sid 复活", {"action": "resume", "sid": ""}),
        ]:
            res = post_json("/api/sessions", payload)
            assert not res.get("ok"), (label, res)
            assert anchor in session_ids(), f"{label} 把当前会话动了！"
            print(f"[2] {label} → 被拒 ✓ {res['detail']}")

        assert set(session_ids()) == set(before), "被拒的操作动了会话集合"
        print("[2] 被拒的操作零副作用（会话集合没变）✓")

        # ── 3. 删 live 会话：人话报错 ─────────────────────────
        live = post_json("/api/sessions", {"action": "forget", "sid": anchor})
        assert not live.get("ok"), live
        assert anchor in live["detail"], live     # 说清是哪一个
        assert "先关闭" in live["detail"], live    # 说清下一步
        print("[3] 删 live 会话 →", live["detail"])

        # ── 4. 删不存在的 id：诚实报错 ────────────────────────
        forget_closed = post_json("/api/sessions",
                                  {"action": "close", "sid": anchor})
        assert forget_closed.get("ok"), forget_closed
        missing = post_json("/api/sessions",
                            {"action": "forget", "sid": "sess_nope_zzz"})
        assert not missing.get("ok"), missing
        assert anchor in session_ids(), "删不存在的 id 不该影响别的会话"
        print("[4] 删不存在的 id →", missing["detail"])

        print("\n冒烟通过：会话行操作全链路（HTTP → 壳 → sidecar）行为正确。")
    finally:
        # 收尾：把本脚本造的会话清掉（先 close 再 forget，这是硬规矩）
        for sid in list(mine):
            if sid not in session_ids():
                continue
            post_json("/api/sessions", {"action": "close", "sid": sid})
            post_json("/api/sessions", {"action": "forget", "sid": sid})
        leftover = [s for s in mine if s in session_ids()]
        print("[收尾] 清掉本脚本造的会话；残留:", leftover or "无")


def main() -> None:
    multiprocessing.freeze_support()

    app = WebApp()
    _Handler.app = app
    server = _Server((HOST, PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        pong = app.start()
        print("[0] sidecar/ping →", pong.get("status"))
        startup_sid = app.shell.session_id
        print("[0] 本进程自己的当前会话 →", startup_sid)
        thread.start()

        # 服务启动时它自建了一个会话，也算"本脚本造的"，收尾一起清掉
        run_checks([startup_sid])
    finally:
        server.shutdown()
        server.server_close()
        app.stop()
        print("[收尾] 服务与 sidecar 已退出")


if __name__ == "__main__":
    main()
