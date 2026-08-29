"""实战文件工具：让 Agent 能"看见"项目目录（只读 + 沙箱）。

📚 安全设计（本课的灵魂，写代码前先读完）
  能力 = 风险。四道闸门缺一不可：
  1. 只读：没有写/删/执行工具——最小权限原则；
  2. 沙箱：所有路径必须落在 ALLOWED_ROOT 内，越界抛 PermissionError；
  3. 限额：read_file 有 max_chars 截断，超大文件直接拒绝——
     防止把上下文窗口和账单撑爆；
  4. 拒绝不崩溃：违规一律走"抛异常 -> 循环捕获 -> 错误回灌"的老路，
     模型会自己道歉并换路径（练习 04 建好的机制开始收利息）。
"""

from pathlib import Path

# 沙箱根：本文件位于 src/harness/，往上两级就是项目根目录。
# 用 __file__ 定位而不是 cwd，保证无论从哪里启动，沙箱都是同一个。
ALLOWED_ROOT = Path(__file__).resolve().parents[2]


def _resolve_safe(path_text: str) -> Path:
    """把模型给的路径解析成沙箱内的绝对路径；越界就抛 PermissionError。

    例子（ALLOWED_ROOT 为 D:/React/wywd-harness 时）：
        _resolve_safe("src")           -> D:/React/wywd-harness/src
        _resolve_safe("../秘密.txt")    -> PermissionError！
    """

    resolved = (ALLOWED_ROOT / path_text).resolve()
    if not resolved.is_relative_to(ALLOWED_ROOT):
        raise PermissionError(f"路径越出沙箱：{path_text}")
    return resolved


def list_dir(path: str = ".") -> str:
    """列出沙箱内一个目录的内容，标记目录/文件和大小。

    返回格式示例：
        harness/  (目录)
        main.py  (文件, 1234 字节)
    """

    resolved = _resolve_safe(path)
    if not resolved.is_dir():
        raise NotADirectoryError(f"不是目录：{path}")
    return "\n".join([
        f"{item.name}/  (目录)" if item.is_dir() else
        f"{item.name}  (文件, {item.stat().st_size} 字节)"
        for item in sorted(resolved.iterdir())
    ])


def read_file(path: str, max_chars: int = 2000) -> str:
    """读取沙箱内的文本文件；超长截断，超大文件直接拒绝。"""

    resolved = _resolve_safe(path)
    if resolved.stat().st_size > 1_000_000:
        raise ValueError(f"文件太大（{resolved.stat().st_size} 字节），拒绝读取")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"……（已截断，原文件共 {len(text)} 字符）"
    return text
