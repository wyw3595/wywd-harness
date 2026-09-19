"""把工具、模型、记忆策略组装成 agent 的运行环境。

新增工具只改这个文件 + 实现模块：
  - 普通工具：写个函数（放 file_tools / std_tools），加进
    build_instant_tools（与目录有关的）或直接挂 handler（无关的）。
  - 延迟工具：Tool(...) 时 defer=True 放进 build_deferred_tools——schema
    不发给模型，等它 ToolSearch 发现后才进会话（s03 省 token）。
  - 桥接工具：ToolSearch / DeferExecuteTool 是 harness 内建协作件，
    handler 用闭包绑 registry，input_schema 必须手写（lambda 反射
    不出 list/dict 类型）。

工作区（2026-09-13）：沙箱根从全局常量升格为会话状态。装配入口是
build_registry_for(workspace)——按上传目录现做一整套工具（闭包绑定
root）；build_registry() / build_policy() 只是"默认工作区 = 项目根"的
兼容包装，老入口（chat / shell / electron）零改动。安全要点：闭包
签名必须**吃掉** root 参数——tool_to_schema 反射 handler 签名的每个
参数，root 若出现在签名里，模型就能在 schema 里看见它、传任意路径
绕沙箱。参数说明书靠继承实现函数的原件（__doc__ 赋值）——原件的
Args 段里没有 root（file_tools 的规矩），继承也不会泄露。

两个工厂（build_instant_tools / build_deferred_tools）的参数名刻意
叫 root_（带下划线）：内层闭包 fs_find / tree_dir 自己有个参数 root
（**搜索起点**，模型可见）。两个"root"是不同概念，名字撞上会互相
遮蔽——内层看不到外层的 root，沙箱根就丢了。下划线一劳永逸。

超 WorkBuddy 的日常工具箱（实现都在 src/harness/std_tools.py）：
  calc —— 安全数学求值（ast 白名单）；
  find_text —— 项目内正则搜索（复用沙箱）；
  tree_dir —— 目录树（低频、长 schema，正好当延迟加载的演示对象）。
"""

from pathlib import Path

from src.harness.file_tools import (
    ALLOWED_ROOT,
    FORBIDDEN_PARTS,
    edit_file,
    list_dir,
    read_file,
    write_file,
)
from src.harness.model_router import ModelRouter, ModelTier
from src.harness.models import FakeModel
from src.harness.permissions import (
    PermissionPolicy,
    WorkspaceScope,
    build_default_policy,
)
from src.harness.real_model import RealModel
from src.harness.std_tools import (
    calc,
    find_text,
    glob_files,
    now,
    run_bash,
    tree_dir,
)
from src.harness.tools import Tool, ToolRegistry
from src.harness.user_memory import (
    NO_USER_MEMORY_PLACEHOLDER,
    UserMemory,
    UserMemoryError,
)
from src.harness.workspace import Workspace
from src.harness.workspace_memory import (
    NO_MEMORY_PLACEHOLDER,
    FactKind,
    WorkspaceMemory,
)

import getpass
import os

# 历史窗口大小（练习 19）：按"条数"计（一条 = 一条消息，一轮工具往返
# 约占 3 条）。数字越小越省钱、记忆越短——这是取舍题，不是优化题。
# 记忆策略归应用层（练习 10 铁律），harness 保持中立；两个入口共用。
MAX_HISTORY_MESSAGES = 20

# 单轮对话的步数上限：一次 model.generate 算一步，所以它直接决定
# "一个任务最多能做几次工具往返"。
#
# 为什么从 5 提到 30（2026-09-16）：5 是 run_agent 的**签名默认值**，本意
# 只是"循环要有个保险丝"；但装配层一直没显式传它，于是这个安全兜底悄悄
# 变成了真实的业务上限——一个需要 6 轮的任务必然以 status="max_steps"
# 收场（对照实验：.workbuddy/scratch/probe_step_limit.py）。
#
# 30 这个数怎么来的：同类框架的默认量级（2026-09 查证）——
#   OpenAI Agents SDK  max_turns = 10（超限抛 MaxTurnsExceeded）
#   LangGraph          recursion_limit = 25
#   CrewAI             max_iter = 25
#   smolagents         max_steps = 20（到顶时**强制给最终答案**）
#   Vercel AI SDK      stepCountIs(20)
# 注意单位不可直接搬：LangGraph 数的是 super-step（一个 super-step 里可以
# 有多个并行节点），我们数的是"一次模型调用"。取 30 略高于它们，因为我们
# 的工具粒度更细（一次调用只做一件事，读一个文件也算一步）。
#
# 调参方法（业界共识，别凭感觉）：跑一批真实任务，统计完成所需轮数的分布，
# 取 P95 + 20% 余量。截断率 > 5% 说明上限偏低，或者模型该停的时候不停。
MAX_AGENT_STEPS_DEFAULT = 30


def resolve_max_agent_steps() -> int:
    """读环境变量 WYWD_MAX_STEPS；没设 / 非法 / 非正数 → 默认 30。

    与 WYWD_IDLE_REAP_SECONDS 同一套约定（见 shell._idle_reap_seconds）：
    配置读不对就**喊一声再退回默认**——静默吞掉错配置比直接报错更难查，
    而且这里退回默认比退回 0 安全（0 会让每一轮都立刻熔断）。
    """

    raw = (os.environ.get("WYWD_MAX_STEPS") or "").strip()
    if not raw:
        return MAX_AGENT_STEPS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        print(f"[toolbox] WYWD_MAX_STEPS 不是整数（{raw!r}），"
              f"用默认 {MAX_AGENT_STEPS_DEFAULT}")
        return MAX_AGENT_STEPS_DEFAULT
    if value <= 0:
        print(f"[toolbox] WYWD_MAX_STEPS 必须为正数（{value}），"
              f"用默认 {MAX_AGENT_STEPS_DEFAULT}")
        return MAX_AGENT_STEPS_DEFAULT
    return value


def bash_enabled() -> bool:
    """是否注册 bash 工具。`WYWD_DISABLE_BASH=1/true/yes/on` → 不注册。

    **默认启用**，理由：它的安全不靠"关掉"，靠"每次执行都要审批"
    （权限层的 `bash.requires_approval` → ASK）；一个开了审批闸门却默认不
    上架的工具，等于白做。这个开关是给"我压根不想让模型碰 shell"的场景
    准备的逃生阀，不是默认状态。

    取舍的另一面：bash 是本项目唯一**跑得出沙箱**的能力（fs_* 的路径会被
    `_resolve_safe` 拦住，命令拦不住）。所以关掉它是合理的保守选择，
    只是不该替所有用户做这个决定。
    """

    raw = (os.environ.get("WYWD_DISABLE_BASH") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def user_memory_root() -> Path:
    """用户记忆的根目录——**跨项目**，所以不放在项目的 `.workbuddy/` 下。

    默认 `~/.workbuddy/user-memory`。`WYWD_USER_MEMORY_ROOT` 可覆盖：
    测试要用临时目录（绝不能往真实用户目录写），多人共用一台机器时也可能
    想各放各的。
    """

    override = (os.environ.get("WYWD_USER_MEMORY_ROOT") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".workbuddy" / "user-memory"


def current_user_id() -> str:
    """当前用户标识。`WYWD_USER_ID` 优先，否则用系统登录名。

    为什么不强制先配环境变量：s11 的价值是"开箱就跨项目记住偏好"，
    要求先设一个变量才能用，等于把门槛设在门口。
    """

    override = (os.environ.get("WYWD_USER_ID") or "").strip()
    return override or getpass.getuser()


def build_user_memory() -> UserMemory:
    """按当前配置造一个 UserMemory（轻对象，只存路径，每次现造没成本）。"""

    return UserMemory(user_memory_root(), current_user_id())


def get_weather(city: str) -> str:
    """查询一个城市今天的天气。

    Args:
        city: 城市名，中文或拼音均可，如 "北京" 或 "beijing"。

    注意：本工具返回的是**演示用模拟数据**（"下紫色雪花"），不代表真实
    天气——只供冒烟验证工具往返（真实值独有的"紫色雪花"可当真伪判别），
    模型必须知道数据是假的，才不会拿它当真去回答用户。
    """

    return f"{city}今天下紫色雪花，气温零下 42 度。"


def build_instant_tools(root_: Path) -> list[Tool]:
    """按工作区根现做一套即时工具（高频、短 schema，直接进模型上下文）。

    与目录无关的工具（天气/时间/计算）不闭包，直接挂原函数——没有
    状态就不该有绑定，读代码的人一眼能看出谁依赖沙箱。

    每个闭包做两件事，缺一不可：
      1. 签名只保留模型该看见的参数——root_ 被闭包**吃掉**。
         tool_to_schema 反射签名生成 schema，沙箱根一旦入签名，模型
         就能传任意路径绕沙箱（这是本函数存在的安全理由）；
      2. docstring 继承实现函数的原件（__doc__ 赋值）——参数说明书
         只有一份，不写副本。原件 Args 段里没有沙箱根参数，所以
         继承也不会泄露。
    """

    def fs_list(path: str = ".") -> str:
        return list_dir(path, root=root_)
    fs_list.__doc__ = list_dir.__doc__

    def fs_read(path: str, offset: int = 1, limit: int = 200) -> str:
        return read_file(path, offset, limit, root=root_)
    fs_read.__doc__ = read_file.__doc__

    def fs_write(path: str, text: str) -> str:
        return write_file(path, text, root=root_)
    fs_write.__doc__ = write_file.__doc__

    def fs_edit(path: str, old_text: str, new_text: str,
                replace_all: bool = False) -> str:
        return edit_file(path, old_text, new_text, replace_all, root=root_)
    fs_edit.__doc__ = edit_file.__doc__

    def fs_find(pattern: str, root: str = ".", max_results: int = 8,
                case_sensitive: bool = False) -> str:
        # 内层的 root 是**搜索起点**（模型可见参数，相对沙箱根）；
        # 沙箱根本身由外层的 root_ 捕获——两个"root"是不同概念。
        return find_text(pattern, root, max_results, case_sensitive,
                         sandbox_root=root_)
    fs_find.__doc__ = find_text.__doc__

    def fs_glob(pattern: str, root: str = ".", max_results: int = 50) -> str:
        # root 同 fs_find：搜索起点；沙箱根在 root_。
        return glob_files(pattern, root, max_results, sandbox_root=root_)
    fs_glob.__doc__ = glob_files.__doc__

    tools = [
        Tool(
            name="get_weather",
            description="查询一个城市今天的天气（演示用模拟数据，不代表真实天气）",
            handler=get_weather,
        ),
        Tool(
            name="now",
            description="获取当前日期和时间（本地时区），返回格式 YYYY-MM-DD HH:MM",
            handler=now,
        ),
        Tool(
            name="fs_list",
            description="列出项目里某个目录的内容（path 是相对项目根的路径，默认 . ）",
            handler=fs_list,
        ),
        Tool(
            name="fs_read",
            description="读取项目里某个文本文件（path 相对项目根），返回带行号的正文；"
            "大文件用 offset/limit 翻页（默认从第 1 行起、最多 200 行）",
            handler=fs_read,
        ),
        Tool(
            name="fs_write",
            description="写入或覆盖项目里某个文本文件（path 相对项目根，整文件覆盖；"
            "需用户审批后才会真正执行）",
            handler=fs_write,
        ),
        Tool(
            name="fs_edit",
            description="修改项目里已有的文本文件（只传改动片段，适合改大文件）："
            "old_text 必须与原文逐字相同且默认要求在文件里唯一；"
            "多次出现要么补上下文要么 replace_all=true。同样需用户审批",
            handler=fs_edit,
        ),
        Tool(
            name="calc",
            description="安全计算数学表达式，如 (1 + 2) * 3 或 sqrt(16) * 2",
            handler=calc,
        ),
        Tool(
            name="fs_find",
            description="在项目里按正则搜索文件内容（返回 文件:行号:行内容 命中）。"
            "自动跳过 .venv/__pycache__/learn-workbuddy 等无关目录；"
            "要搜这些目录就把它们作为 root 显式传进来",
            handler=fs_find,
        ),
        Tool(
            name="fs_glob",
            description="按文件名或路径模式找文件（glob，如 **/*.py、src/**/test_*.py、"
            "*.md）——知道文件叫什么就用它，不要拿 fs_find 去猜内容。"
            "同样自动跳过无关目录；要翻这些目录就把它们作为 root 显式传进来",
            handler=fs_glob,
        ),
    ]

    # bash（2026-09-17）：唯一"跑得出沙箱"的能力，所以单独条件注册。
    # 工具名必须**逐字叫 bash**：权限层那两条规则（bash.hard_deny /
    # bash.requires_approval）是按名字匹配的，换个名字就落进 default.deny
    # （fail-closed，安全但用不了）。
    if bash_enabled():
        def bash(command: str, timeout: int = 60) -> str:
            # 与 fs_* 同一套规矩：root_ 被闭包吃掉，不进签名=不进 schema。
            # 沙箱根在这里只是"工作目录"，挡不住命令自己 cd 出去——真正的
            # 闸门是权限层那条 ASK（每次执行都要人点头）。
            return run_bash(command, timeout, sandbox_root=root_)
        bash.__doc__ = run_bash.__doc__

        tools.append(Tool(
            name="bash",
            description="在项目目录里执行一条 shell 命令（工作目录固定为项目根，"
            "默认超时 60 秒，输出上限 8000 字符）。"
            "每次执行都需要用户审批；sudo/rm/reboot/shutdown/dd 开头的命令会被"
            "直接拒绝且不可审批覆盖。适合跑测试、看 git 状态、装依赖之前先确认。",
            handler=bash,
        ))

    return tools


def write_memory_fact(content: str, kind: str = "outcome",
                      importance: int = 3, root=None) -> str:
    """memory_write 的 handler 本体（root 可注入——测试喂 tmp 目录）。

    写路径的诚实设计：工具只能**追加**原始事实。要不要记住一辈子由蒸馏
    策略说了算——"模型说重要就永久保存"是记忆被提示注入污染的正门
    （教材"常见误区"第二条）。

    所以返回值也刻意说"等待蒸馏策略裁决"，而不是"已记住"：模型读到的
    应该是"我记下来了，但能不能留下来不归我管"。措辞在这里是安全设计
    的一部分——说"已永久记住"会让模型以为它可以操纵长期记忆。
    """

    memory = WorkspaceMemory(root if root is not None else ALLOWED_ROOT)
    fact = memory.append_daily_log(
        content, kind=kind, importance=importance, source="agent")
    return (f"已记录 [{fact.kind}] {fact.content[:60]}"
            f"（{fact.recorded_at[:10]} 日志，等待蒸馏策略裁决）")


def build_deferred_tools(root_: Path) -> list[Tool]:
    """按工作区根现做一套延迟工具（低频、长 schema）。

    schema 不发给模型；模型拿到的是目录里的一行摘要，据用途调
    ToolSearch 发现后，才在本会话加载完整 schema。延迟的原因与
    root 无关，绑定的规矩与即时工具相同（root_ 被闭包吃掉）。
    """

    def tree_dir_tool(root: str = ".", depth: int = 2,
                      include_hidden: bool = False) -> str:
        # 内层的 root 是渲染起点（模型可见）；沙箱根在 root_。
        return tree_dir(root, depth, include_hidden, sandbox_root=root_)
    tree_dir_tool.__doc__ = tree_dir.__doc__

    def memory_write(content: str, kind: str = "outcome",
                     importance: int = 3) -> str:
        # 记忆跟着工作区走：上传目录的会话，日志写进上传目录的
        # .workbuddy/——会话产物（含记忆）整目录带走，不留在本项目。
        return write_memory_fact(content, kind, importance, root=root_)

    # ---- 用户记忆（s11）：跨项目的那一份，与上面那条（跟着工作区走）并列 ----

    def save_user_preference(key: str, value: str,
                             expires_at: str = "") -> str:
        """记下一条跨项目的长期偏好。

        Args:
            key: 偏好键，小写并用 . - _ 分段，如 response.language、editor.tab_size。
            value: 这条偏好当前的值。
            expires_at: 可选，ISO 时间戳且**必须带时区**；不填表示长期有效。
        """

        try:
            written = build_user_memory().set_preference(
                key, value, source="model_tool",
                expires_at=expires_at.strip() or None)
        except UserMemoryError as error:
            return f"没记下来：{error}"
        return f"已记住：{written.render()}"

    def update_user_profile(name: str = "", call_them: str = "",
                            timezone: str = "", notes: str = "") -> str:
        """更新用户资料——**只改传进来的字段**，留空表示不动。

        Args:
            name: 用户的名字。
            call_them: 希望怎么称呼他。
            timezone: 时区，如 UTC+8。
            notes: 其他值得长期记住的用户信息。
        """

        patch = {field: raw for field, raw in
                 (("name", name), ("call_them", call_them),
                  ("timezone", timezone), ("notes", notes))
                 if raw and raw.strip()}
        if not patch:
            return "没给任何字段，什么都没改。"
        try:
            result = build_user_memory().update_profile(patch)
        except UserMemoryError as error:
            return f"没改成功：{error}"
        return f"已更新用户资料：{result.render()}"

    return [
        Tool(
            name="save_user_preference",
            description="记下一条**跨项目**长期有效的偏好（如 response.language、"
            "editor.tab_size）。只在用户明确说「以后都这样」时用；"
            "同一个 key 再写一次是更新而不是新增。"
            "写进去的内容下一轮就会出现在你的系统提示里。",
            handler=save_user_preference,
            defer=True,
        ),
        Tool(
            name="update_user_profile",
            description="更新用户资料：名字 / 怎么称呼 / 时区 / 备注。"
            "只改传进来的字段，留空表示不动。",
            handler=update_user_profile,
            defer=True,
        ),
        Tool(
            name="tree_dir",
            description="渲染一棵目录树，看清项目结构（低频、参数多）",
            handler=tree_dir_tool,
            defer=True,
        ),
        Tool(
            name="memory_write",
            description="往项目工作区记忆追加一条事实（决策/约定/坑/结果）。"
            "只写原始日志，是否晋升长期记忆由 30 天蒸馏策略决定——"
            "不要记寒暄、猜测、密钥或原始工具输出。",
            handler=memory_write,
            defer=True,
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string",
                                "description": "一句话的项目事实，如'存储层确定用 SQLite WAL 模式'"},
                    "kind": {"type": "string",
                             "enum": ["decision", "convention", "pitfall", "outcome"],
                             "description": "事实类型：决策/约定/坑/结果"},
                    "importance": {"type": "integer",
                                   "description": "重要度 1-5，默认 3；>=4 才可能提前晋升"},
                },
                "required": ["content"],
            },
        ),
    ]


# 默认工作区：项目根。所有老入口（chat / shell / electron）走的都是
# 它——"没有上传目录"和"上传了目录"只该差在 root 上，别的都不变。
DEFAULT_WORKSPACE = Workspace(workspace_id="default", root=ALLOWED_ROOT)

# 兼容出口：既有测试与 build_system_prompt 直接 import 这两个名字。它们
# 是"默认工作区的构建产物"——名字和描述静态，handler 绑定项目根，
# 与改造前的行为一致（见 tests/test_std_tools.py 对 DEFERRED_TOOLS 的
# 直接引用，删掉这个名字会炸一片）。
ALL_TOOLS: list[Tool] = build_instant_tools(ALLOWED_ROOT)
DEFERRED_TOOLS: list[Tool] = build_deferred_tools(ALLOWED_ROOT)


def build_history_seed(root=None, max_steps: int | None = None,
                       user_memory: "UserMemory | None" = None) -> list[dict]:
    """sidecar 会话的起步历史：工具目录 + 工作区记忆 + 用户记忆。

    三块**各自成一条 system**，不合并：所有权不同（项目 / 个人），
    分开注入才能在日志里一眼看出哪块是谁的；到 s15 做 Prompt 组装时，
    顺序与预算由那一层统一决定——现在先各就各位。

    两个刻意的取舍：

    - **工具目录永远第一条**：FakeModel"只读第一条消息"的老怪癖不受影响；
    - **每会话开局读一次**，不是每 turn：记忆在会话中途变化不会被察觉，
      换来的是一个固定的、可预期的起步成本（有界视图的教学取舍）。

    空记忆**不追加消息**：seed 与 s06.5 完全一致——"没记忆"和"有记忆"
    的区别只该是多出一条 system，不该改变别的。这也是为什么判空要用
    共享常量而不是就地写字符串字面量。
    """

    seed = with_system([], max_steps)

    workspace = WorkspaceMemory(root if root is not None else ALLOWED_ROOT)
    context = workspace.get_context_for_agent()
    if context and context != NO_MEMORY_PLACEHOLDER:
        seed.append({"role": "system", "content": context})

    # 用户记忆（s11）：跨项目的那一份。注意它**不接收 workspace path**——
    # 两个来源的存储与更新策略完全分开，只在这里并排出现一次。
    if user_memory is not None:
        user_context = user_memory.get_context_for_agent()
        if user_context and user_context != NO_USER_MEMORY_PLACEHOLDER:
            seed.append({"role": "system", "content": user_context})

    return seed

# 免审批白名单（练习 s04）：只读 / 沙箱内的工具显式放行。fs_write
# 刻意不在名单里——它由 path.write_ask 规则拦成 ASK，执行前必须人点头。
# 新工具默认 default.deny：必须有人把它加进某条规则才算"有了治理路径"。
# 注意：策略看见的是桥接工具 ToolSearch / DeferExecuteTool 本身，不是
# 延迟工具 tree_dir——延迟加载把执行藏在桥后面，策略管不到穿透后的
# 那一层（已知边界：tree_dir 只读 + 沙箱内，风险可接受）。
#
# bash（2026-09-17）**绝不能**加进这份名单：它是唯一跑得出沙箱的能力
# （fs_* 的路径被 _resolve_safe 拦住，命令拦不住）。它的治理路径是
# permissions 里那两条专属规则——bash.hard_deny（DENY）+ bash.requires_approval
# （ASK），而且规则顺序保证 ASK 先于白名单，即便有人误加也拦得住。
SAFE_TOOLS: frozenset[str] = frozenset({
    "get_weather", "now", "fs_list", "fs_read",
    "calc", "fs_find", "fs_glob", "ToolSearch", "DeferExecuteTool",
    "memory_write",   # 只追加 .memory/ 原始日志，晋升由蒸馏闸门管（s10）
    # 用户记忆（s11）同样免审批：它们只写自己的记忆文件，而且写进去的内容
    # **下一轮就出现在 system 里**，用户看得见——没有"悄悄发生"的空间。
    # 取舍的另一面：跨项目 + 跨会话的影响面确实比工作区记忆大；但"改一次
    # 偏好弹一次窗"会让这个功能根本没法用（偏好本来就是随手记的）。
    "save_user_preference", "update_user_profile",
})

# 读写工具集合（练习 s04 · 去重）：治理语义集中在装配层声明，permissions
# 只提供通用规则框架。新增写工具只改这里一处（加进 WRITE_TOOLS 并确认
# 不在 SAFE_TOOLS 里），build_policy 会把集合喂给 build_default_policy。
READ_TOOLS: frozenset[str] = frozenset({"fs_read", "fs_list"})
# fs_edit 和 fs_write 一样是改状态的工具，必须走 ASK；刻意不进 SAFE_TOOLS
# （免审批 = 直接放行，这是唯一会被误解成"方便"的坑）。它比 fs_write 更该
# 有人看一眼——改的是已存在的文件，写坏了丢的是原有内容。
WRITE_TOOLS: frozenset[str] = frozenset({"fs_write", "fs_edit"})


def build_policy_for(workspace: Workspace) -> PermissionPolicy:
    """按工作区装配权限策略：决策层跟着会话的沙箱根走。

    scope 用的是 workspace.root——决策层（执行前预判）和执行层
    （file_tools 的 _resolve_safe）认同**同一个**沙箱根，两层不说
    两家话。上传目录的会话：越界判定、禁区判定都以上传目录为界，
    .env/.git 的禁区规则对上传目录原样生效（上传包里若藏了 .env，
    密钥同样读不走）。
    """

    return build_default_policy(
        scope=WorkspaceScope(workspace.root, forbidden_parts=FORBIDDEN_PARTS),
        safe_tools=sorted(SAFE_TOOLS),
        read_tools=sorted(READ_TOOLS),
        write_tools=sorted(WRITE_TOOLS),
    )


def build_policy() -> PermissionPolicy:
    """装配本项目工具箱的权限策略：默认规则 + 沙箱作用域 + 白名单。

    默认工作区（项目根）的兼容包装——老入口无参调用，行为不变。
    """

    return build_policy_for(DEFAULT_WORKSPACE)


# 系统提示的正文。**分节写**，每一节对应一类"模型猜不到、但必须知道"的东西：
#
#   身份       —— 它是什么。一句话就够，不需要长篇大论。
#   怎么做事   —— 工作方式：先查再答、改前先读、能并行就并行、报错先读报错。
#   工具怎么用 —— 两类工具的机制。延迟工具必须先 ToolSearch 这一步**猜不到**。
#   边界       —— **会失败的事**：审批、硬拒、越界、Artifact 指针。
#   预算       —— 步数上限 + "接近上限时主动收尾"的约定。
#   说话方式   —— 结论先行、不编造。
#
# 刻意**不写**的东西，以及为什么：
#   - "要认真""要专业"这类：模型本来就会，写进去只是每轮白烧 token。
#     判据是「删掉它，行为会不会变」——不会变的就是废话。
#   - 工具的详细参数：那是 schema 的职责，重复一遍就是两处维护、迟早漂移。
#   - 用什么语言回答：那是**用户偏好**，归 s11 的用户记忆管——
#     硬编码在这里，换个人用就得改代码。
#
# 每一条约束都有出处（要么是真实踩过的坑，要么是代码里的硬边界），不是许愿。
# 这是本节最重要的一条：**提示词是契约，不是座右铭。**
SYSTEM_PROMPT_TEMPLATE = """你是运行在 wywd-harness 里的 Agent：能读代码、跑命令、改文件的执行者。

# 怎么做事
- **需要事实就查，不要猜。** 文件内容、目录结构、命令输出，先用工具拿到再下结论。
- **改文件之前先读它。** fs_edit 要求你给出原文片段——写不出来就说明还没读。
  这不是刁难，是防止你在错的位置动手。
- 能并行拿到的信息放在同一轮请求里（多个文件、多个搜索），不要一个一个来。
- **工具报错时先读错误信息。** 它通常已经说了原因和下一步
  （"路径不存在"会提示你先 fs_list 看看有什么）。
- 没做完就说没做完。把"看起来完成了"和"确实完成了"分开。

# 工具怎么用
工具分两类：
  - **即时工具**：直接可用，见你的工具列表。
  - **延迟工具**：schema 不在你的工具列表里。要用必须先调 ToolSearch 按名称或用途
    搜索、拿到完整 schema，再用 DeferExecuteTool 执行。
当前可用的延迟工具：
{deferred_directory}

# 边界（撞上会失败，不是建议）
- 文件操作都限制在**工作目录**内。越界路径会被拒绝，报错里会说清界限在哪。
- 写文件、改文件、执行命令都**需要用户审批**。被拒绝时不要重试同一个动作，
  换方法或者问用户。
- 以 sudo / rm / reboot / shutdown / dd 开头的命令会被**硬性拒绝**，审批也翻不了案。
- 工具输出过大时会被替换成"指针 + 预览"，正文落在磁盘上。看到以 `[Artifact: ...]`
  开头的内容，就说明你拿到的**不是全部**——需要细节时用 fs_read 按里面的路径读回来。

# 预算
这一轮最多 {limit} 步（一次模型调用算一步；一次调用里可以同时请求多个工具）。
据此规划：先做最关键的部分，不要重复已经做过的步骤；剩余步数不多时主动收尾——
说清已完成什么、还差什么、下一步该做什么，不要在中途无声停下。

# 说话方式
- 结论先行，不寒暄。不要复述用户已经知道的事（你的工具调用它看得见）。
- 不确定就说不确定，不要编造文件名、函数名或工具输出。
"""


def build_system_prompt(max_steps: int | None = None) -> str:
    """系统提示：会话开局常驻的身份 / 工作方式 / 工具目录 / 边界 / 预算。

    WorkBuddy 里延迟工具的目录是独立工件：模型在启动时必须看到
    "有哪些延迟工具、各是干什么的"（符号表），却看不到完整 schema
    （那要等 ToolSearch 发现后才按需加载）。目录放 system、由应用层
    注入 history 最前——常驻但便宜；而 ToolSearch 的描述保持干净，
    只管"按名/按词召回"。

    目录读模块级 DEFERRED_TOOLS（默认工作区的产物）：工具的名字和
    描述是静态的，不随工作区变化——变的只有 handler 绑定的沙箱根。

    步数预算（2026-09-16 新增）：业界把上限分成两种——**运行时强制**的
    ceiling（模型看不见，撞上就被硬截断，产出半截子结果）与**告知模型**的
    budget（模型据此自我规划：先做关键部分、接近上限时主动收尾）。只给
    前者不够，得把预算"讲出来"，它才可能优雅收尾而不是无声停顿。
    max_steps=None 时退回默认值，所以无参调用照旧可用。
    """

    limit = MAX_AGENT_STEPS_DEFAULT if max_steps is None else max_steps
    deferred_directory = "\n".join(
        f"  - {tool.name}: {tool.description}" for tool in DEFERRED_TOOLS
    )
    return SYSTEM_PROMPT_TEMPLATE.format(
        deferred_directory=deferred_directory,
        limit=limit,
    )


def with_system(history: list[dict] | None,
                max_steps: int | None = None) -> list[dict]:
    """把系统提示（工具目录 + 步数预算）放到会话最前，且幂等——不重复添加。

    对齐 s03 的目录设计：目录是独立工件，常驻模型上下文；历史截断
    可能把 system 切出窗口（窗口比消息少时），这里自动补回；若
    history 第一条已是 system（上一轮的 result.messages 带回来的），
    直接原样返回。谁也不用特判。

    max_steps 透传给 build_system_prompt（步数预算要写进 system）。
    幂等的代价：会话中途改 WYWD_MAX_STEPS，老会话的历史里那条旧 system
    不会更新——预算是"开局讲一次"的约定，不是每轮刷新，这个取舍可接受。

    chat.py / web_app.py / electron_shell.py 三个入口共用。
    """

    system_message = {"role": "system", "content": build_system_prompt(max_steps)}
    if not history:
        return [system_message]
    if history[0].get("role") == "system":
        return history
    return [system_message] + history

# 桥接工具的手写 schema（lambda 参数无类型注解，反射会给 string 兜底，
# 而 queries 明明是数组；形状与 tests/test_deferred_tools.py 一致）。
TOOL_SEARCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要检索的工具名称或用途关键词",
        },
        "top_k": {"type": "integer", "description": "最多返回几个命中，默认 3"},
    },
    "required": ["queries"],
}

DEFER_EXECUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "toolName": {"type": "string", "description": "已加载的延迟工具名"},
        "params": {"type": "object", "description": "传给该工具的参数字典"},
    },
    "required": ["toolName", "params"],
}


def build_registry_for(workspace: Workspace) -> ToolRegistry:
    """按工作区现做一整套注册表：即时 + 延迟 + 桥接，全绑定该 root。

    会话隔离的成立条件都收在这一个函数里：工具闭包吃沙箱根（两个
    build_*_tools 工厂）、桥接闭包捕获**本** registry 和**本**工作区
    的延迟工具名单——A 会话的注册表解析不到 B 会话的任何路径。
    """

    # 工厂参数的 root_ 命名规矩见模块 docstring——内层 fs_find /
    # tree_dir 的 root（搜索起点）不遮蔽它。
    instant = build_instant_tools(workspace.root)
    deferred = build_deferred_tools(workspace.root)

    registry = ToolRegistry()
    for tool in instant + deferred:
        registry.register(tool)

    # 桥接工具：handler 闭包绑 registry——注册表即边界，循环无需特判。
    # 陷阱：lambda 参数名必须与 input_schema 的键逐字一致（queries/top_k、
    # toolName/params）。校验读的是 schema，execute 做 **kwargs 展开——
    # 名字不一致就是 "unexpected keyword argument"，模型猜死循环。
    # 描述刻意保持干净：目录在 build_system_prompt()（system 常驻），
    # ToolSearch 只负责"按名/按词召回"，两者职责分离（对齐 s03）。

    # 延迟工具名单从**本工作区**的构建产物里取（而不是模块级
    # DEFERRED_TOOLS）：查询词的精确名判断必须与该会话实际注册的
    # 延迟工具一致，否则 A 会话会"命中"B 会话才有的工具名。
    deferred_names = {tool.name for tool in deferred}

    def search_tools(
            queries: list[str] | None = None, top_k: int | None = None
        ) -> str:
        """ToolSearch 的真实语义：逐词处理，精确优先、模糊兜底。

        模型传查询词不可预测：可能是精确名 ["tree_dir"]、名字混用途
        ["tree_dir 目录树"]、纯用途词 ["目录树"]——三种都必须命中。
        做法：词的精确值等于某个延迟工具名 → 走加载（load_by_name），
        否则按用途模糊搜（search，内部拆词命中描述）。
        注意 top_k 是 schema 里的可选参数，这里必须带默认值——execute
        只展开模型传了的键，可选参数不给默认就是 TypeError。
        """

        parts: list[str] = []
        for query in queries or []:
            if query in deferred_names:
                parts.append(registry.load_by_name([query]))
            else:
                parts.append(registry.search([query], top_k=top_k or 3))
        return "\n".join(parts) if parts else "（没有查询词）"

    registry.register(Tool(
        name="ToolSearch",
        description="搜索并加载延迟加载的工具的 schema（按名称或用途），"
        "返回找到的工具名、命中理由与完整 JSON schema。"
        "延迟工具不在普通工具列表里，要用它们必须先调我。",
        handler=search_tools,
        input_schema=TOOL_SEARCH_SCHEMA,
    ))
    registry.register(Tool(
        name="DeferExecuteTool",
        description="执行一个已加载的延迟工具（参数 schema 由 ToolSearch 提供）",
        handler=lambda toolName=None, params=None: registry.defer_execute(
            toolName, params or {}
        ),
        input_schema=DEFER_EXECUTE_SCHEMA,
    ))

    return registry


def build_registry() -> ToolRegistry:
    """注册全部工具（即时 + 延迟 + 两个桥接协作件）。

    默认工作区（项目根）的兼容包装：老入口与既有测试都无参调它，
    行为与改造前一致。
    """

    return build_registry_for(DEFAULT_WORKSPACE)


def build_model() -> RealModel:
    """带工具说明书的真实模型。

    由 registry 的 model_schemas() 出目录——即时工具全量，延迟工具
    用目录摘要代替。schema 生成只发生在 tool_to_schema 一处（单一真源）。
    """

    return RealModel(tools=build_registry().model_schemas())


def build_model_router() -> ModelRouter:
    """三级槽位装配（s08）：标签 → 具体模型的解析表。

    教学矩阵说明：无 key 三槽全 FakeModel（离线可跑），有 key 三槽全
    DeepSeek——同一个模型占三个槽看似没分级，但**槽位机制已经立住**：
    真实分级（lite 换便宜厂商 / craft 换旗舰）只改这张映射表，代码
    一行不动。这正是教材"标签路由"的意义：用户改配置换模型。
    """

    routes = {tier: (build_model() if os.getenv("DEEPSEEK_API_KEY")
                     else FakeModel()) for tier in ModelTier}
    return ModelRouter(routes=routes)
