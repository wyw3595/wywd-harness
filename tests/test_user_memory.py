"""用户级记忆（s11）的回归测试：两个契约、幂等、防回滚、过期投影、scope。

全离线。时间戳一律显式传（带时区），不依赖"现在几点"——否则用例会在
某些时刻偶发失败（比如正好跨过某个 expires_at）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.harness.user_memory import (
    NO_USER_MEMORY_PLACEHOLDER,
    Preference,
    PreferenceStatus,
    StalePreferenceUpdateError,
    UserMemory,
    UserMemoryValidationError,
    UserScopeError,
    scope_id,
)


class UserMemoryCase(unittest.TestCase):
    """共用的临时目录与默认用户。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = UserMemory(self.root, "wang@example.com")

    def tearDown(self) -> None:
        try:
            self.tmp.cleanup()
        except OSError:
            pass


class ProfileTests(UserMemoryCase):
    """Profile：显式字段级 patch，值没变就不写盘。"""

    def test_patch_only_touches_present_fields(self) -> None:
        self.memory.update_profile({"name": "老王", "timezone": "UTC+8"})

        result = self.memory.update_profile({"call_them": "王哥"})

        profile = self.memory.read_profile()
        self.assertEqual(profile["call_them"], "王哥")
        self.assertEqual(profile["name"], "老王")        # 没提到的原样不动
        self.assertEqual(profile["timezone"], "UTC+8")
        self.assertEqual(result.changed, ("call_them",))

    def test_none_means_delete(self) -> None:
        """`None` 是明确删除，不是"设成空"——否则字段会永远赖着。"""

        self.memory.update_profile({"name": "老王", "notes": "喜欢简洁"})

        result = self.memory.update_profile({"notes": None})

        self.assertNotIn("notes", self.memory.read_profile())
        self.assertEqual(result.changed, ("notes",))

    def test_unknown_field_is_rejected(self) -> None:
        """封闭字段集：模型不能每轮发明新字段，否则 schema 会糊成一团。"""

        with self.assertRaises(UserMemoryValidationError):
            self.memory.update_profile({"favourite_color": "blue"})

    def test_same_value_does_not_touch_disk(self) -> None:
        """幂等：值没变就不重写文件（mtime 不动）。"""

        self.memory.update_profile({"name": "老王"})
        before = self.memory.profile_file.stat().st_mtime_ns

        result = self.memory.update_profile({"name": "老王"})

        self.assertEqual(result.unchanged, ("name",))
        self.assertFalse(result.touched_disk)
        self.assertEqual(self.memory.profile_file.stat().st_mtime_ns, before)

    def test_blank_value_is_rejected(self) -> None:
        with self.assertRaises(UserMemoryValidationError):
            self.memory.update_profile({"name": "   "})

    def test_profile_projection_is_written(self) -> None:
        """persona/user.md 是人可读投影，写 profile 时要跟着刷新。"""

        self.memory.update_profile({"call_them": "王哥"})

        self.assertIn("王哥", self.memory.persona_file.read_text(encoding="utf-8"))


class PreferenceLifecycleTests(UserMemoryCase):
    """Preference：语义 key 去重 + 三态 + 完整幂等身份。"""

    def test_three_states(self) -> None:
        """CREATED → UNCHANGED → UPDATED，revision 只在真变化时涨。"""

        first = self.memory.set_preference("response.language", "Chinese",
                                           updated_at="2026-09-19T01:00:00Z")
        second = self.memory.set_preference("response.language", "Chinese",
                                            updated_at="2026-09-19T01:05:00Z")
        third = self.memory.set_preference("response.language", "English",
                                           updated_at="2026-09-19T01:10:00Z")

        self.assertIs(first.status, PreferenceStatus.CREATED)
        self.assertEqual(first.current.revision, 1)
        self.assertIs(second.status, PreferenceStatus.UNCHANGED)
        self.assertEqual(second.current.revision, 1)      # 重试不涨版本
        self.assertEqual(second.previous.value, "Chinese")
        self.assertIs(third.status, PreferenceStatus.UPDATED)
        self.assertEqual(third.current.revision, 2)
        self.assertEqual(third.previous.value, "Chinese")

    def test_same_value_different_expiry_is_a_real_update(self) -> None:
        """完整幂等身份含 expires_at：延长期限是**真更新**，不是重试。"""

        self.memory.set_preference("response.detail", "verbose",
                                   updated_at="2026-09-19T01:00:00Z",
                                   expires_at="2026-09-20T01:00:00Z")

        extended = self.memory.set_preference("response.detail", "verbose",
                                              updated_at="2026-09-19T02:00:00Z",
                                              expires_at="2026-09-25T01:00:00Z")

        self.assertIs(extended.status, PreferenceStatus.UPDATED)
        self.assertEqual(extended.current.revision, 2)

    def test_only_updated_at_difference_is_a_retry(self) -> None:
        """只有 updated_at 不同 = 重试 = UNCHANGED（否则 revision 会被推高到没意义）。"""

        self.memory.set_preference("editor.tabs", "tabs",
                                   updated_at="2026-09-19T01:00:00Z")

        retry = self.memory.set_preference("editor.tabs", "tabs",
                                           updated_at="2026-09-19T09:00:00Z")

        self.assertIs(retry.status, PreferenceStatus.UNCHANGED)
        self.assertEqual(retry.current.revision, 1)

    def test_stale_write_cannot_roll_back(self) -> None:
        """重放旧 transcript 不能把新偏好改回旧的——那是静默的数据损坏。"""

        self.memory.set_preference("response.language", "English",
                                   updated_at="2026-09-19T05:00:00Z")

        with self.assertRaises(StalePreferenceUpdateError):
            self.memory.set_preference("response.language", "Chinese",
                                       updated_at="2026-09-19T01:00:00Z")

    def test_delete_is_exact_by_key(self) -> None:
        self.memory.set_preference("response.language", "Chinese")
        self.memory.set_preference("response.detail", "concise")

        self.assertTrue(self.memory.delete_preference("response.language"))
        self.assertFalse(self.memory.delete_preference("response.language"))
        self.assertEqual([item.key for item in self.memory.list_preferences()],
                         ["response.detail"])

    def test_illegal_keys_are_rejected(self) -> None:
        for bad in ("Response.Language", "中文偏好", "has space", "trailing."):
            with self.assertRaises(UserMemoryValidationError, msg=bad):
                self.memory.set_preference(bad, "x")


class ExpiryTests(UserMemoryCase):
    """过期不是删除：记录留着可审计，但不再进投影与 Prompt。"""

    def setUp(self) -> None:
        super().setUp()
        self.memory.set_preference(
            "response.detail", "verbose",
            updated_at="2026-09-19T01:00:00Z",
            expires_at="2026-09-20T01:00:00Z",
        )

    def test_expired_record_survives_in_canonical(self) -> None:
        self.assertEqual(len(self.memory.list_preferences()), 1)
        self.assertEqual(len(self.memory.list_active_preferences(
            as_of="2026-09-25T01:00:00Z")), 0)

    def test_boundary_is_exclusive(self) -> None:
        """正好等于 expires_at 就算过期（一边倒的规则，不留歧义）。"""

        self.assertEqual(len(self.memory.list_active_preferences(
            as_of="2026-09-20T01:00:00Z")), 0)
        self.assertEqual(len(self.memory.list_active_preferences(
            as_of="2026-09-20T00:59:59Z")), 1)

    def test_expired_never_reaches_prompt(self) -> None:
        context = self.memory.get_context_for_agent(as_of="2026-09-25T01:00:00Z")
        self.assertEqual(context, NO_USER_MEMORY_PLACEHOLDER)

    def test_projection_lists_only_active(self) -> None:
        """MEMORY.md 只投影 active 条目——已过期的留在 JSON 里，但不进投影。

        用**构造出来的**记录来测（而不是靠"现在几点"去猜），所以结果稳定。
        """

        expired = Preference(
            key="response.detail", value="verbose", revision=1,
            source="explicit", source_event_id=None,
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
            expires_at="2026-09-02T00:00:00Z",       # 早就过去了
        )
        live = Preference(
            key="editor.tabs", value="tabs", revision=1,
            source="explicit", source_event_id=None,
            created_at="2026-09-01T00:00:00Z",
            updated_at="2026-09-01T00:00:00Z",
            expires_at=None,
        )

        self.memory._render_memory([expired, live])

        text = self.memory.memory_file.read_text(encoding="utf-8")
        self.assertIn("editor.tabs", text)
        self.assertNotIn("response.detail", text)


class TimestampTests(UserMemoryCase):
    """时间戳一律要时区、一律存 UTC。"""

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(UserMemoryValidationError):
            self.memory.set_preference("a.b", "x", updated_at="2026-09-19 10:30")

    def test_stored_as_utc(self) -> None:
        written = self.memory.set_preference(
            "a.b", "x", updated_at="2026-09-19T18:30:00+08:00")
        self.assertEqual(written.current.updated_at, "2026-09-19T10:30:00Z")

    def test_expiry_must_be_after_update(self) -> None:
        with self.assertRaises(UserMemoryValidationError):
            self.memory.set_preference("a.b", "x",
                                       updated_at="2026-09-19T10:00:00Z",
                                       expires_at="2026-09-19T09:00:00Z")


class ScopeTests(UserMemoryCase):
    """作用域隔离：目录按摘要分，读的时候还要再校验一次。"""

    def test_scope_id_is_stable_and_path_safe(self) -> None:
        first = scope_id("wang@example.com")
        self.assertEqual(first, scope_id("wang@example.com"))
        self.assertNotEqual(first, scope_id("li@example.com"))
        self.assertNotIn("@", first)
        self.assertNotIn("/", first)

    def test_directory_is_keyed_by_digest(self) -> None:
        self.assertNotIn("wang@example.com", str(self.memory.directory))
        self.assertIn(self.memory.scope_id, str(self.memory.directory))

    def test_mismatched_scope_is_refused(self) -> None:
        """把 A 的目录复制到 B 的位置 → 拒绝加载，而不是静默注入别人的偏好。

        不校验的话，B 的每轮对话都会带着 A 的偏好，而且**看起来一切正常**
        ——这种静默污染最难查。
        """

        self.memory.update_profile({"name": "老王"})

        intruder = UserMemory(self.root, "li@example.com")
        intruder.directory.mkdir(parents=True, exist_ok=True)
        intruder.profile_file.write_text(
            self.memory.profile_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        with self.assertRaises(UserScopeError):
            intruder.read_profile()


class ProjectionTests(UserMemoryCase):
    """投影是可重建的：canonical 才是真相。"""

    def test_projections_rebuild_from_canonical(self) -> None:
        self.memory.update_profile({"call_them": "王哥"})
        self.memory.set_preference("response.language", "Chinese")

        # 人为破坏两份投影（手工编辑 / 半截写入）
        self.memory.memory_file.write_text("坏了", encoding="utf-8")
        self.memory.persona_file.write_text("也坏了", encoding="utf-8")

        self.memory.render_projections()

        self.assertIn("response.language", self.memory.memory_file.read_text(
            encoding="utf-8"))
        self.assertIn("王哥", self.memory.persona_file.read_text(encoding="utf-8"))

    def test_canonical_json_keeps_scope(self) -> None:
        """canonical 里存着 user_scope——那正是下次读取时校验的依据。"""

        self.memory.update_profile({"name": "老王"})
        payload = json.loads(self.memory.profile_file.read_text(encoding="utf-8"))

        self.assertEqual(payload["user_scope"], "wang@example.com")
        self.assertEqual(payload["schema_version"], 1)


class PromptContextTests(UserMemoryCase):
    """注入 Prompt 的那一段：有内容才渲染，空记忆给占位符。"""

    def test_empty_memory_renders_placeholder(self) -> None:
        self.assertEqual(self.memory.get_context_for_agent(),
                         NO_USER_MEMORY_PLACEHOLDER)

    def test_context_includes_profile_and_active_preferences(self) -> None:
        self.memory.update_profile({"call_them": "王哥"})
        self.memory.set_preference("response.language", "Chinese")

        context = self.memory.get_context_for_agent()

        self.assertIn("王哥", context)
        self.assertIn("response.language = Chinese", context)

    def test_context_is_plain_text_not_json(self) -> None:
        """给模型看的那份是文本——JSON 的括号和引号白烧 token。"""

        self.memory.set_preference("response.language", "Chinese")
        context = self.memory.get_context_for_agent()

        self.assertNotIn("{", context)
        self.assertIn("# 用户偏好", context)


if __name__ == "__main__":
    unittest.main()
