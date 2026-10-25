"""s20 Result Presentation：做完要交付，不只是说。

============================================================
📚 为什么需要它
============================================================
s19 为止 agent 能产出了——但交付环节有个隐藏问题：agent 说"我生成了
report.html，你可以在工作目录找到它"，然后用户自己去文件管理器里翻。
一次生成 5 个文件就是 500 字的"寻物启事"。

答案是 **present_files：唯一的交付入口**。任务做完，模型把交付物
一次性列出来——顺序即优先级（第一个最重要，标注主交付），每个
文件一张 artifact 卡（类型图标/名字/大小/存在性）。用户不需要问
"东西在哪"，东西就在眼前。

三条使用纪律（写进工具描述，教材同款）：
  1. 只 present **新生成的交付物**——读过的不算、改过几行的不算；
  2. 顺序 = 观看优先级，最重要的放第一个；
  3. 它是**补充**：文字说明做了什么 + present_files 展示成果，两者
     一起才是完整交付。

============================================================
📚 与教材（learn-workbuddy/s20）的差异——都是刻意的
============================================================
- 教材的 present 会**自动打开**第一个文件（预览面板/内置浏览器）；
  本项目的聊天 UI 还没有预览面板，本课先落 artifact 卡片（主交付
  打 ★ 标），自动打开留待 UI 有面板时接——机制位（is_primary）已经
  在协议里，前端到时候直接用。
- 教材对任意路径建卡；本项目的模型活在沙箱里，**交付物路径必须
  落在沙箱内**（fail-closed）——对沙箱外的路径建卡等于泄露它的
  存在性与大小，这是信息泄露，不是展示。
- 缺失的文件不炸整个调用：卡片照建、exists=False——"说好的交付物
  不在"是要诚实摆出来的事实，不是要藏起来的错误（证据与派生
  分离的老原则：卡片如实报告磁盘状态）。

注入协议（[files] 标记，与 public/src/store.js 各持字面量，合同测试
钉住——同 s19 [widget] 的手法）。

完成标志：tests/test_presentation.py 全部通过。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

FILES_OPEN = "[files]"
FILES_CLOSE = "[/files]"

# 类型 → (图标, 类别)：展示路由的单一真源。
FILE_TYPE_CONFIG: dict[str, tuple[str, str]] = {
    ".html": ("🌐", "html"), ".htm": ("🌐", "html"),
    ".svg": ("🖼️", "image"), ".png": ("🖼️", "image"),
    ".jpg": ("🖼️", "image"), ".jpeg": ("🖼️", "image"),
    ".pdf": ("📄", "doc"), ".docx": ("📄", "doc"), ".pptx": ("📊", "doc"),
    ".mp4": ("🎬", "video"), ".mov": ("🎬", "video"),
    ".py": ("🐍", "code"), ".js": ("📜", "code"), ".ts": ("📜", "code"),
    ".json": ("📋", "data"), ".csv": ("📋", "data"),
    ".md": ("📝", "doc"), ".txt": ("📝", "doc"),
}


def format_size(bytes_size: int) -> str:
    """字节数 → 人读的大小（B/KB/MB）。"""

    if bytes_size < 1024:
        return f"{bytes_size}B"
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f}KB"
    return f"{bytes_size / (1024 * 1024):.1f}MB"


def _is_url(entry: str) -> bool:
    return str(entry).strip().lower().startswith(("http://", "https://"))


@dataclass(frozen=True)
class ArtifactCard:
    """一张交付物卡片：路径、类型、大小、存在性、是否主交付。"""

    path: str
    name: str
    extension: str
    icon: str
    category: str
    size_text: str
    size_bytes: int
    exists: bool
    is_primary: bool = False
    is_url: bool = False

    def to_dict(self) -> dict:
        """JSON 协议视图（前端按它渲染卡片）。"""

        return {
            "path": self.path, "name": self.name, "icon": self.icon,
            "category": self.category, "size": self.size_text,
            "exists": self.exists, "isPrimary": self.is_primary,
            "isUrl": self.is_url,
        }

    def render(self) -> str:
        """终端友好的单行渲染（chat 入口/日志用）。"""

        marker = "★" if self.is_primary else " "
        if self.is_url:
            return f"  {marker} {self.icon} {self.name}  [URL]"
        if not self.exists:
            return f"  {marker} {self.icon} {self.name}  [未找到]"
        return f"  {marker} {self.icon} {self.name}  ({self.size_text})"


def _url_card(entry: str, is_primary: bool) -> ArtifactCard:
    is_local = "localhost" in entry or "127.0.0.1" in entry
    return ArtifactCard(
        path=entry, name=entry, extension="[URL]",
        icon="🖥️" if is_local else "🔗", category="url",
        size_text="—", size_bytes=0, exists=True,
        is_primary=is_primary, is_url=True,
    )


def _file_card(resolved: Path, display: str, is_primary: bool) -> ArtifactCard:
    ext = resolved.suffix.lower()
    icon, category = FILE_TYPE_CONFIG.get(ext, ("📎", "other"))
    exists = resolved.exists()
    size = resolved.stat().st_size if exists else 0
    return ArtifactCard(
        path=display, name=resolved.name, extension=ext or "(无扩展名)",
        icon=icon, category=category,
        size_text=format_size(size), size_bytes=size, exists=exists,
        is_primary=is_primary,
    )


def build_cards(files: Sequence[str],
                root: Path | None = None) -> tuple[ArtifactCard, ...]:
    """交付清单 → 卡片。顺序即优先级：第一个 is_primary。

    root 给了（装配层传沙箱根）就走**沙箱边界**：相对路径按 root 解析，
    解析结果必须落在 root 里——沙箱外的路径拒绝建卡（存在性与大小
    是信息，不是展示）。URL 不受边界约束（它本来就不在文件系统里）。
    缺失文件不炸：卡片照建、exists=False（说好的交付物不在，要摆出来）。
    """

    if not files:
        raise ValueError("交付清单是空的——没有可展示的东西")
    cards: list[ArtifactCard] = []
    for entry in files:
        text = str(entry).strip()
        if not text:
            raise ValueError("交付清单里有空条目")
        is_primary = not cards   # 第一个（且只有第一个）是主交付
        if _is_url(text):
            cards.append(_url_card(text, is_primary))
            continue
        path = Path(text)
        if root is not None:
            resolved = (root / path).resolve() if not path.is_absolute() \
                else path.resolve()
            root_resolved = Path(root).resolve()
            if not str(resolved).startswith(str(root_resolved)):
                raise ValueError(
                    f"交付物 {text!r} 在沙箱外——只交付本工作区里"
                    "新生成的东西")
            cards.append(_file_card(resolved, text, is_primary))
        else:
            cards.append(_file_card(path, text, is_primary))
    return tuple(cards)


def present_payload(files: Sequence[str], explanation: str = "",
                    root: Path | None = None) -> str:
    """通过校验后，工具结果的正体——前端按 [files] 标记解析 JSON。"""

    cards = build_cards(files, root)
    header = f"交付 {len(cards)} 个文件"
    note = f"：{explanation.strip()}" if explanation.strip() else "。"
    payload = json.dumps(
        [card.to_dict() for card in cards], ensure_ascii=False)
    return f"{header}{note}\n{FILES_OPEN}\n{payload}\n{FILES_CLOSE}"


def render_summary(cards: Sequence[ArtifactCard],
                   explanation: str = "") -> str:
    """给人读的多行摘要（终端 chat 入口用，与卡片并行）。"""

    lines = [f"交付 {len(cards)} 个文件"
             + (f"：{explanation.strip()}" if explanation.strip() else "。")]
    lines += [card.render() for card in cards]
    return "\n".join(lines)
