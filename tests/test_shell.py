"""s06.5 SidecarShell 的编排单测：不真起子进程，注入假 client/假 process。

与 test_sidecar.py 同哲学：进程边界只到"编排逻辑"为止——start 的顺序、
stop 的幂等与收尾顺序、clear 的 destroy→create、并发串行化都是可测的
核心；真 spawn 留给 scripts 冒烟（Windows spawn 重跑主模块，进单测是
自找麻烦）。scripts/shell.py 是 scripts 里唯一配单测的文件（可测编排件）。
"""

import threading
import time
import unittest

from scripts.shell import SidecarShell


class FakeClient:
    """记录调用 + 罐装响应；connect 立刻关掉 cli（防测试 fd 泄漏）。"""

    def __init__(self, user_prompt=None, on_event=None):
        self.user_prompt = user_prompt
        self.on_event = on_event
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False
        self._id = 0

    def connect(self, sock) -> None:
        sock.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.calls.append((method, params))
        if method == "sidecar/ping":
            result = {"status": "ok"}
        elif method == "session/create":
            result = {"sessionId": f"sess_{self._id}"}
        elif method == "session/destroy":
            result = {"status": "ok"}
        else:
            result = {}
        return {"jsonrpc": "2.0", "result": result, "id": self._id}

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """记录 start/join/terminate；is_alive 可控。"""

    def __init__(self, *, alive: bool = True) -> None:
        self.target = None
        self.args = None
        self.started = 0
        self.joined = 0
        self.terminated = 0
        self._alive = alive

    def start(self) -> None:
        self.started += 1

    def join(self, timeout=None) -> None:
        self.joined += 1

    def terminate(self) -> None:
        self.terminated += 1
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


class FakeProcessFactory:
    """制造假进程并记下 target/args（默认 alive 可控）。"""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.proc: FakeProcess | None = None

    def __call__(self, *, target=None, args=None) -> FakeProcess:
        self.proc = FakeProcess(alive=self.alive)
        self.proc.target = target
        self.proc.args = args
        return self.proc


class SidecarShellTests(unittest.TestCase):
    """壳的编排：start 顺序 / stop 幂等 / clear / 并发锁 / 容错。"""

    def _make(self, *, alive: bool = True) -> tuple[SidecarShell, FakeClient, FakeProcessFactory]:
        client = FakeClient()
        factory = FakeProcessFactory(alive=alive)
        shell = SidecarShell(
            _client_factory=lambda **kw: client,
            _process_factory=factory,
        )
        return shell, client, factory

    def test_start_pings_then_creates_session(self) -> None:
        shell, client, factory = self._make()
        pong = shell.start()
        self.assertEqual(pong, {"status": "ok"})
        # 先 ping 验活，再建会话
        self.assertEqual(
            [m for m, _ in client.calls],
            ["sidecar/ping", "session/create"],
        )
        self.assertEqual(shell.session_id, "sess_2")
        # 子进程真的被 start，target 是装配函数
        self.assertEqual(factory.proc.started, 1)
        self.assertEqual(factory.proc.target.__name__, "_sidecar_process")

    def test_start_raises_when_already_started(self) -> None:
        shell, _, _ = self._make()
        shell.start()
        with self.assertRaises(RuntimeError):
            shell.start()

    def test_send_delegates_agent_send(self) -> None:
        shell, client, _ = self._make()
        shell.start()
        shell.send("你好")
        self.assertEqual(client.calls[-1], ("agent/send", {
            "sessionId": "sess_2", "message": "你好",
        }))

    def test_stop_idempotent_and_ordered(self) -> None:
        shell, client, factory = self._make(alive=False)  # 子进程已正常退出
        shell.start()
        shell.stop()
        shell.stop()  # 幂等：第二次直接返回
        self.assertEqual(factory.proc.joined, 1)
        self.assertEqual(factory.proc.terminated, 0)  # 进程已退，不 terminate
        # 礼貌协议执行过：shutdown 被调 + close() 被调（close 不记 calls，看 closed 标志）
        self.assertIn("sidecar/shutdown", [m for m, _ in client.calls])
        self.assertTrue(client.closed)
        self.assertEqual(shell.session_id, "")  # stop 后 sid 清空

    def test_stop_terminates_when_still_alive(self) -> None:
        shell, _, factory = self._make(alive=True)  # 子进程假装还活着
        shell.start()
        shell.stop()
        self.assertEqual(factory.proc.terminated, 1)

    def test_stop_tolerates_dead_sidecar(self) -> None:
        """client.call 抛 ConnectionClosed 时 stop 仍收尾不抛。"""

        class BoomClient(FakeClient):
            def call(self, method, params=None):
                if method == "sidecar/shutdown":
                    raise RuntimeError("connection closed")
                return super().call(method, params)

        client = BoomClient()
        factory = FakeProcessFactory(alive=False)  # sidecar 已死场景
        shell = SidecarShell(
            _client_factory=lambda **kw: client,
            _process_factory=factory,
        )
        shell.start()
        shell.stop()  # 不抛，仍收尾
        self.assertEqual(factory.proc.joined, 1)

    def test_stop_skips_shutdown_when_call_in_flight(self) -> None:
        """有在途 call（锁被占）时：跳过礼貌 shutdown，close() EOF 兜底。"""

        shell, client, factory = self._make()
        shell.start()
        # 测试线程占住锁，模拟"正在跑 agent/send"
        shell._rpc_lock.acquire()
        try:
            shell.stop()
        finally:
            shell._rpc_lock.release()
        methods = [m for m, _ in client.calls]
        self.assertNotIn("sidecar/shutdown", methods)  # 跳过礼貌协议
        self.assertTrue(client.closed)                 # EOF 兜底
        self.assertEqual(factory.proc.terminated, 1)   # 进程已被收掉

    def test_clear_destroys_then_creates(self) -> None:
        shell, client, _ = self._make()
        shell.start()
        new_sid = shell.clear()
        self.assertEqual(
            client.calls[-2:],
            [("session/destroy", {"sessionId": "sess_2"}),
             ("session/create", {"cwd": ".", "mode": "craft"})],
        )
        self.assertEqual(new_sid, "sess_4")
        self.assertEqual(shell.session_id, "sess_4")

    def test_send_serialized_under_concurrency(self) -> None:
        """双线程 send：锁保证同一时刻最多一个 call 在途（坑 2 的锁）。"""

        shell, client, _ = self._make()
        shell.start()
        client.calls.clear()

        depth = {"max": 0, "now": 0}
        depth_lock = threading.Lock()

        orig_call = client.call

        def counting_call(method, params=None):
            with depth_lock:
                depth["now"] += 1
                depth["max"] = max(depth["max"], depth["now"])
            time.sleep(0.01)  # 放大并发窗口
            result = orig_call(method, params)
            with depth_lock:
                depth["now"] -= 1
            return result

        client.call = counting_call

        def worker() -> None:
            for _ in range(5):
                shell.send("x")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(depth["max"], 1)  # 锁生效：从不并发


if __name__ == "__main__":
    unittest.main()
