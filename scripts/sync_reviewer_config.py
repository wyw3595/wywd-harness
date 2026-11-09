"""同步 reviewer 配置：本地 `.workbuddy` → 可跟踪副本 `config/reviewer`。

为什么需要这个脚本（2026-11-09）：

  reviewer 的人格（专家包）与技能（code-review）首先是**本地配置**，
  放在仓库根的 `.workbuddy/` 里；那个目录整目录被 ignore（个人配置，
  不该强迫别人用我的人设）。但 CI 的 review job 需要一个可跟踪的副本，
  于是有了 `config/reviewer/.workbuddy/`。

  两份内容必须一致——漂移的后果不是报错，而是**悄悄用旧人格审代码**：
  本地改了准则、CI 还按上一版跑；或者本地新增的禁用工具没同步，CI 里
  reviewer 又拿到了写权限。所以把"比对/写回"做成一条命令，CI 也能跑
  `--check` 挡这种漂移。

用法：

    python -X utf8 scripts/sync_reviewer_config.py --check   # 一致 0 / 漂移 1
    python -X utf8 scripts/sync_reviewer_config.py --write   # 本地 → 仓库副本

`--check` 只读；`--write` 只写副本目录，永远不反向改本地配置（本地是
真源——人设是用户自己的东西，脚本不该覆盖它）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 需要保持同步的文件（相对 `.workbuddy/` 的路径）。
# 显式列清单而不是整目录复制：`.workbuddy/` 里还有记忆、日志、临时脚本，
# 那些是个人运行数据，绝不能跟着进仓库。
SYNC_FILES = (
    "experts/reviewer.json",
    "skills/code-review/SKILL.md",
)


def diff_files(source: Path, target: Path,
               files: tuple[str, ...] = SYNC_FILES) -> list[str]:
    """比对两份目录，返回人话描述的漂移项；空列表 = 一致。"""

    problems: list[str] = []
    for rel in files:
        src = source / rel
        dst = target / rel
        if not src.is_file():
            problems.append(f"缺少源文件：{src}")
            continue
        if not dst.is_file():
            problems.append(f"缺少副本：{dst}")
            continue
        if src.read_bytes() != dst.read_bytes():
            problems.append(f"内容不同：{rel}")
    return problems


def sync_files(source: Path, target: Path,
               files: tuple[str, ...] = SYNC_FILES) -> list[str]:
    """把源文件复制到副本目录，返回真正写过的相对路径（已一致的不动）。"""

    missing = [rel for rel in files if not (source / rel).is_file()]
    if missing:
        raise FileNotFoundError(
            "本地 reviewer 配置不完整，缺：" + "、".join(missing))

    written: list[str] = []
    for rel in files:
        data = (source / rel).read_bytes()
        dst = target / rel
        if dst.is_file() and dst.read_bytes() == data:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        written.append(rel)
    return written


def _default_source(repo: Path) -> Path:
    return repo / ".workbuddy"


def _default_target(repo: Path) -> Path:
    return repo / "config" / "reviewer" / ".workbuddy"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="同步 reviewer 配置到可跟踪副本（CI 用）")
    parser.add_argument("--check", action="store_true",
                        help="只比对：有漂移返回 1")
    parser.add_argument("--write", action="store_true",
                        help="把本地配置写进副本")
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="仓库根")
    parser.add_argument("--source", default="",
                        help="源目录（默认 <repo>/.workbuddy）")
    parser.add_argument("--target", default="",
                        help="副本目录（默认 <repo>/config/reviewer/.workbuddy）")
    args = parser.parse_args(argv)

    if args.check == args.write:      # 同时给或都不给
        print("二选一：--check（只比对）或 --write（写回副本）",
              file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    source = Path(args.source).resolve() if args.source else _default_source(repo)
    target = Path(args.target).resolve() if args.target else _default_target(repo)

    if not source.is_dir():
        print(f"[sync] 找不到本地配置目录：{source}\n"
              "  reviewer 本地配置缺失，无法比对/同步。", file=sys.stderr)
        return 2

    if args.check:
        problems = diff_files(source, target)
        if not problems:
            print(f"[sync] 一致（{len(SYNC_FILES)} 个文件）")
            return 0
        print("[sync] 发现漂移：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"[sync] 本地={source}\n       副本={target}\n"
              "       修：python -X utf8 scripts/sync_reviewer_config.py --write",
              file=sys.stderr)
        return 1

    try:
        written = sync_files(source, target)
    except (FileNotFoundError, OSError) as error:
        print(f"[sync] 写回失败：{error}", file=sys.stderr)
        return 2
    if not written:
        print("[sync] 已是最新，没有文件需要写")
        return 0
    for rel in written:
        print(f"[sync] 已写：{rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
