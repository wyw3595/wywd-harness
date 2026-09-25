"""
============================================================
Harness 练习 s04:A 批 — 权限决策层(Permission Policy)
============================================================

s03 解决了"模型看见哪些 schema",s04 解决"模型选中工具后,
Harness 允不允许它执行"。本模块把治理拆成可测试的两层:
决策层(PermissionPolicy.decide / WorkspaceScope /
resolve_permission / 默认策略)和执行层(GovernedToolRunner +
AuditTrail——决策的唯一消费者,真正决定"执行 or 拦截")。

核心流水线:
    ToolRequest -> PermissionPolicy.decide() -> PermissionDecision
    PermissionDecision -> resolve_permission() -> PermissionResolution
        其中 ASK 才调 approver;ALLOW/DENY 不交互,DENY 不可覆盖.

设计铁律:
  1. 有序规则 + first-match-wins + 默认拒绝(fail-closed)
     ——顺序就是安全语义:硬拒绝必须先于审批规则,否则 sudo rm -rf
     会被降级成"问问就行";新增工具漏配规则 = 拒绝,不是悄悄放行;
  2. 纯决策,零副作用:Policy.decide 不调 input、不执行 handler;
  3. rule_id(稳定机器可测)+ reason(人读的上下文)缺一不可;
  4. approver 是注入的 Callable——CLI/桌面/测试各不相同,Policy
     不认识任何 UI;
  5. DENY 不可被审批覆盖:弹窗本身会暗示用户有权改系统边界.

面试点:为什么不是一个 is_allowed: bool?
  ——"免审批允许 / 需同意 / 不可覆盖拒绝"是三种治理语义,布尔只
  装得下一个;用户拒绝与系统拒绝必须分账.

完成标志:tests/test_permissions.py 与 tests/test_governed_runner.py
全部通过
============================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence


# ----------------------------------------------------------------------
# 1. 核心数据契约(frozen:决定一经作出,谁也不能偷改)
# ----------------------------------------------------------------------

class PermissionAction(Enum):
    """三种治理语义。Enum 而非 Literal:拼错直接 AttributeError,
    还能做 isinstance/成员比较——字符串暗号能静默拼错。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class ToolRequest:
    """一次工具调用的归一化请求(provider 的 tool block 先转成它)。

    arguments 暂时保留 object:模型输入在校验前不能假设一定是字典
    (policy 会对非 dict 直接 DENY)。
    """

    tool_use_id: str
    name: str
    arguments: object


@dataclass(frozen=True)
class PermissionDecision:
    """一条规则匹配的结果:动作 + 稳定规则号 + 人读的理由。"""

    request: ToolRequest
    action: PermissionAction
    rule_id: str
    reason: str


@dataclass(frozen=True)
class PermissionResolution:
    """决策落地为"最终能否执行"。

    allowed 只有一个布尔;但 approval_status 记录来源(免审批 / 批准 /
    拒绝 / 取消),让"用户拒绝"和"系统拒绝"不混账。
    """

    decision: PermissionDecision
    allowed: bool
    approval_status: str  # not_required / approved / rejected / cancelled


Approver = Callable[[PermissionDecision], bool]  # 传入决策,返回"批准吗"


# ----------------------------------------------------------------------
# 2. 规则与策略:pure 决策,可单测
# ----------------------------------------------------------------------

RuleMatcher = Callable[[ToolRequest], bool]
RuleExplainer = Callable[[ToolRequest], str]


@dataclass(frozen=True)
class PermissionRule:
    """一条有序规则:matches 判命中,explain 按请求生成理由。

    两个调用子分工:matches 回答"命中吗"(bool);explain 只在命中后
    被调,从请求里提取证据(哪条命令/哪个路径)拼成人读理由——
    拒绝必须有据可查,静态文案做不到(测试锁死:理由里要出现 sudo)。
    规则顺序即安全语义,由 PermissionPolicy 负责;本类只管
    "一条规则自己怎么评"。
    """

    rule_id: str
    action: PermissionAction
    matches: RuleMatcher
    explain: RuleExplainer

    def evaluate(self, request: ToolRequest) -> PermissionDecision | None:
        """命中 -> 带动态理由的 PermissionDecision;不命中 -> None。"""

        if not self.matches(request):
            return None
        return PermissionDecision(
            request, self.action, self.rule_id, self.explain(request)
        )


class PermissionPolicy:
    """持有一组有序规则,对请求做 first-match-wins 的纯决策。

    decide 是纯函数:不调用户输入、不执行工具、不写日志——
    才能被单元测试裸奔。
    """

    def __init__(self, rules: Sequence[PermissionRule]) -> None:
        self._rules = list(rules)  # 防御性复制:外部改列表不影响策略

    def decide(self, request: ToolRequest) -> PermissionDecision:
        """按顺序找第一条命中的规则;一条都不中 -> default.deny。"""

        for rule in self._rules:
            decision = rule.evaluate(request)
            if decision is not None:
                return decision
        # 循环结束还没 return,说明一条都没命中——fail-closed 默认拒绝。
        return PermissionDecision(
            request,
            PermissionAction.DENY,
            "default.deny",
            f"no permission rule matched tool {request.name!r}",
        )


class WorkspaceScope:
    """工作区路径作用域:判定"模型给的路径是否允许访问"。

    覆盖三类越界:
      a) ../outside/secret.txt  相对路径穿越
      b) /etc/hosts             绝对外部路径
      c) link/new.txt           符号链接指向工作区外
    以及界内禁区(s04 追加):路径任一段撞上 forbidden_parts(如 .env/.git)
    同样拒绝——沙箱管"有没有越界",禁区管"界内哪些不能碰"。
    resolve(strict=False):目标尚不存在也允许解析(要写入的文件还没
    创建),同时解开已存在的父级符号链接。
    """

    def __init__(self, root: Path, forbidden_parts: Sequence[str] = ()) -> None:
        self._root = root.resolve()  # 归一化成绝对路径,防止 root 自身带 ".."
        self._forbidden = frozenset(forbidden_parts)

    def resolve(self, path_text: str) -> Path:
        """把相对/绝对文本解析成受保护的绝对路径;越界抛 PermissionError。"""

        resolved = (self._root / path_text).resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"路径越出工作区:{path_text}")
        return resolved

    def has_forbidden_part(self, path_text: str) -> bool:
        """解析后任一路径段撞上禁区 -> True(与 file_tools._resolve_safe 同款判定)。

        越界不归这里管:has_forbidden_part 只回答"界内是否撞禁区",越界返回
        False 交给排在前面的 outside 规则。判定沿 file_tools:查整条动线而
        不只查终点——fs_read(".git/config") 撞禁区的是路径中段的 .git。
        """

        if not self._forbidden:
            return False
        try:
            resolved = self.resolve(path_text)
        except PermissionError:
            return False  # 越界:那是 outside 规则的管辖范围
        relative = resolved.relative_to(self._root)
        return any(part in self._forbidden for part in relative.parts)


# ----------------------------------------------------------------------
# 3. 审批:只有 ASK 才有人工参与
# ----------------------------------------------------------------------

def resolve_permission(
    decision: PermissionDecision,
    approver: Approver,
) -> PermissionResolution:
    """把决策解析成最终决议;approver 只在 ASK 分支被调用。

    - ALLOW -> allowed=True,  not_required(不问)
    - DENY  -> allowed=False, not_required(不让老板出场,不可覆盖)
    - ASK   -> 调 approver(decision):True -> approved / False -> rejected
    """

    if decision.action is PermissionAction.ALLOW:
        return PermissionResolution(decision, True, "not_required")
    if decision.action is PermissionAction.DENY:
        return PermissionResolution(decision, False, "not_required")
    if decision.action is PermissionAction.ASK:
        approved = approver(decision)
        return PermissionResolution(
            decision, approved,
            "approved" if approved else "rejected",
        )
    # 未知动作:显式炸,而不是静默当 ASK——枚举再扩展时不踩"悄悄放行"。
    raise AssertionError(f"未知权限动作:{decision.action!r}")


# ----------------------------------------------------------------------
# 4. 装配默认规则:默认拒绝 + bash 分级 + 路径作用域 + 免审批白名单
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# 3.5 只读命令白名单（2026-09-23，对齐 Claude Code 的权限 Tier 1）
# ----------------------------------------------------------------------
#
# **为什么需要**：审批是**阻塞**的，且占着唯一的 RPC 通道（scripts/shell.py
# 的 _rpc_lock）——"什么命令都问"既拖慢 agent，又会在没人应答时把整个前端
# 堵住（实测：一次 bash 审批 = 前端最长卡 5 分钟）。而实际用得最多的是
# `git status` / `ls` / 跑测试这类**只读**命令，它们不该每次都要人点头。
#
# Claude Code 的源码分析里那句判据值得抄下来：
#   没有中间层的话，每个陌生命令都会撞到"问用户"，而**权限疲劳是真实的
#   安全风险——被问得太频繁的用户会不读就点批准**。
#
# ⚠️ 安全前提（**三条必须同时成立**才放行，缺一条就是漏洞）：
#   1. 命令里**不含任何 shell 元字符**。这是最关键的一条：没有它，
#      `ls; rm -rf /` 会因为"以 ls 开头"被放行——那是个致命漏洞。
#   2. 首个子命令命中白名单，且白名单项后面必须是**空白或结尾**
#      （否则 `listing` 会被 `ls` 前缀匹配上）。
#   3. 只收"**只能看、不能改**"的命令：任何会写盘、装包、改仓库状态的
#      都不进这张表（`git push` / `npm install` / `rm` 一律不在）。
READ_ONLY_COMMAND_PREFIXES = (
    # git 的只读子命令
    "git status", "git diff", "git log", "git show", "git branch",
    "git remote", "git ls-files", "git rev-parse",
    # 看文件 / 找东西
    "ls", "dir", "pwd", "cat", "head", "tail", "wc", "find", "grep", "echo",
    # 跑测试（写只有 __pycache__ / .pytest_cache，属于"验证"而非"改项目"）
    "python -m unittest", "python -m pytest", "pytest",
    # 看版本 / 看依赖清单
    "python --version", "node --version", "git --version", "pip list",
)

# 出现任何一个就不走白名单。~ 也拦：`cat ~/.ssh/id_rsa` 是典型的越界读。
_SHELL_METACHARS = frozenset(";|&><`$(){}\n\r~")


def build_default_policy(
    scope: WorkspaceScope | None = None,
    safe_tools: Sequence[str] = (),
    read_tools: Sequence[str] = ("fs_read", "fs_list"),
    write_tools: Sequence[str] = ("fs_write",),
) -> PermissionPolicy:
    """装配一套默认规则(顺序不可乱)。

    顺序 = 安全语义:
      1. bash.hard_deny(危险命令)    -> DENY
      2. path.outside_workspace    -> DENY(先于读写规则!)
      3. path.forbidden_zone       -> DENY(界内禁区,同越界一样不可覆盖)
      4. path.read_allow           -> ALLOW(只读工具 + 界内,显式放行)
      5. path.write_allow          -> ALLOW(写工具 + 界内:2026-09-23 起放行,越界/禁区仍 DENY)
      6. bash.read_only_allow      -> ALLOW(只读命令免审批,见 READ_ONLY_COMMAND_PREFIXES)
      7. bash.requires_approval    -> ASK
      8. tool.allow_safe(可选)     -> ALLOW(免审批白名单,见 safe_tools)
      9. default.deny              -> 由 decide 兜底,无需显式规则

    read_tools / write_tools 由装配层(toolbox.py)声明本项目的读写工具
    集合,permissions 只提供通用框架——新增写工具不改这里,改装配层
    一处即可(去重:写/读集合不再散在多处硬编码)。默认值保证"不带任何
    参数"也能跑出 fail-closed 的教学默认。禁区由 scope.forbidden_parts
    表达(WorkspaceScope 持有),界内撞禁区和越界一样是 DENY——权限层
    预判在先,执行层 file_tools._resolve_safe 兜底在后,两层同一语义。

    safe_tools 是免审批白名单:只读/沙箱内工具的名字集合。不在名单里
    的新工具没有任何规则管它 -> default.deny,必须有人显式写规则才算
    "有了治理路径"——这就是 fail-closed 的落点。写工具永远不该进
    这份名单:改状态的能力只能走 ASK,由人点头。
    """

    DANGEROUS = {"sudo", "rm", "reboot", "shutdown", "dd"}
    read_set = frozenset(read_tools)
    write_set = frozenset(write_tools)
    path_tools = read_set | write_set

    def command_text(request: ToolRequest) -> str:
        """提取命令原文给 explain 用(matches 已保证形状,这里只防御)。"""

        if isinstance(request.arguments, dict):
            command = request.arguments.get("command")
            if isinstance(command, str):
                return command
        return "<命令不可读>"

    def path_text(request: ToolRequest) -> str:
        """提取路径原文给 explain 用,防御同上。"""

        if isinstance(request.arguments, dict):
            path = request.arguments.get("path")
            if isinstance(path, str):
                return path
        return "<路径不可读>"

    def path_arg(request: ToolRequest) -> str | None:
        """路径工具的参数形状:path 必须是字符串,否则 None(交给 default.deny)。"""

        if not isinstance(request.arguments, dict):
            return None
        path = request.arguments.get("path")
        return path if isinstance(path, str) else None

    def is_hard_deny(request: ToolRequest) -> bool:
        if request.name != "bash":
            return False
        if not isinstance(request.arguments, dict):
            return False
        command = request.arguments.get("command")
        if not isinstance(command, str):
            return False
        # 空命令没有"首词"。旧写法 `command.strip().split()[0]` 在这里会
        # IndexError——这条规则在 bash 工具存在之前永远不会被调用，所以
        # 一直没暴露（2026-09-17 上架 bash 时才发现）。空命令不是"危险"，
        # 交给后面的规则（is_bash 也不认它，最终落到 default.deny）。
        head = command.strip().split()
        return bool(head) and head[0] in DANGEROUS

    def is_read_only_bash(request: ToolRequest) -> bool:
        """这条 bash 命令是不是"常见的只读命令"（够格免审批）。

        保守优先：**拿不准就返回 False**，让它落回 `bash.requires_approval`
        去问人。误判成"要问"只是多一次点击；误判成"免问"可能是一条 rm。
        """

        command = command_text(request)
        if any(char in command for char in _SHELL_METACHARS):
            return False
        stripped = command.strip()
        for prefix in READ_ONLY_COMMAND_PREFIXES:
            if stripped == prefix or stripped.startswith(prefix + " "):
                return True
        return False

    def is_outside(request: ToolRequest) -> bool:
        """越界:路径工具 + 参数里的 path 解析后跑出工作区。"""

        if scope is None or request.name not in path_tools:
            return False
        path = path_arg(request)
        if path is None:
            return False
        try:
            scope.resolve(path)  # 没抛 = 在界内
        except PermissionError:
            return True  # 越界,命中
        return False

    def is_forbidden(request: ToolRequest) -> bool:
        """界内禁区:路径工具 + 参数里的 path 撞上 scope 的禁区段。"""

        if scope is None or request.name not in path_tools:
            return False
        path = path_arg(request)
        if path is None:
            return False
        return scope.has_forbidden_part(path)

    def is_read_inside(request: ToolRequest) -> bool:
        """命中的条件:只读工具 + 参数里的 path 能通过 scope(界内)。"""

        if scope is None or request.name not in read_set:
            return False
        path = path_arg(request)
        if path is None:
            return False
        try:
            scope.resolve(path)  # 能解析 = 在界内
        except PermissionError:
            return False  # 越界 -> 不命中 read_allow(留给 outside 规则 DENY)
        return True

    def is_write_inside(request: ToolRequest) -> bool:
        """写路径工具(装配层声明):界内写 -> ASK。这是 Harness 里唯一的
        改状态能力;"越界写"在这里返回 False,交给排在前面的 outside 规则
        硬拒,轮不到审批——顺序即安全语义。"""

        if scope is None or request.name not in write_set:
            return False
        path = path_arg(request)
        if path is None:
            return False
        try:
            scope.resolve(path)
        except PermissionError:
            return False
        return True

    def is_bash(request: ToolRequest) -> bool:
        if not isinstance(request.arguments, dict):
            return False  # 参数都不是对象，谈不上"平凡 bash"，交给 default.deny
        command = request.arguments.get("command")
        # 空命令不认：它不是"要执行的命令"，走 ASK 只会白打扰用户一次
        # （审批弹出来才发现没内容）。不命中任何规则 → default.deny。
        if not isinstance(command, str) or not command.strip():
            return False
        return request.name == "bash" and not is_hard_deny(request)

    rules = [
        PermissionRule(
            "bash.hard_deny", PermissionAction.DENY, is_hard_deny,
            lambda r: f"命令 {command_text(r)!r} 属于危险集,硬性拒绝",
        ),
        PermissionRule(
            "path.outside_workspace", PermissionAction.DENY, is_outside,
            lambda r: f"路径 {path_text(r)!r} 越出工作区,拒绝",
        ),
        PermissionRule(
            "path.forbidden_zone", PermissionAction.DENY, is_forbidden,
            lambda r: f"路径 {path_text(r)!r} 撞上禁区,拒绝",
        ),
        PermissionRule(
            "path.read_allow", PermissionAction.ALLOW, is_read_inside,
            lambda r: "只读文件操作且在工作区内,免审批放行",
        ),
        PermissionRule(
            # 2026-09-23：**界内的写操作不再问人**（用户决定）。
            #
            # 为什么敢放：沙箱边界已经由前面两条 DENY 守住了——
            # `path.outside_workspace`（越界）和 `path.forbidden_zone`（界内禁区）
            # 都在它之前，所以放行的只是"工作区内的普通文件"。而"改工作区里的
            # 文件"本来就是 agent 该干的活。
            #
            # 为什么该放：审批**阻塞唯一的 RPC 通道**（shell.py 的 _rpc_lock），
            # 一个"给项目加个后端"的任务要写 5 个文件 → 问 5 次 + 每次没人点
            # 就堵 60 秒。更要命的是**权限疲劳**：被问太多次的人会不读就点，
            # 那比不问更危险（Claude Code 源码分析的结论）。
            #
            # 仍然守住的（一条都没松）：
            #   - 越界 / 禁区        → DENY（不可审批覆盖）
            #   - bash 硬拒前缀      → DENY（sudo / rm / reboot…）
            #   - bash 非只读命令    → ASK（bash 没有路径约束，不能按文件放行）
            "path.write_allow", PermissionAction.ALLOW, is_write_inside,
            lambda r: f"写入 {path_text(r)!r}（工作区内），放行",
        ),
        PermissionRule(
            # 只读命令先放行（2026-09-23）：它必须排在 requires_approval **之前**，
            # 否则所有 bash 都会先撞上 ASK。硬拒规则在更前面——顺序即安全语义。
            "bash.read_only_allow", PermissionAction.ALLOW, is_read_only_bash,
            lambda r: f"只读命令 {command_text(r)!r} 免审批放行",
        ),
        PermissionRule(
            "bash.requires_approval", PermissionAction.ASK, is_bash,
            lambda r: f"命令 {command_text(r)!r} 需人工审批",
        ),
    ]

    if safe_tools:
        allowed = frozenset(safe_tools)  # 冻结:闭包持有不可变集合
        rules.append(PermissionRule(
            "tool.allow_safe", PermissionAction.ALLOW,
            lambda r: r.name in allowed,
            lambda r: f"{r.name} 在免审批白名单内,显式放行",
        ))

    return PermissionPolicy(rules)


# ----------------------------------------------------------------------
# 5. 执行层(B 批):GovernedToolRunner + AuditTrail
#    决策层的消费者——真正决定"执行 or 拦截"
# ----------------------------------------------------------------------

class ToolExecutionStatus(Enum):
    """一次治理执行的三态结局。"""

    BLOCKED = "blocked"      # 没碰 handler:决策 deny 或用户拒绝或参数毒
    SUCCEEDED = "succeeded"  # 调了 handler,正常返回
    FAILED = "failed"        # 调了 handler,但它抛了


@dataclass(frozen=True)
class ToolExecutionResult:
    """执行阶段统一的结果,+ to_protocol_block 编码成 agent 可回的文本。"""

    request: ToolRequest
    status: ToolExecutionStatus
    output: str  # 给模型看的结果文案(BLOCKED 时是拒绝原因)

    def to_protocol_block(self) -> str:
        """把结果转成 agent loop 回灌的文本(只加前缀,不造内容)。

        分工:rule_id + reason 由 runner(run)在构造结果前拼进
        self.output;这里只做包装,不重新拼 rule_id——rule_id
        只能有一个真值,就是 runner 放进 output 的那份。

        契约:
          - BLOCKED   -> "Error [permission_blocked]: " + self.output
          - SUCCEEDED -> self.output(原样透传)
          - FAILED    -> "Error [execution_error]: " + self.output
        """

        if self.status is ToolExecutionStatus.SUCCEEDED:
            return self.output
        if self.status is ToolExecutionStatus.BLOCKED:
            return f"Error [permission_blocked]: {self.output}"
        return f"Error [execution_error]: {self.output}"


class AuditTrail:
    """内存审计:每次治理调用记三笔账,可复盘"为什么/批没批/结果如何"。

    records 形如:
      {"kind": "request",    "tool": "read_file", "args": {..}}
      {"kind": "permission", "tool": "read_file", "rule": "...", "outcome": "not_required"}
      {"kind": "result",     "tool": "read_file", "status": "succeeded"}
    """

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, kind: str, **data: object) -> None:
        """记一笔账,统一带 kind + tool 字段便于归类。"""

        data.setdefault("kind", kind)
        self.records.append(data)

    def by_kind(self, kind: str) -> list[dict]:
        """按 kind 过滤已记账目(测试断言用)。"""

        return [r for r in self.records if r.get("kind") == kind]


class GovernedToolRunner:
    """带权限的执行器:decide -> resolve -> 拦截 or 执行 -> 记账。

    policy / approver / registry / audit 全部构造器注入——
    不 import 任何具体实现,和 A 批的 approver 同一哲学。
    """

    def __init__(
        self,
        policy: PermissionPolicy,
        approver: Approver,
        registry: Any | None = None,   # 执行 handler 的地方;None 则只决策不执行
        audit: AuditTrail | None = None,
    ) -> None:
        self._policy = policy
        self._approver = approver
        self._registry = registry
        self._audit = audit

    def _log(self, kind: str, **data: object) -> None:
        if self._audit is not None:
            self._audit.record(kind, **data)

    def run(self, request: ToolRequest) -> ToolExecutionResult:
        """一条固定的治理流水线(顺序不可换):

          1. 审计 request(带 call_id 便于按调用关联三笔账)
          2. policy.decide -> PermissionDecision
          3. resolve_permission -> PermissionResolution(ASK 才请老板)
          4. 审计 permission(带 rule/outcome)
          5. 被拦(allowed=False):审计 result(BLOCKED)+ 返回,不碰 handler
          6. 放行:registry 缺失 -> FAILED;参数不是 dict -> BLOCKED;
             调 registry.execute 成功 -> SUCCEEDED,异常 -> FAILED
          7. 每个出口都记 result 审计——"记满三笔"不只对被拦有效,
             FAILED / 参数毒 BLOCKED 同样是决策轨迹,不能从复盘里消失
        """

        # ① 审计预检
        self._log(
            "request", tool=request.name,
            call_id=request.tool_use_id, args=request.arguments,
        )
        # ② 决策 -> ③ 决议(ASK 才请老板)
        decision = self._policy.decide(request)
        resolution = resolve_permission(decision, self._approver)
        # ④ 审计:哪条规则 + 什么结局
        self._log(
            "permission",
            tool=request.name,
            call_id=request.tool_use_id,
            rule=decision.rule_id,
            outcome=resolution.approval_status,
        )
        # ⑤ 被拦:不碰 handler,审计也要记
        if not resolution.allowed:
            self._log(
                "result", tool=request.name,
                call_id=request.tool_use_id, status="blocked",
            )
            return ToolExecutionResult(
                request,
                ToolExecutionStatus.BLOCKED,
                f"{decision.rule_id}: {decision.reason}",
            )
        # ⑥ 放行执行:registry 缺失 / 参数毒 / handler 异常,三个兜底。
        #    每个出口都记 result 审计(带 error 归因)。
        if self._registry is None:
            self._log(
                "result", tool=request.name,
                call_id=request.tool_use_id, status="failed",
                error="runner 没有绑定 registry",
            )
            return ToolExecutionResult(
                request, ToolExecutionStatus.FAILED,
                "runner 没有绑定 registry,无法执行工具",
            )
        if not isinstance(request.arguments, dict):
            self._log(
                "result", tool=request.name,
                call_id=request.tool_use_id, status="blocked",
                error="arguments 不是对象",
            )
            return ToolExecutionResult(
                request, ToolExecutionStatus.BLOCKED,
                "arguments 不是对象,无法安全执行",
            )
        try:
            content = self._registry.execute(
                request.name, **dict(request.arguments)
            )
        except Exception as exc:
            self._log(
                "result", tool=request.name,
                call_id=request.tool_use_id, status="failed",
                error=str(exc),
            )
            return ToolExecutionResult(
                request, ToolExecutionStatus.FAILED, str(exc)
            )
        # ⑦ 审计 + 成功返回
        self._log(
            "result", tool=request.name,
            call_id=request.tool_use_id, status="succeeded",
        )
        return ToolExecutionResult(
            request, ToolExecutionStatus.SUCCEEDED, str(content)
        )
