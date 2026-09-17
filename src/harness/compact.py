"""上下文压缩（教材 s14）：上下文总会满，要有办法腾地方。

============================================================
📚 为什么需要它
============================================================

agent 跑得越久消息越多。一次对话几十条消息，每条工具返回几 KB——几轮下来
就是几万 token。窗口再大也是有限的，一旦超限 API 就直接报错。

两种偷懒的做法都不行：

  - **不删**：API 调用失败；
  - **简单删掉旧消息**：agent 失忆，重复做过的事。

正解是**分层压缩**：从最廉价的开始做，不够再做更激进的，层层递进。每层都
记"省了多少"的账，才知道哪一层真正起了作用（教材的四层管线）。

============================================================
📚 四层，从轻到重
============================================================

| 层 | 策略 | 代价 |
|----|------|------|
| L1 | 超大工具结果截断 | 低（不丢消息，只丢最不值得看的部分）|
| L2 | 同一文件重复读取只留最新 | 低（不丢消息）|
| L3 | 修剪旧消息，保最新 N 条 | 中（可能丢细节）|
| L4 | 生成式摘要替换旧历史 | 高（一次模型调用）|

**每层跑完检查是否达标，达标就停**——压缩不是越多越好，做多了是白丢信息。

============================================================
📚 铁律：DurableContextState 永不进有损压缩
============================================================

这是 s14 与"简单截断"的根本区别。已确认事实、未决事项**不能交给生成式
摘要保管**：摘要可能改写事实（把 "SQLite WAL" 写成 "JSON 文件"），也可能
漏掉未决事项——而那是灾难性的。

所以它们单独走一条**无损旁路**：`DurableContextState` 是 frozen + tuple 的
结构化数据，每轮由 `render_durable_context()` 重新渲染注入。这样"摘要错了"
的后果被限制在一段摘要里，改不动事实本身。system 提示与工具定义同理，
也不进压缩层。

============================================================
📚 与教材（learn-workbuddy/s14）的差异——都是刻意的
============================================================

1. **按本项目的方言实现**。教材用 Anthropic 的 content blocks
   （`tool_result` block），我们的消息是 `{"role": "tool", "tool_call_id": ...}`
   + assistant 上的 `tool_calls`。所以 L2 不用教材那种"执行时给 block 打
   `_read_path` 标签"的做法，而是**从 assistant 的 tool_calls 反查参数**——
   同一份数据只存一处，不打第二份标签就不会漂移。
2. **L2 替换正文而不是删消息**。教材把旧的 `tool_result` block 整个删掉，
   但对应的 `tool_use` 还在——那是个**孤儿调用**，OpenAI 兼容协议会报错。
   我们只把正文换成一句"已省略"，消息结构保持完整（`tool_call_id` 仍能配上）。
3. **L3 按条数**而不是教材的 turn 概念（我们没有 turn 分组）。
4. **L4 注入式**：summarizer 是参数，默认 None = 跳过本层并在报告里标注。
   harness 不内置模型调用——它不认识任何 provider（同 real_model 的边界）。
"""

import copy
import json
import re
from dataclasses import dataclass
from typing import Callable

# 4 字符 ≈ 1 token 的粗略估算（教材口径）。精确计数要 tiktoken 那种外部词典，
# 那会让"压不压"依赖词典版本；估算只需要单调 + 相对准——它决定的是**要不要压**，
# 不是"花了多少钱"（后者由 provider 的 usage 说了算）。
CHARS_PER_TOKEN = 4

# 超过它就开始压（token）。给得比模型的硬上限低不少：压缩本身要留出余量，
# 而且"刚好卡在线上"会让每一轮都触发压缩（抖动）。
COMPACT_THRESHOLD_TOKENS = 60_000

# L1：单条 tool 消息的 token 上限。与 s13 的外化阈值（20K 字符）同量级——
# s13 处理"值得落盘的大输出"，L1 是**不依赖 s13 的兜底**（外化关掉时它顶上）。
MAX_TOOL_RESULT_TOKENS = 5_000

# L3/L4：保留最近多少条消息原样不动。
KEEP_RECENT_MESSAGES = 12

# L2 只对"读同一路径结果必然一致"的工具去重。写操作（fs_write/fs_edit）的
# 结果绝不能丢——它们是"我做过什么"的证据，而且第二次写的**内容可能不同**。
DEDUPABLE_TOOLS = frozenset({"fs_read", "fs_list"})


# ═══════════════════════════════════════════════════════════════
# 一、Token 估算
# ═══════════════════════════════════════════════════════════════

def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算消息列表的 token 数。

    算三样：正文、tool_calls 里的参数、以及 tool 消息的正文。参数也要算——
    模型看得见它们，工具目录越大，真实账单越高。
    """

    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content) // CHARS_PER_TOKEN
        for call in message.get("tool_calls") or []:
            blob = json.dumps(call, ensure_ascii=False)
            total += len(blob) // CHARS_PER_TOKEN
    return total


# ═══════════════════════════════════════════════════════════════
# 二、无损旁路：DurableContextState
# ═══════════════════════════════════════════════════════════════

def _require_text(value: object, field_name: str) -> str:
    """必填文本：空串 / 空白串 / 非字符串一律拒绝。

    校验放在构造期而不是渲染期：让"半成品状态"根本构造不出来，后面所有
    代码就都不用再判空。这也是 frozen dataclass 该有的用法——不可变 +
    构造即合法。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value


def _require_zoned_timestamp(value: object, field_name: str) -> str:
    """时间戳必须带时区。

    没有时区的 "2026-09-17 15:30" 在跨时区协作里是歧义的——同一个字符串
    能指两个时刻。durable state 要长期留存，歧义会被放大。
    """

    text = _require_text(value, field_name)
    if not (text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text)):
        raise ValueError(
            f"{field_name} 必须带时区（如 2026-09-17T15:30:00+08:00 或 ...Z）"
        )
    return text


@dataclass(frozen=True)
class DurableFact:
    """一条已确认的事实 + 它能被追溯回哪里。"""

    fact_id: str
    content: str
    source_pointer: str
    last_confirmed_at: str

    def __post_init__(self) -> None:
        _require_text(self.fact_id, "fact_id")
        _require_text(self.content, "content")
        _require_text(self.source_pointer, "source_pointer")
        _require_zoned_timestamp(self.last_confirmed_at, "last_confirmed_at")

    def render(self) -> str:
        return (f"- [{self.fact_id}] {self.content}"
                f"（来源：{self.source_pointer}；确认于 {self.last_confirmed_at}）")


@dataclass(frozen=True)
class PendingItem:
    """一件还没做完的事——同样必须能追溯回来源。"""

    item_id: str
    description: str
    source_pointer: str
    last_confirmed_at: str

    def __post_init__(self) -> None:
        _require_text(self.item_id, "item_id")
        _require_text(self.description, "description")
        _require_text(self.source_pointer, "source_pointer")
        _require_zoned_timestamp(self.last_confirmed_at, "last_confirmed_at")

    def render(self) -> str:
        return (f"- [{self.item_id}] {self.description}"
                f"（来源：{self.source_pointer}；确认于 {self.last_confirmed_at}）")


@dataclass(frozen=True)
class DurableContextState:
    """压缩流程碰不得的那一份状态。

    frozen + tuple 是刻意的：压缩管线只能**读**它，不能就地改——想改就得
    构造一份新的，那个动作在代码里一眼可见（不会藏在某个 helper 里）。
    """

    facts: tuple[DurableFact, ...] = ()
    pending_items: tuple[PendingItem, ...] = ()

    def __post_init__(self) -> None:
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("facts 里有重复的 fact_id——ID 是追溯用的，不能撞车")
        item_ids = [item.item_id for item in self.pending_items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("pending_items 里有重复的 item_id")

    @property
    def is_empty(self) -> bool:
        return not self.facts and not self.pending_items


EMPTY_DURABLE_STATE = DurableContextState()


def render_durable_context(state: DurableContextState | None) -> str:
    """把无损状态渲染成可注入的文本；空状态返回空串（调用方据此决定加不加）。

    **每轮重新渲染**：它不参与压缩，所以哪怕对话历史刚被摘要改写，这里的事实
    仍是原始那份。这正是"摘要错了也只污染摘要"的实现方式。
    """

    if state is None or state.is_empty:
        return ""

    lines: list[str] = []
    if state.facts:
        lines.append("# 已确认的事实（无损记录，不参与压缩）")
        lines.extend(fact.render() for fact in state.facts)
    if state.pending_items:
        if lines:
            lines.append("")
        lines.append("# 未决事项（无损记录，不参与压缩）")
        lines.extend(item.render() for item in state.pending_items)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 三、四层策略（每层都是纯函数：进 list、出 (list, 省了多少)）
# ═══════════════════════════════════════════════════════════════

def truncate_tool_results(
    messages: list[dict], max_tokens: int = MAX_TOOL_RESULT_TOKENS
) -> tuple[list[dict], int]:
    """L1：把超大的 tool 消息截断，并**注明**被截断。

    为什么先做它：工具输出是上下文膨胀的主因，而它的绝大部分通常是噪声
    （几千行日志里模型只要那几行错误）。截断最便宜——不丢消息、不丢结构，
    只丢最不值得看的部分。注明是必须的：模型得知道"这不是全部"，
    才会换更精确的命令重来。
    """

    saved = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        tokens = len(content) // CHARS_PER_TOKEN
        if tokens <= max_tokens:
            continue
        keep_chars = max_tokens * CHARS_PER_TOKEN
        message["content"] = (
            content[:keep_chars]
            + f"\n\n[... 已截断：原始 {len(content)} 字符，"
              f"此处保留前 {keep_chars} 字符 ...]"
        )
        saved += tokens - max_tokens
    return messages, saved


def dedup_file_reads(messages: list[dict]) -> tuple[list[dict], int]:
    """L2：同一个文件被读过多次时，只保留最新那次的正文。

    怎么认出"同一个文件"：tool 消息自己只有 `tool_call_id`，参数在它上方那条
    assistant 的 `tool_calls` 里。所以先扫一遍建立 `call_id → (工具名, 路径)`，
    再按 (工具名, 路径) 找出每个键的最后一次出现。

    **只替换正文、不删消息**：删掉 tool 消息会让上方那条 `tool_calls` 变成
    孤儿调用（OpenAI 兼容协议会直接报错）。结构必须完整，缩的只能是内容。
    """

    # 第一遍：call_id → (工具名, 路径)
    targets: dict[str, tuple[str, str]] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            name = call.get("name")
            arguments = call.get("arguments")
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if name in DEDUPABLE_TOOLS and isinstance(path, str):
                targets[call.get("call_id", "")] = (name, path)

    # 第二遍：每个键的最后一次出现（下标最大那次）
    latest: dict[tuple[str, str], int] = {}
    for index, message in enumerate(messages):
        key = targets.get(message.get("tool_call_id", ""))
        if key is not None:
            latest[key] = index

    # 第三遍：早于最后一次的，正文换成一句说明
    saved = 0
    for index, message in enumerate(messages):
        key = targets.get(message.get("tool_call_id", ""))
        if key is None or latest[key] == index:
            continue
        content = message.get("content")
        if isinstance(content, str):
            saved += len(content) // CHARS_PER_TOKEN
        message["content"] = (
            f"（{key[1]} 这之后又被读过一次，此处内容已省略）"
        )
    return messages, saved


def _leading_context(messages: list[dict]) -> list[dict]:
    """开头必须保住的那几条：system（工具目录 / 步数预算）+ 第一条 user 消息。

    为什么是"第一条 user"而不是只保"第一条消息"：第一条通常是我们注入的
    system 提示，而**用户最初的目标**紧跟在它后面——丢了这个，agent 就不知道
    自己在干嘛了（教材"常见误区"第二条：简单删除早期消息会丢掉用户原始意图）。
    """

    head = messages[:1]
    if len(messages) > 1 and messages[1].get("role") == "user":
        head = messages[:2]
    return head


def prune_old_messages(
    messages: list[dict], keep: int = KEEP_RECENT_MESSAGES
) -> tuple[list[dict], int]:
    """L3：保住开头的 system 与用户最初的目标，再接最近 `keep` 条。

    **绝不能留孤儿 tool 消息**：如果切口正好落在某条 `assistant(tool_calls)`
    之后，尾巴开头那条 tool 消息就没有调用方了——协议会报错。所以从切口往后
    一直剥，直到第一条不是 tool 消息为止。

    为什么开头那两条要单独保（而不是只保第一条）：见 `_leading_context`。
    """

    if len(messages) <= keep + 1:
        return messages, 0

    before = estimate_tokens(messages)
    head = _leading_context(messages)
    recent = messages[-keep:]
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    pruned = head + recent
    return pruned, before - estimate_tokens(pruned)


# L4 的摘要函数：旧消息 → 一段文本；返回 None / 空串表示"放弃这层"。
# 注入式——harness 不内置模型调用，它不认识任何 provider（同 real_model 的边界）。
Summarizer = Callable[[list[dict]], "str | None"]


def summarize_history(
    messages: list[dict],
    summarizer: Summarizer | None,
    keep: int = KEEP_RECENT_MESSAGES,
) -> tuple[list[dict], int]:
    """L4：用一段摘要替换旧消息（最贵的一层，要调模型）。

    摘要只覆盖**旧消息**，最近 `keep` 条原样保留——否则 agent 会失去"刚才
    发生了什么"的手感，而那是它继续干活最需要的部分。

    摘要挂进**第一条 system**（有的话就追加、没有才新插一条）。为什么不单独
    插一条中间位置的 system：多数 OpenAI 兼容实现允许，但"system 只在开头"
    是更保守的约定，而把它并进已有的那条 system 消息零风险、也不增加消息数。

    **摘要失败就整层放弃**（summarizer 返回 None/空串）。用一段空摘要换掉
    整段历史，比不压缩严重得多——那才是真的失忆。
    """

    if summarizer is None or len(messages) <= keep + 1:
        return messages, 0

    head = _leading_context(messages)
    body = messages[len(head):]
    old = body[:-keep] if len(body) > keep else []
    recent = body[-keep:]
    if not old:
        return messages, 0

    summary = summarizer(old)
    if not summary or not summary.strip():
        # 纯空白也算失败（`not "   "` 是 False，光判真假值会漏掉它）。
        # 把整段历史换成一段空白，比不压缩严重得多——那才是真的失忆。
        return messages, 0

    before = estimate_tokens(messages)
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]

    digest = "# 之前对话的摘要（由压缩生成，细节可能有损）\n" + summary

    if head and head[0].get("role") == "system":
        first = dict(head[0])
        first["content"] = f"{first.get('content', '')}\n\n{digest}"
        compacted = [first] + recent
    else:
        compacted = head + [{"role": "system", "content": digest}] + recent

    return compacted, before - estimate_tokens(compacted)


# ═══════════════════════════════════════════════════════════════
# 四、管线：从轻到重，够用就停
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CompactionReport:
    """压缩账本：压前压后多少 token、每层各省了多少。

    为什么要记账而不是只返回结果：压缩是**有损**的，说不清"为什么变短了"
    就没人敢信它。终端/网页能把这份账直接显示出来（同 usage 的待遇）。
    """

    before_tokens: int
    after_tokens: int
    steps: tuple[tuple[str, int], ...] = ()

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    @property
    def layers(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.steps)

    @property
    def changed(self) -> bool:
        return bool(self.steps)

    def render(self) -> str:
        if not self.steps:
            return (f"未压缩（{self.before_tokens} token，在阈值内）")
        body = "；".join(f"{name} -{saved}" for name, saved in self.steps)
        return (f"{self.before_tokens} → {self.after_tokens} token"
                f"（省 {self.saved_tokens}）：{body}")


def compact(
    messages: list[dict],
    budget_tokens: int = COMPACT_THRESHOLD_TOKENS,
    summarizer: Summarizer | None = None,
    keep: int = KEEP_RECENT_MESSAGES,
    max_tool_tokens: int = MAX_TOOL_RESULT_TOKENS,
) -> tuple[list[dict], CompactionReport]:
    """四层压缩管线。返回（可用的新列表, 账本）。

    入口就**深拷贝**：调用方给的往往是会话 store 里那份历史，就地改会污染
    transcript 证据；更糟的是下一轮会在"已经被压过的历史"上继续压，越压越短
    直到彻底失忆（教材的常见误区："原地修改 messages 会连带污染 Transcript
    回放或调用方保存的证据视图"）。
    """

    view = copy.deepcopy(messages)
    before = estimate_tokens(view)
    if before <= budget_tokens:
        return view, CompactionReport(before, before)

    steps: list[tuple[str, int]] = []

    def run(name: str, layer: Callable[[list[dict]], tuple[list[dict], int]]) -> None:
        nonlocal view
        view, saved = layer(view)
        if saved:
            steps.append((name, saved))

    # L1 无条件跑：它便宜，而且它压的是"最不值得看"的部分，先做不亏。
    run("L1 截断超大工具结果",
        lambda got: truncate_tool_results(got, max_tool_tokens))

    if estimate_tokens(view) > budget_tokens:
        run("L2 去重重复文件读取", dedup_file_reads)
    if estimate_tokens(view) > budget_tokens:
        run("L3 修剪旧消息", lambda got: prune_old_messages(got, keep))
    if estimate_tokens(view) > budget_tokens:
        # L4 保留得比 L3 **更少**（一半）——这个细节不做对，L4 就永远跑不起来：
        # L3 已经把历史裁到"head + keep"了，若 L4 用同一个 keep，它的 body
        # （head 之后的部分）长度正好等于 keep，"旧消息"恒为空，于是每轮都
        # 直接返回。两者错开之后，L4 压的是**中间那一段**，最近的内容仍保留
        # 原文——这正是"上下文满了，先把中段换成摘要"的常规做法。
        run("L4 生成摘要", lambda got: summarize_history(
            got, summarizer, max(2, keep // 2)))

    return view, CompactionReport(before, estimate_tokens(view), tuple(steps))


# ═══════════════════════════════════════════════════════════════
# 五、L4 的现成实现：拿一个 Model 做摘要
# ═══════════════════════════════════════════════════════════════

SUMMARY_INSTRUCTION = (
    "把下面的对话压缩成要点，供后续继续工作时参考。\n"
    "必须保留：用户的目标与约束、已经确认的结论、还没做完的事、"
    "以及关键的文件路径或标识符。\n"
    "丢弃：寒暄、失败的尝试、重复的解释。\n"
    "只输出摘要正文，不要调用任何工具。"
)


def render_for_summary(messages: list[dict]) -> str:
    """把旧消息渲染成纯文本，喂给摘要模型。

    存取分离（同 s06 的规矩）：消息是结构化的，渲染只发生在"要给人/模型看"
    的那一刻，且只在这一处——渲染结果不进任何存储。
    """

    lines: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(f"[{message.get('role', '?')}] {content}")
        for call in message.get("tool_calls") or []:
            arguments = json.dumps(call.get("arguments"), ensure_ascii=False)
            lines.append(f"[{message.get('role', '?')}→工具] "
                         f"{call.get('name')} {arguments}")
    return "\n".join(lines)


def model_summarizer(model, instruction: str = SUMMARY_INSTRUCTION) -> Summarizer:
    """拿一个 Model 做 L4 摘要（失败返回 None → 该层放弃）。

    为什么这一份放在 harness 层、而不是两个入口各写一遍：它只需要 `Model`
    协议（`generate → ModelReply`），而那正是 harness 自己的抽象——它不认识
    任何具体 provider，所以 sidecar 和 CLI 能共用同一份实现。

    三种失败都必须放弃本层：调用炸了、模型跑去调工具了（RealModel 带着
    tools，这事很常见）、返回空文本。用空摘要换掉整段历史比不压缩更糟。
    """

    def summarize(old_messages: list[dict]) -> "str | None":
        try:
            reply = model.generate([
                {"role": "system", "content": instruction},
                {"role": "user", "content": render_for_summary(old_messages)},
            ])
        except Exception as error:
            print(f"[compact] L4 摘要失败，历史保持原样：{error}")
            return None
        if reply.kind != "final" or not reply.text.strip():
            return None
        return reply.text

    return summarize
