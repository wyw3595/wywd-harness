"""标准工具集 + 工具箱装配的回归测试（std_tools 全离线，不碰模型）。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src.harness import file_tools
from src.harness.std_tools import (
    _glob_to_regex,
    _should_skip_file,
    calc,
    find_text,
    glob_files,
    now,
    tree_dir,
)


class NowTests(unittest.TestCase):
    """时钟工具：返回可解析的本地日期时间，且和真实时钟同拍。"""

    def test_now_returns_parseable_local_datetime(self) -> None:
        """格式是 YYYY-MM-DD HH:MM，能被 strptime 解析——模型能直接用。"""

        parsed = datetime.strptime(now(), "%Y-%m-%d %H:%M")
        # 解析出的时间距离真实时钟不超过 1 小时：格式对 + 数值对。
        delta = abs((datetime.now() - parsed).total_seconds())
        self.assertLess(delta, 3600)

    def test_now_minute_precision_does_not_drop_date(self) -> None:
        """返回里同时有日期和时刻，不只是几点——模型要知道"今天几号"。"""

        result = now()
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIn(str(datetime.now().year), result)


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


class GlobToRegexTests(unittest.TestCase):
    """glob → 正则的纯函数：语义对不对全在这里打住，不用碰文件系统。"""

    def test_star_does_not_cross_slash(self) -> None:
        pattern = _glob_to_regex("src/*.py")
        self.assertTrue(pattern.match("src/agent.py"))
        self.assertFalse(pattern.match("src/harness/agent.py"))

    def test_double_star_slash_matches_zero_or_more_dirs(self) -> None:
        """**/ 允许零层——这正是 "**/*.py" 能命中根目录 .py 文件的原因，
        也正是 fnmatch 做不到、必须自己翻译一回的地方。"""

        pattern = _glob_to_regex("**/*.py")
        self.assertTrue(pattern.match("agent.py"))               # 零层
        self.assertTrue(pattern.match("src/harness/agent.py"))   # 多层

    def test_double_star_in_the_middle(self) -> None:
        pattern = _glob_to_regex("src/**/*.py")
        self.assertTrue(pattern.match("src/agent.py"))
        self.assertTrue(pattern.match("src/harness/agent.py"))

    def test_bare_double_star_matches_anything(self) -> None:
        pattern = _glob_to_regex("docs/**")
        self.assertTrue(pattern.match("docs/a/b/c.txt"))
        self.assertFalse(pattern.match("other/a.txt"))

    def test_question_mark_is_exactly_one_char(self) -> None:
        pattern = _glob_to_regex("a?.py")
        self.assertTrue(pattern.match("ab.py"))
        self.assertFalse(pattern.match("abc.py"))

    def test_dot_is_escaped_not_a_wildcard(self) -> None:
        """re.escape 的分内事：模式里的 "." 只能当点，不能当通配符，
        否则 "test_*.py" 连 "test_aXpy" 都会命中。"""

        pattern = _glob_to_regex("test_*.py")
        self.assertTrue(pattern.match("test_a.py"))
        self.assertFalse(pattern.match("test_aXpy"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(_glob_to_regex("**/*.py").match("README.PY"))


class GlobFilesTests(unittest.TestCase):
    """按名字找：补上"读什么"的三格里缺的那一格。"""

    def test_finds_project_python_files(self) -> None:
        result = glob_files("src/**/*.py", max_results=100)
        self.assertIn("src/harness/agent.py", result)
        self.assertIn("src/harness/tools.py", result)

    def test_double_star_reaches_nested_and_root(self) -> None:
        result = glob_files("**/*.md", max_results=50)
        self.assertIn("README.md", result)          # 根目录（零层）
        self.assertIn("NEXT_SESSION.md", result)

    def test_skips_ignored_dirs(self) -> None:
        """忽略名单同样生效——第三方依赖和教材里的文件不该出现。"""

        result = glob_files("**/*.py", max_results=1000)
        self.assertIn("src/harness/tools.py", result)   # 本项目自己的要找到
        self.assertNotIn(".venv", result)
        self.assertNotIn("learn-workbuddy", result)

    def test_explicit_root_bypasses_ignore_list(self) -> None:
        """pattern 相对 root 解释（与 Path.glob 一致），报出的路径相对项目根。"""

        result = glob_files("*.md", root="learn-workbuddy", max_results=10)
        self.assertIn("learn-workbuddy/README.md", result)

    def test_no_match_message_carries_the_pattern(self) -> None:
        # 没找到时要把模式原样回显出来——模型据此才知道自己写的是什么。
        result = glob_files("**/*.zzz", max_results=10)
        self.assertIn("没有", result)
        self.assertIn("**/*.zzz", result)

    def test_truncation_hints_how_to_narrow(self) -> None:
        result = glob_files("**/*.py", max_results=3)
        self.assertIn("已截断", result)
        listed = [line for line in result.splitlines() if not line.startswith("…")]
        self.assertEqual(len(listed), 3)

    def test_rejects_outside_sandbox(self) -> None:
        with self.assertRaises(PermissionError):
            glob_files("*.py", root="../secret-outside")


class SkipPredicateTests(unittest.TestCase):
    """忽略名单的纯函数：打表即可，不用碰文件系统。

    _is_probably_binary 的测试在 test_file_tools.py——它已迁到 file_tools
    （read_file 也要用，住 std_tools 会绕成循环导入）。
    """

    def test_should_skip_file_by_suffix(self) -> None:
        for name in ("a.pyc", "logo.png", "book.pdf", "archive.ZIP", "PHOTO.JPG"):
            with self.subTest(name=name):
                self.assertTrue(_should_skip_file(name))

    def test_should_skip_file_keeps_source_files(self) -> None:
        for name in ("main.py", "README.md", "Makefile", ".gitignore", "config.toml"):
            with self.subTest(name=name):
                self.assertFalse(_should_skip_file(name))

    def test_should_skip_file_only_takes_last_suffix(self) -> None:
        # Path.suffix 只认最后一段：a.tar.gz -> ".gz"（不是 ".tar.gz"）。
        self.assertTrue(_should_skip_file("a.tar.gz"))
        # 没有后缀的名字返回空串，不能炸也不能误判。
        self.assertFalse(_should_skip_file("Makefile"))
        self.assertFalse(_should_skip_file(".env"))


class SearchScopeTests(unittest.TestCase):
    """搜索范围：忽略目录真的被剪枝，但"显式起点"永远放行。

    用临时目录当沙箱（mock 把 ALLOWED_ROOT 换掉），不碰真实项目树。
    patch 的前提是 find_text 在**调用时**读 file_tools.ALLOWED_ROOT
    （它确实如此，见 std_tools 的导入注释），而不是定义时把值焊死。
    """

    def _build_tree(self, tmp: str) -> Path:
        """造一棵含"该搜到的"和"不该搜到的"的目录树。"""

        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "keep.py").write_text("NEEDLE_IN_SRC\n", encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "cached.py").write_text(
            "NEEDLE_IN_PYCACHE\n", encoding="utf-8")
        (root / ".venv" / "Lib").mkdir(parents=True)
        (root / ".venv" / "Lib" / "vendor.py").write_text(
            "NEEDLE_IN_VENV\n", encoding="utf-8")
        # 后缀挡住的那一类：名字是 .pyc，内容其实是纯文本——
        # 内容探测救不了它，只有后缀名单能挡住。
        (root / "copy.pyc").write_text("NEEDLE_IN_PYC_COPY\n", encoding="utf-8")
        return root

    def test_ignored_dirs_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._build_tree(tmp)
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp).resolve()):
                result = find_text("NEEDLE_", root=".")
        self.assertIn("NEEDLE_IN_SRC", result)
        self.assertNotIn("NEEDLE_IN_PYCACHE", result)
        self.assertNotIn("NEEDLE_IN_VENV", result)

    def test_ignored_suffix_skipped_during_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._build_tree(tmp)
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp).resolve()):
                result = find_text("NEEDLE_IN_PYC_COPY", root=".")
        self.assertIn("未找到匹配", result)

    def test_explicit_root_bypasses_ignore_list(self) -> None:
        """效率语义可以被参数绕过——想搜被忽略的目录就直接指过去
        （ripgrep 同款：显式指定的路径不受忽略规则约束）。"""

        with tempfile.TemporaryDirectory() as tmp:
            self._build_tree(tmp)
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp).resolve()):
                result = find_text("NEEDLE_IN_PYCACHE", root="__pycache__")
        self.assertIn("NEEDLE_IN_PYCACHE", result)

    def test_binary_content_never_matches(self) -> None:
        """后缀挡不住的二进制（名字是 .dat）由 NUL 探测兜底。"""

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "blob.dat").write_bytes(b"BEFORE\x00AFTER_NEEDLE")
            with mock.patch.object(file_tools, "ALLOWED_ROOT", Path(tmp).resolve()):
                result = find_text("AFTER_NEEDLE", root=".")
        self.assertIn("未找到匹配", result)


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

    def test_edit_tool_is_registered_and_visible_to_model(self) -> None:
        from scripts.toolbox import build_registry

        registry = build_registry()
        self.assertIsNotNone(registry.get("fs_edit"))
        names = [schema["function"]["name"] for schema in registry.model_schemas()]
        self.assertIn("fs_edit", names)

    def test_edit_tool_is_gated_by_ask_not_whitelisted(self) -> None:
        """新增写工具的接线两头都要钉住。

        漏进 WRITE_TOOLS -> 掉进 default.deny（fail-closed，表现为"工具
        坏了"）；误进 SAFE_TOOLS -> 免审批直接放行，改状态不问人，那是真
        的安全漏洞。两个方向各一条断言，少一条就漏一个方向。
        """

        from scripts.toolbox import SAFE_TOOLS, WRITE_TOOLS, build_policy
        from src.harness.permissions import PermissionAction, ToolRequest

        self.assertIn("fs_edit", WRITE_TOOLS)
        self.assertNotIn("fs_edit", SAFE_TOOLS)

        decision = build_policy().decide(
            ToolRequest(
                tool_use_id="t1",
                name="fs_edit",
                arguments={"path": "README.md"},
            )
        )
        self.assertIs(decision.action, PermissionAction.ASK)
        self.assertEqual(decision.rule_id, "path.write_ask")


if __name__ == "__main__":
    unittest.main()