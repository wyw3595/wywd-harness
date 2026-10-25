"""s20 Result Presentation 的离线单元测试：卡片 × 优先级 × 沙箱 × 协议。

核心断言群：
  - 顺序即优先级：第一个且只有第一个 is_primary；
  - 沙箱边界：越界路径拒绝建卡（存在性是信息，不是展示），URL 不受
    文件系统边界约束；
  - 缺失文件不炸：卡片照建、exists=False——"说好的交付物不在"要
    摆出来；
  - [files] 注入协议的合同测试：复刻 store.js 的解析（indexOf +
    JSON.parse），两端协议谁改谁红。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.harness.presentation import (
    FILES_CLOSE,
    FILES_OPEN,
    ArtifactCard,
    build_cards,
    format_size,
    present_payload,
    render_summary,
)


def _cards(files, root=None):
    return build_cards(files, root)


# ═══════════════════════════════════════════════════════════════
# 一、卡片：类型路由 × 大小 × 存在性
# ═══════════════════════════════════════════════════════════════

class CardTests(unittest.TestCase):

    def test_format_size(self) -> None:
        self.assertEqual(format_size(512), "512B")
        self.assertEqual(format_size(2048), "2.0KB")
        self.assertEqual(format_size(3 * 1024 * 1024), "3.0MB")

    def test_file_card_from_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.html").write_text("<h1>x</h1>" * 20,
                                              encoding="utf-8")
            card = _cards(["report.html"], root)[0]
        self.assertEqual(card.name, "report.html")
        self.assertEqual(card.icon, "🌐")
        self.assertEqual(card.category, "html")
        self.assertTrue(card.exists)
        self.assertGreater(card.size_bytes, 0)
        self.assertEqual(card.extension, ".html")

    def test_missing_file_is_honest_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = _cards(["ghost.md"], Path(tmp))[0]
        self.assertFalse(card.exists)
        self.assertEqual(card.size_bytes, 0)
        # 单行渲染把"未找到"摆出来，而不是悄悄漏掉。
        self.assertIn("未找到", card.render())

    def test_type_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, icon in (("a.svg", "🖼️"), ("b.py", "🐍"),
                               ("c.json", "📋"), ("d.weird", "📎")):
                (root / name).write_text("x", encoding="utf-8")
            cards = {c.name: c for c in _cards(
                ["a.svg", "b.py", "c.json", "d.weird"], root)}
        self.assertEqual(cards["a.svg"].category, "image")
        self.assertEqual(cards["b.py"].category, "code")
        self.assertEqual(cards["c.json"].category, "data")
        self.assertEqual(cards["d.weird"].category, "other")

    def test_url_cards(self) -> None:
        cards = _cards(["https://example.com/page",
                       "http://localhost:3000"])
        self.assertTrue(cards[0].is_url)
        self.assertEqual(cards[0].icon, "🔗")
        self.assertEqual(cards[1].icon, "🖥️")   # localhost 专门标注
        self.assertEqual(cards[0].size_text, "—")


# ═══════════════════════════════════════════════════════════════
# 二、优先级：顺序即观看优先级
# ═══════════════════════════════════════════════════════════════

class PriorityTests(unittest.TestCase):

    def test_first_and_only_first_is_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("a.md", "b.md", "c.md"):
                (root / name).write_text("x", encoding="utf-8")
            cards = _cards(["c.md", "a.md", "b.md"], root)
        self.assertTrue(cards[0].is_primary)
        self.assertEqual(cards[0].name, "c.md")   # 顺序原样保留
        self.assertFalse(any(c.is_primary for c in cards[1:]))

    def test_empty_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _cards([])
        with self.assertRaises(ValueError):
            _cards(["  "])


# ═══════════════════════════════════════════════════════════════
# 三、沙箱边界：存在性是信息，不是展示
# ═══════════════════════════════════════════════════════════════

class SandboxTests(unittest.TestCase):

    def test_relative_resolves_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "out").mkdir()
            (root / "out" / "r.md").write_text("x", encoding="utf-8")
            card = _cards(["out/r.md"], root)[0]
        self.assertTrue(card.exists)

    def test_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (Path(outside) / "secret.env").write_text("k=v",
                                                      encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                _cards([str(Path(outside) / "secret.env")], root)
            self.assertIn("沙箱外", str(ctx.exception))
            # 相对路径的 ../ 穿越同样拒绝。
            with self.assertRaises(ValueError):
                _cards(["../secret.env"], root)

    def test_url_bypasses_filesystem_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cards = _cards(["https://example.com"], Path(tmp))
        self.assertTrue(cards[0].is_url)


# ═══════════════════════════════════════════════════════════════
# 四、注入协议：合同测试（复刻 store.js 的解析）
# ═══════════════════════════════════════════════════════════════

class PayloadTests(unittest.TestCase):

    def test_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.html").write_text("x", encoding="utf-8")
            payload = present_payload(["r.html", "missing.md"],
                                      "报告与数据", root)
        self.assertTrue(payload.startswith("交付 2 个文件：报告与数据"))
        self.assertIn(FILES_OPEN, payload)
        self.assertIn(FILES_CLOSE, payload)

    def test_frontend_parse_contract(self) -> None:
        # 复刻 store.js：indexOf 夹块 + JSON.parse——payload 必须能被
        # 那段 JS 无损还原出卡片数组。谁改协议哪端，这条就红。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.html").write_text("<h1>x</h1>", encoding="utf-8")
            payload = present_payload(
                ["r.html", "https://example.com", "ghost.md"], "", root)
        start = payload.index(FILES_OPEN)
        end = payload.index(FILES_CLOSE)
        cards = json.loads(payload[start + len(FILES_OPEN):end].strip())
        self.assertEqual(len(cards), 3)
        by_name = {c["name"]: c for c in cards}
        self.assertTrue(by_name["r.html"]["isPrimary"])
        self.assertTrue(by_name["r.html"]["exists"])
        self.assertTrue(by_name["https://example.com"]["isUrl"])
        self.assertFalse(by_name["ghost.md"]["exists"])

    def test_render_summary_for_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.md").write_text("x", encoding="utf-8")
            cards = _cards(["r.md", "ghost.md"], root)
        text = render_summary(cards, "本次产出")
        self.assertIn("★", text)           # 主交付标注
        self.assertIn("未找到", text)       # 缺失如实摆出


# ═══════════════════════════════════════════════════════════════
# 五、工具接线：真延迟轨道 + 越界回灌
# ═══════════════════════════════════════════════════════════════

class ToolboxPresentTests(unittest.TestCase):

    def _tools(self, root: Path) -> dict:
        from scripts.toolbox import build_deferred_tools

        return {tool.name: tool for tool in build_deferred_tools(root)}

    def test_tool_is_deferred_and_safe(self) -> None:
        from scripts.toolbox import SAFE_TOOLS

        with tempfile.TemporaryDirectory() as tmp:
            tools = self._tools(Path(tmp))
            self.assertIn("present_files", tools)
            self.assertTrue(tools["present_files"].defer)
            self.assertIn("present_files", SAFE_TOOLS)

    def test_handler_builds_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "报告.md").write_text("# 交付", encoding="utf-8")
            handler = self._tools(root)["present_files"].handler
            result = handler(files=["报告.md"], explanation="本次产出")
        self.assertIn(FILES_OPEN, result)
        self.assertIn("本次产出", result)

    def test_handler_outside_sandbox_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "x.md").write_text("x", encoding="utf-8")
            handler = self._tools(Path(tmp))["present_files"].handler
            result = handler(files=[str(Path(outside) / "x.md")])
        self.assertIn("没交付", result)
        self.assertIn("沙箱外", result)

    def test_handler_empty_list_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._tools(Path(tmp))["present_files"].handler
            result = handler(files=[])
        self.assertIn("没交付", result)

    def test_full_rail_through_defer_execute(self) -> None:
        from scripts.toolbox import build_registry_for
        from src.harness.workspace import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r.md").write_text("x", encoding="utf-8")
            registry = build_registry_for(
                Workspace(workspace_id="p-test", root=root))
            self.assertIn("present_files", registry.load_by_name(
                ["present_files"]))
            result = registry.defer_execute(
                "present_files",
                {"files": ["r.md"], "explanation": "端到端"})
            self.assertIn(FILES_OPEN, result)


if __name__ == "__main__":
    unittest.main()
