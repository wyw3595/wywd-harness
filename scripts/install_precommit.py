"""把 reviewer 安装成 git pre-commit 钩子（本地，可选）。

为什么不自动装：钩子会让每次 commit 都多一次模型调用（有 key 时），
属于个人工作流选择；这里只提供一条命令，装不装由你决定。

    python -X utf8 scripts/install_precommit.py          # 安装
    python -X utf8 scripts/install_precommit.py --force  # 覆盖已有钩子
    python -X utf8 scripts/install_precommit.py --uninstall

行为：
  - 只审**已暂存**的改动（`git diff --cached`），不审工作区无关文件；
  - 出现 `[blocker]` 才挡提交（`--fail-on blocker`）；major/minor 只打印；
  - 没有 DEEPSEEK_API_KEY / reviewer 配置缺失 / 模型起不来 → 跳过，
    **不阻塞**提交（本地钩子不该因为外部服务不可用把人锁死）；
  - `WYWD_SKIP_REVIEW=1 git commit ...` 可临时绕过。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HOOK = """#!/bin/sh
# 由 scripts/install_precommit.py 生成；删掉本文件或跑 --uninstall 卸载。
# 只审暂存区；[blocker] 才挡提交；服务不可用时跳过，不锁死本地提交。
[ "${WYWD_SKIP_REVIEW:-}" = "1" ] && exit 0
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "[review] 没有 DEEPSEEK_API_KEY，跳过审查（不阻塞提交）" >&2
  exit 0
fi
# 安装时写死当前解释器（通常是 .venv）；搬家后重跑安装器即可。
PY="__PYTHON__"
if [ ! -x "$PY" ]; then
  if command -v python >/dev/null 2>&1; then
    PY=python
  elif command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    echo "[review] 找不到 python，跳过审查（不阻塞提交）" >&2
    exit 0
  fi
fi
"$PY" -X utf8 scripts/review.py --staged --fail-on blocker
code=$?
case "$code" in
  0|1) exit "$code" ;;
  *)
    echo "[review] reviewer 起不来（退出码 $code），跳过（不阻塞）" >&2
    exit 0
    ;;
esac
"""


def _hook_text() -> str:
    python = str(Path(sys.executable).resolve()).replace("\\", "/")
    return HOOK.replace("__PYTHON__", python)


def _hooks_dir(repo_root: Path) -> Path:
    git = Path(repo_root) / ".git"
    if not git.is_dir():
        raise RuntimeError(f"不是 git 仓库（没有 .git 目录）：{git}")
    return git / "hooks"


def install(repo_root: Path = PROJECT_ROOT, force: bool = False) -> Path:
    """写入 pre-commit 钩子，返回路径。已有钩子时默认拒绝覆盖。"""

    hook = _hooks_dir(repo_root) / "pre-commit"
    if hook.exists() and not force:
        raise RuntimeError(
            f"已有 pre-commit 钩子：{hook}；确认要覆盖再加 --force")
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(_hook_text(), encoding="utf-8", newline="\n")
    try:
        os.chmod(hook, 0o755)
    except OSError:
        pass                    # Windows 上 chmod 不完整，Git 仍能跑
    return hook


def uninstall(repo_root: Path = PROJECT_ROOT) -> bool:
    """删掉本安装器生成的钩子；不是它生成的就不动。"""

    hook = _hooks_dir(repo_root) / "pre-commit"
    if not hook.is_file():
        return False
    text = hook.read_text(encoding="utf-8", errors="replace")
    if "scripts/install_precommit.py" not in text:
        raise RuntimeError(f"{hook} 不是本安装器生成的，拒绝删除")
    hook.unlink()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 reviewer pre-commit 钩子")
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="仓库根")
    parser.add_argument("--force", action="store_true", help="覆盖已有钩子")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.uninstall:
            removed = uninstall(repo)
            print("已卸载" if removed else "没有可卸载的钩子")
            return 0
        hook = install(repo, force=args.force)
    except RuntimeError as error:
        print(f"[precommit] {error}")
        return 2
    print(f"[precommit] 已安装：{hook}")
    print("  只审暂存区；[blocker] 挡提交；没 key 自动跳过；"
          "WYWD_SKIP_REVIEW=1 可绕过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
