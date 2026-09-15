"""冒烟：原生目录选择框的真实子进程链路（离线可跑）。

单测把 subprocess.run 换掉了，验的是"协议怎么解析"。这里验的是**真起进程**：
pythonw 找得到吗、argv 传得对吗、子进程真能 import 到自己的模块吗、
回话真能读回来吗——这些在 mock 下全是假的。

    python scripts/smoke_native_dialog.py                  # 安全步骤（不弹窗）
    python scripts/smoke_native_dialog.py --with-dialog     # 连真弹框一起验

为什么真弹框要单独开开关：它会开窗口，而受限 shell（代理/沙箱、CI、无桌面
会话）在"孙进程开窗"时会把整个进程组掐掉——实测如此，连非沙箱模式也一样。
默认不跑就不会把冒烟变成"看起来挂了"；在正常桌面会话里加上
`--with-dialog` 就能把"起进程 → COM → 真弹窗 → 关闭 → 回话"整条链验穿。
"""

import argparse
import multiprocessing
import subprocess
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import native_dialog  # noqa: E402


def check_available() -> bool:
    can, reason = native_dialog.available()
    print(f"[1] 探活 → {can}（{reason}）")
    if not can:
        print("    这台机器弹不出框：真服务上会降级成手输路径（不是死路）✓")
    return can


def check_interpreter() -> None:
    interpreter = Path(native_dialog._interpreter())
    print(f"[2] 子进程解释器 → {interpreter.name}")
    assert interpreter.name.lower() in ("pythonw.exe", "python.exe"), interpreter


def check_selftest() -> None:
    """不建窗：起进程 + 回话协议 + 路径解析这一侧，全是真的。"""

    picked = native_dialog.pick_directory(str(PROJECT_ROOT), selftest=True)
    print(f"[3] 自检回话 → {picked}")
    assert picked == str(PROJECT_ROOT.resolve()), picked
    print("    起进程 + 协议 + 路径解析都是真的 ✓")


def check_unavailable_path() -> None:
    with mock.patch.object(native_dialog, "available",
                           return_value=(False, "没有图形环境")):
        try:
            native_dialog.pick_directory()
            raise AssertionError("探活说不该弹，却还是弹了")
        except native_dialog.DialogUnavailable as exc:
            print(f"[4] 不可用时抛 DialogUnavailable → {exc} ✓")


def check_real_dialog() -> None:
    """真弹框 + **自动按下"选择文件夹"**：整条链无人值守验穿。

    子进程起来后，后台线程等窗口真的可见，再把"选择文件夹"按钮按下去——
    于是 `Show()` 走的是"用户选了一个"的正常分支，`GetResult` /
    `GetDisplayName` 那一段也被验到，能直接断言返回值。

    直接跑子进程（而不是 through pick_directory）：受限 shell 会掐掉
    "父进程等子进程开窗"这种形状，直接跑子进程才活得下来。
    """

    target = str(PROJECT_ROOT.resolve())
    print(f"[5] 真弹框（起始目录 {target}，随后自动按「选择文件夹」）…")
    done = subprocess.run(
        [sys.executable, str(Path(native_dialog.__file__)), "--child",
         target, "--flash-ms=8000", "--auto-ok"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    print(f"    退出码 {done.returncode}")
    if done.stderr.strip():
        for line in done.stderr.splitlines():
            if "flash" in line or "dialog" in line:
                print(f"    {line.strip()}")
    assert done.returncode == 0, "子进程没能正常收场"

    result_line = ""
    for line in done.stdout.splitlines():
        if line.startswith(native_dialog._RESULT_PREFIX):
            result_line = line[len(native_dialog._RESULT_PREFIX):].strip()
    assert result_line, f"没有回话行：{done.stdout!r}"
    assert result_line == target, f"选中的不是起始目录：{result_line}"
    print("    窗口真出现 → 真按下选择 → 取回路径 一致 ✓")
    print("    观感（长什么样、翻目录顺不顺手）请人工点一次确认")


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-dialog", action="store_true",
                        help="连真弹框一起验（需要正常桌面会话）")
    args = parser.parse_args()

    if not check_available():
        return
    check_interpreter()
    check_selftest()
    check_unavailable_path()

    if args.with_dialog:
        check_real_dialog()
    else:
        print("[5] 真弹框已跳过。要看它：\n"
              "        python scripts/smoke_native_dialog.py --with-dialog\n"
              "    或直接点页面上的「打开目录」。")

    print("\n冒烟通过：原生选择框的子进程链路成立。")


if __name__ == "__main__":
    main()
