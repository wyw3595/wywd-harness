"""reviewer 评测：把已知 bug 的**修复前**提交拉成 worktree，让 reviewer 审，
统计召回 / 关键词命中 / 成本。

   为什么用 worktree 而不是 `git checkout`：评测要把历史代码交给 reviewer 读，
   但当前仓库不能被切走（还要用它跑 CLI、加载 reviewer 专家包/技能）。

   用法：
     python -X utf8 scripts/eval_review.py                # 全部 case
     python -X utf8 scripts/eval_review.py --case env-leak
     python -X utf8 scripts/eval_review.py --keep         # 保留 worktree

   前置：DEEPSEEK_API_KEY；reviewer 专家包与 code-review 技能在当前仓库
   （`--config-dir` 默认当前仓库；被测代码在 worktree 里）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

WORK = PROJECT_ROOT / ".fe-card-check-work" / "review-eval"
REPORT = WORK / "eval-report.md"

# 每个 case：从历史里挑一个「引入 bug 的提交」，审它自己的 diff。
# expect 是若干组关键词，**每组至少命中一个**才算召回（同义词组）。
CASES = [
    {
        "name": "restart-list",
        "commit": "297ea6a",
        "note": "list() 的 glob 与 _transcript 的 sess_<数字> 校验两套口径",
        "expect": [["sess_abc", "isdigit", "sess_<数字>"], ["list"]],
    },
    {
        "name": "audit-collision",
        "commit": "15e5bf2",
        "note": "audit.jsonl 落在 .sessions/，会被会话目录扫描当 transcript",
        # 关键词必须指向**集成点**：只提 audit.py 内部问题不算抓到本 case
        # （首轮评测就吃过这个亏：关键词太松，reviewer 没提碰撞也判了通过）。
        "expect": [["jsonl_store", "store.list", "glob", "会话目录", "会话文件"],
                   ["audit"]],
    },
    {
        "name": "env-leak",
        "commit": "deab242",
        "note": "PYTHONHOME 无条件剥 / 相对 PYTHONPATH 按进程 cwd 解析",
        "expect": [["PYTHONHOME"], ["PYTHONPATH"]],
    },
    {
        "name": "delivery-history",
        "commit": "f0db847",
        "note": "交付卡片只在直播态；刷新后历史里没有（设计缺口）",
        # 必须指向「刷新/历史里没有」这个缺口；只提卡片细节不算
        "expect": [["历史", "history", "刷新", "重开"],
                   ["卡片", "交付", "file-card"]],
    },
]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:300]}")
    return proc.stdout


def _worktree(case: dict) -> Path:
    path = WORK / f"wt-{case['name']}"
    if path.exists():
        _git(PROJECT_ROOT, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    _git(PROJECT_ROOT, "worktree", "add", "--detach", str(path), case["commit"])
    return path


def _run_case(case: dict, keep: bool = False) -> dict:
    out = WORK / f"{case['name']}.md"
    out.unlink(missing_ok=True)       # 旧报告不能冒充本轮结果
    worktree = _worktree(case)
    command = [
        sys.executable, "-X", "utf8", str(PROJECT_ROOT / "scripts" / "review.py"),
        "--repo", str(worktree), "--config-dir", str(PROJECT_ROOT),
        "--base", f"{case['commit']}~1", "--head", case["commit"],
        "--out", str(out), "--max-steps", "8",
    ]
    started = time.time()
    proc = None
    for attempt in range(2):          # API 偶发失败：同 case 重试一次
        try:
            proc = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900)
        finally:
            if not keep:
                _git(PROJECT_ROOT, "worktree", "remove", "--force",
                     str(worktree), check=False)
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        combined = current + (proc.stdout or "") + (proc.stderr or "")
        transient = ("调用模型失败" in combined
                     or "强制定稿失败" in combined
                     or proc.returncode == 2)
        if not transient or attempt == 1:
            break
        print(f"        [warn] 模型调用失败，5s 后重试 {case['name']}",
              file=sys.stderr)
        out.unlink(missing_ok=True)   # 失败输出不要混进重试结果
        time.sleep(5)
        if not keep:
            worktree = _worktree(case)          # 重试前重建 worktree
    elapsed = time.time() - started

    text = proc.stdout or ""
    if out.exists():
        text = out.read_text(encoding="utf-8") + "\n" + text
    lower = text.lower()
    api_failed = ("调用模型失败" in text or "强制定稿失败" in text
                  or proc.returncode == 2)
    missing_report = not out.exists()
    failed = api_failed or missing_report

    groups = []
    for group in case["expect"]:
        groups.append(any(word.lower() in lower for word in group))
    tokens = re.search(r"tokens (\d+)/(\d+)", proc.stderr or "")
    prompt_tokens = int(tokens.group(1)) if tokens else 0
    completion_tokens = int(tokens.group(2)) if tokens else 0
    # 峰值价、全按 cache miss 估上限（真实只会更便宜）
    cost_upper = prompt_tokens * 0.3 / 1e6 + completion_tokens * 1.2 / 1e6
    if failed and proc.stderr:
        print("        [stderr] " + proc.stderr.strip()[-500:], file=sys.stderr)
    return {
        "name": case["name"],
        "commit": case["commit"],
        "note": case["note"],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "exit": proc.returncode,
        "elapsed": round(elapsed, 1),
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "costUpper": round(cost_upper, 6),
        "groups": groups,
        "recall": all(groups) and not failed,
        "error": ("调用模型失败（已重试）" if api_failed
                  else "没有产出报告" if missing_report else ""),
        "major": len(re.findall(r"\[(?:major|blocker)\]", text, re.I)),
        "minor": len(re.findall(r"\[minor\]", text, re.I)),
        "report": str(out) if out.exists() else "",
        "text": text,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reviewer 评测（历史 bug 回放）")
    parser.add_argument("--case", default="", help="只跑某个 case")
    parser.add_argument("--keep", action="store_true", help="保留 worktree")
    parser.add_argument("--score-only", action="store_true",
                        help="不跑模型，按已有 case 报告重算关键词/召回")
    args = parser.parse_args(argv)

    if not args.score_only and not os.environ.get("DEEPSEEK_API_KEY"):
        print("[eval] 没有 DEEPSEEK_API_KEY，跑不了真模型", file=sys.stderr)
        return 2
    WORK.mkdir(parents=True, exist_ok=True)
    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"[eval] 没有这个 case：{args.case}", file=sys.stderr)
        return 2

    stored: dict[str, dict] = {}
    json_path = WORK / "eval-report.json"
    if json_path.exists():
        try:
            for row in json.loads(json_path.read_text(encoding="utf-8")):
                stored[row["name"]] = row
        except Exception:       # 旧报告坏了不影响这次重跑
            stored = {}

    if args.score_only:
        # 只重算关键词：首轮关键词太松，audit-collision 被误判成命中。
        # 报告文本还在，不必再花一次 API 调用的钱。
        for case in cases:
            path = WORK / f"{case['name']}.md"
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            row = dict(stored.get(case["name"], {
                "name": case["name"], "commit": case["commit"],
                "note": case["note"], "exit": 0, "elapsed": 0,
                "promptTokens": 0, "completionTokens": 0, "costUpper": 0,
                "error": "", "major": 0, "minor": 0,
            }))
            lower = text.lower()
            groups = [any(word.lower() in lower for word in group)
                      for group in case["expect"]]
            row.update({
                "groups": groups,
                "recall": all(groups) and not row.get("error"),
                "report": str(path),
                "text": text,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            stored[case["name"]] = row
            print(f"[eval] rescore {case['name']}: {groups} "
                  f"→ {'✅' if row['recall'] else '❌'}", file=sys.stderr)
    else:
        for case in cases:
            print(f"[eval] {case['name']} ← {case['commit']}（{case['note']}）",
                  file=sys.stderr)
            result = _run_case(case, keep=args.keep)
            stored[result["name"]] = result
            print(f"       召回={'✅' if result['recall'] else '❌'} "
                  f"groups={result['groups']} tokens={result['promptTokens']}/"
                  f"{result['completionTokens']} 用时={result['elapsed']}s "
                  f"major={result['major']} minor={result['minor']}"
                  f"{'  error=' + result['error'] if result['error'] else ''}",
                  file=sys.stderr)

    order = {c["name"]: i for i, c in enumerate(CASES)}
    results = sorted(stored.values(), key=lambda row: order.get(row["name"], 99))
    lines = [
        "# reviewer 评测（历史 bug 回放）",
        "",
        "| case | 提交 | 召回 | 关键词组 | major/minor | tokens | 估算上限 | 用时 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        mark = "❌ 模型失败" if r.get("error") else ("✅" if r["recall"] else "❌")
        lines.append(
            f"| {r['name']} | `{r['commit']}` | {mark} | "
            f"{r['groups']} | {r['major']}/{r['minor']} | "
            f"{r['promptTokens']}/{r['completionTokens']} | "
            f"${r['costUpper']:.4f} | {r['elapsed']}s |")
    lines += ["", "## 明细", ""]
    for r in results:
        lines += [f"### {r['name']}（`{r['commit']}`）", "",
                  f"- 说明：{r['note']}",
                  f"- 退出码：{r['exit']}；报告：`{r['report']}`", ""]
        if r.get("error"):
            lines += [f"- **{r['error']}**", ""]
        lines += ["```text", (r.get("text") or "（无输出）").strip()[:6000],
                  "```", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    payload = [{k: v for k, v in r.items() if k != "text"} for r in results]
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[eval] 报告：{REPORT}")
    return 0 if results and all(r["recall"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
