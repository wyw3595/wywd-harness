"""技能系统（教材 s16）：**技能先列目录，用到时再展开**。

============================================================
📚 为什么需要它
============================================================

agent 有基础工具、有记忆、有身份，但不同任务需要不同的**操作指南**：提交代码有
commit 规范、做 review 有 review 流程、部署有检查清单。全塞进系统提示会膨胀到
不可接受——而绝大多数轮次根本用不到其中任何一份。

所以学图书馆：**平时只列目录（有哪些书、讲什么），要用时才翻开那一本。**

```
启动时：扫 SKILL.md 的 frontmatter → 建索引 → 注入系统提示（每技能 ~50 token）
用到时：用户输入命中触发词 → 加载那份 SKILL.md 全文（~500-5000 token）
```

这和 s03 的**延迟工具**是同一个思想的两个应用——那边是"工具 schema 不常驻，
先 ToolSearch 再执行"，这边是"技能正文不常驻，先看目录再加载"。区别是：
延迟工具由**模型**主动发现，技能由**触发词自动匹配**（服务端做，省一次往返）。

============================================================
📚 权限：manifest 是**请求**，不是授权（本章最重要的一条）
============================================================

SKILL.md 的 frontmatter 里可以声明 `permissions`。但要记住：

    有效权限 = Harness 基础权限 ∩ 已审核的 Skill manifest

也就是说 manifest **只能收窄、不能放宽**。一个技能声明 `tools: [bash]`，
不等于它就能跑 bash——它仍然要过 s04 的审批闸门与 s23 的沙盒。
把"技能声明"当成授权是本章列的常见误区之一。

`paths` 只约束**能理解结构化 `path` 参数**的工具；`bash` 这类多用途工具
仍需要沙盒与审批——**不能靠字符串扫描获得真正的隔离**。

============================================================
📚 存储：两级目录，项目级优先
============================================================

    用户级  ~/.workbuddy/skills/      个人、跨项目
    项目级  {workspace}/.workbuddy/skills/   项目特定、团队共享

同名技能**项目级覆盖用户级**——越具体的作用域越优先（和记忆那套一样）。

============================================================
📚 与教材的两处差异
============================================================

1. **不引入 PyYAML**。项目一直是零依赖的纯标准库（写工具、记忆、压缩都不用
   第三方），为一份 frontmatter 装一个解析器不值得。所以这里有一个**受限的
   解析器**：只认 SKILL.md 用到的子集，遇到不认识的语法**明确报错**——
   宁可让作者改一行，也不要"猜"出一个可能错的配置。
2. **正文有长度上限**。教材没提；但"按需加载"如果一次能载入几十万字符，
   就等于没做这个功能。超限截断并注明。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

SKILL_FILE_NAME = "SKILL.md"
SKILLS_DIR_NAME = "skills"

# 单个技能正文的上限：按需加载的意义就是"一次别吃太多"。
MAX_SKILL_BODY_CHARS = 20_000
MAX_SUMMARY_CHARS = 200
MAX_TRIGGERS = 20

# frontmatter 的字段白名单（严格）。未知字段直接拒绝：一个打错的键
# （比如把 read_when 写成 readwhen）如果被静默忽略，那个技能就永远不触发，
# 而作者还以为配好了——这种沉默的失败比报错难查得多。
ALLOWED_FIELDS = frozenset({"title", "summary", "read_when",
                            "agent_created", "permissions"})
ALLOWED_PERMISSION_FIELDS = frozenset({"tools", "network", "paths"})
ALLOWED_PATH_FIELDS = frozenset({"read", "write"})

# 兼容宿主（WorkBuddy）的 SKILL.md 字段名：它用 name / description，
# 教材用 title / summary。认这两个别名，是为了让**机器上已有的技能**也能
# 被列出来——不然这套实现就只能认自己手写的文件，那没什么用。
FIELD_ALIASES = {"name": "title", "description": "summary"}


class SkillError(RuntimeError):
    """技能系统的错误基类。"""


class SkillFormatError(SkillError):
    """SKILL.md 的 frontmatter 不合法。"""


class SkillNotFoundError(SkillError):
    """索引里没有这个技能。"""


# ═══════════════════════════════════════════════════════════════
# 一、受限的 frontmatter 解析器（不引入 PyYAML）
# ═══════════════════════════════════════════════════════════════

# 不接受的 YAML 特性：与其猜，不如让作者改一行
_UNSUPPORTED_VALUE_PREFIXES = {
    "|": "多行字符串（|）",
    ">": "折叠字符串（>）",
    "&": "锚点（&）",
    "*": "别名（*）",
    "{": "流式映射（{}）",
}


def _unquote(value: str) -> str:
    """去掉一对成对的引号（只做最朴素的判断，不做转义解析）。"""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_inline_list(value: str, field_name: str) -> list[str]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [_unquote(item.strip()) for item in inner.split(",") if item.strip()]


def _parse_scalar(value: str, field_name: str):
    """解析一个标量（`键: 值` 的右侧，非空那一侧）。"""

    for prefix, label in _UNSUPPORTED_VALUE_PREFIXES.items():
        if value.startswith(prefix):
            raise SkillFormatError(
                f"{field_name} 用了不支持的 YAML 语法：{label}——请写成单行")
    if value.startswith("[") and value.endswith("]"):
        return _parse_inline_list(value, field_name)
    if value in ("true", "false"):
        return value == "true"
    return _unquote(value)


def _parse_entries(entries: list[tuple[int, str]], start: int, indent: int):
    """按**缩进**递归解析同层的一组条目。返回 (值, 下一行的下标)。

    刻意不设深度上限：`permissions.paths.read` 就已经是两层了，再硬编码
    "只支持两层"迟早被撞破。缩进本身就是层级信息，照着它递归就行。
    """

    if entries[start][1].startswith("- "):
        items: list[str] = []
        index = start
        while index < len(entries) and entries[index][0] == indent:
            text = entries[index][1]
            if not text.startswith("- "):
                raise SkillFormatError(f"列表里混进了非列表项：{text!r}")
            items.append(_unquote(text[2:].strip()))
            index += 1
        return items, index

    result: dict = {}
    index = start
    while index < len(entries):
        line_indent, text = entries[index]
        if line_indent < indent:
            break                                   # 回到上一层
        if line_indent > indent:
            raise SkillFormatError(f"意外的缩进：{text!r}")
        if ":" not in text:
            raise SkillFormatError(f"这一行不是 `键: 值`：{text!r}")

        key, _, raw_value = text.partition(":")
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise SkillFormatError(f"键名是空的：{text!r}")
        index += 1

        if index < len(entries) and entries[index][0] > indent:
            if value:
                # `title: demo` 后面还跟着缩进行 —— 那行没有归属。静默忽略的
                # 后果是作者以为自己配上了（比如权限声明没生效），所以报错。
                raise SkillFormatError(f"{key} 已经有值，后面却又跟了缩进子项")
            child, index = _parse_entries(entries, index, entries[index][0])
            result[key] = child
        else:
            result[key] = _parse_scalar(value, key)

    return result, index


def _parse_frontmatter_block(raw: str) -> dict:
    """解析 frontmatter 的正文块（`---` 之间的部分）。"""

    entries: list[tuple[int, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append((len(line) - len(line.lstrip(" \t")), stripped))

    if not entries:
        return {}

    parsed, _index = _parse_entries(entries, 0, entries[0][0])
    if not isinstance(parsed, dict):
        raise SkillFormatError("frontmatter 的顶层必须是一组 `键: 值`")
    return parsed


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """把 SKILL.md 拆成 (frontmatter 字典, 正文)。

    只支持这个子集：

        key: value                字符串 / true / false
        key: [a, b]               行内列表
        key:                      块列表
          - a
          - b
        key:                      一层嵌套映射
          read: ["**"]
          write: []

    其余（多行字符串、锚点、流式映射、深层嵌套）**一律报错**而不是猜。
    """

    if not text.startswith("---"):
        raise SkillFormatError("SKILL.md 必须以 `---` 开头的 frontmatter 起始")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillFormatError("找不到 frontmatter 的结束 `---`")

    return _parse_frontmatter_block(parts[1]), parts[2].strip()


# ═══════════════════════════════════════════════════════════════
# 二、权限清单：只能收窄，不能放宽
# ═══════════════════════════════════════════════════════════════

def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillFormatError(f"{field_name} 必须是字符串列表")
    return tuple(value)


def _path_list(value: object, field_name: str) -> tuple[str, ...]:
    """路径白名单：只接受**相对**路径，且不含 `..`。

    绝对路径与 `..` 意味着"跑到技能目录之外"，那不是能力声明，那是越界。
    """

    items = _string_list(value, field_name)
    for item in items:
        looks_absolute = (item.startswith(("/", "\\"))
                          or (len(item) > 1 and item[1] == ":"))
        if looks_absolute or ".." in item.replace("\\", "/").split("/"):
            raise SkillFormatError(
                f"{field_name} 里有越界路径：{item!r}——只允许相对路径且不含 `..`")
    return items


@dataclass(frozen=True)
class SkillPermissions:
    """技能**请求**的能力。

    ⚠️ 它只是请求。有效权限永远是「Harness 基础权限 ∩ 这份 manifest」——
    声明得再大胆也越不过 s04 的审批与 s23 的沙盒。
    """

    tools: tuple[str, ...] = ()
    network: bool = False
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: object) -> "SkillPermissions":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise SkillFormatError("permissions 必须是一个映射")

        unknown = set(payload) - ALLOWED_PERMISSION_FIELDS
        if unknown:
            raise SkillFormatError(
                f"permissions 里有不认识的字段：{sorted(unknown)}；"
                f"允许的有：{sorted(ALLOWED_PERMISSION_FIELDS)}")

        network = payload.get("network", False)
        if not isinstance(network, bool):
            raise SkillFormatError("permissions.network 必须是 true / false")

        paths = payload.get("paths") or {}
        if not isinstance(paths, Mapping):
            raise SkillFormatError("permissions.paths 必须是一个映射")
        unknown_paths = set(paths) - ALLOWED_PATH_FIELDS
        if unknown_paths:
            raise SkillFormatError(
                f"permissions.paths 里有不认识的字段：{sorted(unknown_paths)}")

        return cls(
            tools=_string_list(payload.get("tools"), "permissions.tools"),
            network=network,
            read_paths=_path_list(paths.get("read"), "permissions.paths.read"),
            write_paths=_path_list(paths.get("write"), "permissions.paths.write"),
        )

    def render(self) -> str:
        parts = []
        if self.tools:
            parts.append("工具 " + "/".join(self.tools))
        if self.network:
            parts.append("需要联网")
        if self.write_paths:
            parts.append("会写 " + "/".join(self.write_paths))
        return "、".join(parts) if parts else "无额外请求"


# ═══════════════════════════════════════════════════════════════
# 三、索引条目（**不含正文**）
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Skill:
    """索引里的一条。刻意**没有 content 字段**——正文按需再读。

    这不是省内存（磁盘读一次很便宜），而是省**上下文**：只要正文不挂在这个
    对象上，"不小心把索引当全文注进 prompt" 这类错误就不可能发生。
    """

    name: str
    summary: str
    read_when: tuple[str, ...]
    path: Path
    scope: str                     # "user" | "project"
    agent_created: bool = False
    permissions: SkillPermissions = SkillPermissions()

    def render_line(self) -> str:
        """目录里的一行（每个技能约 50 token 的账就花在这里）。"""

        trigger = "、".join(self.read_when[:6])
        suffix = f"（用于：{trigger}）" if trigger else ""
        return f"  - {self.name}: {self.summary}{suffix}"


def _read_skill(skill_file: Path, scope: str) -> Skill:
    """读一个 SKILL.md，**只取 frontmatter**。"""

    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as error:
        raise SkillError(f"读不了技能文件 {skill_file}：{error}") from error

    try:
        frontmatter, _body = parse_frontmatter(text)
    except SkillFormatError as error:
        # 错误信息必须带上**是哪个文件**坏了：扫描是批量的，不带路径的
        # "frontmatter 不合法"在十几个技能里等于没说。
        raise SkillFormatError(f"{skill_file}：{error}") from error

    # 兼容宿主格式的字段名（name / description）。别名必须在字段白名单**之前**
    # 归一化——否则它们会被当成"不认识的字段"直接拒掉。
    for alias, canonical in FIELD_ALIASES.items():
        if alias in frontmatter:
            frontmatter.setdefault(canonical, frontmatter.pop(alias))

    unknown = set(frontmatter) - ALLOWED_FIELDS
    if unknown:
        raise SkillFormatError(
            f"{skill_file}：frontmatter 里有不认识的字段 {sorted(unknown)}；"
            f"允许的有：{sorted(ALLOWED_FIELDS)}")
    raw_triggers = frontmatter.get("read_when") or []
    triggers = _string_list(raw_triggers, "read_when")[:MAX_TRIGGERS]
    summary = str(frontmatter.get("summary") or "")[:MAX_SUMMARY_CHARS]

    return Skill(
        name=str(frontmatter.get("title") or skill_file.parent.name),
        summary=summary,
        read_when=triggers,
        path=skill_file,
        scope=scope,
        agent_created=bool(frontmatter.get("agent_created", False)),
        permissions=SkillPermissions.from_dict(frontmatter.get("permissions")),
    )


# ═══════════════════════════════════════════════════════════════
# 四、索引：建目录、匹配触发词、按需加载正文
# ═══════════════════════════════════════════════════════════════

class SkillIndex:
    """两级技能目录的索引。**只列目录，正文等用到再读。**"""

    def __init__(self, user_dir: Optional[Path] = None,
                 project_dir: Optional[Path] = None) -> None:
        self.user_dir = Path(user_dir) if user_dir is not None else (
            Path.home() / ".workbuddy" / SKILLS_DIR_NAME)
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self._skills: list[Skill] = []
        self.errors: list[str] = []
        self.reload()

    # ---- 建索引 ----------------------------------------------------------

    def reload(self) -> None:
        """重新扫两个目录。

        扫描顺序就是优先级顺序：**先用户级、后项目级**——后者覆盖同名者，
        因为"这个项目的约定"比"我个人的习惯"更具体。

        单个技能格式错**不拖垮整次扫描**：记进 `errors` 并跳过。
        一个坏文件让所有技能都用不了，那个代价没人愿意付。
        """

        by_name: dict[str, Skill] = {}
        self.errors = []

        for scope, directory in (("user", self.user_dir),
                                 ("project", self.project_dir)):
            if directory is None or not Path(directory).is_dir():
                continue
            for skill_file in sorted(Path(directory).glob(f"*/{SKILL_FILE_NAME}")):
                try:
                    skill = _read_skill(skill_file, scope)
                except SkillError as error:
                    # 路径在这里**统一**加：扫描是批量的，不带主语的
                    # "frontmatter 不合法"在十几个技能里等于没说。
                    self.errors.append(f"{skill_file}：{error}")
                    continue
                by_name[skill.name] = skill      # 同名时后扫的（项目级）覆盖

        self._skills = sorted(by_name.values(), key=lambda item: item.name)

    # ---- 查询 ------------------------------------------------------------

    def skills(self) -> list[Skill]:
        return list(self._skills)

    def get(self, name: str) -> Skill:
        for skill in self._skills:
            if skill.name == name:
                return skill
        raise SkillNotFoundError(
            f"没有这个技能：{name}——可用的有：{[s.name for s in self._skills] or '（空）'}")

    def match(self, text: str) -> list[Skill]:
        """按触发词匹配（大小写不敏感的子串匹配）。

        这是**确定性**的字符串匹配，不是语义判断。所以宁可多命中一个也别漏：
        多命中的代价是几百 token，漏掉的代价是模型不知道该怎么做这件事。
        """

        if not text:
            return []
        lowered = text.lower()
        return [skill for skill in self._skills
                if any(trigger.lower() in lowered for trigger in skill.read_when)]

    # ---- 按需加载正文 ----------------------------------------------------

    def load(self, name: str) -> str:
        """读技能的完整正文——**这才是"用到时再展开"的那一步**。"""

        skill = self.get(name)
        try:
            text = skill.path.read_text(encoding="utf-8")
        except OSError as error:
            raise SkillError(f"读不了技能正文 {skill.path}：{error}") from error

        _frontmatter, body = parse_frontmatter(text)
        if len(body) > MAX_SKILL_BODY_CHARS:
            body = body[:MAX_SKILL_BODY_CHARS] + (
                f"\n\n[... 技能正文被截断：原始 {len(body)} 字符，"
                f"上限 {MAX_SKILL_BODY_CHARS} ...]")
        return body

    # ---- 注入 Prompt 的那两段 --------------------------------------------

    def render_directory(self) -> str:
        """渲染**目录**：只有名字 + 摘要 + 何时用。空索引返回空串。

        这一段常驻系统提示（每技能约 50 token）；正文不进这里——
        这就是整个机制省钱的地方。
        """

        if not self._skills:
            return ""
        lines = ["# 可用的技能（用到再展开）",
                 "下面是这个环境里可用的操作指南。需要时用 load_skill 取全文；",
                 "匹配到你的任务就直接照它做。"]
        lines.extend(skill.render_line() for skill in self._skills)
        return "\n".join(lines)

    def render_matches(self, text: str) -> str:
        """对一段输入做匹配，把命中技能的**正文**拼成可注入的一段。

        没有命中返回空串（调用方据此决定加不加消息）。
        """

        hits = self.match(text)
        if not hits:
            return ""
        blocks = [
            f"# 技能：{skill.name}（命中触发词后自动展开）\n\n{self.load(skill.name)}"
            for skill in hits
        ]
        return "\n\n---\n\n".join(blocks)
