"""标准工具集：能看时间、能算、能按内容搜、能按名字找、能看树、能立计划（全部只读 + 沙箱内）。

往这里加"通用能力"工具：now（当前时间）、calc（安全数学）、
find_text（按内容搜）、glob_files（按文件名找）、tree_dir（目录树）、
plan_write（任务计划，plan-and-execute 第 1 步）。
回归测试对应 tests/test_std_tools.py。

find_text 与 glob_files 是**正交**的两件事——前者回答"内容在哪"，后者回答
"文件叫什么"。缺一格，模型就会拿另一个硬凑（用内容去猜文件名，或者把
tree_dir 的输出当文件名单），既慢又容易漏。

安全原则沿袭 file_tools.py（能力 = 风险）：
  - calc：ast 白名单求值，不允许任何变量/属性/文件访问；
  - find_text / tree_dir：复用 file_tools 的沙箱解析（越界抛
    PermissionError），并跳过禁区（.env / .git）。

沙箱根参数化（2026-09-13，工作区支持）：find_text / glob_files /
tree_dir 多了一个 sandbox_root: Path | None = None——None 表示
"运行时读 file_tools.ALLOWED_ROOT"（哨兵模式，与 file_tools 的
root 参数同一条规矩，包括"不暴露给模型"）。名字刻意不叫 root：
这三个函数的 root 是**搜索起点**（模型可见的参数），和沙箱根是
两个不同的概念，撞名会把语义搅浑。

搜索的两套名单，语义必须分清（本模块最容易搞混的一处）：
  - FORBIDDEN_PARTS（.env / .git）是**安全**语义：必须拒，任何参数都绕不过；
  - IGNORED_DIRS 是**效率**语义：看了也没用（.venv / 缓存 / 第三方教材），
    跳过只是为了别把时间浪费在 1.5 万个无关文件上。也正因为它只是效率
    问题，**显式指定 root 时起点永远放行**——要搜教材就直接指过去。
"""

import ast
import math
import operator
import os
import re
import signal
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

# 沙箱根按"模块"引用，不按"值"导入。区别在测试里会现形：ALLOWED_ROOT
# 是要参与路径计算的可变配置（测试会 mock 它把沙箱换到 tmp 目录），
# 按值 import 会复制出一份永不更新的副本——mock 只对 file_tools 生效、
# 对这里失效，于是"换沙箱"的测试全炸。FORBIDDEN_PARTS 是常量、不参与
# 路径计算，按值导入没有这个问题。
from src.harness import file_tools
from src.harness.file_tools import (
    FORBIDDEN_PARTS,
    _is_probably_binary,
    _resolve_safe,
)


def now() -> str:
    """返回当前本地日期与时间，如 "2026-09-05 14:30"。

    Agent 的上下文里没有时钟——模型不知道"今天几号"，这个工具就是
    它的表。返回人类可读文本而非时间戳：模型能直接读懂、直接引用，
    不用先解析（"返回有意义上下文，给 agent 能直接行动的信息"的样板）。
    零状态纯函数，免审批。
    """

    return datetime.now().strftime("%Y-%m-%d %H:%M")


# plan_write 的状态前缀（GFM checkbox 习惯，模型零学习成本）：
# [x] 完成（大小写都认）  [>] 进行中  [!] 失败  无前缀 = 待办。
_PLAN_MARKERS = ("[x]", "[>]", "[!]")


def plan_write(tasks: list[str]) -> str:
    """整份重写任务计划：多步任务先列计划，做完一项或有变化就整份重发。

    Args:
        tasks: 计划全文，每项一条；可带状态前缀 [x] 已完成 / [>] 进行中 /
            [!] 失败，不带前缀 = 待办。支持 "- [x]" 的 markdown checkbox
            写法（前导 - / * 会被剥掉）。

    无状态设计（plan-as-tool）：计划不落在任何服务端存储里，**活在
    transcript 里**——每次调用返回规范化全文，最近一次的工具结果就是
    当前计划。跨会话天然隔离（不同会话各有各的历史），也绝不串台；
    已知边界：上下文压缩可能把旧计划吃掉，靠系统提示教模型"找不到
    就凭记忆重写一份"。数组参数的类型契约反射不出（TYPE_MAP 只有
    标量），schema 由装配层手写（桥接工具同款先例）。
    """

    if not isinstance(tasks, list):
        # validate_arguments 已按 schema 的 "array" 拦过一道；这里兜底
        # 防 handler 被直调（测试 / 将来的非模型调用方）。
        return "参数错误：tasks 必须是数组"
    if not tasks:
        return "计划不能为空：tasks 至少要有一项"
    items: list[tuple[str, str]] = []
    for index, raw in enumerate(tasks, start=1):
        if not isinstance(raw, str):
            return (f"第 {index} 项必须是字符串（收到 {type(raw).__name__}）"
                    "——每项一条纯文本，状态用前缀表达")
        text = re.sub(r"^[-*]\s+", "", raw.strip())
        if not text:
            return f"第 {index} 项是空的：写上内容，或把这项从计划里删掉"
        marker = "[ ]"
        for want in _PLAN_MARKERS:
            head = text[:3]
            if head == want or (want == "[x]" and head.lower() == "[x]"):
                marker = want
                text = text[3:].strip()
                break
        if not text:
            return (f"第 {index} 项只有状态前缀没有内容："
                    "前缀后面要写这条步骤在干什么")
        items.append((marker, text))

    done = sum(1 for marker, _ in items if marker == "[x]")
    header = f"【计划 {done}/{len(items)}"
    header += " · 全部完成】" if done == len(items) else "】"
    lines = [header]
    for index, (marker, text) in enumerate(items, start=1):
        lines.append(f"  {marker} {index}. {text}")
    return "\n".join(lines)


def calc(expression: str) -> str:
    """安全计算数学表达式，结果去掉多余尾零。

    Args:
        expression: 数学表达式，如 "(1 + 2) * 3" 或 "sqrt(16) * 2"；
            只允许数字、运算符、括号和常用数学函数。

    安全实现：先 ast.parse 拿到语法树，再在白名单节点上递归求值——
    没有名字查找、没有属性访问、没有内置函数，越界一律 ValueError。
    """

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"无法解析表达式：{exc}") from exc

    value = _eval_node(tree.body)
    formatted = f"{value:.4f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


# -- calc 的 ast 求值器（白名单） ------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_MATH_FUNCS = frozenset(
    {"abs", "round", "min", "max", "sqrt", "log", "log10",
     "sin", "cos", "tan", "ceil", "floor", "pow"}
)


def _eval_node(node: ast.AST) -> object:
    """在白名单节点上递归求值；遇到名单外的语法直接 ValueError。"""

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("只支持数值常量")
    if isinstance(node, ast.Name):
        if node.id in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[node.id]
        raise ValueError(f"不允许使用名字/变量：{node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](
            _eval_node(node.left), _eval_node(node.right)
        )
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand)
        raise ValueError("不支持的运算符")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in _MATH_FUNCS:
            raise ValueError(f"只允许白名单数学函数：{node.func.id}")
        args = [_eval_node(arg) for arg in node.args]
        return getattr(math, node.func.id)(*args)
    raise ValueError("表达式中包含不允许的语法")


# -- 搜索的"看了也没用"名单（效率语义，不是安全语义） ------------------

# 递归途中永久剪枝的目录名。与项目根 .gitignore 保持同一份意图
# （.venv / learn-workbuddy / __pycache__ / .workbuddy / .sessions 都在那），
# 但这里刻意再硬编码一份：某天 .gitignore 被改坏或删掉，搜索也不会
# 退化成全树扫描——真源可以共享，兜底不能共享。
# 用 frozenset 而不是 set：这是常量，"不可变"正好表达"不该被改"。
#
# 动机（实测）：不留神跳名单时，一次未命中的搜索要遍历 15448 个文件 /
# 225.7 MB（.venv 7157 + learn-workbuddy 8193 = 99%），耗时 42~54 秒；
# 收窄到约 100 个文件后回到毫秒级。
IGNORED_DIRS = frozenset({
    "__pycache__",                                   # 编译产物：二进制且与源码重复
    ".venv", "venv", "env", "node_modules",           # 依赖：几千个第三方文件
    "build", "dist", "htmlcov",                      # 构建产物
    ".pytest_cache", ".ruff_cache",                  # 测试/lint 缓存
    ".idea", ".vscode", ".trae",                     # 编辑器个人配置
    ".files", ".sessions", ".memory", ".workbuddy",  # 运行时数据
    "learn-workbuddy",                               # 第三方教材：只读参考，不是本项目源码
})

# 明显不是文本的文件后缀：连读都不用读。这张表只是"快速挡一层"，
# 真正的兜底是下面的 _is_probably_binary——后缀可以骗人，NUL 字节不会。
IGNORED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".pdf", ".zip", ".gz", ".whl", ".tar",
    ".exe", ".dll", ".so",
    ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".mov", ".wav",
    ".db", ".sqlite", ".xlsx", ".docx", ".pptx",
})


def _should_skip_file(filename: str) -> bool:
    """文件名后缀是否在忽略名单里。纯函数——测试不用碰文件系统。

    Path.suffix 只取最后一段后缀且带点（"a.tar.gz" -> ".gz"，
    "Makefile" -> ""）。Windows 上文件名大小写不固定，统一 lower()
    再比，否则 "PHOTO.PNG" 会漏网。
    """

    return Path(filename).suffix.lower() in IGNORED_SUFFIXES


def find_text(
    pattern: str,
    root: str = ".",
    max_results: int = 8,
    case_sensitive: bool = False,
    sandbox_root: Path | None = None,
) -> str:
    """在项目里按正则搜索文件内容，返回 文件:行号:行内容 命中列表。

    Args:
        pattern: 正则表达式，如 "def get_weather"。
        root: 相对项目根的搜索起点，默认项目根。
        max_results: 最多返回多少条命中（防撑爆上下文）。
        case_sensitive: 是否区分大小写，默认不区分。

    （sandbox_root 是内部沙箱根参数，由装配层注入，不暴露给模型。）

    跳过三类东西：
      1. 禁区（.git / .env）—— 安全语义，任何参数都绕不过（_resolve_safe 把关）；
      2. 忽略目录（IGNORED_DIRS：.venv / __pycache__ / learn-workbuddy 等）
         —— 效率语义，递归途中剪枝。**显式指定的起点永远放行**，
         所以要找教材内容时用 root="learn-workbuddy" 直接指过去；
      3. 二进制文件（后缀名单 + 内容 NUL 探测）—— 读进去只会是乱码。
    """

    # 沙箱根哨兵落地：None 时读全局（mock 沙箱的测试靠这一步），
    # 传了就以上传目录为界。命中的展示路径也以它为基准——模型拿到的
    # 相对路径可以直接喂回同一会话的 fs_read（同一套工具绑同一个根）。
    sandbox = sandbox_root if sandbox_root is not None else file_tools.ALLOWED_ROOT
    base = _resolve_safe(root, sandbox)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"无法解析正则：{exc}") from exc

    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        # 剪枝的关键写法：dirnames[:] = ... 是**切片赋值**，就地改写
        # walk 手里那个列表对象本身，所以削掉谁就不会走进谁。
        # 写成 dirnames = [...] 只是让本地名字指向新列表，walk 手里的
        # 老列表没变，剪枝完全失效——这两行的区别是本步的核心。
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in FORBIDDEN_PARTS and name not in IGNORED_DIRS
        )
        for filename in sorted(filenames):
            if filename in FORBIDDEN_PARTS or _should_skip_file(filename):
                continue
            file_path = Path(dirpath) / filename
            try:
                if file_path.stat().st_size > 1_000_000:
                    continue
                # 先读字节、再解码：中间插一道 NUL 探测，让二进制文件
                # 根本不进正则（旧写法直接 read_text，二进制会被
                # errors="replace" 静默变成一串 U+FFFD 乱码喂给模型）。
                data = file_path.read_bytes()
            except OSError:
                continue
            if _is_probably_binary(data):
                continue
            content = data.decode("utf-8", errors="replace")
            for lineno, raw_line in enumerate(content.splitlines(), start=1):
                if regex.search(raw_line):
                    relative = file_path.relative_to(sandbox)
                    hits.append(f"{relative}:{lineno}: {raw_line.strip()[:120]}")
                    if len(hits) >= max_results:
                        break
            if len(hits) >= max_results:
                break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"未找到匹配 {pattern!r}"
    suffix = f"\n…（已截断，超过 {max_results} 条）" if len(hits) >= max_results else ""
    return "\n".join(hits) + suffix


# -- 按文件名找（fs_glob）：和 find_text 是正交的两件事 ------------------
#
# find_text 回答"内容在哪"，glob_files 回答"文件叫什么"。缺了这一格，模型
# 只好拿内容去猜文件名（或者把 tree_dir 的输出当名单用），既慢又容易漏。
# 这一格补上，"读什么"的三种问法就齐了：按内容搜 / 按名字找 / 按位置读。


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 模式编译成正则。纯函数。

    支持三种通配：
      ?    匹配路径里的单个字符；
      *    匹配一段路径里任意多的字符，**但不跨 /**；
      **/  匹配"零层或多层目录"。

    最后一条是 glob 最反直觉的地方，也最要紧：正因为 **/ 允许零层，
    "**/*.py" 才能同时命中 "agent.py" 和 "src/harness/agent.py"。
    （pathlib 的 Path.glob、gitignore、ripgrep 都是这个语义。）

    为什么不直接借现成的：
      - fnmatch：它的 * 会跨 /，于是 "**/*.py" 反过来匹配不到根目录下的
        .py 文件——语义错了，而且错得很安静；
      - Path.glob：语义是对的，但它绕不过忽略名单，实测扫全树 1.76 秒，
        正是第一步刚修掉的那个毛病（同时 fs_find 只要 0.04 秒）。
    自己翻译这十几行，语义和性能就都在手里。
    """

    parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            if pattern.startswith("**/", index):
                # "**/" = 零个或多个"某目录/"——零层也能匹配的关键在这
                parts.append("(?:[^/]+/)*")
                index += 3
            else:
                # 裸 "**"（出现在模式末尾）= 任意多段
                parts.append(".*")
                index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            # re.escape：把 "." "+" 这类正则元字符当普通字符——不然
            # "test_*.py" 里的那个点会被当成"任意一个字符"，连 testXpy
            # 都会命中。
            parts.append(re.escape(pattern[index]))
            index += 1
    parts.append("$")
    # 不区分大小写：与 fs_find 的默认保持一致（Windows 文件系统本来也
    # 不区分），"**/*.PY" 也能找到 .py。
    return re.compile("".join(parts), re.IGNORECASE)


def glob_files(
    pattern: str,
    root: str = ".",
    max_results: int = 50,
    sandbox_root: Path | None = None,
) -> str:
    """按文件名或路径模式（glob）找文件，返回相对项目根的路径。

    Args:
        pattern: glob 模式，用 "/" 分隔，如 "**/*.py"、"src/**/test_*.py"、"*.md"。
        root: 相对项目根的搜索起点，默认项目根。
        max_results: 最多返回多少个文件，默认 50。

    （sandbox_root 是内部沙箱根参数，由装配层注入，不暴露给模型。）

    和 fs_find 的分工：**fs_find 按内容找，fs_glob 按名字找**——知道文件
    叫什么就用这个，比拿内容去猜快得多也准得多。

    pattern 按**相对 root** 解释（与 pathlib 的 Path.glob 一致）：所以
    glob_files("*.md", root="learn-workbuddy") 命中的是教材目录的第一层
    .md，而返回的路径始终以项目根为基准，方便直接喂给 fs_read。

    跳过忽略目录（.venv / __pycache__ / learn-workbuddy 等）；显式指定
    root 时起点永远放行（要翻教材就 root="learn-workbuddy"）。

    这里**不按后缀过滤二进制文件**，和 fs_find 不同——因为这里只看文件名、
    从不读内容，没有"读成一串乱码"的风险；模型要找 "**/*.png" 就该找到。
    """

    # 沙箱根哨兵落地：与 find_text 同一条规矩（见它的注释）。
    sandbox = sandbox_root if sandbox_root is not None else file_tools.ALLOWED_ROOT
    base = _resolve_safe(root, sandbox)
    # Windows 上习惯写反斜杠，先统一成正斜杠——模式与候选路径都是 / 分隔。
    expression = _glob_to_regex(pattern.replace("\\", "/"))
    hits: list[str] = []

    for dirpath, dirnames, filenames in os.walk(base):
        # 和 find_text 同一套剪枝：dirnames[:] 就地改写，削掉谁就不进谁。
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in FORBIDDEN_PARTS and name not in IGNORED_DIRS
        )
        for filename in sorted(filenames):
            if filename in FORBIDDEN_PARTS:
                continue
            full = Path(dirpath) / filename
            # 拿"相对 root"的路径去匹配（pattern 的坐标系），
            # 但报出去的是"相对项目根"的路径（调用方的坐标系）。
            # as_posix() 把 Windows 的 "\\" 换成正斜杠，否则模式里的 /
            # 永远对不上，全项目一个文件都匹配不到。
            if expression.match(full.relative_to(base).as_posix()):
                hits.append(full.relative_to(sandbox).as_posix())
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"没有文件名或路径匹配 {pattern!r} 的文件"
    if len(hits) >= max_results:
        return "\n".join(hits) + (
            f"\n…（已截断，超过 {max_results} 个；收窄模式或调大 max_results）"
        )
    return "\n".join(hits)


def tree_dir(
    root: str = ".",
    depth: int = 2,
    include_hidden: bool = False,
    sandbox_root: Path | None = None,
) -> str:
    """渲染一棵目录树（低频工具，天然适合做延迟加载演示）。

    Args:
        root: 相对项目根的起点，默认项目根。
        depth: 往下递归的层数上限，默认 2。
        include_hidden: 是否显示隐藏文件/目录（.git/.env 永不显示）。

    （sandbox_root 是内部沙箱根参数，由装配层注入，不暴露给模型。）
    """

    # 沙箱根哨兵落地：与 find_text 同一条规矩（见它的注释）。
    sandbox = sandbox_root if sandbox_root is not None else file_tools.ALLOWED_ROOT
    base = _resolve_safe(root, sandbox)
    lines: list[str] = [f"{root or '.'}/"]

    def walk(folder: Path, prefix: str, level: int) -> None:

        def key(item: Path) -> tuple[bool, str]:
            # 目录排前面，同级按名称忽略大小写排。
            return (not item.is_dir(), item.name.lower())

        entries = sorted(folder.iterdir(), key=key)
        visible = [
            item for item in entries
            if item.name not in FORBIDDEN_PARTS
            and (include_hidden or not item.name.startswith("."))
        ]
        for index, item in enumerate(visible):
            last = index == len(visible) - 1
            branch = "└── " if last else "├── "
            lines.append(f"{prefix}{branch}{item.name}" + ("/" if item.is_dir() else ""))
            if item.is_dir() and level < depth:
                walk(item, prefix + ("    " if last else "│   "), level + 1)

    walk(base, "", 1)
    return "\n".join(lines)


# -- 执行 shell 命令（bash 工具）：本项目唯一"跑得出沙箱"的能力 --------------
#
# 为什么它和别的工具不是一个风险等级：fs_* 的路径会被 _resolve_safe 拦在
# 沙箱内，而一条 shell 命令**拦不住**——`cd /` 之后想去哪去哪。所以这里不
# 假装它是沙箱，而是把边界写在明面上：
#
#   闸门①（真正的闸门，在权限层，不在本文件）
#     - `bash.hard_deny`：sudo / rm / reboot / shutdown / dd 开头 → DENY，审批也翻不了案
#     - `bash.requires_approval`：其余命令 → ASK，**每次执行都要用户点头**
#     这两条规则在 permissions.build_default_policy 里早就写好了，一直空着——
#     本段就是把那个预留位置填上，所以工具名必须**逐字叫 bash**，否则规则不命中，
#     而规则不命中就落到 default.deny（fail-closed，安全但用不了）。
#   闸门② WYWD_DISABLE_BASH=1 → 装配层根本不注册这个工具。
#
# 本函数自己只管三件事：能跑、不卡死、不喷爆上下文。
# 另注意"黑名单"的性质：它是**心理安全**，不是沙箱（`find . -delete` 就绕过
# rm 那条）。真正的保险是"每次都要人点头"。

BASH_TIMEOUT_DEFAULT = 60
BASH_TIMEOUT_MAX = 300
BASH_OUTPUT_LIMIT = 8000

# 按名字特征剥环境变量：`env` 这条命令本来能把 DEEPSEEK_API_KEY 原样打出来，
# 而工具输出是要回灌给模型的（还随 transcript 落盘）——等于把密钥送给模型。
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _clean_env() -> dict[str, str]:
    """继承当前环境，但剥掉看起来是密钥的变量。

    按名字特征过滤，不追求完备——能想到的都挡住，想不到的靠"每次都审批"
    兜底。宁可偶尔少给一个无关变量，也不能把密钥漏进上下文。
    """

    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in _SENSITIVE_ENV_MARKERS)
    }


def _decode_output(data: bytes) -> str:
    """按 utf-8 → gbk 的顺序尝试解码，最后兜底 replace。

    Windows 的 cmd 输出中文是 cp936（GBK），直接按 utf-8 解会抛异常；POSIX
    上反过来基本只会是 utf-8。所以顺序尝试。兜底用 errors="replace"：宁可
    显示成乱码，也不能因为编码问题让整个工具崩掉。
    """

    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clip_output(text: str) -> str:
    """裁掉尾部空白；超长时留头部并注明被截断。

    为什么不外部化到磁盘（教材 s13 那套）：那是下一步的事。这里先做一个
    诚实的止损——**明确告诉模型"被截断了，用更精确的命令重来"**，比默默
    丢一半输出好，也比把几十 MB 灌进上下文好。
    """

    text = text.rstrip()
    if not text:
        return "（无输出）"
    if len(text) <= BASH_OUTPUT_LIMIT:
        return text
    return (
        text[:BASH_OUTPUT_LIMIT]
        + f"\n…（输出被截断，原始共 {len(text)} 字符；"
          "请用更精确的命令缩小范围）"
    )


def _kill_process_tree(pid: int) -> None:
    """杀掉 pid 及其全部子孙进程（失败吞掉——杀树不该炸工具本身）。

    为什么必须杀"树"而不能只杀直接子进程：shell=True 时 Popen 的 pid 是
    cmd/sh 外壳，真正的活（npm install、dev server、node -e）是它的
    **子孙**。只杀外壳 = 杀了壳留下躯干：npm 继续写盘、dev server 继续
    占端口，工具说"已终止"系统里却还挂着一堆孤儿——2026-09-23 现场：
    超时返回 20 分钟后 node_modules 还在被写（turn 也因此假死）。

    两平台的"整树"语义：
    - Windows：taskkill /T 沿进程树连杀（/F 强制）。树信息由 OS 维护，
      不依赖我们记录父子关系；
    - POSIX：spawn 时 start_new_session=True 让外壳自成**进程组**，
      对组发 SIGKILL 一网打尽（子进程没再 setsid 逃逸的普通命令都覆盖）。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
        # taskkill 对"进程已退出"会非零退出+报错文本——那是好事（没东西可
        # 杀），不值得关心细节，所以不检查返回码。
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # 组已不存在 / 无权杀：同样吞掉


def run_bash(command: str, timeout: int = BASH_TIMEOUT_DEFAULT,
             sandbox_root: Path | None = None) -> str:
    """在项目目录里执行一条 shell 命令，返回退出码与输出。

    Args:
        command: 要执行的命令，如 "git status --short" 或
            "python -m unittest discover -s tests"。
        timeout: 超时秒数，默认 60、上限 300；超时会**连同全部子进程一起**
            终止命令（杀整棵进程树，见 _kill_process_tree）并返回已产生的输出。

    （sandbox_root 是内部沙箱根参数，由装配层注入，不暴露给模型。）

    工作目录固定为沙箱根，所以命令里的相对路径都落在项目里；但**命令本身
    不受沙箱约束**（`cd /` 之后能走到任何地方）——这个工具的边界是"每次执行
    都要用户审批"，不是"跑不出去"。stdout 与 stderr 合并返回，超过 8000
    字符截断。Windows 下走 cmd、POSIX 下走 /bin/sh（shell=True 的平台默认）。
    """

    if not command or not command.strip():
        return "（命令为空，没有执行任何东西）"

    # 超时钳到 [1, MAX]：模型可能传 0（一启动就掐断）或 99999（一个 turn 卡死）。
    limit = max(1, min(int(timeout), BASH_TIMEOUT_MAX))
    sandbox = sandbox_root if sandbox_root is not None else file_tools.ALLOWED_ROOT
    # 复用沙箱解析拿工作目录（"." 一定在界内），顺带确认它确实存在。
    cwd = _resolve_safe(".", sandbox)

    # run → Popen（2026-09-24 改造）：subprocess.run 的 TimeoutExpired 只
    # kill 直接子进程（cmd/sh 外壳），npm/node 这些**孙进程**全部成为孤儿
    # ——dev server 场景下 turn 必然假死（见 _kill_process_tree 的注释）。
    # Popen + 手动杀树才能把"整棵树"清干净。
    popen_kwargs = {}
    if os.name != "nt":
        # POSIX：外壳自成进程组，超时后 killpg 一组全杀（Windows 上此参数
        # 无意义，条件传参避免歧义）。Windows 的整树靠 taskkill /T。
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,   # stdout / stderr 都由我们接管，不直接喷终端
            stderr=subprocess.PIPE,
            env=_clean_env(),
            **popen_kwargs,
        )
    except OSError as error:
        return f"(无法执行：{error})"

    try:
        stdout_b, stderr_b = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as expired:
        # 先杀整棵树，再收尸拿"已经产生的输出"——它可能就是问题本身
        # （比如卡在一个交互式提示上，输出里留着半句提示语）。
        _kill_process_tree(proc.pid)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # 树里有刺头不肯死（极少见）：管道还开着，communicate 就等不到。
            # 放弃残留输出，把工具调用诚实地还回去，别让它把 turn 拖死。
            stdout_b, stderr_b = expired.stdout or b"", expired.stderr or b""
        partial = _decode_output((stdout_b or b"") + (stderr_b or b""))
        return (
            f"(超时：{limit} 秒内没有结束，命令连同其全部子进程已被终止)\n"
            f"{_clip_output(partial)}"
        )

    merged = _decode_output(stdout_b + stderr_b)
    return f"(exit {proc.returncode})\n{_clip_output(merged)}"


# ═══════════════════════════════════════════════════════════
# 后台任务（2026-09-24 改造③）：dev server 等长驻进程的正确出口
# ═══════════════════════════════════════════════════════════

_BG_BUF_LINES = 500      # 每任务缓冲的尾部行数（长日志只留尾巴，内存有界）
_BG_MAX_TASKS = 32       # 登记上限：满了挤掉最老的已结束任务


class _BgTask:
    """一个后台任务的全部状态：进程 + 行缓冲 + 读游标。

    缓冲用 deque(maxlen=...)：dev server 一打就是几千行日志，全留会把
    内存和上下文一起撑爆——只留尾部，输出端再由 _clip_output 二次限流。
    total_written 是 drain 线程累计写入的行数，delivered 是已交付给模型
    的行数，两者之差就是 bash_output 该给的增量（deque 淘汰头部后，差值
    超过缓冲的部分自然只取到尾部——丢的是"被淘汰还没读"的，有界取舍）。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.buf: deque = deque(maxlen=_BG_BUF_LINES)
        self.total_written = 0
        self.delivered = 0
        self.lock = threading.Lock()


_BG_TASKS: dict = {}          # task_id -> _BgTask；模块级：跨工具调用/跨 turn 存活
_BG_LOCK = threading.Lock()


def _drain(stream, task: _BgTask) -> None:
    """把一根管道持续读进行缓冲（每任务两根线程，stdout/stderr 各一）。

    为什么必须持续读：不读的话管道缓冲（约 64KB）一满，子进程的写入就会
    阻塞——dev server 打几屏日志就冻住，比同步等待还隐蔽。读一行存一行，
    管道 EOF 后线程自然退出。
    """
    for raw in iter(stream.readline, b""):
        with task.lock:
            task.buf.append(_decode_output(raw).rstrip("\r\n"))
            task.total_written += 1
    try:
        stream.close()
    except OSError:
        pass


def run_bash_background(command: str,
                        sandbox_root: Path | None = None) -> str:
    """后台起一条命令，**立即**返回 task_id（run_bash 的长驻进程出口）。

    与 run_bash 的三个不同：
    - 不等待：Popen 后立即返回，turn 马上继续（dev server 不再拖死收尾）；
    - 不设超时：长驻进程"活多久"由业务决定，bash_stop 才是它的终点；
    - 输出走 bash_output 增量读，缓冲只留尾部 _BG_BUF_LINES 行。

    registry 是模块级的：sidecar 进程内跨工具调用、跨 turn 存活。
    sidecar 重启后清空，旧进程成孤儿继续跑（服务本来就该活着；要停
    bash_stop 或让用户自己杀——已知边界，注释在此立此存照）。
    """
    if not command or not command.strip():
        return "（命令为空，没有执行任何东西）"
    sandbox = sandbox_root if sandbox_root is not None else file_tools.ALLOWED_ROOT
    cwd = _resolve_safe(".", sandbox)

    popen_kwargs = {}
    if os.name != "nt":
        # POSIX：自成进程组，bash_stop 的 killpg 才能连树杀净（同 run_bash）
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_clean_env(),
            **popen_kwargs,
        )
    except OSError as error:
        return f"(无法执行：{error})"

    task = _BgTask(proc)
    threading.Thread(target=_drain, args=(proc.stdout, task), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, task), daemon=True).start()

    with _BG_LOCK:
        # 登记上限：挤掉最老的已结束任务（running 的不动——那是活着的进程）
        if len(_BG_TASKS) >= _BG_MAX_TASKS:
            for old_id in list(_BG_TASKS):
                old = _BG_TASKS[old_id]
                if old.proc.poll() is not None:
                    old.proc.wait()          # 顺手回收（POSIX 防僵尸）
                    del _BG_TASKS[old_id]
                    break
        task_id = "bg-" + uuid.uuid4().hex[:8]
        _BG_TASKS[task_id] = task

    return (f"(后台已启动 task {task_id}；用 bash_output 查看输出与探活，"
            f"bash_stop 停止。不要再用同步方式跑同一条命令——会等到超时)")


def _get_task(task_id: str) -> "_BgTask | None":
    with _BG_LOCK:
        return _BG_TASKS.get(task_id)


def bash_output(task_id: str) -> str:
    """读一个后台任务的新增输出与状态（增量：已交付过的行不再重复）。

    返回三件事：任务状态（仍在运行 / 已结束 + exit code）、本次新增行数、
    新增内容。探活就用它——curl healthcheck 是往命令里写，这里是看任务
    自己的输出，两个都通。
    """
    task = _get_task(task_id or "")
    if task is None:
        with _BG_LOCK:
            known = "、".join(sorted(_BG_TASKS)) or "（无）"
        return f"(没有这个后台任务：{task_id}。现有的：{known})"

    with task.lock:
        unread = task.total_written - task.delivered
        lines = list(task.buf)
        take = lines[max(0, len(lines) - unread):] if unread > 0 else []
        task.delivered = task.total_written

    running = task.proc.poll() is None
    if not running:
        task.proc.wait()      # 已退出：顺手回收（POSIX 防僵尸）
    head = (f"(task {task_id}: "
            f"{'仍在运行' if running else '已结束，exit ' + str(task.proc.returncode)}"
            f"，本次新增 {len(take)} 行)")
    body = "\n".join(take) if take else "（无新增输出）"
    return f"{head}\n{_clip_output(body)}"


def bash_stop(task_id: str) -> str:
    """停止一个后台任务——杀整棵进程树（复用 run_bash 超时的那把刀）。

    只能停 registry 里登记过的任务：参数是 task_id 不是 pid，影响面
    被关死在"agent 自己起的东西"，这也是它敢进免审批白名单的底气。
    """
    task = _get_task(task_id or "")
    if task is None:
        with _BG_LOCK:
            known = "、".join(sorted(_BG_TASKS)) or "（无）"
        return f"(没有这个后台任务：{task_id}。现有的：{known})"
    if task.proc.poll() is not None:
        return f"(task {task_id} 已经结束（exit {task.proc.returncode}），无需停止)"

    _kill_process_tree(task.proc.pid)
    try:
        task.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return (f"(task {task_id} 已发送终止信号但进程 5 秒内未退出"
                "——罕见；可用系统工具确认)")
    with task.lock:
        tail = list(task.buf)[-50:]
        task.delivered = task.total_written
    return (f"(task {task_id} 已终止，exit {task.proc.returncode})\n"
            f"{_clip_output(chr(10).join(tail)) if tail else '（无输出）'}")