"""只读代码审查 CLI：git diff → reviewer 专家包 → 结构化发现。

   为什么是一个独立脚本而不是又一个网页入口：审查是**批处理**动作——
   给定 base..head，跑一次，拿一份报告。它不该占着会话、也不该让人等
   在界面前面。

   只读是怎么保证的（不是靠提示词）：
   - 专家包的 tools_config 在这里**真的**落到注册表上：select_tools 摘掉
     bash / fs_write / fs_edit，模型连工具都看不见；
   - 审批一律拒绝（审查不需要任何"这次特殊"的放行）；
   - 输出只写用户指定的 --out 文件，仓库本身不动。

   用法：
     python -X utf8 scripts/review.py --base HEAD~1
     python -X utf8 scripts/review.py --base main --head HEAD --path src/
     python -X utf8 scripts/review.py --base HEAD~5 --out review.md

   前置：DEEPSEEK_API_KEY（真模型）；仓库根 .workbuddy/experts/reviewer.json
   与 .workbuddy/skills/code-review/ 存在。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.toolbox import (          # noqa: E402
    build_model,
    build_policy_for,
    build_registry_for,
    build_skill_index,
)
from src.harness.agent import run_agent                    # noqa: E402
from src.harness.experts import (                          # noqa: E402
    ExpertPackage,
    ExpertIndex,
    render_specialization,
    select_tools,
)
from src.harness.permissions import AuditTrail, GovernedToolRunner  # noqa: E402
from src.harness.real_model import RealModel                 # noqa: E402
from src.harness.tools import ToolRegistry                  # noqa: E402
from src.harness.workspace import Workspace                 # noqa: E402

MAX_DIFF_CHARS = 60_000


def _run_git(repo: Path, *args: str) -> str:
    """跑一条只读 git 命令；失败带着 stderr 抛人话。"""

    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:500]
        raise RuntimeError(f"git {' '.join(args)} 失败：{stderr}")
    return proc.stdout


def collect_diff(repo: Path, base: str, head: str = "HEAD",
                 path: str = "") -> tuple[str, str, bool]:
    """(stat, diff, 是否截断)。path 非空时只看该路径。"""

    stat_args = ["diff", "--stat", f"{base}..{head}"]
    diff_args = ["diff", "-U3", f"{base}..{head}"]
    if path:
        stat_args += ["--", path]
        diff_args += ["--", path]
    stat = _run_git(repo, *stat_args).strip()
    body = _run_git(repo, *diff_args).rstrip()
    truncated = len(body) > MAX_DIFF_CHARS
    if truncated:
        body = (body[:MAX_DIFF_CHARS]
                + f"\n…（diff 被截断，原始 {len(body)} 字符；需要的文件请自己读）")
    return stat, body, truncated


def filter_registry(registry: ToolRegistry,
                    package: ExpertPackage) -> tuple[ToolRegistry, tuple[str, ...]]:
    """按专家包的 tools_config 收窄工具；返回 (新注册表, 被摘掉的名字)。

    专家包只能**少给**工具——这是 s18 的铁律，脚本这里没有例外。
    """

    kept, disabled = select_tools(registry.names(), package.tools_config)
    filtered = ToolRegistry()
    for name in kept:
        filtered.register(registry.get(name))
    return filtered, disabled


# 2026-11-08 评测（2/4）暴露的短板：reviewer 擅长 diff 内部，跨文件集成差。
# 两个失败的 case 都是“改动本身对，但和邻居打架”：audit.jsonl 没被联想到
# 会话目录的 *.jsonl glob；交付卡片没被联想到历史回放路径。所以把集成检查
# 固定进系统提示——不是让模型自由发挥，而是按清单至少读一个调用方/邻居。
_INTEGRATION_CHECKLIST = (
    "## 集成检查（先做这一步，再看 diff 细节）\n"
    "1. 这次改动的**调用方**和**邻居文件**是谁？至少读 1 个，确认行为没被改坏。\n"
    "2. 契约两端是否对称：create/list、写/读、发/收、实时渲染/历史回放。\n"
    "3. 新增或改变落盘路径时，同目录还有谁在扫（glob/list）？\n"
    "4. 数据从哪来、被谁控制：工具输出、文件内容、URL 都当不可信。\n"
    "5. 错误语义变了（抛异常 vs 返回 False/None）？找出所有调用方。\n"
    "6. 改动是 UI 数据/渲染时：实时路径（SSE/live）和历史回放路径\n"
    "   （transcript/历史列表）都要走一遍——只画实时不算完成。"
)


def build_system_prompt(package: ExpertPackage, skill_body: str,
                        repo: Path) -> str:
    return "\n\n".join([
        render_specialization(package),
        _INTEGRATION_CHECKLIST,
        "## 本仓库评审技能（code-review）\n" + skill_body.strip(),
        f"仓库根：{repo}\n只审用户给出的 diff；需要上下文时用 "
        "fs_read / fs_glob / fs_find 读文件，不要改任何文件。\n"
        "diff 已经完整给出——不要通读仓库；总共读 2-4 个文件，其中至少 1 个是"
        "本次改动的调用方或邻居文件。读完就给结论，不要无限取证。",
    ])


def build_task(base: str, head: str, stat: str, body: str,
               truncated: bool = False) -> str:
    note = "\n（注意：diff 被截断，未展示的部分按需读文件）" if truncated else ""
    return (
        f"请审查 {base}..{head} 的改动。\n\n"
        f"变更统计：\n{stat or '（无）'}\n\n"
        f"diff：{note}\n```diff\n{body or '（空）'}\n```\n\n"
        "输出要求：\n"
        "- 先按系统提示里的集成检查走一遍（调用方/邻居、契约两端、"
        "落盘目录、数据来源），再逐条看 diff\n"
        "- 每条发现一行：`[severity] file:line — 一句话`，"
        "下面 2-4 行说清什么输入/状态下会错、建议怎么改；"
        "severity 用 blocker / major / minor\n"
        "- 只报 diff 里改动的行；不确定的不要报；没有发现就写「没有发现」\n"
        "- 不要修改任何文件"
    )


def _deny(decision) -> bool:      # Approver 协议：收到决策，返回批不批
    print(f"  ⚠ 审查模式拒绝审批：{decision.rule_id}（{decision.reason}）",
          file=sys.stderr)
    return False


def _on_event(event: str, data: dict) -> None:
    if event == "tool_start":
        print(f"  🔧 {data.get('name', '?')}", file=sys.stderr)
    elif event == "round_start" and (data.get("step") or 0) > 0:
        print(f"  ── 第 {(data.get('step') or 0) + 1} 轮", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读代码审查（reviewer 专家包 + code-review 技能）")
    parser.add_argument("--base", default="HEAD~1", help="基线（默认 HEAD~1）")
    parser.add_argument("--head", default="HEAD", help="目标（默认 HEAD）")
    parser.add_argument("--path", default="", help="只看某个路径")
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="仓库根")
    parser.add_argument("--config-dir", default="",
                        help="reviewer 专家包/技能所在目录（默认同 --repo；"
                             "用 worktree 评测修复前代码时指向当前仓库）")
    parser.add_argument("--out", default="", help="把报告写到文件（可选）")
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    config_dir = Path(args.config_dir).resolve() if args.config_dir else repo
    stat, body, truncated = collect_diff(repo, args.base, args.head, args.path)
    if not body.strip():
        print("没有 diff（基线/目标相同，或该路径没有改动）")
        return 0

    workspace = Workspace(workspace_id="review", root=repo)
    try:
        package = ExpertIndex(config_dir).load("reviewer")
        skill_body = build_skill_index(config_dir).load("code-review")
    except Exception as error:      # ExpertError / 技能缺失：人话，不抛栈
        print(f"[review] 缺少 reviewer 本地配置：{error}\n"
              "  需要 .workbuddy/experts/reviewer.json 与 "
              ".workbuddy/skills/code-review/SKILL.md", file=sys.stderr)
        return 2
    registry, disabled = filter_registry(build_registry_for(workspace), package)
    runner = GovernedToolRunner(
        policy=build_policy_for(workspace),
        approver=_deny,
        registry=registry,
        audit=AuditTrail(),
    )
    history = [{"role": "system",
                "content": build_system_prompt(package, skill_body, repo)}]

    print(f"[review] {args.base}..{args.head}  {stat.splitlines()[0] if stat else '无改动'}")
    print(f"[review] 工具收窄：摘掉 {', '.join(disabled) or '（无）'}", file=sys.stderr)
    try:
        result = run_agent(
            build_task(args.base, args.head, stat, body, truncated),
            model=build_model(registry),
            registry=registry,
            max_steps=args.max_steps,
            history=history,
            runner=runner,
            on_event=_on_event,
        )
    except RuntimeError as error:          # 缺 key 之类：人话，别抛栈
        print(f"[review] 起不来：{error}", file=sys.stderr)
        return 2

    output = result.output
    forced = False
    if result.status == "max_steps" and result.messages:
        # 步数用尽 = 取证停不下来，不是没话可说。这里用**已经读到的上下文**
        # 强制作一份结论：无工具的模型调用只做总结，不再要求继续探。
        # （deepseek-chat 对"读 2-4 个文件"这类预算指令执行力很差，
        #   真机实测：65 行 diff 读了 20 轮还没收尾。）
        forced = True
        try:
            # 定稿**必须用非思考模型**：DeepSeek 思考模式要求把上一轮的
            # reasoning_content 原样回传，而 `_to_wire_messages` 出于安全
            # 白名单把它丢了——带工具历史 + thinking 的请求会 400
            # （2026-11-08 评测真机抓到）。总结不需要思考链，用 deepseek-chat。
            final = RealModel(tools=None, thinking=False,
                              system_prompt="").generate(
                list(result.messages) + [{
                    "role": "user",
                    "content": "现在只输出审查结论（按上面的格式），不要再调用工具。",
                }])
            output = final.text
        except RuntimeError as error:
            # 定稿也是一次模型调用，会失败；别让 traceback 穿出去。
            print(f"[review] 强制定稿失败：{error}（本轮没有拿到结论）",
                  file=sys.stderr)
            return 2

    print(output)
    if args.out:
        Path(args.out).write_text(
            f"# 代码审查：{args.base}..{args.head}\n\n{output}\n",
            encoding="utf-8")
        print(f"[review] 报告已写入 {args.out}", file=sys.stderr)
    usage = result.usage or {}
    print(f"[review] 状态 {result.status}{'（强制定稿）' if forced else ''}；"
          f"tokens {usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)}",
          file=sys.stderr)
    return 0 if output.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
