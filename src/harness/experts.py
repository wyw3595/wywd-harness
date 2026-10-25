"""s18 Experts System：领域专家，整包加载——Skills 加能力，Experts 换人格。

============================================================
📚 为什么需要它（承接 s16 技能 / s17 连接器）
============================================================
技能是"点"（我能做代码审查了）、连接器是"外接的手"（我能调 GitHub
了）——都是增量，不改变 agent 的基本行为模式。要 agent 像技术总监
那样思考、以 UI 设计师的视角审查界面，需要换的是**人格**：系统提示、
工具倾向、行为准则、知识背景，一整包。

      Skill：按需触发、影响单次调用、追加说明
      Expert：会话级激活、重塑整个会话、叠加（不覆盖）核心身份

============================================================
📚 两个刻意的安全边界
============================================================
1. **专家叠加，不覆盖**：specialization 包在 <expert_specialization>
   标签里追加在核心提示**之后**——基础身份、工具边界、审批纪律都
   还是 harness 说的算。专家说"大胆重构"，fs_write 照样走 ASK。
2. **工具配置只能收窄**：tools_config 的 disabled_tools 从工具池里
   摘除（少给），preferred_tools 只调排序倾向（不新给任何能力）——
   专家包永远不可能比基础装配多出一个工具。与 s16 技能 manifest、
   s17 grant 同一条铁律：**扩展生态只能收窄，不能放宽**。

============================================================
📚 与教材（learn-workbuddy/s18）的差异——都是刻意的
============================================================
- 教材的专家包来自**市场缓存**（metadata.json + fetch_from_marketplace）；
  本项目无市场，专家包是工作区里的普通 JSON 文件
  （{root}/.workbuddy/experts/*.json）——谁都能读、能 diff、能进 git，
  "市场"只是换一个包来源，机制不变。
- 教材的会话级持久化靠 sessions 表的 expert_id 字段；本项目**转录即
  证据**：激活时把 specialization 作为一条 system 消息写进会话起步
  历史，resume 从转录重放——专家状态跟着 transcript 走，不需要
  额外字段（证据与派生分离的老原则）。
- 教材 switch_expert 运行中切换；本课先做"创建时激活"（切换 = 开
  新会话），运行中切换要处理"旧人格已入转录"的叠加语义，留给
  后续单独一课。

完成标志：tests/test_experts.py 全部通过。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

EXPERTS_DIR_NAME = ".workbuddy/experts"

# 专家 id 进文件名：字母数字加 -_.，别的字符一律拒绝——路径穿越
# （../evil）和平台怪名字（冒号、反斜杠）在门口就挡掉。
_EXPERT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TOOLS_CONFIG_KEYS = frozenset({"preferred_tools", "disabled_tools"})


class ExpertError(RuntimeError):
    """专家包读写与校验的人话异常（RPC 层翻译成 ValueError）。"""


def _clean(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ExpertError(f"专家包字段 {field_name} 不能为空")
    return text


@dataclass(frozen=True)
class ToolsConfig:
    """专家的工具倾向：preferred 调排序、disabled 摘除——只能收窄。

    从 dict 解析（from_dict）：未知键 fail-closed（拼错的配置静默
    失效，比报错更难查）。
    """

    preferred: frozenset[str] = frozenset()
    disabled: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, payload: object) -> "ToolsConfig":
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise ExpertError("tools_config 必须是对象"
                              "（preferred_tools / disabled_tools）")
        unknown = set(payload) - _TOOLS_CONFIG_KEYS
        if unknown:
            raise ExpertError(
                f"tools_config 有不认识的键：{sorted(unknown)}；"
                f"只允许 {sorted(_TOOLS_CONFIG_KEYS)}")
        return cls(
            preferred=frozenset(str(t) for t in payload.get("preferred_tools", ())),
            disabled=frozenset(str(t) for t in payload.get("disabled_tools", ())),
        )


@dataclass(frozen=True)
class ExpertPackage:
    """一个领域专家包：人格（system_prompt）+ 准则 + 工具倾向 + 技能束。"""

    expert_id: str
    name: str
    system_prompt: str
    category: str = "通用"
    description: str = ""
    guidelines: tuple[str, ...] = ()
    tools_config: ToolsConfig = field(default_factory=ToolsConfig)
    skill_bundles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expert_id = _clean(self.expert_id, "expert_id")
        if not _EXPERT_ID_PATTERN.match(expert_id):
            raise ExpertError(
                f"expert_id {expert_id!r} 不合法——字母数字开头，"
                "只含字母数字、点、下划线、连字符")
        object.__setattr__(self, "expert_id", expert_id)
        object.__setattr__(self, "name", _clean(self.name, "name"))
        object.__setattr__(
            self, "system_prompt", _clean(self.system_prompt, "system_prompt"))
        object.__setattr__(
            self, "category", str(self.category).strip() or "通用")
        object.__setattr__(
            self, "description", str(self.description).strip())
        object.__setattr__(
            self, "guidelines",
            tuple(str(item).strip() for item in self.guidelines
                  if str(item).strip()))
        if not isinstance(self.tools_config, ToolsConfig):
            object.__setattr__(
                self, "tools_config",
                ToolsConfig.from_dict(self.tools_config))
        object.__setattr__(
            self, "skill_bundles",
            tuple(str(item).strip() for item in self.skill_bundles
                  if str(item).strip()))

    @classmethod
    def from_dict(cls, payload: dict) -> "ExpertPackage":
        try:
            return cls(
                expert_id=payload["expert_id"],
                name=payload["name"],
                system_prompt=payload["system_prompt"],
                category=payload.get("category", "通用"),
                description=payload.get("description", ""),
                guidelines=tuple(payload.get("guidelines") or ()),
                tools_config=ToolsConfig.from_dict(
                    payload.get("tools_config")),
                skill_bundles=tuple(payload.get("skill_bundles") or ()),
            )
        except KeyError as error:
            raise ExpertError(
                f"专家包缺必填字段：{error.args[0]!r}"
                "（expert_id / name / system_prompt）") from error

    def summary(self) -> dict:
        """给 UI/清单的不透明摘要（不含正文——正文很贵）。"""

        return {
            "expert_id": self.expert_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
        }


def render_specialization(package: ExpertPackage) -> str:
    """激活时注入的 <expert_specialization> 块——叠加，不覆盖。

    刻意带标签：s15 的技能去重、上下文压缩都要能认出"这段是专家
    人格"；无准则时省掉空块，不浪费 token。
    """

    lines = [
        "<expert_specialization>",
        f"你现在以「{package.name}」的领域人格工作。",
        "",
        package.system_prompt,
    ]
    if package.guidelines:
        lines += ["", "<behavior_guidelines>"]
        lines += [f"- {item}" for item in package.guidelines]
        lines.append("</behavior_guidelines>")
    if package.skill_bundles:
        lines += ["", "相关技能束（用 load_skill 按名加载）："
                  + "、".join(package.skill_bundles)]
    lines.append("</expert_specialization>")
    return "\n".join(lines)


def select_tools(tool_names: Sequence[str],
                 config: ToolsConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把工具配置落到一份工具名单上：摘除 disabled、preferred 提前。

    纯函数（可单测、可重放）：输入名单 + 配置 → (留下的, 摘掉的)。
    preferred 只调**排序倾向**（列在前面的先被模型看见），不新增
    任何名字——名单里没有的 preferred 是配置噪音，静默忽略。
    """

    kept: list[str] = []
    disabled: list[str] = []
    for name in tool_names:
        (disabled if name in config.disabled else kept).append(name)
    ordered = ([n for n in kept if n in config.preferred]
               + [n for n in kept if n not in config.preferred])
    return tuple(ordered), tuple(disabled)


# ═══════════════════════════════════════════════════════════════
# 索引：目录常驻、整包按需加载（与技能系统同一个思想）
# ═══════════════════════════════════════════════════════════════

class ExpertIndex:
    """工作区专家目录的索引。只列目录，整包等激活时再读全文。"""

    def __init__(self, root: Path) -> None:
        self._dir = Path(root) / ".workbuddy" / "experts"

    def _package_path(self, expert_id: str) -> Path:
        return self._dir / f"{expert_id}.json"

    def list(self) -> list[dict]:
        """目录（按 id 稳定排序）：给 UI 的摘要，不给正文。"""

        if not self._dir.exists():
            return []
        summaries = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                summaries.append(
                    self.load(path.stem).summary())
            except ExpertError as error:
                # 坏包不拖垮整个目录：列出来时打上标记，加载时才炸。
                summaries.append({
                    "expert_id": path.stem,
                    "name": "（包损坏）",
                    "category": "?",
                    "description": str(error),
                })
        return summaries

    def load(self, expert_id: str) -> ExpertPackage:
        """按 id 加载整包。缺失/坏 JSON/缺字段都翻成 ExpertError 人话。"""

        expert_id = str(expert_id).strip()
        if not _EXPERT_ID_PATTERN.match(expert_id):
            raise ExpertError(f"不认识的专家 id：{expert_id!r}")
        path = self._package_path(expert_id)
        if not path.exists():
            raise ExpertError(
                f"没有这个专家包：{expert_id!r}（目录 {self._dir}）")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ExpertError(f"专家包 {expert_id!r} 不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise ExpertError(f"专家包 {expert_id!r} 的顶层必须是 JSON 对象")
        return ExpertPackage.from_dict(payload)

    def render_directory(self) -> str:
        """给人/提示看的目录文本（名字 + 分类 + 一句话）。"""

        entries = self.list()
        if not entries:
            return ""
        return "\n".join(
            f"- {entry['expert_id']}（{entry['name']} · {entry['category']}）："
            f"{entry['description'] or '（无描述）'}"
            for entry in entries)
