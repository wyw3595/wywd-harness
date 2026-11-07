"""标准工具集 + 工具箱装配的回归测试（std_tools 全离线，不碰模型）。"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src.harness import file_tools
from src.harness.std_tools import (
    BASH_OUTPUT_LIMIT,
    _HARNESS_ROOT,
    _clean_env,
    _glob_to_regex,
    _should_skip_file,
    bash_output,
    bash_stop,
    calc,
    find_text,
    glob_files,
    now,
    plan_write,
    run_bash,
    run_bash_background,
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


class PlanWriteTests(unittest.TestCase):
    """计划工具（plan-and-execute 第 1 步）：全文重写 + 状态前缀规范化。

    无状态是它的安全声明：计划活在 transcript 的工具结果里，工具本身
    只做"规范化渲染"。这里钉住输出格式（前端计划卡靠它渲染）、状态
    前缀的容错解析、以及坏参数的人话报错。
    """

    def test_mixed_statuses_render_canonically(self) -> None:
        """各种写法规范化成同一格式：进度行 + 对齐的编号清单。"""

        result = plan_write([
            "[x] 读配置文件",
            "- [>] 修依赖",          # markdown checkbox 习惯也要吃
            "跑测试",
            "[!] 部署",
        ])
        self.assertEqual(result, "\n".join([
            "【计划 1/4】",
            "  [x] 1. 读配置文件",
            "  [>] 2. 修依赖",
            "  [ ] 3. 跑测试",
            "  [!] 4. 部署",
        ]))

    def test_done_marker_is_case_insensitive(self) -> None:
        result = plan_write(["[X] 已完成的事"])
        self.assertIn("[x] 1. 已完成的事", result)
        self.assertIn("【计划 1/1 · 全部完成】", result)

    def test_all_done_header_announces_completion(self) -> None:
        result = plan_write(["[x] 甲", "[x] 乙"])
        self.assertTrue(result.startswith("【计划 2/2 · 全部完成】"))

    def test_empty_list_gets_actionable_error(self) -> None:
        """空计划不是崩溃是可行动文案：模型能据此修正参数。"""

        self.assertIn("至少", plan_write([]))
        self.assertIn("第 2 项是空的", plan_write(["有内容", "   "]))

    def test_non_string_item_gets_actionable_error(self) -> None:
        """模型把对象塞进数组时（顶层 array 校验拦不住），错误指明第几项。"""

        self.assertIn("第 2 项必须是字符串", plan_write(["a", {"task": "b"}]))

    def test_bare_marker_without_text_gets_actionable_error(self) -> None:
        self.assertIn("只有状态前缀没有内容", plan_write(["[x]"]))


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

    def test_states_the_hard_boundaries(self) -> None:
        """四类硬边界必须写清楚——它们对应代码里**真实存在**的拒绝路径。

        提示词与实现对不上（写了不存在的边界、或漏了存在的边界）都会让模型
        白撞一次，而撞墙的代价是一次完整的工具往返。
        """

        from scripts.toolbox import build_system_prompt

        prompt = build_system_prompt()
        for fragment in ("工作目录", "审批", "硬性拒绝", "[Artifact:"):
            self.assertIn(fragment, prompt, f"缺了边界说明：{fragment}")

    def test_states_the_step_budget(self) -> None:
        """预算要跟着参数走——它是"讲给模型听的上限"，不是写死的文案。"""

        from scripts.toolbox import MAX_AGENT_STEPS_DEFAULT, build_system_prompt

        self.assertIn(f"{MAX_AGENT_STEPS_DEFAULT} 步", build_system_prompt())
        self.assertIn("7 步", build_system_prompt(max_steps=7))

    def test_does_not_hardcode_user_preferences(self) -> None:
        """用户偏好（语言、风格）**不进**系统提示——那归 s11 的用户记忆管。

        硬编码在这里，换个人用就得改代码。这条测试就是那个设计决定的钉子：
        提示词里一旦出现具体语言名，说明有人把偏好写回了代码。
        """

        from scripts.toolbox import build_system_prompt

        prompt = build_system_prompt()
        # 2026-09-24 边界收紧为"**回答语言**不许硬编码"：推理链是展示给人看的
        # （前端思考行），提示词里那段"思考过程用中文写"的软引导是刻意的、
        # 且明确写了"回答正文的语言照常跟随用户"——所以裸词"中文"不再算漏。
        # 这里钉的是真正归用户记忆管的偏好：回答/回复语言指令。
        for leak in ("回复用中文", "用中文回复", "回答用中文", "English", "Chinese"):
            self.assertNotIn(leak, prompt, f"系统提示里不该出现语言偏好：{leak}")

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
        # calc / get_weather 自 2026-09-23 起**也是**延迟工具：实测
        # 「发出去的 schema 越多，deepseek-flash 越不给思考」（12 个时 reasoning
        # 直接为空），而这两个又不常用——降级它们省 schema 预算。
        self.assertNotIn("calc", names)
        self.assertNotIn("get_weather", names)
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

    def test_edit_tool_is_workspace_allowed_not_whitelisted(self) -> None:
        """新增写工具的接线两头都要钉住（2026-09-23 政策后更新）。

        漏进 WRITE_TOOLS -> 掉进 default.deny（fail-closed，表现为"工具
        坏了"）；误进 SAFE_TOOLS -> 免审批直接放行，改状态不问人。界内写
        现在由 `path.write_allow` 放行（越界/禁区 DENY 在它之前守住边界），
        所以这条钉的是：fs_edit 走的是**路径边界规则**，不是无条件白名单。
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
        self.assertIs(decision.action, PermissionAction.ALLOW)
        self.assertEqual(decision.rule_id, "path.write_allow")

    def test_plan_tool_wiring(self) -> None:
        """plan_write 的接线三头（2026-09-27，plan-and-execute 第 1 步）：
        免审批白名单（无状态字符串工具，同 memory_write 一条理由）、
        策略实际放行（tool.allow_safe）、registry 里拿得到且 schema
        的 tasks 是数组（反射出不了 list，手写 schema 必须生效）。"""

        from scripts.toolbox import SAFE_TOOLS, build_policy, build_registry
        from src.harness.permissions import PermissionAction, ToolRequest

        self.assertIn("plan_write", SAFE_TOOLS)
        decision = build_policy().decide(
            ToolRequest(
                tool_use_id="t1",
                name="plan_write",
                arguments={"tasks": ["[x] 甲"]},
            )
        )
        self.assertIs(decision.action, PermissionAction.ALLOW)
        self.assertEqual(decision.rule_id, "tool.allow_safe")

        registry = build_registry()
        schema = registry.get("plan_write").model_schema()
        parameters = schema["function"]["parameters"]
        self.assertEqual(parameters["properties"]["tasks"]["type"], "array")
        self.assertEqual(parameters["required"], ["tasks"])


class BashToolTests(unittest.TestCase):
    """bash 工具（2026-09-17）：能跑、不卡死、不喷爆上下文、不漏密钥。

    这些用例会真的起子进程，但都用 `sys.executable -c ...`——跨平台，
    不依赖系统里装了哪些命令（`echo` 是唯一的例外，两个平台都有）。

    权限层那两道专属闸门（hard_deny / requires_approval）也在本类里测：
    它们按**工具名**匹配，和工具本体是同一个功能的两个面，分开测容易漏。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        # 宿主的"安全删除"会拦 unlink（见项目记忆），清理失败不该算测试失败。
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _py(self, code: str) -> str:
        """拼一条"用当前解释器跑一段代码"的命令：跨平台、可控、无外部依赖。"""

        return f'"{sys.executable}" -c "{code}"'

    def test_runs_command_and_reports_exit_code(self) -> None:
        result = run_bash("echo hi", sandbox_root=self.root)

        self.assertIn("exit 0", result)
        self.assertIn("hi", result)

    def test_nonzero_exit_is_data_not_exception(self) -> None:
        """失败要变成**数据**回灌给模型，不能抛异常打断循环（同 s02 的
        "未知工具/参数错"一律转成错误结果）。"""

        result = run_bash(self._py("import sys; sys.exit(3)"),
                          sandbox_root=self.root)

        self.assertIn("exit 3", result)

    def test_working_directory_is_the_sandbox_root(self) -> None:
        """命令里的相对路径能落在项目里，靠的就是 cwd=沙箱根。"""

        result = run_bash(self._py("import os; print(os.getcwd())"),
                          sandbox_root=self.root)

        self.assertIn(self.root.resolve().name, result)

    def test_timeout_kills_a_stuck_command(self) -> None:
        """一条 sleep 不能把整个 turn 拖死——这是"必须有超时"的全部理由。"""

        result = run_bash(self._py("import time; time.sleep(10)"),
                          timeout=1, sandbox_root=self.root)

        self.assertIn("超时", result)
        # 且不把等 10 秒才攒到的输出也端着：超时返回的是"已产生的部分"。
        self.assertLess(len(result), 500)

    def test_timeout_kills_the_whole_tree_not_just_the_shell(self) -> None:
        """超时必须连**孙进程**一起杀（2026-09-24 树杀改造的验收）。

        现场（2026-09-23）：agent 跑 npm install 超时，旧实现只杀 cmd 外壳，
        npm/node 孙进程成孤儿继续写盘 20 分钟，turn 假死。本用例复刻该结构：

            外壳（python -c ... sleep 30）
              └─ 孙进程（每 0.2s 往哨兵文件写一笔，共 40s）

        杀干净 → 哨兵不再增长；只杀外壳的旧实现会让它继续写 20 秒。
        孙进程由外壳 Popen 后**不 wait**——它就是那个"工具已返回、进程还
        活着"的孤儿，正是要验证被连杀的对象。
        """

        sentinel = self.root / "_sentinel.txt"
        script = self.root / "_grandchild.py"
        script.write_text(
            "import time\n"
            f"with open(r'{sentinel}', 'a', encoding='utf-8') as f:\n"
            "    for _ in range(200):\n"
            "        f.write('x')\n"
            "        f.flush()\n"
            "        time.sleep(0.2)\n",
            encoding="utf-8",
        )
        inner = (
            "import subprocess,sys;"
            f"subprocess.Popen([sys.executable, r'{script}']);"
            "import time;time.sleep(30)"
        )
        command = f'"{sys.executable}" -c "{inner}"'

        # timeout=3（不是 1）：外壳要先"cmd 解析 + python 冷启动"才轮到
        # Popen 孙进程——1 秒会在孙进程出生前就把整棵树杀了，前提不成立
        #（今天实测就翻过车）。3 秒对"出生 + 写哨兵"足够宽裕。
        result = run_bash(command, timeout=3, sandbox_root=self.root)
        self.assertIn("超时", result)

        # 给"没死透的孙进程"留出继续写哨兵的窗口：旧实现下它每 0.2s 写一次，
        # 这 2 秒里至少再写 8 笔——mtime 会紧贴检查时刻，下面的断言必炸。
        time.sleep(2)

        self.assertTrue(sentinel.exists(), "孙进程没跑起来，测试前提不成立")
        gap = time.time() - sentinel.stat().st_mtime
        self.assertGreater(
            gap, 1.2,
            "哨兵 1.2 秒内还在被写——孙进程没被连杀，树杀没生效")

    # ── 后台任务（2026-09-24 改造③）─────────────────────────

    def _start_bg(self, code: str) -> str:
        """后台起一个任务并从返回里抠出 task_id（各用例的公共第一步）。"""

        result = run_bash_background(self._py(code), sandbox_root=self.root)
        match = re.search(r"task (bg-[0-9a-f]{8})", result)
        self.assertIsNotNone(match, f"返回里没有 task_id：{result}")
        return match.group(1)

    def test_background_returns_at_once_and_streams_output(self) -> None:
        """后台启动必须**立即**返回（长驻进程不拖 turn），且输出可增量读到。

        这就是"dev server 场景"的形状：print 一行 → sleep 挂住。同步版会把
        turn 拖到超时；后台版 t=0 返回，t≈1s 就能看到那行日志。
        """

        started = time.time()
        # flush=True 是必须的：子进程 stdout 接管道时是全缓冲，不 flush 的
        # print 会一直躺在子进程缓冲里，drain 线程什么都读不到。
        task_id = self._start_bg(
            "print('bg-ok', flush=True); import time; time.sleep(8)")
        self.assertLess(time.time() - started, 5, "后台启动不该等命令结束")

        deadline = time.time() + 8
        out = ""
        while time.time() < deadline:
            out = bash_output(task_id)
            if "bg-ok" in out:
                break
            time.sleep(0.3)
        self.assertIn("bg-ok", out)
        self.assertIn("仍在运行", out)
        bash_stop(task_id)   # 收尾：不留孤儿进程挂在测试机上

    def test_background_output_reports_exit_code(self) -> None:
        """任务结束后 bash_output 要如实报 exit code（非零也是**数据**）。"""

        task_id = self._start_bg("print('done-mark'); import sys; sys.exit(5)")

        deadline = time.time() + 10
        out = ""
        while time.time() < deadline:
            out = bash_output(task_id)
            if "已结束" in out:
                break
            time.sleep(0.3)

        self.assertIn("已结束", out)
        self.assertIn("exit 5", out)
        self.assertIn("done-mark", out)

    def test_bash_stop_terminates_the_task(self) -> None:
        """bash_stop 之后任务不再是 running——dev server 的正常收尾通道。"""

        task_id = self._start_bg("import time; time.sleep(60)")

        stopped = bash_stop(task_id)
        self.assertIn("已终止", stopped)

        out = bash_output(task_id)
        self.assertNotIn("仍在运行", out)

    def test_bash_output_unknown_task_is_data_not_exception(self) -> None:
        """错误的 task_id 回人话 + 列出现有任务，不抛异常打断 turn。"""

        out = bash_output("bg-nope")
        self.assertIn("没有这个后台任务", out)

    def test_timeout_lower_bound_is_clamped(self) -> None:
        """模型传 timeout=0 时钳到 1 秒，而不是"一启动就掐断"。

        上界（300）没法在不真等的情况下观察，所以只测下界；上界的正确性
        由常量与 max(1, min(...)) 这一行保证。
        """

        result = run_bash("echo ok", timeout=0, sandbox_root=self.root)

        self.assertIn("ok", result)

    def test_long_output_is_clipped_with_a_visible_notice(self) -> None:
        """截断必须**说出来**——模型只知道"输出被截断了"才会换更精确的命令，
        默默丢一半它会以为那就是全部（这是 s13 输出外化的引子）。"""

        result = run_bash(self._py("print('x' * 20000)"),
                          sandbox_root=self.root)

        self.assertIn("被截断", result)
        self.assertLess(len(result), BASH_OUTPUT_LIMIT + 300)

    def test_empty_command_is_refused(self) -> None:
        self.assertIn("为空", run_bash("   ", sandbox_root=self.root))

    def test_empty_output_is_labelled(self) -> None:
        """没有输出 ≠ 出错：说一句"（无输出）"，别让模型对着空白猜。"""

        result = run_bash(self._py("pass"), sandbox_root=self.root)

        self.assertIn("exit 0", result)
        self.assertIn("（无输出）", result)

    def test_secret_env_vars_are_stripped(self) -> None:
        """`env` 本来能把密钥原样打出来——而工具输出要回灌给模型、
        还随 transcript 落盘，等于把密钥送给模型。"""

        with mock.patch.dict(os.environ, {"WYWD_FAKE_SECRET": "leak-me"}):
            result = run_bash(
                self._py("import os; "
                         "print(os.environ.get('WYWD_FAKE_SECRET', 'ABSENT'))"),
                sandbox_root=self.root,
            )

        self.assertIn("ABSENT", result)
        self.assertNotIn("leak-me", result)
        # 顺带直接测那个纯函数：名字里带 SECRET/KEY/TOKEN 的一律不传下去。
        self.assertNotIn("WYWD_FAKE_SECRET", _clean_env())

    def test_interpreter_env_vars_do_not_leak(self) -> None:
        """PYTHONHOME（shim 那份）/ harness 自己的 PYTHONPATH 不该进用户命令。

        2026-11-06 真机验收：uv 建的 venv 把 PYTHONHOME 指到自己的 3.12
        标准库（值等于 sys.base_prefix），用户机器上的 python 3.13 一
        import 就崩 SRE module mismatch；shell.py 注入的 PYTHONPATH 还会
        遮蔽同名包。

        2026-11-07 审查细化：只剥等于 base_prefix 的那份；相对条目
        （"."）按子进程 cwd 解析，原样保留。
        """

        user_path = str(Path(self.root) / "user-lib")
        with mock.patch.dict(os.environ, {
            "PYTHONHOME": sys.base_prefix,
            "PYTHONPATH": os.pathsep.join([_HARNESS_ROOT, user_path, "."]),
        }):
            env = _clean_env()

        self.assertNotIn("PYTHONHOME", env)
        # harness 那份剥掉，用户路径 + 相对条目原样保留。
        self.assertEqual(env.get("PYTHONPATH"),
                         os.pathsep.join([user_path, "."]))

    def test_user_pythonhome_is_preserved(self) -> None:
        """用户自己指向别的解释器的 PYTHONHOME 不该被 harness 替天行道。"""

        with mock.patch.dict(os.environ, {"PYTHONHOME": "/custom/python"}):
            env = _clean_env()

        self.assertEqual(env.get("PYTHONHOME"), "/custom/python")

    def test_harness_pythonpath_removed_entirely(self) -> None:
        """PYTHONPATH 里只有 harness 根时，整个变量都别传给子进程。"""

        with mock.patch.dict(os.environ, {"PYTHONPATH": _HARNESS_ROOT}):
            env = _clean_env()

        self.assertNotIn("PYTHONPATH", env)

    def test_policy_sends_bash_to_approval(self) -> None:
        """普通命令必须走 ASK——"每次都要人点头"是它敢默认上架的唯一理由。

        注意：**只读白名单内的命令不走 ASK**（2026-09-23 起，`git status`
        之类免审批）。所以这里要用一条**不在白名单里**的命令来验 ASK。
        """

        from src.harness.permissions import PermissionAction, ToolRequest
        from scripts.toolbox import build_policy

        decision = build_policy().decide(ToolRequest(
            tool_use_id="t1", name="bash",
            arguments={"command": "git commit -m wip"},
        ))

        self.assertIs(decision.action, PermissionAction.ASK)
        self.assertEqual(decision.rule_id, "bash.requires_approval")

    def test_policy_hard_denies_dangerous_commands(self) -> None:
        """危险命令是 DENY 而不是 ASK：审批**翻不了案**——弹窗会让用户以为
        自己有权放行，那就不算边界（permissions 模块的设计铁律）。"""

        from src.harness.permissions import PermissionAction, ToolRequest
        from scripts.toolbox import build_policy

        for command in ("sudo apt install x", "rm -rf build",
                        "shutdown /s", "dd if=/dev/zero of=x"):
            decision = build_policy().decide(ToolRequest(
                tool_use_id="t1", name="bash", arguments={"command": command},
            ))
            self.assertIs(decision.action, PermissionAction.DENY, command)
            self.assertEqual(decision.rule_id, "bash.hard_deny", command)

    def test_empty_command_falls_through_to_default_deny(self) -> None:
        """空命令既不弹审批（白打扰用户）也不许 IndexError。

        这是 bash 上架当天发现的潜藏 bug：`is_hard_deny` 里
        `command.strip().split()[0]` 在空串上会抛 IndexError——那条规则在
        bash 工具存在之前永远不会被调用，所以一直没暴露。这个用例把角落钉住。
        """

        from src.harness.permissions import PermissionAction, ToolRequest
        from scripts.toolbox import build_policy

        for command in ("", "   "):
            decision = build_policy().decide(ToolRequest(
                tool_use_id="t1", name="bash", arguments={"command": command},
            ))
            self.assertIs(decision.action, PermissionAction.DENY, repr(command))
            self.assertEqual(decision.rule_id, "default.deny", repr(command))

    def test_schema_does_not_leak_the_sandbox_root(self) -> None:
        """bash 的 schema 里不能出现 sandbox_root——闭包把它吃掉了。

        泄露的后果在 bash 上比在 fs_* 上更重：fs_* 就算传了 root 也只是
        "搜索起点"（仍受 _resolve_safe 管），而 bash 的工作目录一旦可传，
        模型就能把它设成任意位置，连沙箱的**起点**都不在项目里了。
        """

        from scripts.toolbox import DEFAULT_WORKSPACE, build_registry_for
        from src.harness.tools import tool_to_schema

        params = tool_to_schema(
            build_registry_for(DEFAULT_WORKSPACE).get("bash")
        )["function"]["parameters"]

        # background 是 2026-09-24 改造③的合法新增（长驻进程出口）；
        # explain 同日新增（给人看的命令说明，随 arguments 到前端）。
        # 这条测试真正要守的是**沙箱根不外泄**——新增参数可以，多出 root 不行。
        self.assertEqual(set(params["properties"]),
                         {"command", "timeout", "background", "explain"})
        self.assertNotIn("sandbox_root", json.dumps(params))
        self.assertEqual(params["required"], ["command"])

    def test_registry_runs_bash_through_the_gate(self) -> None:
        """端到端：过 runner（approver 模拟用户点"允许"）→ 真跑 → 结果文本。

        单测 run_bash 只证明"函数本身能用"；这条证明它**接线正确**——
        工具名能命中规则、handler 能拿到闭包里的沙箱根、结果能被编码回灌。
        """

        from scripts.toolbox import (
            DEFAULT_WORKSPACE,
            build_policy_for,
            build_registry_for,
        )
        from src.harness.permissions import GovernedToolRunner, ToolRequest

        runner = GovernedToolRunner(
            policy=build_policy_for(DEFAULT_WORKSPACE),
            approver=lambda decision: True,      # 模拟用户点"允许"
            registry=build_registry_for(DEFAULT_WORKSPACE),
        )

        result = runner.run(ToolRequest(
            tool_use_id="t1", name="bash", arguments={"command": "echo e2e-ok"},
        ))

        self.assertEqual(result.status.value, "succeeded")
        self.assertIn("e2e-ok", result.output)


class LoadSkillToolTests(unittest.TestCase):
    """load_skill 延迟工具（装配层，s16）：正文带统一标记头，未知技能给人话。

    项目级技能目录建在临时 root（{root}/.workbuddy/skills）——**不碰**
    真实的 ~/.workbuddy/skills。技能名用独占前缀：同名时项目级覆盖用户级，
    撞上开发者机器上的真实技能会让断言依赖用户环境。
    """

    def _make_tools(self, root: Path) -> dict:
        from scripts.toolbox import build_deferred_tools

        skill_dir = root / ".workbuddy" / "skills" / "demo-load-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "title: demo-load-skill\n"
            "summary: 演示加载\n"
            "read_when:\n  - 演示加载\n"
            "---\n\n正文：第一步、第二步。\n",
            encoding="utf-8",
        )
        return {tool.name: tool for tool in build_deferred_tools(root)}

    def test_returns_marker_plus_body(self) -> None:
        from src.harness.skills import skill_marker

        with tempfile.TemporaryDirectory() as tmp:
            handler = self._make_tools(Path(tmp))["load_skill"].handler
            result = handler(name="demo-load-skill")

        # 标记头是去重的锚点：这份正文随 tool 消息进 transcript，后续轮次
        # 触发词再命中时 _inject_skills 靠它认出"模型已经看得到了"。
        self.assertTrue(result.startswith(skill_marker("demo-load-skill")))
        self.assertIn("正文：第一步、第二步。", result)

    def test_unknown_skill_returns_actionable_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._make_tools(Path(tmp))["load_skill"].handler
            result = handler(name="no-such-skill")

        self.assertTrue(result.startswith("没取到："))
        self.assertIn("no-such-skill", result)


if __name__ == "__main__":
    unittest.main()