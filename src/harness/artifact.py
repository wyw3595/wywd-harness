"""工具输出外化（教材 s13）：上下文不够时，把大输出换到磁盘，只留指针。

============================================================
📚 为什么需要它
============================================================

一条 `grep -r` 能返回几 MB，一次 `pytest -v` 几十万字符——工具输出比上下文
窗口还大，这不是边缘情况，是干活的常态。两种偷懒的做法都不行：

  - **全塞回去**：上下文当场爆掉，后面的对话全废；
  - **直接截断**：模型可能正好需要第 40000 行那个错误（截断把它丢了）。

正解是**换页**：完整输出写磁盘（外存），上下文里只留"指针 + 预览"（内存）。
和操作系统的虚拟内存是同一个思想——内存不够就把页换出去，页表里留个条目。

| 概念 | 操作系统 | 本项目 |
|------|---------|--------|
| 内存 | RAM | 上下文窗口 |
| 外存 | 磁盘 swap | `.sessions/artifacts/*.txt` |
| 内存条目 | 页表条目 | 指针 + 头尾预览 |
| 读回数据 | 缺页中断 | 模型用 fs_read 按路径读回 |

============================================================
📚 与教材（learn-workbuddy/s13）的差异——都是刻意的
============================================================

1. **目录按工作区、不按会话**。教材是 `<session>/tool-results/`，一会话一目录；
   我们的 `TurnRunner` 协议是 `(message, history) -> (output, messages)`，
   **拿不到会话 id**（session 层不认识 agent 层，这条隔离是刻意设计的，
   不为了外化去破坏它）。所以用一个共享目录，靠"独占创建 + 序号"保证互不覆盖。
2. **只做两级表示**。教材分三种（artifact 文件 / context pointer / memory
   reference）；memory reference 属于 s10–s12 的保留策略，这里不做。
3. **数值全部重调，并加了一条"省得出来才换页"的底线**。教材的
   `BASH_MAX_OUTPUT_LENGTH = 30000` 与它的预览总长（head 6KB + tail 24KB
   ≈ 30720）几乎相等——bash 输出一旦触发外化，写回上下文的指针**比原文还长**，
   换页白做。而且它的预览按 KB（字节）算，我们的输出里可能有大段中文
   （一个字符三字节，按字符切会放大三倍）。所以这里三个数字互相咬合：
   ```
   预览 head 2K + tail 8K = 10K 字符   ← 换页后留在上下文里的上限
   阈值 bash 20K 字符 / 其它 20KB 字节  ← 值不值得落盘
   MIN_SHRINK_RATIO = 2               ← 输出不到预览预算的两倍就不换页
   ```
   最后那条是通用底线：外化至少要省一半，否则磁盘 IO 和一次读回都白付。

============================================================
📚 这层的职责与接缝
============================================================

`ArtifactStore.externalize(tool_name, output)`：超阈值 → 落盘 + 返回指针文本；
不超 → 原样返回。它不知道模型是谁、会话是谁、权限是什么。

接缝在 `run_agent` 的 `externalize` 钩子上（可选、默认 None = 完全关掉，
既有行为零变化）。这样 harness 认识"外化"这个机制，但不认识磁盘布局——
布局由装配层用 `ArtifactStore(root)` 决定。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

# ---- 阈值：超过就换页 ----------------------------------------------------

# bash 的输出波动最大（可能几字节，也可能几十 MB），所以它有一条独立的
# 字符阈值；其它工具按**字节**算（中文一个字符三字节，按字符算会低估体积）。
BASH_MAX_OUTPUT_CHARS = 20_000
TOOL_RESULT_THRESHOLD_KB = 20

# ---- 预览：换页后留在上下文里的那部分 ------------------------------------

# 头尾都留（教材的取舍）：关键信息经常在**末尾**——编译错误的最后几行、
# 测试结果的 summary、命令的退出状态。只留头，模型会以为那就全部；
# 只留尾，它会丢掉"这东西是怎么开始的"。
#
# 为什么比教材小（10K 字符 vs 它的 30KB）：按字符切 + 中文占比高的现实，
# 30K 字符的中文预览能吃掉两万多 token——"有界表示"就不有界了。
PREVIEW_HEAD_CHARS = 2 * 1024
PREVIEW_TAIL_CHARS = 8 * 1024

# 外化至少要省下这么多倍才值得：输出不到"预览预算 × 这个数"就不换页。
# 没有这条会出现"指针比原文还长"——白搭一次磁盘写 + 模型还要读回一次。
MIN_SHRINK_RATIO = 2

SUMMARY_MAX_CHARS = 120
ARTIFACT_SUBDIR = "tool-results"


def summarize(output: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """确定性摘要：首个非空行 + 规模。

    刻意不调模型：摘要生成失败不能成为整条链路失败的理由，而且离线测试
    需要可复现的输出。生产实现可以换成"任务感知摘要"，但仍必须限长——
    摘要本身也是对上下文的承诺。
    """

    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped[:limit]
            break
    else:
        first = "(空输出)"
    return f"{first}（共 {len(output)} 字符）"


@dataclass(frozen=True)
class Artifact:
    """一份外化的工具输出（正文在磁盘上，这里只是它的档案件）。"""

    source_id: str          # 稳定 ID，如 tool_result_001
    path: Path              # 正文所在的绝对路径
    tool_name: str
    sha256: str
    size_bytes: int
    summary: str

    def to_pointer(self, output: str,
                   head_chars: int = PREVIEW_HEAD_CHARS,
                   tail_chars: int = PREVIEW_TAIL_CHARS) -> str:
        """把完整输出编码成"有界表示"——这是**唯一**会进上下文的东西。

        形状（教材的 make_pointer）：来源头 + 头预览 + 省略提示 + 尾预览。
        来源头四项各有用途：source_id 用于引用，summary 让模型知道值不值得
        读全文，sha256 让审计能验证"读回来的还是当年那份"，path 是缺页中断
        的入口（模型用 fs_read 去取）。
        """

        head = output[:head_chars]
        tail = output[-tail_chars:] if len(output) > head_chars + tail_chars else ""
        omitted = len(output) - len(head) - len(tail)

        lines = [
            f"[Artifact: {self.source_id}]",
            f"Summary: {self.summary}",
            f"Source: {self.tool_name}; SHA-256: {self.sha256}",
            f"Full output: {self.path}",
            "",
            head,
        ]
        if omitted > 0:
            lines.append(
                f"\n... [省略 {omitted} 字符；完整输出在 {self.path}，"
                f"需要就用 fs_read 读它] ...\n"
            )
        if tail:
            lines.append(tail)
        return "\n".join(lines)


class ArtifactStore:
    """管一个目录下的 artifact 文件：独占创建、序号递增、**绝不覆盖**。

    "绝不覆盖"不是洁癖：指针里写着路径，覆盖等于让旧指针指向新内容——
    审计时读回来的是另一份东西，还查不出问题出在哪。
    """

    def __init__(
        self,
        root: Path,
        bash_max_chars: int = BASH_MAX_OUTPUT_CHARS,
        blob_threshold_kb: int = TOOL_RESULT_THRESHOLD_KB,
        head_chars: int = PREVIEW_HEAD_CHARS,
        tail_chars: int = PREVIEW_TAIL_CHARS,
    ) -> None:
        self.directory = Path(root) / ARTIFACT_SUBDIR
        self._bash_max_chars = bash_max_chars
        self._blob_threshold_kb = blob_threshold_kb
        self._head_chars = head_chars
        self._tail_chars = tail_chars
        # 进程内计数器只是"从哪里开始找"，真正的去重靠 exist_ok=False
        # （进程重启后计数器回零也不会踩到旧文件——教材的 _next_artifact_path）。
        self._counter = 0

    # ---- 判断 ------------------------------------------------------------

    def _preview_budget(self) -> int:
        """换页后留在上下文里的上限（字符）——阈值和它必须咬合。"""

        return self._head_chars + self._tail_chars

    def should_externalize(self, tool_name: str, output: str) -> bool:
        """要不要换页。两道条件都过才换：

        1. **超过阈值**（bash 按字符、其它工具按字节，两套口径的理由见常量注释）；
        2. **省得出来**——输出至少是预览预算的两倍。

        第 2 条是通用底线，也是教材缺的那条：没有它就会出现"外化后的指针
        比原文还长"（教材的 bash 阈值 30000 与它的预览 6KB+24KB 正好撞上）。
        换页要付两次代价——落盘的 IO、以及模型想读全文时的一次 fs_read——
        省不到一半就不值。
        """

        if len(output) < self._preview_budget() * MIN_SHRINK_RATIO:
            return False
        if tool_name == "bash":
            return len(output) > self._bash_max_chars
        return len(output.encode("utf-8")) > self._blob_threshold_kb * 1024

    # ---- 落盘 ------------------------------------------------------------

    def _next_path(self) -> Path:
        """取一个**尚未存在**的文件名，并把它占住（独占创建）。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            self._counter += 1
            candidate = self.directory / f"tool_result_{self._counter:03d}.txt"
            try:
                candidate.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                continue        # 已有证据，跳过——绝不覆盖
            return candidate

    def store(self, tool_name: str, output: str) -> Artifact:
        """把完整输出落盘，返回它的档案件。"""

        path = self._next_path()
        data = output.encode("utf-8")
        path.write_bytes(data)
        return Artifact(
            source_id=path.stem,
            path=path,
            tool_name=tool_name,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            summary=summarize(output),
        )

    # ---- 接缝 ------------------------------------------------------------

    def externalize(self, tool_name: str, output: str) -> str:
        """`run_agent` 的钩子入口：超阈值 → 指针文本；否则原样返回。

        返回值就是**要写进 messages 的内容**——调用方不做第二次判断，
        阈值口径只有这一处（否则两份判据迟早漂移）。
        """

        if not self.should_externalize(tool_name, output):
            return output
        artifact = self.store(tool_name, output)
        return artifact.to_pointer(output, self._head_chars, self._tail_chars)
