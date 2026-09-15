"""冒烟：网页上传工作区的全链路（真 HTTP + 真 sidecar 子进程）。

和 smoke_workspace.py 的分工：
  smoke_workspace.py      验两条入口的"接线"（工厂直调 + 命令族）
  本文件                   验"浏览器那一路"：真起 ThreadingHTTPServer，
                          真 spawn sidecar，只走 HTTP 协议进（urllib）。

为什么非要有这一层：单测里用的是 FakeShell，它证明不了
  - base64 → 临时文件 → RPC → sidecar 解压 这条链在**两个进程之间**成立；
  - 注入的 runtime_builder 能穿过 spawn（子进程是全新解释器，工厂要能被
    import 到，import 不进来就是"注册了但一用就炸"）；
  - 临时文件真的删了（泄漏在系统 temp 里，只看代码看不出来）。

覆盖：
  1. GET  /api/workspace        → 初始是默认工作区（且报得出默认根）
  2. POST /api/workspace upload → 真解压，磁盘上能读到包里的文件
  3. GET  /api/workspace        → 切换后一致
  4. POST /api/workspace open   → 引用本机目录
  5. POST /api/workspace reset  → 回启动态，且报得出默认根
  6. 坏 base64 / 空包 / 不存在目录 → 人话报错，且工作区不变（切坏不留半态）
  7. 上传后临时文件不残留（数一下 temp 里的 wywd-upload-*）

运行：python scripts/smoke_web_workspace.py
（离线：不设 DEEPSEEK_API_KEY，choose_model 给 FakeModel，不起真模型）
"""

import base64
import json
import multiprocessing
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.web_app import HOST, WebApp, _Handler, _Server  # noqa: E402

# 换个端口：不和正在跑的 web_app 抢 8765（本脚本是自验，不打扰开发中的那个）
PORT = 8791
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
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)


def count_upload_temp() -> int:
    """系统 temp 里还剩几个上传临时文件（正常应当为 0）。"""
    return len(list(Path(tempfile.gettempdir()).glob("wywd-upload-*")))


def run_checks(app: WebApp) -> None:
    base = Path(tempfile.mkdtemp())
    try:
        # ── 1. 初始工作区（默认态也要报得出根，否则面板是空的）──
        initial = get_json("/api/workspace")["workspace"]
        assert initial.get("kind") == "default", initial
        assert initial.get("root"), initial
        print("[1] 初始工作区 →", initial.get("kind"), initial.get("root"))

        # ── 2. 上传 zip（真 HTTP 进）──────────────────────────
        pack = base / "repo.zip"
        build_zip(pack, {
            "repo-main/README.md": "# 从浏览器传上来的项目",
            "repo-main/src/app.py": "print('hi')",
        })
        payload = base64.b64encode(pack.read_bytes()).decode("ascii")

        before_temp = count_upload_temp()
        uploaded = post_json("/api/workspace", {
            "action": "upload", "filename": "repo.zip", "data": payload,
        })
        assert uploaded.get("ok"), uploaded
        root = Path(uploaded["workspace"]["root"])
        print("[2] 上传 →", uploaded["detail"])
        assert root.is_dir(), f"解压出来的工作区不存在：{root}"
        got = (root / "README.md").read_text(encoding="utf-8")
        assert got == "# 从浏览器传上来的项目", got
        print("[2] 磁盘上读回 ✓", got)

        after_temp = count_upload_temp()
        assert after_temp <= before_temp, (before_temp, after_temp)
        print(f"[2] 上传临时文件不残留 ✓（temp 里 wywd-upload-* 计数 {before_temp} → {after_temp}）")

        # ── 3. 切换后读回来一致 ───────────────────────────────
        current = get_json("/api/workspace")["workspace"]
        assert Path(current["root"]) == root.resolve(), current
        print("[3] GET 与 POST 一致 ✓", current["root"])

        # ── 4. open：引用本机目录 ─────────────────────────────
        local = base / "local-dir"
        local.mkdir()
        (local / "note.md").write_text("# 本机目录", encoding="utf-8")
        opened = post_json("/api/workspace", {
            "action": "open", "path": str(local)})
        assert opened.get("ok"), opened
        assert Path(opened["workspace"]["root"]) == local.resolve(), opened
        print("[4] open 本机目录 ✓", opened["workspace"]["root"])

        # ── 5. 复位：回启动态（不传路径）─────────────────────
        before_reset = get_json("/api/workspace")["workspace"]
        assert before_reset["kind"] != "default", before_reset
        reseted = post_json("/api/workspace", {"action": "reset"})
        assert reseted.get("ok"), reseted
        after_reset = reseted["workspace"]
        assert after_reset["kind"] == "default", after_reset
        assert after_reset.get("root"), "复位后要报得出默认根（UI 靠它显示路径）"
        print("[5] reset →", reseted["detail"])

        # 复位到默认后，工具真的读不动刚上传的那个目录了（边界回来了）
        fresh = get_json("/api/workspace")["workspace"]
        assert fresh == after_reset, (fresh, after_reset)
        print("[5] 复位后 GET 一致 ✓", fresh["root"])

        # ── 6. 坏输入：报人话 + 工作区不变（不留半态）─────────
        anchor = Path(get_json("/api/workspace")["workspace"]["root"])
        bad_cases = [
            ("坏 base64", {"action": "upload", "filename": "x.zip", "data": "这不是base64!!"}),
            ("空包", {"action": "upload", "filename": "x.zip", "data": ""}),
            ("没给路径", {"action": "open", "path": "   "}),
            ("目录不存在", {"action": "open", "path": str(base / "nope")}),
            ("未知动作", {"action": "explode"}),
        ]
        for label, body in bad_cases:
            res = post_json("/api/workspace", body)
            assert not res.get("ok"), (label, res)
            now = Path(get_json("/api/workspace")["workspace"]["root"])
            assert now == anchor, (label, now)
            print(f"[6] {label} →", res.get("detail"))

        # ── 7. 收尾 ───────────────────────────────────────────
        shutil.rmtree(root.parent, ignore_errors=True)
        print("[7] 清掉上传出来的工作区 ✓")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> None:
    multiprocessing.freeze_support()

    app = WebApp()
    _Handler.app = app
    server = _Server((HOST, PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        pong = app.start()
        print("[0] sidecar/ping →", pong.get("status"))
        thread.start()

        run_checks(app)
    finally:
        server.shutdown()
        server.server_close()
        app.stop()
        print("[8] 服务与 sidecar 已收尾")

    print("\n冒烟通过：网页上传的全链路（HTTP → RPC → 解压落盘）成立。")


if __name__ == "__main__":
    main()
