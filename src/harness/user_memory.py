"""用户级记忆（教材 s11）：跨项目仍然有效的稳定信息与明确偏好。

============================================================
📚 与工作区记忆（s10）的分工
============================================================

| | 回答的问题 | 作用域 | 例子 |
|---|---|---|---|
| 工作区记忆 | 这个**项目**长期有效的事实是什么 | 一个项目 | "测试跑 unittest discover" |
| 用户记忆 | 这个**人**跨项目仍然有效的是什么 | 一个用户 | "用中文回复"、"叫我王哥" |

**所有权必须分开**（教材划的边界）：`UserMemory` 不接收 workspace path、也不读
s10 的日志。两者只在"组装 Prompt"那一刻合并（那是 s15 的事），存储与更新策略
各归各——混在一起就会出现"换个项目，偏好也跟着变了"这种说不清的行为。

============================================================
📚 两个契约（本章的核心）
============================================================

**Profile**（`profile.json`）——回答"这个用户是谁"：

  - 显式**字段级 patch**：只改请求里出现的字段，没提到的原样不动；
  - `None` 表示**明确删除**该字段；
  - 不在 `PROFILE_FIELDS` 里的字段直接报错——防模型每轮发明新字段，
    否则 schema 会被"顺手加的东西"撑成一团泥；
  - 相同值计入 `unchanged` 且**不重写文件**（幂等：重复调用不产生噪音）；
  - Profile **不是**从聊天里自动抽取的画像，只有用户明确提供或要求保存的
    才进来。

**Preference**（`preferences.json`）——回答"跨项目默认怎么做"：

  - 按**语义 key** 去重（`response.language`），不是比较整段文本：
    "回复用中文"和"以后回复用英文"不是两条并存的事实，而是同一个 key
    的两个版本；
  - 三态：`CREATED` / `UNCHANGED` / `UPDATED`（后者 revision +1）；
  - **完整幂等身份** = value + source + expires_at + source_event_id。
    只有 `updated_at` 不同 = 一次重试 = `UNCHANGED`；延长了期限、
    或补了新的来源证据 = 真更新；
  - **防回滚**：新写入的时间必须晚于 canonical 的 `updated_at`
    （重放旧 transcript 时不能把新偏好改回旧的——那是静默的数据损坏）；
  - `expires_at` 到点即 expired：记录**留在 JSON 里**（好解释它曾经为何生效），
    但不再进投影、不进 Prompt。

============================================================
📚 Canonical 与 projection（沿用 s10 那套）
============================================================

| 文件 | 角色 | 是真相来源吗 |
|---|---|---|
| `profile.json` | 结构化用户资料 | 是 |
| `preferences.json` | 带 revision / 有效期 / 来源的完整偏好 | 是（含已过期）|
| `persona/user.md` | Profile 的人可读投影 | 否，可重建 |
| `MEMORY.md` | **active** 偏好的投影 | 否，可重建 |

Markdown 只负责"给人看"，JSON 负责"确定性更新"。投影坏了就从 canonical 重建。
这样避免让一个 Markdown 同时当数据库、编辑协议和 Prompt 来源（教材的告诫）。

**与教材的差异**：不做 `persona/core.md` / `identity.md` / `bootstrap.md`——
那三件是**助手自己的身份**（它是谁、叫什么名字），不是"用户记忆"。本模块
只管用户那一侧。

============================================================
📚 存储位置与 scope
============================================================

`<root>/users/<scope-id>/`。`scope-id` 是规范化用户标识的**稳定摘要**
（sha256 前 16 位）：邮箱、`/`、空格不该出现在路径里。canonical JSON 里另存
`user_scope`，读取时**校验一致**——文件被错误复制进另一个用户的目录时拒绝
加载，而不是静默把张三的偏好注给李四。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

from src.harness.file_tools import atomic_write_text

SCHEMA_VERSION = 1

# Profile 的合法字段：**封闭集**。模型不能自创字段——否则每轮加一个，
# 三个月后没人说得清 profile.json 里那些键都是干什么的。
PROFILE_FIELDS = frozenset({"name", "call_them", "timezone", "notes"})

MAX_PREFERENCE_CHARS = 4_000
MAX_VALUE_CHARS = 2_000
MAX_SOURCE_EVENT_ID_CHARS = 200

# 偏好 key 的形状：小写词，用 . - _ 分段（response.language、editor.tab_size）。
# 刻意不接受空格与中文——key 是**机器用的冲突域**，value 才是给人看的。
PREFERENCE_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

# 来源事件 ID 的形状：由 Harness 附上（s09 的 event ID），不是模型编的。
SOURCE_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

PROFILE_FILE = "profile.json"
PREFERENCES_FILE = "preferences.json"
MEMORY_FILE = "MEMORY.md"
PERSONA_DIR = "persona"
PERSONA_USER_FILE = "user.md"

NO_USER_MEMORY_PLACEHOLDER = "(no user memory yet)"


class UserMemoryError(RuntimeError):
    """用户记忆的错误基类。"""


class UserScopeError(UserMemoryError):
    """目录里的 user_scope 和请求的用户对不上——拒绝加载。

    场景：把 A 的目录整个复制到 B 的位置。不校验的话，B 的每轮对话都会
    被注入 A 的偏好，而且**看起来一切正常**——这种静默污染最难查。
    """


class UserMemoryValidationError(UserMemoryError):
    """输入不合法（字段名、key 形状、长度、时间戳）。"""


class StalePreferenceUpdateError(UserMemoryError):
    """写入的时间早于 canonical 的 updated_at——拒绝回滚。"""


class PreferenceStatus(str, Enum):
    """一次 set_preference 的结果。"""

    CREATED = "created"
    UNCHANGED = "unchanged"
    UPDATED = "updated"


# ---- 时间戳：一律带时区、一律存 UTC ---------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    """解析 ISO 时间戳，**必须带时区**。

    没有时区的 "2026-09-19 10:30" 是歧义的——跨时区协作里同一个字符串指两个
    时刻。而且偏好要长期留存，歧义会被放大（教材的 "统一保存为 UTC"）。
    """

    if not isinstance(value, str) or not value.strip():
        raise UserMemoryValidationError(f"{field_name} 必须是非空字符串")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise UserMemoryValidationError(
            f"{field_name} 不是合法的 ISO 时间戳：{value!r}") from error
    if parsed.tzinfo is None:
        raise UserMemoryValidationError(
            f"{field_name} 必须带时区（如 2026-09-19T10:30:00+08:00 或 ...Z）："
            f"{value!r}")
    return parsed


def _format_utc(value: datetime) -> str:
    """统一成 UTC 的 ISO 串（末尾 Z）——存进去的格式只有这一种。"""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_timestamp(value: Optional[str], *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _format_utc(_parse_timestamp(value, field_name=field_name))


# ---- 校验 -----------------------------------------------------------------

def _clean_text(value: object, *, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise UserMemoryValidationError(f"{field_name} 必须是字符串")
    text = value.strip()
    if not text:
        raise UserMemoryValidationError(f"{field_name} 不能是空白")
    if len(text) > max_chars:
        raise UserMemoryValidationError(
            f"{field_name} 太长（{len(text)} 字符，上限 {max_chars}）")
    return text


def _check_preference_key(key: object) -> str:
    if not isinstance(key, str) or not PREFERENCE_KEY_PATTERN.match(key.strip()):
        raise UserMemoryValidationError(
            f"偏好 key 形状不合法：{key!r}——只允许小写字母数字，"
            "用 . - _ 分段（如 response.language）")
    return key.strip()


def _check_source_event_id(value: Optional[str]) -> Optional[str]:
    """来源事件 ID 由 Harness 提供，格式收紧（它不是模型生成的正文）。"""

    if value is None:
        return None
    if not isinstance(value, str) or not SOURCE_EVENT_ID_PATTERN.match(value):
        raise UserMemoryValidationError(
            f"source_event_id 形状不合法：{value!r}")
    return value


def scope_id(user_id: str) -> str:
    """把用户标识摘要成稳定的目录名。

    为什么不用原标识：邮箱、空格、`/` 都不该进路径（既能撑坏目录结构，
    又把用户身份暴露在文件名里）。摘要只需要**稳定**，不需要可逆。
    """

    clean = _clean_text(user_id, field_name="user_id", max_chars=200)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]


# ---- 数据结构 -------------------------------------------------------------

@dataclass(frozen=True)
class Preference:
    """一条偏好。JSON 里的每一行都是这个形状。"""

    key: str
    value: str
    revision: int
    source: str
    source_event_id: Optional[str]
    created_at: str          # UTC ISO
    updated_at: str          # UTC ISO
    expires_at: Optional[str]  # UTC ISO；None = 长期有效

    def is_active(self, as_of: datetime) -> bool:
        """在 `as_of` 这一刻是否仍然生效。

        边界取**开区间**：正好等于 expires_at 就算过期（教材：到达那一刻即
        expired）——"到 8 月 2 日 1 点为止"和"到点失效"是两回事，歧义要靠
        一边倒的规则消掉。
        """

        if self.expires_at is None:
            return True
        return as_of < _parse_timestamp(self.expires_at, field_name="expires_at")

    def identity(self) -> tuple:
        """完整幂等身份：这四样全同才算"重复调用"。

        刻意**不含** updated_at：同样的内容重试一次不该产生新 revision，
        否则每次重启、每次重放都会把 revision 推高，那个数字就失去意义了。
        """

        return (self.value, self.source, self.source_event_id, self.expires_at)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "revision": self.revision,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Preference":
        return cls(
            key=str(payload["key"]),
            value=str(payload["value"]),
            revision=int(payload["revision"]),          # type: ignore[arg-type]
            source=str(payload["source"]),
            source_event_id=(str(payload["source_event_id"])
                             if payload.get("source_event_id") else None),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            expires_at=(str(payload["expires_at"])
                        if payload.get("expires_at") else None),
        )


@dataclass(frozen=True)
class PreferenceWrite:
    """一次写入的结果：三态 + 新旧两份记录。"""

    status: PreferenceStatus
    current: Preference
    previous: Optional[Preference] = None

    @property
    def changed(self) -> bool:
        return self.status is not PreferenceStatus.UNCHANGED

    def render(self) -> str:
        before = self.previous.value if self.previous else "None"
        return (f"{self.status.value.upper()} revision={self.current.revision}"
                f"  previous={before}  current={self.current.value}")


@dataclass(frozen=True)
class ProfileWrite:
    """一次 profile patch 的结果：哪些字段真改了、哪些值没变。"""

    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def touched_disk(self) -> bool:
        return bool(self.changed)

    def render(self) -> str:
        return (f"改动：{list(self.changed) or '（无）'}；"
                f"值未变：{list(self.unchanged) or '（无）'}")


# ---- 主类 -----------------------------------------------------------------

class UserMemory:
    """一个用户的跨项目记忆。目录按 scope-id 分，互不干扰。"""

    def __init__(self, root: Path, user_id: str) -> None:
        self.user_id = _clean_text(user_id, field_name="user_id", max_chars=200)
        self.user_scope = self.user_id
        self.scope_id = scope_id(self.user_id)
        self.directory = Path(root) / "users" / self.scope_id
        self.profile_file = self.directory / PROFILE_FILE
        self.preferences_file = self.directory / PREFERENCES_FILE
        self.memory_file = self.directory / MEMORY_FILE
        self.persona_file = self.directory / PERSONA_DIR / PERSONA_USER_FILE

    # ---- 载入（读取时校验 user_scope）-------------------------------------

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise UserMemoryError(f"{path.name} 不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise UserMemoryError(f"{path.name} 的顶层必须是对象")
        stored = payload.get("user_scope")
        if stored is not None and stored != self.user_scope:
            raise UserScopeError(
                f"{path.name} 属于用户 {stored!r}，当前请求的是 "
                f"{self.user_scope!r}——拒绝把别人的记忆注进这次对话")
        return payload

    def read_profile(self) -> dict:
        """读 canonical profile（缺失字段不臆造）。"""

        return dict(self._load_json(self.profile_file).get("fields") or {})

    def list_preferences(self) -> list[Preference]:
        """全部 canonical 偏好——**含已过期**（它们要留着解释历史）。"""

        payload = self._load_json(self.preferences_file)
        records = payload.get("preferences") or []
        return [Preference.from_dict(item) for item in records]

    def list_active_preferences(
        self, *, as_of: Optional[str] = None
    ) -> list[Preference]:
        """只返回在 `as_of`（默认此刻）仍然生效的偏好——可注入集合。

        历史 `as_of` 查询是**纯读取**：不会把历史视图写回当前 MEMORY.md
        （教材：否则一次"看看 8 月那会儿生效什么"就会污染现在）。
        """

        moment = (_parse_timestamp(as_of, field_name="as_of")
                  if as_of else _now_utc())
        return [item for item in self.list_preferences() if item.is_active(moment)]

    # ---- Profile：显式字段级 patch ----------------------------------------

    def update_profile(self, patch: Mapping[str, object]) -> ProfileWrite:
        """按 patch 更新资料。只动出现的字段；`None` = 删除该字段。

        返回 ProfileWrite：哪些字段真改了、哪些值没变。**值没变就不写盘**
        ——重复调用同样内容的幂等性在这里落地。
        """

        if not isinstance(patch, Mapping):
            raise UserMemoryValidationError("patch 必须是映射（字段 → 值）")
        unknown = set(patch) - PROFILE_FIELDS
        if unknown:
            raise UserMemoryValidationError(
                f"不认识的资料字段：{sorted(unknown)}；"
                f"允许的有：{sorted(PROFILE_FIELDS)}")

        fields = self.read_profile()
        changed: list[str] = []
        unchanged: list[str] = []

        for field_name, raw in patch.items():
            if raw is None:
                if field_name in fields:
                    del fields[field_name]
                    changed.append(field_name)
                else:
                    unchanged.append(field_name)      # 本来就没有 = 无变化
                continue
            value = _clean_text(raw, field_name=field_name, max_chars=MAX_VALUE_CHARS)
            if fields.get(field_name) == value:
                unchanged.append(field_name)
            else:
                fields[field_name] = value
                changed.append(field_name)

        if changed:
            self._save_profile(fields)
        return ProfileWrite(tuple(sorted(changed)), tuple(sorted(unchanged)))

    def _save_profile(self, fields: Mapping[str, object]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "user_scope": self.user_scope,
            "fields": dict(fields),
        }
        atomic_write_text(
            self.profile_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        self._render_persona(fields)

    # ---- Preference：按 key 去重 + 幂等 + 防回滚 ---------------------------

    def set_preference(
        self,
        key: str,
        value: str,
        *,
        source: str = "explicit",
        source_event_id: Optional[str] = None,
        updated_at: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> PreferenceWrite:
        """写一条偏好，返回三态结果（CREATED / UNCHANGED / UPDATED）。

        `source_event_id` 是 **Harness provenance**（s09 的 event ID），模型
        工具不该能提交或伪造它——所以它只在这个程序化入口上，不在工具 schema 里。
        """

        clean_key = _check_preference_key(key)
        clean_value = _clean_text(value, field_name="value",
                                  max_chars=MAX_PREFERENCE_CHARS)
        clean_source = _clean_text(source, field_name="source", max_chars=80)
        clean_event = _check_source_event_id(source_event_id)

        moment = (_parse_timestamp(updated_at, field_name="updated_at")
                  if updated_at else _now_utc())
        stamp = _format_utc(moment)
        expiry = _optional_timestamp(expires_at, field_name="expires_at")
        if expiry is not None and expiry <= stamp:
            raise UserMemoryValidationError(
                f"expires_at（{expiry}）必须晚于 updated_at（{stamp}）")

        records = self.list_preferences()
        current = next((item for item in records if item.key == clean_key), None)

        if current is not None and stamp < current.updated_at:
            # 防回滚：重放旧 transcript 不能把新偏好改回旧版本。
            raise StalePreferenceUpdateError(
                f"写入时间 {stamp} 早于现有的 {current.updated_at}"
                f"（key={clean_key}）——拒绝用旧状态覆盖新状态")

        candidate = Preference(
            key=clean_key,
            value=clean_value,
            revision=1 if current is None else current.revision + 1,
            source=clean_source,
            source_event_id=clean_event,
            created_at=stamp if current is None else current.created_at,
            updated_at=stamp,
            expires_at=expiry,
        )

        if current is not None and current.identity() == candidate.identity():
            # 完整身份相同 = 一次重试。不写盘、不涨 revision——只更新
            # updated_at 会让"重试"看起来像"改动"，那 revision 就废了。
            return PreferenceWrite(PreferenceStatus.UNCHANGED, current, current)

        status = (PreferenceStatus.CREATED if current is None
                  else PreferenceStatus.UPDATED)
        updated = [item for item in records if item.key != clean_key] + [candidate]
        updated.sort(key=lambda item: item.key)
        self._save_preferences(updated)
        return PreferenceWrite(status, candidate, current)

    def delete_preference(self, key: str) -> bool:
        """按 key 精确删除（不做模糊匹配）。返回是否真的删掉了。

        为什么不做模糊匹配：Harness 要能对用户展示"将删除哪一条"，也要能在
        审计里记下明确目标。模糊匹配两个都做不到。
        """

        clean_key = _check_preference_key(key)
        records = self.list_preferences()
        remaining = [item for item in records if item.key != clean_key]
        if len(remaining) == len(records):
            return False
        self._save_preferences(remaining)
        return True

    def _save_preferences(self, records: list[Preference]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "user_scope": self.user_scope,
            "preferences": [item.to_dict() for item in records],
        }
        atomic_write_text(
            self.preferences_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        self._render_memory(records)

    # ---- 投影：Markdown 只是"给人看的那一份" ------------------------------

    def _render_persona(self, fields: Mapping[str, object]) -> None:
        lines = ["# 用户资料", "",
                 "> 这是 `profile.json` 的投影，直接改它会在下次写入时被覆盖。", ""]
        if fields:
            lines.extend(f"- **{name}**：{value}" for name, value in
                         sorted(fields.items()))
        else:
            lines.append("（还没有记录任何资料）")
        self.persona_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.persona_file, "\n".join(lines) + "\n")

    def _render_memory(self, records: list[Preference]) -> None:
        """MEMORY.md 只投影 **active** 偏好（已过期的不进这里）。"""

        moment = _now_utc()
        active = [item for item in records if item.is_active(moment)]
        lines = ["# 用户偏好（跨项目）", "",
                 "> `preferences.json` 的投影，只显示当前生效的条目。", ""]
        if active:
            for item in active:
                suffix = f"（至 {item.expires_at}）" if item.expires_at else ""
                lines.append(f"- `{item.key}` = {item.value}{suffix}")
        else:
            lines.append("（还没有生效中的偏好）")
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.memory_file, "\n".join(lines) + "\n")

    def render_projections(self) -> None:
        """按 canonical state 重建两份 Markdown 投影。

        投影坏了（手工编辑、半截写入、跨盘复制）不用慌——canonical 才是真相，
        这一句就能修回来。
        """

        self._render_persona(self.read_profile())
        self._render_memory(self.list_preferences())

    # ---- 注入 Prompt ------------------------------------------------------

    def get_context_for_agent(self, *, as_of: Optional[str] = None) -> str:
        """渲染注入 Prompt 的用户上下文块（空记忆时返回占位符）。

        只投影 active 偏好——这正是"过期不是删除"的落点：记录留在 JSON 里
        可审计，但不再影响模型的判断。

        与 s10 的 `get_context_for_agent` 同名同形状（都是一段给人/模型读的
        文本）：到 s15 组装 Prompt 时，两个来源各出一块，顺序与预算由那里定。
        """

        fields = self.read_profile()
        active = self.list_active_preferences(as_of=as_of)
        if not fields and not active:
            return NO_USER_MEMORY_PLACEHOLDER

        lines: list[str] = []
        if fields:
            lines.append("# 用户（跨项目仍然有效）")
            for name, value in sorted(fields.items()):
                lines.append(f"- {name}：{value}")
        if active:
            if lines:
                lines.append("")
            lines.append("# 用户偏好")
            for item in active:
                lines.append(f"- {item.key} = {item.value}")
        return "\n".join(lines)
