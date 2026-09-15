"""冒烟：工作区的两条入口接线（离线跑，不碰网络/密钥）。

第一段——chat.py 的 /ws 命令族：
  1. /ws <路径> 切本地目录后，运行件真的绑到了新 root；
  2. /ws-zip 解压上传后，模型工具能读到包里的文件；
  3. /ws-reset 回到默认工作区；
  4. 越界访问在新沙箱里照样被拒（权限层 + 执行层同一根）。

第二段——web/sidecar 的 workspace/set 路由（真 spawn 一个子进程）：
  5. 注入的 runtime_builder 能穿过 spawn 进子进程（这一步最容易坏：
     工厂要能在"全新解释器"里被 import 到）；
  6. workspace/set 真的换了沙箱（拿新 registry 读新目录里的文件）；
  7. 坏包/坏路径回的是人话，不是异常；
  8. workspace/get 与 sidecar/status 都带着当前工作区；
  9. 复位（kind=default）回启动态，且报得出默认根。
"""

import multiprocessing
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.chat import build_runtime, handle_workspace_command  # noqa: E402
from scripts.shell import SidecarShell, _workspace_runtime  # noqa: E402
from scripts.toolbox import DEFAULT_WORKSPACE  # noqa: E402


def chat_command_family(base: Path) -> None:
    """第一段：chat.py 的 /ws 命令族。"""

    # ── 1. 切本地目录 ──────────────────────────────────────────
    project = base / "my-project"
    project.mkdir()
    (project / "hello.txt").write_text("本地目录的内容", encoding="utf-8")

    result = handle_workspace_command(f"/ws {project}", DEFAULT_WORKSPACE, None)
    workspace, cleared = result
    registry, runner = build_runtime(workspace)
    print("[1] 切换本地目录 →", workspace.root, "清记忆:", cleared)
    print("    工具读到:", registry.execute("fs_read", "hello.txt").splitlines()[0])

    # ── 2. zip 上传（带一层 GitHub 式顶层目录）────────────────
    zips = base / "zips"
    zips.mkdir()
    pack = zips / "upload.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("repo-main/README.md", "# 上传的项目")
        zf.writestr("repo-main/src/app.py", "print('hi')")

    result = handle_workspace_command(f"/ws-zip {pack}", workspace, None)
    uploaded, cleared = result
    registry2, runner2 = build_runtime(uploaded)
    print("[2] zip 上传 →", uploaded.workspace_id, uploaded.root)
    print("    工具读到:", registry2.execute("fs_read", "README.md").splitlines()[0])

    # ── 3. 越界在最外层被拒（新沙箱的边界是真的）──────────────
    try:
        registry2.execute("fs_read", f"../{project.name}/hello.txt")
        print("[3] 越界未拦截 —— 有问题！")
    except PermissionError as exc:
        print("[3] 越界被拒 ✓", str(exc)[:40], "…")

    # ── 4. 回到默认工作区 ─────────────────────────────────────
    result = handle_workspace_command("/ws-reset", uploaded, None)
    back, cleared = result
    print("[4] 复位 →", back.workspace_id, back.root)
    assert back.workspace_id == "default"

    uploaded.cleanup(confirm=True)
    print("[4] 清理上传工作区 →", not uploaded.root.parent.exists())


def sidecar_workspace_probe(base: Path) -> None:
    """第二段：sidecar 的 workspace/set（真 spawn 一个子进程）。

    注意：这一段必须在 `if __name__ == "__main__":` 里被调用。
    Windows 用 spawn 起子进程时，解释器会重新 import 本文件；
    没有守卫的话，子进程会再跑一遍这段代码，再 spawn 一次 —— 无限递归。
    """

    # ── 5. 注入的工厂本身能用（先单独验一遍，出问题好定位）────
    probe = base / "probe-dir"
    probe.mkdir()
    (probe / "note.md").write_text("# 探针目录", encoding="utf-8")
    probe_registry, probe_policy, probe_info = _workspace_runtime(
        {"kind": "dir", "path": str(probe)})
    print("[5] 工厂直调 →", probe_info["root"])
    print("    工具读到:", probe_registry.execute("fs_read", "note.md").splitlines()[0])

    # ── 6. 真起 sidecar：工厂要能穿过 spawn 到子进程 ──────────
    shell = SidecarShell()
    try:
        pong = shell.start()
        print("[6] sidecar 启动 →", pong.get("status"))

        opened = shell.set_workspace("dir", str(probe))
        assert opened.get("status") == "ok", opened
        print("[6] workspace/set(dir) →", opened["workspace"]["root"])

        current = shell.workspace()["workspace"]
        assert current["root"] == str(probe.resolve()), current
        print("[6] workspace/get 一致 ✓")

        status = shell.status()["workspace"]
        print("[6] sidecar/status 也带着工作区 →", status["root"])

        # ── 7. 坏输入回人话（不是异常/堆栈）────────────────────
        bad = shell.set_workspace("zip", str(base / "zips" / "not-a-zip.zip"))
        print("[7] 坏包 →", "ok" if bad.get("status") == "ok" else bad.get("error"))

        bad_dir = shell.set_workspace("dir", str(base / "no-such-dir"))
        print("[7] 坏目录 →", bad_dir.get("error"))

        bad_kind = shell.set_workspace("ftp", str(probe))
        print("[7] 坏类型 →", bad_kind.get("error"))

        # ── 8. 复位：回启动态（且报得出默认根，UI 才好显示）────
        back = shell.set_workspace("default", "")
        assert back.get("status") == "ok", back
        assert back["workspace"]["kind"] == "default", back
        assert back["workspace"].get("root"), back
        print("[8] 复位回默认 →", back["workspace"]["root"])
    finally:
        shell.stop()
        print("[9] sidecar 已收尾")


def main() -> None:
    # spawn 起来的子进程会重新 import 本模块，freeze_support 保证
    # 这一句在子进程里是空操作，不会重复执行 main 的内容。
    multiprocessing.freeze_support()

    base = Path(tempfile.mkdtemp())
    try:
        chat_command_family(base)
        sidecar_workspace_probe(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n冒烟通过：两条入口的接线都正确。")


if __name__ == "__main__":
    main()
