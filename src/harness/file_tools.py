"""实战文件工具：让 Agent 能"看见"也能"修改"项目（沙箱 + 审批）。

📚 安全设计（本课的灵魂，写代码前先读完）
  能力 = 风险。五道闸门缺一不可：
  1. 默认只读：list_dir / read_file / find_text / tree_dir 全程不改状态；
     唯一的写工具 write_file 在权限层被 path.write_ask 规则拦成 ASK——
     界内写也必须人点头（练习 s04 审批闸门），本模块只管"被放行之后
     怎么安全地写"；
  2. 沙箱：所有路径必须落在 ALLOWED_ROOT 内，越界抛 PermissionError；
  3. 限额：read_file 有 max_chars 截断，read/write 对超大内容直接
     拒绝——防止把上下文窗口、账单和磁盘撑爆；
  4. 拒绝不崩溃：违规一律走"抛异常 -> 循环捕获 -> 错误回灌"的老路，
     模型会自己道歉并换路径（练习 04 建好的机制开始收利息）。
  5. 禁区（练习 16）：界内也有皇冠珠宝——.env 里的密钥、.git 里的
     版本库，路径任意一截撞上 FORBIDDEN_PARTS 直接拒绝。
     沙箱管"有没有越界"，禁区管"界内哪些不能碰"。
"""

from pathlib import Path

# 沙箱根：本文件位于 src/harness/，往上两级就是项目根目录。
# 用 __file__ 定位而不是 cwd，保证无论从哪里启动，沙箱都是同一个。
ALLOWED_ROOT = Path(__file__).resolve().parents[2]

# 沙箱保镖（练习 16）：界内禁区。.env 里的密钥、.git 里的版本库一旦被
# read_file 读走，密钥就会流进对话、轨迹页和网页界面。
FORBIDDEN_PARTS = {".env", ".git"}


def _resolve_safe(path_text: str) -> Path:
    """把模型给的路径解析成沙箱内的绝对路径；越界就抛 PermissionError。

    例子（ALLOWED_ROOT 为 D:/React/wywd-harness 时）：
        _resolve_safe("src")           -> D:/React/wywd-harness/src
        _resolve_safe("../秘密.txt")    -> PermissionError！
    """

    resolved = (ALLOWED_ROOT / path_text).resolve()
    if not resolved.is_relative_to(ALLOWED_ROOT):
        raise PermissionError(f"路径越出沙箱：{path_text}")
    # 禁区检查：查整条动线而不只查终点——read_file(".git/config") 的
    # 文件名是 config，撞禁区的是路径中段的 .git。
    relative = resolved.relative_to(ALLOWED_ROOT)
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        raise PermissionError(f"禁区文件，拒绝访问：{path_text}")
    return resolved


def list_dir(path: str = ".") -> str:
    """列出沙箱内一个目录的内容，标记目录/文件和大小。

    Args:
        path: 相对项目根的路径，"." 表示项目根目录。

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
    """读取沙箱内的文本文件；超长截断，超大文件直接拒绝。

    Args:
        path: 相对项目根的文件路径。
        max_chars: 最多返回的字符数，默认 2000；超出部分截断并附原长说明。
    """

    resolved = _resolve_safe(path)
    if resolved.stat().st_size > 1_000_000:
        raise ValueError(f"文件太大（{resolved.stat().st_size} 字节），拒绝读取")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"……（已截断，原文件共 {len(text)} 字符）"
    return text


# 写入限额（练习 s04）：读有 max_chars，写也要有。危险点不同——读撑的是
# 上下文，写改的是状态；这里限的是"一次工具调用能改多少状态"。
MAX_WRITE_CHARS = 20_000


def write_file(path: str, text: str) -> str:
    """写入或覆盖沙箱内的文本文件；唯一改状态的工具，必须过人工审批。

    Args:
        path: 相对项目根的目标文件路径；父目录必须已存在，不会自动创建。
        text: 要写入的完整文本——整文件覆盖，不是追加。

    沙箱和禁区由 _resolve_safe 把关（与读工具同一套机制）；超过
    MAX_WRITE_CHARS 直接拒绝。审批闸门在权限层（permissions.py 的
    path.write_ask 规则），执行到这里意味着人或规则已经放行。
    """

    resolved = _resolve_safe(path)
    if len(text) > MAX_WRITE_CHARS:
        raise ValueError(
            f"写入内容过长（{len(text)} 字符，上限 {MAX_WRITE_CHARS}），拒绝写入"
        )
    resolved.write_text(text, encoding="utf-8")
    return f"已写入 {path}（{len(text)} 字符）"
