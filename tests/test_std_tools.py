"""标准工具集 + 工具箱装配的回归测试（std_tools 全离线，不碰模型）。"""

import unittest

from src.harness.std_tools import calc, find_text, tree_dir


class CalcTests(unittest.TestCase):
    """安全数学求值：算得到 + 攻不破。"""

    def test_arithmetic(self) -> None:
        self.assertEqual(calc("1 + 2 * 3"), "7")
        self.assertEqual(calc("(1 + 2) * 3 - 1"), "8")
        self.assertEqual(calc("10 / 4"), "2.5")

    def test_pow_and_math_function(self) -> None:
        self.assertEqual(calc("2 ** 10"), "1024")
        self.assertEqual(calc("sqrt(16) * 2"), "8")

    def test_negative_number(self) -> None:
        self.assertEqual(calc("-5 + 3"), "-2")

    def test_rejects_import(self) -> None:
        with self.assertRaises(ValueError):
            calc("__import__('os')")

    def test_rejects_variable_access(self) -> None:
        with self.assertRaises(ValueError):
            calc("open('/etc/passwd')")

    def test_rejects_syntax_error(self) -> None:
        with self.assertRaises(ValueError):
            calc("1 +")


class FindTextTests(unittest.TestCase):
    """项目内搜索：搜得到 + 不越界。"""

    def test_finds_known_line(self) -> None:
        # toolbox.py 里有一个稳定字符串，项目自己可控。
        result = find_text("def get_weather", root="scripts")
        self.assertIn("toolbox.py", result)
        self.assertIn("def get_weather", result)

    def test_case_insensitive_by_default_matches(self) -> None:
        # 项目里统一是 def get_weather，大写变体必须靠忽略大小写命中。
        result = find_text("DEF GET_WEATHER", root="scripts")
        self.assertIn("toolbox.py", result)

    def test_no_match_returns_message(self) -> None:
        result = find_text("绝对不存在的关键字 xyzzy_not_found", root="scripts", max_results=3)
        self.assertIn("未找到匹配", result)

    def test_rejects_outside_sandbox(self) -> None:
        with self.assertRaises(PermissionError):
            find_text("whatever", root="../secret-outside")


class TreeDirTests(unittest.TestCase):
    """目录树：渲染稳定 + 尊重沙箱。"""

    def test_lists_top_level_src(self) -> None:
        result = tree_dir("src", depth=1)
        self.assertIn("harness/", result)

    def test_depth_limits_output(self) -> None:
        shallow = tree_dir("src", depth=1)
        self.assertIn("harness/", shallow)
        # depth=2 应该更深一层（出现 harness 下的子目录/文件）
        deep = tree_dir("src", depth=2)
        self.assertNotEqual(shallow, deep)

    def test_rejects_outside_sandbox(self) -> None:
        with self.assertRaises(PermissionError):
            tree_dir("../secret-outside")


class ToolboxAssemblyTests(unittest.TestCase):
    """组装层：桥接工具就位 + 延迟工具被模型目录排除 + 节省可量化。"""

    def test_bridge_and_deferred_registered(self) -> None:
        from scripts.toolbox import build_registry

        registry = build_registry()
        self.assertIsNotNone(registry.get("ToolSearch"))
        self.assertIsNotNone(registry.get("DeferExecuteTool"))
        self.assertIsNotNone(registry.get("tree_dir"))

    def test_bridge_parameter_names_match_schema(self) -> None:
        """校验读 schema、execute 做 **kwargs 展开——两套判据脱节就是
        "unexpected keyword argument" 的源头（实战踩过的坑）：lambda
        参数名必须与 input_schema 的键逐字一致。"""
        import inspect

        from scripts.toolbox import build_registry

        registry = build_registry()
        for name in ("ToolSearch", "DeferExecuteTool"):
            tool = registry.get(name)
            sig_names = set(inspect.signature(tool.handler).parameters)
            schema_keys = set(tool.input_schema.get("properties", {}))
            self.assertEqual(
                sig_names, schema_keys,
                f"{name} 的 handler 参数名与手写 schema 键不一致",
            )

    def test_tool_search_handles_all_query_shapes(self) -> None:
        """ToolSearch 的查询词不可预测：精确名 / 名字混用途词 / 纯用途词
        三种传法都必须能命中 tree_dir（实测踩坑：模型传 ['tree_dir 目录树']，
        旧 handler 一棍子交给按名精确匹配 → "没有这个延迟工具"）。"""
        from scripts.toolbox import build_registry

        registry = build_registry()
        search = registry.get("ToolSearch").handler
        # 离线只测：必填参数齐全才进 handler（注册表校验窗在 s02）
        cases = {
            "精确名": (["tree_dir"],),
            "名字混用途": (["tree_dir 目录树"],),
            "纯用途词": (["目录树"],),
        }
        for label, args in cases.items():
            with self.subTest(label=label):
                rendered = search(*args)
                self.assertIn("tree_dir", rendered)
                self.assertIn("✓", rendered, f"{label} 未命中")

    def test_system_prompt_carries_deferred_directory(self) -> None:
        """目录是独立工件、常驻系统提示（对齐 s03 的 symbol table）——
        模型在启动时看到"有哪些延迟工具、各是干什么的"（符号表）。"""
        from scripts.toolbox import DEFERRED_TOOLS, build_system_prompt

        prompt = build_system_prompt()
        for deferred in DEFERRED_TOOLS:
            self.assertIn(deferred.name, prompt)

    def test_tool_search_description_stays_clean(self) -> None:
        """ToolSearch 描述与目录职责分离：目录常驻 system，描述只负责
        "按名/按词召回"——不挟带目录（s03 原版：描述干净，前置检查
        由 system 完成）。"""
        from scripts.toolbox import DEFERRED_TOOLS, build_registry

        registry = build_registry()
        ts_description = registry.get("ToolSearch").description
        for deferred in DEFERRED_TOOLS:
            self.assertNotIn(deferred.name, ts_description)

    def test_model_schemas_exclude_deferred(self) -> None:
        from scripts.toolbox import build_registry

        registry = build_registry()
        names = [schema["function"]["name"] for schema in registry.model_schemas()]
        self.assertIn("calc", names)
        self.assertIn("ToolSearch", names)
        self.assertNotIn("tree_dir", names)  # 延迟工具对模型不可见

    def test_token_report_quantifies_saving(self) -> None:
        from scripts.toolbox import build_registry

        report = build_registry().token_report()
        self.assertGreater(report["full"], report["current"])
        self.assertGreater(report["saved"], 0)


if __name__ == "__main__":
    unittest.main()