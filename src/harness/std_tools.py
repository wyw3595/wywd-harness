"""标准工具集：让 Agent 能算、能搜、能看树（全部只读 + 沙箱内）。

往这里加"通用能力"工具：calc（安全数学）、find_text（项目内搜索）、
tree_dir（目录树）。回归测试对应 tests/test_std_tools.py。

安全原则沿袭 file_tools.py（能力 = 风险）：
  - calc：ast 白名单求值，不允许任何变量/属性/文件访问；
  - find_text / tree_dir：复用 file_tools 的沙箱解析（越界抛
    PermissionError），并跳过禁区（.env / .git）。
"""

import ast
import math
import operator
import os
import re
from pathlib import Path

from src.harness.file_tools import ALLOWED_ROOT, FORBIDDEN_PARTS, _resolve_safe


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


def find_text(
    pattern: str,
    root: str = ".",
    max_results: int = 8,
    case_sensitive: bool = False,
) -> str:
    """在项目里按正则搜索文件内容，返回 文件:行号:行内容 命中列表。

    Args:
        pattern: 正则表达式，如 "def get_weather"。
        root: 相对项目根的搜索起点，默认项目根。
        max_results: 最多返回多少条命中（防撑爆上下文）。
        case_sensitive: 是否区分大小写，默认不区分。

    跳过禁区目录（.git/.env）和超大文件（>1MB），保证只读安全。
    """

    base = _resolve_safe(root)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"无法解析正则：{exc}") from exc

    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in FORBIDDEN_PARTS)
        for filename in sorted(filenames):
            if filename in FORBIDDEN_PARTS:
                continue
            file_path = Path(dirpath) / filename
            try:
                if file_path.stat().st_size > 1_000_000:
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, raw_line in enumerate(content.splitlines(), start=1):
                if regex.search(raw_line):
                    relative = file_path.relative_to(ALLOWED_ROOT)
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


def tree_dir(root: str = ".", depth: int = 2, include_hidden: bool = False) -> str:
    """渲染一棵目录树（低频工具，天然适合做延迟加载演示）。

    Args:
        root: 相对项目根的起点，默认项目根。
        depth: 往下递归的层数上限，默认 2。
        include_hidden: 是否显示隐藏文件/目录（.git/.env 永不显示）。
    """

    base = _resolve_safe(root)
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