"""s19 Visualizer 的离线单元测试：设计指引 × 校验 × 注入协议 × 工具接线。

核心断言群：
  - 指引与校验互相咬合：按指引（svg_bar_chart 等构建器）生成的代码
    必须通过校验——两套规则不允许各说各话；
  - 校验只管协议形状（开头/禁标签/尺寸），错误文案可行动；
  - 注入协议（[widget] 标记）的形状被测试钉住——前端 store.js 靠它
    解析，两端各持字面量，谁改谁红；
  - 工具走真延迟轨道（ToolSearch → defer_execute），校验失败回灌
    人话。
"""

import tempfile
import unittest
from pathlib import Path

from src.harness.widgets import (
    MAX_WIDGET_CHARS,
    WIDGET_CLOSE,
    WIDGET_OPEN,
    list_modules,
    render_guide,
    svg_arrow,
    svg_bar_chart,
    svg_node,
    theme_colors,
    validate_widget,
    widget_payload,
)


def _svg(inner: str = '<rect width="10" height="10"/>') -> str:
    return (f'<svg viewBox="0 0 680 400" '
            f'xmlns="http://www.w3.org/2000/svg">{inner}</svg>')


# ═══════════════════════════════════════════════════════════════
# 一、设计指引：模块 × 主题
# ═══════════════════════════════════════════════════════════════

class GuideTests(unittest.TestCase):

    def test_five_modules(self) -> None:
        self.assertEqual(
            list_modules(),
            ("art", "chart", "diagram", "interactive", "mockup"))

    def test_guide_has_palette_rules_typography(self) -> None:
        guide = render_guide("diagram")
        self.assertIn("调色板", guide)
        self.assertIn("--primary:", guide)          # CSS 变量给了具体色值
        self.assertIn("viewBox", guide)             # 指引里带着硬约束
        self.assertIn("排版", guide)

    def test_unknown_module_fails_closed_with_options(self) -> None:
        guide = render_guide("nope")
        self.assertIn("没有", guide)
        self.assertIn("diagram", guide)             # 人话指路

    def test_themes(self) -> None:
        light = theme_colors("light")
        dark = theme_colors("dark")
        self.assertNotEqual(light["bg"], dark["bg"])
        for palette in (light, dark):
            for key in ("bg", "fg", "primary", "muted"):
                self.assertIn(key, palette)
        # 未知主题 fail-closed 回浅色——不发明颜色。
        self.assertEqual(theme_colors("neon"), light)


# ═══════════════════════════════════════════════════════════════
# 二、校验：协议形状，错误可行动
# ═══════════════════════════════════════════════════════════════

class ValidationTests(unittest.TestCase):

    def test_valid_svg_and_html_pass(self) -> None:
        self.assertEqual(validate_widget(_svg()), [])
        self.assertEqual(validate_widget(
            '<div style="padding:8px">对比表格</div>'), [])

    def test_svg_needs_the_agreed_viewbox(self) -> None:
        errors = validate_widget(
            '<svg viewBox="0 0 300 200"><rect/></svg>')
        self.assertEqual(len(errors), 1)
        self.assertIn("0 0 680", errors[0])         # 错误文案给出正确值

    def test_html_fragment_rejects_document_tags(self) -> None:
        for bad in ("<!DOCTYPE html><div>x</div>",
                    "<html><body>x</body></html>",
                    "<head><title>x</title></head>"):
            errors = validate_widget(bad)
            self.assertTrue(errors, bad)
            self.assertIn("片段", "".join(errors))

    def test_empty_and_oversized_rejected(self) -> None:
        self.assertEqual(len(validate_widget("   ")), 1)
        errors = validate_widget("<div>" + "x" * (MAX_WIDGET_CHARS + 1)
                                 + "</div>")
        self.assertTrue(any("上限" in e for e in errors))

    def test_plain_text_rejected_with_guidance(self) -> None:
        errors = validate_widget("这是一段普通文字")
        self.assertEqual(len(errors), 1)
        self.assertIn("<svg", errors[0])            # 告诉模型该给什么


# ═══════════════════════════════════════════════════════════════
# 三、注入协议：前端靠它解析，形状钉死
# ═══════════════════════════════════════════════════════════════

class PayloadTests(unittest.TestCase):

    def test_payload_shape(self) -> None:
        payload = widget_payload("系统架构", _svg())
        self.assertTrue(payload.startswith("widget「系统架构」"))
        self.assertIn(WIDGET_OPEN, payload)
        self.assertIn(WIDGET_CLOSE, payload)

    def test_frontend_parse_logic(self) -> None:
        # 复刻 store.js 的解析（indexOf + 正则取标题）：payload 必须能
        # 被那段 JS 无损还原出标题与代码——两端协议的"合同测试"。
        code = _svg('<circle cx="10" cy="10" r="5"/>')
        payload = widget_payload("数据流", code)
        start = payload.index(WIDGET_OPEN)
        end = payload.index(WIDGET_CLOSE)
        extracted = payload[start + len(WIDGET_OPEN):end].strip()
        self.assertEqual(extracted, code.strip())
        import re
        title = re.match(r"^widget「(.+?)」", payload).group(1)
        self.assertEqual(title, "数据流")


# ═══════════════════════════════════════════════════════════════
# 四、构建器：按指引生成的代码必须通过校验（指引 ↔ 校验咬合）
# ═══════════════════════════════════════════════════════════════

class BuildersTests(unittest.TestCase):

    def test_bar_chart_passes_validation_both_themes(self) -> None:
        for theme in ("light", "dark"):
            chart = svg_bar_chart(
                [("Python", 82), ("Go", 74), ("Rust", 91)],
                theme_colors(theme), title="性能对比")
            self.assertEqual(validate_widget(chart), [], theme)
            self.assertIn("Python", chart)
            self.assertIn("91", chart)              # 数值标注在

    def test_diagram_fragments_compose_into_valid_svg(self) -> None:
        palette = theme_colors("light")
        parts = [
            '<svg viewBox="0 0 680 400" xmlns="http://www.w3.org/2000/svg">',
            '<defs><marker id="arrow" markerWidth="10" markerHeight="10" '
            'refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" '
            f'fill="{palette["muted"]}"/></marker></defs>',
            svg_node(80, 100, "API", palette),
            svg_node(320, 100, "DB", palette),
            svg_arrow(200, 130, 320, 130, palette),
            "</svg>",
        ]
        diagram = "\n".join(parts)
        self.assertEqual(validate_widget(diagram), [])
        self.assertIn("API", diagram)


# ═══════════════════════════════════════════════════════════════
# 五、工具接线：真延迟轨道 + 校验失败回灌
# ═══════════════════════════════════════════════════════════════

class ToolboxWidgetTests(unittest.TestCase):

    def _tools(self, root: Path) -> dict:
        from scripts.toolbox import build_deferred_tools

        return {tool.name: tool for tool in build_deferred_tools(root)}

    def test_tools_are_deferred_and_safe(self) -> None:
        from scripts.toolbox import SAFE_TOOLS

        with tempfile.TemporaryDirectory() as tmp:
            tools = self._tools(Path(tmp))
            self.assertIn("show_widget", tools)
            self.assertIn("read_widget_guide", tools)
            for name in ("show_widget", "read_widget_guide"):
                self.assertTrue(tools[name].defer)   # 不常驻 schema
                self.assertIn(name, SAFE_TOOLS)      # 纯呈现免审批

    def test_guide_tool_returns_guide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._tools(Path(tmp))["read_widget_guide"].handler
            result = handler(module="chart", theme="dark")
        self.assertIn("chart", result)
        self.assertIn("--primary:", result)

    def test_show_widget_valid_returns_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._tools(Path(tmp))["show_widget"].handler
            result = handler(title="架构图",
                             widget_code=_svg(),
                             loading_messages=["准备图形"])
        self.assertIn(WIDGET_OPEN, result)
        self.assertIn("架构图", result)

    def test_show_widget_invalid_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._tools(Path(tmp))["show_widget"].handler
            result = handler(title="坏图",
                             widget_code='<svg viewBox="0 0 100 100"/>')
        self.assertIn("没渲染", result)
        self.assertIn("0 0 680", result)            # 告诉模型怎么改

    def test_full_rail_through_defer_execute(self) -> None:
        # 端到端：ToolSearch 加载 → defer_execute（参数过 schema 校验）
        # → 校验通过 → payload。手写 schema 的键名与 handler 参数对齐。
        from scripts.toolbox import build_registry_for
        from src.harness.workspace import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            registry = build_registry_for(
                Workspace(workspace_id="w-test", root=Path(tmp)))
            loaded = registry.load_by_name(["show_widget"])
            self.assertIn("show_widget", loaded)
            result = registry.defer_execute(
                "show_widget",
                {"title": "性能对比",
                 "widget_code": svg_bar_chart(
                     [("A", 3), ("B", 5)], theme_colors()),
                 "loading_messages": ["画图中"]})
            self.assertIn(WIDGET_OPEN, result)


if __name__ == "__main__":
    unittest.main()
