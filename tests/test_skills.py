"""技能系统（s16）的回归测试：解析子集、两级优先、触发匹配、按需加载、权限。

全离线、零依赖。技能目录一律建在临时目录里——**绝不碰真实的
`~/.workbuddy/skills`**（那是用户的东西）。
"""

import tempfile
import unittest
from pathlib import Path
from typing import Optional

from src.harness.skills import (
    MAX_SKILL_BODY_CHARS,
    SkillFormatError,
    SkillIndex,
    SkillNotFoundError,
    SkillPermissions,
    parse_frontmatter,
)


def write_skill(directory: Path, folder: str, *, title: Optional[str] = None,
                summary: str = "干这个用的", read_when=("触发词",),
                body: str = "正文：第一步、第二步。",
                extra_lines=(), permissions_yaml=None) -> Path:
    """往临时目录里放一个 SKILL.md。"""

    lines = ["---", f"title: {title or folder}", f"summary: {summary}"]
    if read_when:
        lines.append("read_when:")
        lines.extend(f"  - {item}" for item in read_when)
    lines.extend(extra_lines)
    if permissions_yaml is not None:
        lines.append("permissions:")
        lines.extend(f"  {item}" for item in permissions_yaml)
    lines.append("---")

    path = directory / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n\n" + body + "\n", encoding="utf-8")
    return path


class TemporarySkillCase(unittest.TestCase):
    """共用的临时目录收尾。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        try:
            self.tmp.cleanup()
        except OSError:
            pass


class FrontmatterParserTests(unittest.TestCase):
    """受限解析器：认得的就认，不认得的**报错**而不是猜。"""

    def test_parses_scalars_lists_and_nested_maps(self) -> None:
        frontmatter, body = parse_frontmatter(
            "---\n"
            "title: demo\n"
            "summary: 一句话\n"
            "agent_created: true\n"
            "read_when: [提交, commit]\n"
            "permissions:\n"
            '  tools: [bash]\n'
            "  network: false\n"
            "---\n"
            "正文在这里\n"
        )

        self.assertEqual(frontmatter["title"], "demo")
        self.assertIs(frontmatter["agent_created"], True)
        self.assertEqual(frontmatter["read_when"], ["提交", "commit"])
        self.assertEqual(frontmatter["permissions"]["tools"], ["bash"])
        self.assertIs(frontmatter["permissions"]["network"], False)
        self.assertEqual(body, "正文在这里")

    def test_parses_block_list(self) -> None:
        frontmatter, _ = parse_frontmatter(
            "---\nread_when:\n  - 提交代码\n  - git push\n---\n正文\n")

        self.assertEqual(frontmatter["read_when"], ["提交代码", "git push"])

    def test_parses_two_level_nesting(self) -> None:
        """`permissions.paths.read` 是**两层**嵌套——教材的 SKILL.md 就长这样。

        只支持一层的话，整条权限声明会被拒，技能反而加载不进来——
        这个 bug 是跑演示时才发现的（项目级技能静默消失）。
        """

        frontmatter, _ = parse_frontmatter(
            "---\n"
            "title: demo\n"
            "permissions:\n"
            "  tools: [bash]\n"
            "  network: false\n"
            "  paths:\n"
            '    read: ["**"]\n'
            "    write: []\n"
            "---\n正文\n"
        )

        permissions = frontmatter["permissions"]
        self.assertEqual(permissions["tools"], ["bash"])
        self.assertIs(permissions["network"], False)
        self.assertEqual(permissions["paths"]["read"], ["**"])
        self.assertEqual(permissions["paths"]["write"], [])

    def test_strips_matching_quotes(self) -> None:
        frontmatter, _ = parse_frontmatter('---\ntitle: "带引号的标题"\n---\n正文\n')
        self.assertEqual(frontmatter["title"], "带引号的标题")

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        frontmatter, _ = parse_frontmatter(
            "---\n# 这是注释\ntitle: demo\n\nread_when:\n\n  - x\n---\n正文\n")

        self.assertEqual(frontmatter["title"], "demo")
        self.assertEqual(frontmatter["read_when"], ["x"])

    def test_missing_opening_marker_is_rejected(self) -> None:
        with self.assertRaises(SkillFormatError):
            parse_frontmatter("title: demo\n正文\n")

    def test_missing_closing_marker_is_rejected(self) -> None:
        with self.assertRaises(SkillFormatError):
            parse_frontmatter("---\ntitle: demo\n正文（没有结束标记）\n")

    def test_unsupported_yaml_is_rejected_not_guessed(self) -> None:
        """多行字符串 / 锚点 / 流式映射：**报错**。

        猜错一个配置的后果（技能永远不触发、权限写宽了），比让作者改一行严重。
        """

        for bad in ("---\nnotes: |\n  a\n  b\n---\n正文\n",
                    "---\nnotes: >\n  a\n---\n正文\n",
                    "---\nnotes: &anchor x\n---\n正文\n",
                    "---\nnotes: {a: 1}\n---\n正文\n"):
            with self.assertRaises(SkillFormatError, msg=bad):
                parse_frontmatter(bad)

    def test_stray_indent_is_rejected(self) -> None:
        with self.assertRaises(SkillFormatError):
            parse_frontmatter("---\ntitle: demo\n   跑偏的一行\n---\n正文\n")

    def test_line_without_colon_is_rejected(self) -> None:
        with self.assertRaises(SkillFormatError):
            parse_frontmatter("---\ntitle demo\n---\n正文\n")


class PermissionTests(unittest.TestCase):
    """manifest 是**请求**能力——字段严格白名单，越界路径直接拒。"""

    def test_parses_full_manifest(self) -> None:
        granted = SkillPermissions.from_dict({
            "tools": ["bash", "fs_read"],
            "network": True,
            "paths": {"read": ["**"], "write": ["reports/*.md"]},
        })

        self.assertEqual(granted.tools, ("bash", "fs_read"))
        self.assertTrue(granted.network)
        self.assertEqual(granted.write_paths, ("reports/*.md",))

    def test_rejects_unknown_field(self) -> None:
        with self.assertRaises(SkillFormatError):
            SkillPermissions.from_dict({"tools": ["bash"], "sudo": True})

    def test_rejects_unknown_path_field(self) -> None:
        with self.assertRaises(SkillFormatError):
            SkillPermissions.from_dict({"paths": {"execute": ["**"]}})

    def test_rejects_absolute_and_parent_paths(self) -> None:
        """绝对路径 / `..` 意味着"跑到技能目录之外"——那不是能力声明，是越界。"""

        for bad in ("/etc/passwd", "C:\\Windows", "../../secrets",
                    "a/../../b"):
            with self.assertRaises(SkillFormatError, msg=bad):
                SkillPermissions.from_dict({"paths": {"read": [bad]}})

    def test_rejects_non_boolean_network(self) -> None:
        with self.assertRaises(SkillFormatError):
            SkillPermissions.from_dict({"network": "yes"})

    def test_empty_manifest_is_allowed(self) -> None:
        granted = SkillPermissions.from_dict(None)
        self.assertEqual(granted.tools, ())
        self.assertIn("无额外请求", granted.render())


class IndexTests(TemporarySkillCase):
    """建索引：两级目录、项目级优先、坏文件不拖垮扫描。"""

    def test_indexes_both_scopes(self) -> None:
        user_dir = self.root / "user"
        project_dir = self.root / "project"
        write_skill(user_dir, "git-commit", summary="提交流程")
        write_skill(project_dir, "api-design", summary="接口设计")

        index = SkillIndex(user_dir=user_dir, project_dir=project_dir)

        self.assertEqual([s.name for s in index.skills()],
                         ["api-design", "git-commit"])
        self.assertEqual({s.scope for s in index.skills()},
                         {"user", "project"})

    def test_project_scope_wins_on_name_collision(self) -> None:
        """同名技能**项目级覆盖用户级**：越具体的作用域越优先。"""

        user_dir = self.root / "user"
        project_dir = self.root / "project"
        write_skill(user_dir, "test-conventions", summary="个人偏好")
        write_skill(project_dir, "test-conventions", summary="团队公约")

        index = SkillIndex(user_dir=user_dir, project_dir=project_dir)

        self.assertEqual(len(index.skills()), 1)
        self.assertEqual(index.get("test-conventions").summary, "团队公约")
        self.assertEqual(index.get("test-conventions").scope, "project")

    def test_broken_skill_does_not_break_the_scan(self) -> None:
        """一个坏文件不该让**所有**技能都用不了。"""

        user_dir = self.root / "user"
        write_skill(user_dir, "good-one", summary="好的")
        broken = user_dir / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("没有 frontmatter 的文件", encoding="utf-8")

        index = SkillIndex(user_dir=user_dir)

        self.assertEqual([s.name for s in index.skills()], ["good-one"])
        self.assertEqual(len(index.errors), 1)
        self.assertIn("broken", index.errors[0])

    def test_unknown_frontmatter_field_is_rejected(self) -> None:
        """打错的键如果被静默忽略，那个技能就永远不触发——而作者以为配好了。"""

        user_dir = self.root / "user"
        write_skill(user_dir, "typo", extra_lines=["readwhen: [x]"])

        index = SkillIndex(user_dir=user_dir)

        self.assertEqual(index.skills(), [])
        self.assertIn("readwhen", index.errors[0])

    def test_accepts_host_style_field_names(self) -> None:
        """兼容宿主（WorkBuddy）格式：它用 name / description。

        不认这两个别名的代价是"机器上已有的技能一个都列不出来"——
        而列不出来就等于这个功能没接上。
        """

        user_dir = self.root / "user"
        path = user_dir / "host-style" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\n"
            "name: host-style\n"
            "description: 宿主写的技能\n"
            "---\n"
            "正文\n",
            encoding="utf-8",
        )

        index = SkillIndex(user_dir=user_dir)

        self.assertEqual([s.name for s in index.skills()], ["host-style"])
        self.assertEqual(index.get("host-style").summary, "宿主写的技能")

    def test_missing_directories_are_fine(self) -> None:
        index = SkillIndex(user_dir=self.root / "nope", project_dir=None)
        self.assertEqual(index.skills(), [])


class MatchAndLoadTests(TemporarySkillCase):
    """匹配触发词 + 按需加载正文。"""

    def setUp(self) -> None:
        super().setUp()
        write_skill(self.root / "skills", "git-commit", summary="提交流程",
                    read_when=("提交代码", "commit"), body="先 git status 再提交。")
        write_skill(self.root / "skills", "review", summary="评审流程",
                    read_when=("代码评审", "review"), body="逐文件看。")
        self.index = SkillIndex(user_dir=self.root / "skills")

    def test_matches_by_trigger(self) -> None:
        self.assertEqual([s.name for s in self.index.match("帮我提交代码")],
                         ["git-commit"])
        self.assertEqual([s.name for s in self.index.match("做一次 review")],
                         ["review"])

    def test_matching_is_case_insensitive(self) -> None:
        self.assertEqual([s.name for s in self.index.match("COMMIT 一下")],
                         ["git-commit"])

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(self.index.match("今天天气不错"), [])
        self.assertEqual(self.index.match(""), [])

    def test_load_returns_body_without_frontmatter(self) -> None:
        """加载的是**正文**——frontmatter 是给索引用的，不该混进上下文。"""

        body = self.index.load("git-commit")

        self.assertIn("先 git status 再提交", body)
        self.assertNotIn("read_when", body)
        self.assertNotIn("---", body)

    def test_load_unknown_skill_raises(self) -> None:
        with self.assertRaises(SkillNotFoundError):
            self.index.load("没有这个技能")

    def test_oversized_body_is_clipped_with_notice(self) -> None:
        """正文有上限：不然"按需加载"一次就能吃掉几万 token。"""

        write_skill(self.root / "skills", "huge", body="很长的正文。" * 5_000)
        self.index.reload()

        body = self.index.load("huge")

        self.assertLess(len(body), MAX_SKILL_BODY_CHARS + 200)
        self.assertIn("被截断", body)


class RenderTests(TemporarySkillCase):
    """注入 Prompt 的两段：目录（常驻、极短）+ 命中正文（按需、临时）。"""

    def setUp(self) -> None:
        super().setUp()
        write_skill(self.root / "skills", "git-commit", summary="提交流程",
                    read_when=("提交",), body="正文里的第 42 号秘密。")
        self.index = SkillIndex(user_dir=self.root / "skills")

    def test_directory_lists_names_but_not_bodies(self) -> None:
        """**目录里不能有正文**——这是整个机制省钱的地方。"""

        directory = self.index.render_directory()

        self.assertIn("git-commit", directory)
        self.assertIn("提交流程", directory)
        self.assertIn("提交", directory)          # 触发词也要露出来，模型才知道何时用
        self.assertNotIn("第 42 号秘密", directory)

    def test_empty_index_renders_nothing(self) -> None:
        empty = SkillIndex(user_dir=self.root / "nothing-here")
        self.assertEqual(empty.render_directory(), "")

    def test_render_matches_embeds_body_only_when_hit(self) -> None:
        hit = self.index.render_matches("帮我提交一下")
        miss = self.index.render_matches("今天天气不错")

        self.assertIn("第 42 号秘密", hit)         # 命中才展开全文
        self.assertIn("git-commit", hit)
        self.assertEqual(miss, "")


if __name__ == "__main__":
    unittest.main()
