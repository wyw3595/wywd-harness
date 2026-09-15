"""实战文件工具：让 Agent 能"看见"也能"修改"项目（沙箱 + 审批）。

📚 安全设计（本课的灵魂，写代码前先读完）
  能力 = 风险。六道闸门缺一不可：
  1. 默认只读：list_dir / read_file / find_text / tree_dir 全程不改状态；
     两个写工具 write_file / edit_file 都在权限层被 path.write_ask 规则
     拦成 ASK——界内写也必须人点头（练习 s04 审批闸门），本模块只管
     "被放行之后怎么安全地写"；
  2. 沙箱：所有路径必须落在 ALLOWED_ROOT 内，越界抛 PermissionError；
  3. 限额：read_file 四道（行数 / 单行长度 / 单次字符总量 / 文件大小），
     write_file 一道（MAX_WRITE_CHARS）——防止把上下文窗口、账单和磁盘
     撑爆；
  4. 拒绝不崩溃：违规一律走"抛异常 -> 循环捕获 -> 错误回灌"的老路，
     模型会自己道歉并换路径（练习 04 建好的机制开始收利息）。
  5. 禁区（练习 16）：界内也有皇冠珠宝——.env 里的密钥、.git 里的
     版本库，路径任意一截撞上 FORBIDDEN_PARTS 直接拒绝。
     沙箱管"有没有越界"，禁区管"界内哪些不能碰"。
  6. 原子落盘（2026-09-12）：两个写工具都走"同目录临时文件 -> fsync ->
     os.replace 改名"。任何观察者要么看到旧的完整文件、要么看到新的完整
     文件，**不存在"半个文件顶着正式名字"**——写大文件时崩溃或断电，
     原有内容也不会被毁掉。

  另有一条贯穿全模块的规矩：**读写对称**。读进来是字节、写出去也是字节，
  中间统一在 \n 方言里工作、落盘还原文件原本的换行风格。详见
  _normalize_newlines / _existing_newline / _write_text_bytes。

  沙箱根参数化（2026-09-13，工作区支持）：所有公共函数都多了一个
  root: Path | None = None——None 表示"运行时读模块全局 ALLOWED_ROOT"，
  传了就以它为沙箱根。刻意用 None 哨兵而不是 root: Path = ALLOWED_ROOT
  当默认值：默认参数在**函数定义时**求值，那会把值焊死，mock
  ALLOWED_ROOT 换沙箱的测试全部失效（tests/test_file_tools.py 的前提
  就是"调用时读全局"）。哨兵解析只有 _resolve_safe 一处——机制只有
  一份。另一个刻意的规矩：这个参数**永远不暴露给模型**——
  tool_to_schema 反射 handler 签名的每个参数，root 若进了注册的
  handler 签名，模型就能传任意路径绕沙箱；装配层（toolbox）用闭包
  把它吃掉。所以本模块各函数的 docstring 里不把 root 写进 Args 段。
"""

import os
import tempfile
from pathlib import Path

# 沙箱根：本文件位于 src/harness/，往上两级就是项目根目录。
# 用 __file__ 定位而不是 cwd，保证无论从哪里启动，沙箱都是同一个。
ALLOWED_ROOT = Path(__file__).resolve().parents[2]

# 沙箱保镖（练习 16）：界内禁区。.env 里的密钥、.git 里的版本库一旦被
# read_file 读走，密钥就会流进对话、轨迹页和网页界面。
FORBIDDEN_PARTS = {".env", ".git"}


def _resolve_safe(path_text: str, root: Path | None = None) -> Path:
    """把模型给的路径解析成沙箱内的绝对路径；越界/撞禁区就抛 PermissionError。

    例子（ALLOWED_ROOT 为 D:/React/wywd-harness 时）：
        _resolve_safe("src")           -> D:/React/wywd-harness/src
        _resolve_safe("../秘密.txt")    -> PermissionError！

    root 是沙箱根（工作区支持）：None 表示"运行时读模块全局
    ALLOWED_ROOT"，传了就以它为沙箱根——哨兵模式的原因见模块
    docstring，这里不重复。哨兵解析只住在本函数：所有工具函数
    把 root 原样往下传，None 传到这里才落地。

    错误消息必须**可行动**（Anthropic "错误要给 agent 下一步"）：光说
    "越界了"模型只能瞎猜，所以把沙箱根和正确写法一起告诉它。
    """

    if root is None:
        root = ALLOWED_ROOT  # 哨兵落地：唯一的运行时读全局点
    root = root.resolve()  # 归一化 root 自身（防带 ".." 或未规范化的注入）
    resolved = (root / path_text).resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError(
            f"{path_text} 越出沙箱——沙箱根是 {root}。"
            f"请改用相对项目根的路径，例如 'src/harness/agent.py'"
        )
    # 禁区检查：查整条动线而不只查终点——read_file(".git/config") 的
    # 文件名是 config，撞禁区的是路径中段的 .git。
    relative = resolved.relative_to(root)
    forbidden = sorted(set(relative.parts) & FORBIDDEN_PARTS)
    if forbidden:
        raise PermissionError(
            f"禁区不可访问：{path_text}（命中 {'、'.join(forbidden)}）——"
            f".env 里的密钥、.git 里的版本库对工具永久关闭，换个文件吧"
        )
    return resolved


def list_dir(path: str = ".", root: Path | None = None) -> str:
    """列出沙箱内一个目录的内容，标记目录/文件和大小。

    Args:
        path: 相对项目根的路径，"." 表示项目根目录。

    返回格式示例：
        harness/  (目录)
        main.py  (文件, 1234 字节)

    （root 是内部沙箱根参数，由装配层注入，不暴露给模型——见模块
    docstring 的参数化说明。）
    """

    resolved = _resolve_safe(path, root)
    # 存在性和类型分开报：旧写法只检查 is_dir，"路径不存在"和"这是个文件"
    # 会撞出同一条模糊消息（甚至掉到底层的 FileNotFoundError 原文）。
    if not resolved.exists():
        raise FileNotFoundError(
            f"没有这个路径：{path}——用 fs_list('.') 看项目根下有什么，"
            f"或用 fs_glob('**/*名字片段*') 按名字找"
        )
    if not resolved.is_dir():
        raise NotADirectoryError(
            f"{path} 是文件不是目录——用 fs_read('{path}') 读它的内容"
        )
    return "\n".join([
        f"{item.name}/  (目录)" if item.is_dir() else
        f"{item.name}  (文件, {item.stat().st_size} 字节)"
        for item in sorted(resolved.iterdir())
    ])


# 读取的四道限额，对应四种"被撑爆"的方式（缺一道就能被绕过去）：
#   limit           —— 行数，防的是"大文件一次全塞进上下文"；
#   MAX_LINE_CHARS  —— 单行长度，防的是"压缩 JSON / minified JS 一行几十万字符"；
#   MAX_READ_CHARS  —— 单次返回的字符总量，防的是前两道**组合**起来仍然太大
#                      （200 行 × 每行 2000 字符 = 40 万字符；实测传一遍
#                      limit=5000 就能吐出 54580 字符）；
#   MAX_READ_BYTES  —— 文件大小，防的是"把整个大文件读进内存再截断"。
DEFAULT_READ_LINES = 200
MAX_LINE_CHARS = 2000
MAX_READ_CHARS = 30_000
MAX_READ_BYTES = 1_000_000


def _is_probably_binary(data: bytes) -> bool:
    """前 4KB 里出现 NUL 字节就认定是二进制。纯函数。

    文本文件里不会出现 NUL，二进制文件里遍地都是——比穷举后缀表可靠，
    而且不用维护。只看前 4KB 是因为文件头结构就在最前面，没必要为一个
    大文件多读几 MB。

    住在这里而不是 std_tools：读和搜都要用它，而 std_tools 已经 import
    了本模块——放那边会绕成循环导入。它是"文件内容分类"，本来就属于
    文件工具这一层。
    """

    return b"\x00" in data[:4096]


def _normalize_newlines(text: str) -> tuple[str, str]:
    """把正文统一到 \\n 方言，并回报它原本的换行风格。纯函数。

    为什么必须做：本仓库工作区的文件大多是 CRLF（git core.autocrlf=true），
    而 fs_read 交给模型的行是用 \\n 拼起来的——模型据此写出的多行 old_text
    自然也是 \\n。拿它去和原样的 \\r\\n 正文逐字匹配，永远匹配不到。

    所以读写都统一在 \\n 方言里工作，只在落盘那一刻还原原本的风格。
    返回 (统一后的正文, 原本的换行符)；正文里没有 \\r\\n 就认为它是 LF。
    """

    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def _existing_newline(path: Path, probe_bytes: int = 8192) -> str:
    """已有文件原本的换行风格；文件不存在（或探不到 \\r\\n）就用 \\n。

    只读开头 8KB：换行风格在文件头就定了，没必要为一个大文件读全文。
    这是"覆盖写要保持原风格"的落点——不做的话，改一个词会把整篇的换行
    都换掉，git diff 里看起来像全文件重写。
    """

    try:
        with path.open("rb") as handle:
            head = handle.read(probe_bytes)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in head else "\n"


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """原子替换：要么是完整的新文件，要么旧文件原封不动。**机制只有这一份。**

    四步，顺序不能换：
      1. `tempfile.mkstemp(dir=path.parent)` —— 临时文件必须和目标**同目录**。
         `os.replace` 只保证"同一文件系统内"原子；跨盘会退化成"复制 + 删除"，
         那个窗口里文件是半截的。
      2. 写入 + `flush` + `os.fsync` —— fsync 把数据真正推给磁盘。少了它，
         `os.replace` 之后断电仍可能丢内容（改名是原子的，但数据可能还躺在
         操作系统的页缓存里）。
      3. `os.replace(tmp, path)` —— 原子改名。任何观察者要么看到旧的完整文件、
         要么看到新的完整文件，**不存在中间态**。
      4. `finally: tmp.unlink(missing_ok=True)` —— 成功时 tmp 已经不存在了
         （`missing_ok=True` 所以不炸），失败时清掉残骸，不留垃圾文件。

    为什么不用 `path.write_bytes()`：它是"先截断再写"，写一半崩溃就是半个
    文件顶着正式名字，原有内容已经被毁了。文件越大这个窗口越宽——而
    `fs_edit` 正是为改大文件而生的，所以更不能走那条路。
    """

    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_text_bytes(path: Path, text: str, newline: str = "\n") -> None:
    """按指定换行风格把正文**原子地**写回磁盘。必须写字节，不能用 write_text。

    实测过的坑（2026-09-11）：文本模式写盘会把正文里的 \\n 再翻译一次成
    os.linesep，而原本的 \\r 原样保留——于是 b'a\\r\\nb\\r\\n' 写出来变成
    b'a\\r\\r\\nb\\r\\r\\n'，CR 翻倍，文件被写坏。本仓库的 .py 文件全是
    CRLF，一改就中。

    读进来是字节、写出去也是字节，中间不做任何翻译，才谈得上"没动过的行
    一个字都没变"；再加上原子改名，才谈得上"要么全改、要么没改"。

    两个写工具（write_file / edit_file）都从这里落盘——**机制只有一份**。
    """

    if newline != "\n":
        text = text.replace("\n", newline)
    _atomic_write_bytes(path, text.encode("utf-8"))


def _truncate_line(line: str) -> str:
    """单行超长就截断并注明原长。纯函数。

    Args:
        line: 一行正文（不含行号前缀）。

    为什么要单独管一行：代码文件里一行 2000 字符很少见，但压缩后的 JSON、
    单行 CSV、minified JS 都是一行几十万字符——只限行数拦不住它们。
    截断而不是丢弃：让模型知道"这里有东西但太长"，而不是以为行就这么短。
    """

    if len(line) > MAX_LINE_CHARS:
        return line[:MAX_LINE_CHARS] + f" …（本行共 {len(line)} 字符，已截断）"
    return line


def read_file(
    path: str,
    offset: int = 1,
    limit: int = DEFAULT_READ_LINES,
    root: Path | None = None,
) -> str:
    """读取沙箱内文本文件的指定行区间，返回带行号的正文。

    Args:
        path: 相对项目根的文件路径。
        offset: 从第几行开始读，从 1 计数，默认 1（文件开头）。
        limit: 这一次最多返回多少行，默认 200。

    返回格式是 cat -n 风格：右对齐行号 + 制表符 + 该行原文。**拿这里的
    正文去构造编辑锚点时，不要带上行号前缀**——制表符之后才是文件内容。
    最后一行是页码小结，告诉你文件共几行、要不要换更大的 offset 接着读。

    四道限额，对应四种"被撑爆"的方式：
      1. 行数（limit）—— 默认 200 行，靠 offset 翻页读完整个大文件；
      2. 单行长度（MAX_LINE_CHARS）—— 压缩过的 JSON / minified JS 一行
         几十万字符，光限行数拦不住；
      3. 单次返回的字符总量（MAX_READ_CHARS）—— 前两道组合起来仍然可能
         太大（200 行 × 2000 字符 = 40 万字符），所以再上一道总量闸；
      4. 文件大小（MAX_READ_BYTES）—— 超过直接拒，不把整个文件读进内存。

    目录和二进制文件都明确拒绝，各自的错误消息会给下一步（旧实现在
    Windows 上把"目录"报成 PermissionError，把 PNG 解成一串乱码）。

    （root 是内部沙箱根参数，由装配层注入，不暴露给模型。）
    """

    resolved = _resolve_safe(path, root)

    # 目录必须先显式拦下。旧写法会掉进 read_text 的 IsADirectoryError，
    # 在 Windows 上被翻译成 "PermissionError: [Errno 13] Permission denied"
    # ——模型读到"权限不足"会去猜怎么提权，方向完全错了。
    if resolved.is_dir():
        raise IsADirectoryError(
            f"{path} 是目录不是文件——用 fs_list('{path}') 看它下面有什么"
        )

    # stat() 同时兼作存在性检查：文件不存在就在这里抛 FileNotFoundError。
    size = resolved.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(
            f"{path} 有 {size} 字节，超过单次读取上限 {MAX_READ_BYTES} 字节"
            f"——用 fs_find 在里面搜关键词定位，不要整文件读"
        )

    data = resolved.read_bytes()
    if _is_probably_binary(data):
        raise ValueError(
            f"{path} 看起来是二进制文件（{size} 字节）——文本工具读它只会得到"
            f"乱码，图片/压缩包/可执行文件请换别的办法处理"
        )
    # 统一到 \n 方言再分行——CRLF 文件分出来的行，才和模型自己用 \n
    # 拼出来的多行锚点一致（不做这一步，fs_edit 对 CRLF 文件永远匹配不到）。
    text = data.decode("utf-8", errors="replace")
    lines = _normalize_newlines(text)[0].splitlines()

    if not lines:
        return f"（{path} 是空文件）"

    # 参数错误一律走 ValueError——和 fs_find 的坏正则同一条回灌通道，
    # 模型拿到的都是"改哪个参数、改成什么"，不用去分辨错误类型。
    if offset < 1:
        raise ValueError(f"offset 从 1 开始计数，收到 {offset}")
    if limit < 1:
        raise ValueError(f"limit 至少为 1，收到 {limit}")

    total = len(lines)
    if offset > total:
        raise ValueError(
            f"offset={offset} 超出文件末尾——{path} 共 {total} 行，"
            f"offset 应在 1..{total} 之间"
        )

    window = lines[offset - 1: offset - 1 + limit]
    # 行号宽度按本页最大行号算：读上千行的文件时右对齐才不会参差不齐。
    width = len(str(offset + len(window) - 1))
    prefix = width + 1  # 行号 + 制表符，也要算进字符预算

    # 总量闸（MAX_READ_CHARS）：逐行累加真实占用，装不下就停在这一行。
    # 第一行无论如何都留下——_truncate_line 已保证单行不超过 MAX_LINE_CHARS，
    # 一定装得下，所以不会出现"返回空正文"的尴尬。
    shown: list[str] = []
    used = 0
    for line in window:
        rendered = _truncate_line(line)
        cost = prefix + len(rendered) + 1  # +1 是行尾换行
        if shown and used + cost > MAX_READ_CHARS:
            break
        shown.append(rendered)
        used += cost

    body = "\n".join(
        f"{number:>{width}}\t{text}"
        for number, text in enumerate(shown, start=offset)
    )

    shown_end = offset + len(shown) - 1
    if shown_end >= total:
        tail = f"（共 {total} 行，已全部显示）"
    elif len(shown) < len(window):
        # 行数还没用完就停了 → 是撞到字符上限。必须说清楚，否则模型会以为
        # 自己传的 limit 没生效，然后把 limit 调得更大（更糟）。
        tail = (f"（共 {total} 行，已显示 {offset}-{shown_end} 行——本页触到"
                f"单次 {MAX_READ_CHARS} 字符上限；继续读用 offset={shown_end + 1}）")
    else:
        tail = (f"（共 {total} 行，已显示 {offset}-{shown_end} 行；"
                f"继续读用 offset={shown_end + 1}）")
    return f"{body}\n{tail}"


# 写入限额（练习 s04）：读有限额，写也要有。危险点不同——读撑的是上下文
# （三道：行数 / 单行 / 文件大小），写改的是状态；这里限的是"一次工具调用
# 能改多少状态"。
MAX_WRITE_CHARS = 20_000


def write_file(path: str, text: str, root: Path | None = None) -> str:
    """写入或覆盖沙箱内的文本文件；整文件覆盖，改一处请用 fs_edit。

    Args:
        path: 相对项目根的目标文件路径；父目录必须已存在，不会自动创建。
        text: 要写入的完整文本——整文件覆盖，不是追加。

    沙箱和禁区由 _resolve_safe 把关（与读工具同一套机制）；超过
    MAX_WRITE_CHARS 直接拒绝。审批闸门在权限层（permissions.py 的
    path.write_ask 规则），执行到这里意味着人或规则已经放行。

    两个改状态的工具分工：**新建文件、或内容整体重写用 fs_write；
    只改已有文件的某几处用 fs_edit**——后者只传改动片段，代价与文件
    大小无关，不会被 MAX_WRITE_CHARS 卡住。

    换行风格：覆盖已有文件时沿用它原本的风格（CRLF 保持 CRLF），新建
    文件用 \\n。落盘是**原子的**（同目录临时文件 -> fsync -> os.replace），
    写一半崩溃也不会留下半个文件顶着正式名字：详见 _normalize_newlines /
    _existing_newline / _write_text_bytes。

    （root 是内部沙箱根参数，由装配层注入，不暴露给模型。）
    """

    resolved = _resolve_safe(path, root)
    if len(text) > MAX_WRITE_CHARS:
        raise ValueError(
            f"写入内容过长（{len(text)} 字符，上限 {MAX_WRITE_CHARS}）——"
            f"改已有文件请用 fs_edit（只传改动的那一段），或者拆成几次写"
        )
    if resolved.is_dir():
        raise IsADirectoryError(
            f"{path} 是目录不是文件——要写哪个文件请把文件名补齐"
        )
    if not resolved.parent.is_dir():
        raise FileNotFoundError(
            f"{path} 的父目录不存在——本工具不会自动创建目录，"
            f"先用 fs_list 确认上一级，或者把文件落在已存在的目录里"
        )
    normalized, _ = _normalize_newlines(text)
    _write_text_bytes(resolved, normalized, _existing_newline(resolved))
    return f"已写入 {path}（{len(normalized)} 字符）"


# -- 编辑工具（fs_edit）：只传改动片段，不重写全文 ----------------------
#
# 为什么必须有它：fs_write 是整文件覆盖，改一行的代价是重写全文，于是
# 一头撞上 MAX_WRITE_CHARS——NEXT_SESSION.md 这类 4 万字符的文件等于改不动。
# fs_edit 只传 old_text / new_text 两个片段，代价与文件大小无关。
#
# 业界共识（Claude Code 文档："Prefer Edit over Write when modifying existing
# files — Edit sends only the changed fragment"；Anthropic 在 SWE-bench 工程
# 博客里试过多种编辑方案，字符串替换可靠性最高）：**逐字匹配 + 要求唯一，
# 不唯一就报错让模型自己补上下文重试**。第二条是关键——它的错误消息质量
# 直接决定模型能不能自愈。

DIFF_MAX_LINES = 20  # 片段对照最多展示多少行（一次编辑塞几百行只会撑爆上下文）


def _render_fragment_diff(old_text: str, new_text: str) -> str:
    """把一次编辑渲染成 - 旧 / + 新 的片段对照。纯函数。

    只用于展示，不做行级对齐（真正的 diff 算法是另一个课题）。超过
    DIFF_MAX_LINES 行就截断并注明还剩多少行——那时让模型自己 fs_read
    看结果比塞进上下文划算。

    这个返回值是给**人**看的：审批闸门弹出 ASK 时，审批人只看到
    "要改 README.md" 就点头，等于盲签；带上对照才好判断。
    """

    def block(prefix: str, text: str) -> list[str]:
        lines = text.splitlines()
        shown = [f"{prefix} {line}" for line in lines[:DIFF_MAX_LINES]]
        if len(lines) > DIFF_MAX_LINES:
            shown.append(f"{prefix} …（还有 {len(lines) - DIFF_MAX_LINES} 行）")
        return shown

    return "\n".join(block("-", old_text) + block("+", new_text))


def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    root: Path | None = None,
) -> str:
    """对沙箱内已有的文本文件做精确字符串替换，只传改动的片段。

    Args:
        path: 相对项目根的文件路径。
        old_text: 要被替换的原文，必须与文件内容逐字相同（含空格与缩进），且默认必须在文件里唯一。
        new_text: 替换成的新文本；留空字符串表示把这段原文删掉。
        replace_all: old_text 在文件里出现多次时是否全部替换，默认 false（此时多次出现会报错）。

    用 fs_edit 而不是 fs_write：fs_write 整文件覆盖，改一行也要重写全文，
    4 万字符的文件会直接撞上 MAX_WRITE_CHARS——等于改不动。这里只传两个
    片段，代价与文件大小无关。

    三条必须说清的规矩：
      1. 逐字匹配。**从 fs_read 拿正文时不要带行号前缀**——制表符之后才是
         文件内容，带上 "  91\\t" 一定匹配不到。
      2. 唯一性。默认要求 old_text 只出现一次；出现多次会报错并告诉你次数，
         要么多带 2~3 行上下文让它唯一，要么 replace_all=true。
      3. 先读再改。本层无法强制"先 fs_read 再 fs_edit"（工具是无状态函数，
         拿不到会话历史），这条靠约定和审批人把关，别凭猜测构造 old_text。

    落盘方式与 fs_write 一致：**字节写回 + 沿用文件原本的换行风格**——
    所以没被改动过的行一个字都不会变（不会出现"改一个词、整篇换行都变了"，
    也不会把 CRLF 写成 \\r\\r\\n）。原子替换留给下一步与 fs_write 一起改，
    不然同一个项目里会出现两套写法。

    （root 是内部沙箱根参数，由装配层注入，不暴露给模型。）
    """

    resolved = _resolve_safe(path, root)
    if resolved.is_dir():
        raise IsADirectoryError(
            f"{path} 是目录不是文件——用 fs_list('{path}') 看它下面有什么"
        )
    if not old_text:
        raise ValueError(
            "old_text 不能是空字符串——空串在文件里处处匹配。新建文件用 fs_write，"
            "要在文件末尾追加就把最后几行一起写进 old_text"
        )

    # 归一化换行要放在"是否相同 / 是否超长"判断之前：判断必须和最终的
    # 匹配用同一个方言，否则 "a\r\n" 与 "a\n" 会被判成两个不同的片段。
    old_text, _ = _normalize_newlines(old_text)
    new_text, _ = _normalize_newlines(new_text)

    if old_text == new_text:
        raise ValueError("old_text 与 new_text 完全相同——这次编辑不会有任何变化")
    if len(new_text) > MAX_WRITE_CHARS:
        raise ValueError(
            f"new_text 过长（{len(new_text)} 字符，上限 {MAX_WRITE_CHARS}）——"
            f"单次编辑只传改动的片段；确实要整体重写请用 fs_write"
        )

    size = resolved.stat().st_size  # 文件不存在时在这里抛 FileNotFoundError
    if size > MAX_READ_BYTES:
        raise ValueError(
            f"{path} 有 {size} 字节，超过可编辑上限 {MAX_READ_BYTES} 字节"
            f"——替换要先读全文，这么大的文件请先拆分"
        )

    data = resolved.read_bytes()
    if _is_probably_binary(data):
        raise ValueError(
            f"{path} 看起来是二进制文件（{size} 字节）——文本工具改不了它"
        )
    # 正文也归一化：CRLF 文件的原样正文里是 \r\n，而模型的多行 old_text
    # 是用 \n 拼的，不归一化就永远匹配不到（newline 留着落盘时还原）。
    text, newline = _normalize_newlines(data.decode("utf-8", errors="replace"))

    occurrences = text.count(old_text)
    if occurrences == 0:
        raise ValueError(
            f"old_text 在 {path} 里找不到——必须逐字匹配（含空格与缩进）。"
            f"先用 fs_read 看清原文，并确认没有把行号前缀一起复制进去"
        )
    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"old_text 在 {path} 里出现了 {occurrences} 次，无法确定改哪一处——"
            f"在 old_text 里多带 2~3 行上下文让它唯一，或设 replace_all=true 全改"
        )

    first_index = text.index(old_text)
    # 首处的行号：数一数它前面有多少个换行符。
    first_line = text.count("\n", 0, first_index) + 1
    changed = occurrences if replace_all else 1
    replaced = text.replace(old_text, new_text, -1 if replace_all else 1)
    # 落盘按文件原本的换行风格还原，并且走字节写（文本模式会把 \n 再翻译
    # 一次，CRLF 文件因此 CR 翻倍——见 _write_text_bytes）。
    _write_text_bytes(resolved, replaced, newline)

    return (
        f"已编辑 {path}：{changed} 处替换（首处在第 {first_line} 行），"
        f"文件 {len(replaced.splitlines())} 行\n"
        f"{_render_fragment_diff(old_text, new_text)}"
    )
