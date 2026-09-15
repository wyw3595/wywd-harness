"""标准工具集：能看时间、能算、能按内容搜、能按名字找、能看树（全部只读 + 沙箱内）。

往这里加"通用能力"工具：now（当前时间）、calc（安全数学）、
find_text（按内容搜）、glob_files（按文件名找）、tree_dir（目录树）。
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