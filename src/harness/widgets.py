"""s19 Visualizer：不只是文字，还能画图——SVG/HTML widget 流式注入。

============================================================
📚 为什么需要它
============================================================
s18 为止 agent 的输出全是纯文本。让用户看一个系统架构，500 行文字
不如一张图——人类看图 3 秒，看文字 3 分钟。这不是调 DALL-E 画位图：
**代码可视化**——模型生成 SVG/HTML 代码，注入对话流，浏览器原地
渲染。矢量、无限缩放、可编辑、即时。

两步协议（教材同款，防"徒手拼 SVG"）：

  ① read_widget_guide(module, theme)
      → 设计指引：CSS 变量、颜色、排版、布局规则、SVG/HTML 约束
  ② show_widget(title, widget_code, loading_messages)
      → harness **校验**通过后注入对话；不合规给人话错误回灌

为什么模型不能直接吐 SVG：错位、超界、颜色随缘——指引把"画得
好看"从模型自由发挥变成按规范生成；校验把"画坏了"拦在注入之前。

============================================================
📚 安全边界（本项目加的重点）
============================================================
widget_code 是**模型生成的 HTML/SVG**，进前端就是潜在 XSS 载体。
两道防线：
  1. 服务端校验（本模块）：SVG 必须 `<svg` 开头、viewBox 以
     `0 0 680` 起；HTML 片段禁 DOCTYPE/html/head/body（防整页
     劫持）；尺寸上限。
  2. 前端渲染在 **sandbox iframe** 里（sandbox="allow-scripts" 且
     **不带** allow-same-origin——脚本可跑、父页面不可触）。
服务端校验管"协议形状"，iframe 沙箱管"运行时行为"，各查各的。

============================================================
📚 与教材（learn-workbuddy/s19）的差异——都是刻意的
============================================================
- 教材的 WidgetRenderer 把代码写进 .widgets/ 文件并在终端"预览"；
  本项目注入的是**真聊天 UI**：工具结果走 tool_end 事件 → 前端按
  工具名分流（plan_write 计划卡的既有轨道）→ 卡片内 sandbox iframe
  原地渲染。历史重放时证据留在转录的工具消息里（与计划卡同款边界）。
- 教材的 read_me 是"内部准备步骤"（指示模型别向用户叙述它）；
  本项目照搬这条纪律写进工具描述。
- SVG 构建器（svg_node/arrow/bar_chart）教材给模型当拐杖；本项目
  的模型不调它们（那是生成端的活），它们是**测试的确定性参照物**：
  按指引生成的代码必须通过校验——指引与校验互相咬合的证据。

完成标志：tests/test_widgets.py 全部通过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

# viewBox 的约定宽（教材同款）：680 保证两种主题布局下都不溢出。
VIEWBOX_WIDTH = "0 0 680"
MAX_WIDGET_CHARS = 20_000

# 注入协议的标记（前端 store.js 的 show_widget 分支按它解析；
# 两端各持一份字面量——改一个必须改另一个，测试钉住）。
WIDGET_OPEN = "[widget]"
WIDGET_CLOSE = "[/widget]"

_THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#ffffff", "fg": "#1a1a2e", "primary": "#3b82f6",
        "accent": "#f59e0b", "border": "#e2e8f0", "muted": "#64748b",
        "grid": "#f1f5f9",
    },
    "dark": {
        "bg": "#1a1a2e", "fg": "#e2e8f0", "primary": "#60a5fa",
        "accent": "#fbbf24", "border": "#334155", "muted": "#94a3b8",
        "grid": "#27324a",
    },
}


def theme_colors(theme: str = "light") -> dict[str, str]:
    """当前主题的调色板。未知主题 fail-closed 回浅色（不发明颜色）。"""

    return dict(_THEMES.get(str(theme).strip().lower(), _THEMES["light"]))


@dataclass(frozen=True)
class DesignGuide:
    """一个设计模块的规范：变量 + 规则 + 排版。"""

    css_vars: tuple[str, ...]
    rules: tuple[str, ...]
    typography: tuple[str, ...]


_DESIGN_MODULES: dict[str, DesignGuide] = {
    "diagram": DesignGuide(
        css_vars=("bg", "fg", "primary", "accent", "border", "muted"),
        rules=(
            "SVG 的 viewBox 必须以 '0 0 680' 开头（如 0 0 680 400）",
            "节点用圆角矩形（rx=8），填充 primary 的 15% 透明度 + 描边",
            "连接用带箭头 marker 的线，颜色用 muted",
            "一张图最多 7 个节点——多了就拆成多张，中间用文字串联",
        ),
        typography=("标题 20px bold", "正文 14px", "标签 12px"),
    ),
    "chart": DesignGuide(
        css_vars=("bg", "fg", "primary", "accent", "grid", "muted"),
        rules=(
            "SVG 的 viewBox 必须以 '0 0 680' 开头",
            "同图所有系列用一致的调色板（primary / accent）",
            "坐标轴必须有标签；网格线用 grid 色、保持克制",
            "条形图从同一基线起画，宽度间隔一致",
        ),
        typography=("标题 18px bold", "轴标签 12px", "数值标注 12px"),
    ),
    "mockup": DesignGuide(
        css_vars=("bg", "fg", "primary", "border", "muted"),
        rules=(
            "HTML 片段：不许 <!DOCTYPE>/<html>/<head>/<body>",
            "布局用 flex/grid 写在 <style> 里，尺寸按 680 宽设计",
            "控件状态齐全：默认 / hover / disabled 至少给默认态",
            "文字用真实文案，别用 lorem——mockup 是给人评审的",
        ),
        typography=("标题 16px", "正文 13px", "按钮 13px"),
    ),
    "interactive": DesignGuide(
        css_vars=("bg", "fg", "primary", "accent", "border"),
        rules=(
            "HTML 片段：不许 <!DOCTYPE>/<html>/<head>/<body>",
            "脚本写 <script> 里，只操作自己的 DOM（你在沙箱 iframe 里）",
            "初始状态就有内容——不是空白等交互",
            "交互要即时时反馈（hover/点击立刻可见变化）",
        ),
        typography=("正文 14px", "控件标签 13px"),
    ),
    "art": DesignGuide(
        css_vars=("bg", "fg", "primary", "accent"),
        rules=(
            "SVG 的 viewBox 必须以 '0 0 680' 开头",
            "装饰性图形也要有语义锚点——标题或一句说明",
            "配色只用给的变量，不引入第五种颜色",
        ),
        typography=("说明 12px",),
    ),
}

# 两个模块都懂的公共约束（render_guide 里展开讲）。
_SHARED_RULES = (
    "widget_code 是原始 SVG 或 HTML 片段，不是 markdown 代码块",
    "先调 read_widget_guide 拿指引，再动笔；换模块要重新拿",
    "复杂主题拆成多个小 widget，中间用文字串联叙事",
)


def list_modules() -> tuple[str, ...]:
    return tuple(sorted(_DESIGN_MODULES))


def render_guide(module: str, theme: str = "light") -> str:
    """把一个设计模块的指引渲染成给模型看的文本。

    未知模块 fail-closed：列出合法模块让人话指路，不瞎猜。
    """

    name = str(module).strip().lower()
    guide = _DESIGN_MODULES.get(name)
    if guide is None:
        return (f"没有 {name!r} 这个设计模块——可选："
                f"{'、'.join(list_modules())}")
    palette = theme_colors(theme)
    lines = [
        f"# 设计指引：{name}（{theme} 主题）",
        "",
        "## 调色板（CSS 变量 → 具体色值）",
    ]
    lines += [f"- --{key}: {palette[key]}" for key in guide.css_vars
              if key in palette]
    lines += ["", "## 规则"]
    lines += [f"- {rule}" for rule in _SHARED_RULES + guide.rules]
    lines += ["", "## 排版"]
    lines += [f"- {item}" for item in guide.typography]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 校验：注入之前，协议形状必须对
# ═══════════════════════════════════════════════════════════════

_FORBIDDEN_HTML = ("<!doctype", "<html", "<head", "<body")


def validate_widget(code: str) -> list[str]:
    """校验 widget_code 的协议形状，返回错误清单（空 = 通过）。

    只管**形状**（开头/禁标签/尺寸），不管美丑与语义——美丑归设计
    指引（生成端），运行时行为归前端沙箱（渲染端）。三层各司其职。
    """

    text = str(code or "").strip()
    errors: list[str] = []
    if not text:
        return ["widget_code 是空的——没有可渲染的内容"]
    if len(text) > MAX_WIDGET_CHARS:
        errors.append(
            f"widget_code 有 {len(text)} 字符，上限 {MAX_WIDGET_CHARS}"
            "——拆小一点，或简化内容")
    lowered = text.lower()
    if lowered.startswith("<svg"):
        # SVG 路线：viewBox 约定宽度。
        if not re.search(r'viewBox\s*=\s*"0 0 680', text[:500]):
            errors.append(
                f"SVG 的 viewBox 必须以 {VIEWBOX_WIDTH!r} 开头"
                "（如 viewBox=\"0 0 680 400\"）")
    elif any(tag in lowered for tag in _FORBIDDEN_HTML):
        # HTML 路线的禁项：只收片段，整页文档标签出现在任何位置都拒绝。
        errors.append(
            "widget_code 是 HTML 片段，不许包含 "
            "<!DOCTYPE>/<html>/<head>/<body>——去掉外层文档标签")
    elif not lowered.startswith("<"):
        # 既不是 <svg 开头、也不是任何标签开头——不是可渲染的代码。
        errors.append(
            "widget_code 必须以 <svg 开头（SVG），或是不含文档标签的 "
            "HTML 片段（如 <div 或 <style）")
    return errors


def widget_payload(title: str, code: str) -> str:
    """通过校验后，工具结果的正体——前端按 [widget] 标记解析。

    title 放第一行（「」包裹，前端拿去当卡片头）；代码夹在标记中间。
    协议两端各持字面量（这里与 public/src/store.js），测试钉住形状。
    """

    return (f"widget「{str(title).strip() or '未命名'}」已注入对话。\n"
            f"{WIDGET_OPEN}\n{str(code).strip()}\n{WIDGET_CLOSE}")


# ═══════════════════════════════════════════════════════════════
# SVG 构建器：按指引生成的**确定性参照物**（测试用）
# ═══════════════════════════════════════════════════════════════

def svg_node(x: int, y: int, label: str, palette: Mapping[str, str],
             width: int = 120, height: int = 60) -> str:
    """一个圆角矩形节点：primary 15% 填充 + 描边（diagram 模块规则）。"""

    center = x + width // 2
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="{palette["primary"]}" rx="8" opacity="0.15"/>\n'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="none" stroke="{palette["primary"]}" rx="8"/>\n'
        f'<text x="{center}" y="{y + height // 2 + 5}" '
        f'text-anchor="middle" fill="{palette["fg"]}" '
        f'font-size="14">{label}</text>'
    )


def svg_arrow(x1: int, y1: int, x2: int, y2: int,
              palette: Mapping[str, str]) -> str:
    """带箭头 marker 的连接线（diagram 模块规则：muted 色）。"""

    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{palette["muted"]}" '
        f'stroke-width="2" marker-end="url(#arrow)"/>'
    )


def svg_bar_chart(data: list[tuple[str, int]], palette: Mapping[str, str],
                  title: str = "") -> str:
    """一张条形图：同基线、等间距、轴标签齐全（chart 模块规则）。"""

    bars = data or [("", 0)]
    top, bottom = 40, 360
    left, right = 90, 660
    peak = max(value for _, value in bars) or 1
    slot = (right - left) / len(bars)
    bar_width = min(90, slot * 0.6)
    parts = [
        f'<svg viewBox="{VIEWBOX_WIDTH} 400" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="680" height="400" fill="{palette["bg"]}" rx="12"/>',
    ]
    if title:
        parts.append(
            f'<text x="340" y="26" text-anchor="middle" '
            f'fill="{palette["fg"]}" font-size="18" font-weight="bold">'
            f'{title}</text>')
    # 网格线（克制：3 条）。
    for frac in (0.25, 0.5, 0.75):
        y = top + (bottom - top) * frac
        parts.append(
            f'<line x1="{left}" y1="{y:.0f}" x2="{right}" y2="{y:.0f}" '
            f'stroke="{palette["grid"]}" stroke-width="1"/>')
    for index, (label, value) in enumerate(bars):
        x = left + slot * index + (slot - bar_width) / 2
        height = (bottom - top) * (value / peak)
        parts.append(
            f'<rect x="{x:.0f}" y="{bottom - height:.0f}" '
            f'width="{bar_width:.0f}" height="{height:.0f}" '
            f'fill="{palette["primary"]}" rx="4"/>')
        parts.append(
            f'<text x="{x + bar_width / 2:.0f}" y="{bottom - height - 6:.0f}" '
            f'text-anchor="middle" fill="{palette["fg"]}" '
            f'font-size="12">{value}</text>')
        parts.append(
            f'<text x="{x + bar_width / 2:.0f}" y="{bottom + 18:.0f}" '
            f'text-anchor="middle" fill="{palette["muted"]}" '
            f'font-size="12">{label}</text>')
    # 基线 + 轴。
    parts.append(
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" '
        f'stroke="{palette["muted"]}" stroke-width="2"/>')
    parts.append("</svg>")
    return "\n".join(parts)
