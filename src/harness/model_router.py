"""s08 Model Routing：便宜的做粗筛，贵的做推理。

📚 这一课在做什么
  到目前为止整个 harness 只用一个模型实例干所有事。"用 AI 管理 AI"的
  第一步是承认：不是所有任务都该用最贵的模型——分类/粗筛用 lite，
  规划/执行用 default，面向用户的推理用 craft。本课把"模型选择"升级
  为一张路由表 + 一个路由器：

  ┌──────────┐   ┌──────────────┐   ┌──────────────┐
  │  lite    │   │   default    │   │   craft      │
  │ 粗筛/分类 │   │  规划/执行    │   │  推理/交互   │
  └──────────┘   └──────────────┘   └──────────────┘
  explore/title    planner/compact    cli（主对话）

📚 本课最漂亮的一步：Router 实现 Model 协议
  Model 协议（models.py）只要求 generate(messages) -> ModelReply——
  结构子类型。ModelRouter 提供同签名的 generate（内部路由到 craft 槽
  再转发），于是 run_agent(model=router) 零改动可用，sidecar 装配只换
  一个函数。s01 埋的依赖注入，在 s08 兑现成"路由器无感插入"。

📚 与教材的差异（架构适配，不是偷工减料）
  - 教材是独立 demo + mock LLM；我们的路由器转发给**真的 Model 实例**
    （FakeModel/ScriptedModel/RealModel），槽位映射在装配层注入
    （toolbox.build_model_router）——"标签 → 具体模型"的解析表换模型
    只改配置不改代码，这正是标签机制的意义。
  - 价格按输入/输出拆开（教材单一价）：真实厂商就是分开计价，且和
    练习 18 的 usage（prompt_tokens/completion_tokens）直接对上。
  - 教材的 MemorySelectorRouter（bounded candidates + tools=() 的
    零工具选择器）砍掉留给 s12：它需要检索层供给候选，现在做就是纯
    摆设。lite 槽位先在路由表预注册 explore/title，等 s10-s12 上用户。
  - usage 记账复用练习 18 的数据流：call 后从 ModelReply.usage 读
    tokens（离线模型 usage 为空 → 记 0，不撒谎）。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.harness.models import Model, ModelReply


# ═══════════════════════════════════════════════════════════════
# ModelTier — 三级成本层级
# ═══════════════════════════════════════════════════════════════

class ModelTier(str, Enum):
    """三级模型层级（str 混血：进 JSON/进字典键不用 .value，s07 同款）。"""

    LITE = "lite"        # 粗筛/分类——便宜、快、可大量并发
    DEFAULT = "default"  # 规划/执行/压缩——中间档
    CRAFT = "craft"      # 推理/用户交互——最贵、留给主对话


@dataclass(frozen=True)
class ModelInfo:
    """一个层级的计价与画像（教学价格矩阵，量级对齐教材）。

    拆输入/输出两价的原因：真实厂商分开计价（如 DeepSeek 输出贵数倍），
    且正好对上 usage 的两个键——账单数据流不用换算。
    """

    tier: ModelTier
    name: str                  # 人类可读名（展示用）
    input_per_million: float   # 输入价格：USD / 1M tokens
    output_per_million: float  # 输出价格：USD / 1M tokens
    latency_ms: int            # 预期延迟画像（展示用，不真实 sleep）

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_per_million
                + completion_tokens * self.output_per_million) / 1_000_000


# 教学价格矩阵（教材数量级；换真实价格只改这张表）
PRICES: dict[ModelTier, ModelInfo] = {
    ModelTier.LITE: ModelInfo(
        tier=ModelTier.LITE, name="lite（轻量）",
        input_per_million=0.25, output_per_million=1.25, latency_ms=200),
    ModelTier.DEFAULT: ModelInfo(
        tier=ModelTier.DEFAULT, name="default（均衡）",
        input_per_million=3.0, output_per_million=15.0, latency_ms=800),
    ModelTier.CRAFT: ModelInfo(
        tier=ModelTier.CRAFT, name="craft（主力推理）",
        input_per_million=15.0, output_per_million=75.0, latency_ms=2000),
}

# 路由表：agent 名 → 层级。唯一已存在的 agent 是 cli（主对话）；
# 其他是 s10+ 的预注册槽位——现在立名，到课再接真调用方。
DEFAULT_AGENT_ROUTES: dict[str, ModelTier] = {
    "cli": ModelTier.CRAFT,        # 主对话：直接面对用户，最强
    "planner": ModelTier.DEFAULT,  # 规划（s10 预注册）
    "compact": ModelTier.DEFAULT,  # 上下文压缩（s14 预注册）
    "explore": ModelTier.LITE,     # 代码库搜索粗筛（s10 预注册）
    "title": ModelTier.LITE,       # 终端标题生成（预注册）
}


# ═══════════════════════════════════════════════════════════════
# CostTracker — 按 tier 聚合的层级成本账本
# ═══════════════════════════════════════════════════════════════

class CostTracker:
    """按层级累计调用次数与 tokens（跨调用聚合——练习 18 的 usage 是
    单次运行的账单，这里是"哪个层级花了多少"的层级视图，分工不同）。
    """

    def __init__(self) -> None:
        # 三级各三本账（键用 ModelTier 成员——s07 _ALLOWED_TRANSITIONS 同款）
        self.prompt_tokens = {t: 0 for t in ModelTier}
        self.completion_tokens = {t: 0 for t in ModelTier}
        self.calls = {t: 0 for t in ModelTier}


    def track(self, tier: ModelTier, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens[tier] += prompt_tokens
        self.completion_tokens[tier] += completion_tokens
        self.calls[tier] += 1

    def cost_by_tier(self) -> dict[ModelTier, float]:
        return {tier: PRICES[tier].cost(self.prompt_tokens[tier],
                                        self.completion_tokens[tier])
                for tier in ModelTier}

    def total_cost(self) -> float:
        return sum(self.cost_by_tier().values())

    def summary(self) -> list[dict]:
        # t.value 才是小写字符串（"lite"）；t.name 是成员名（"LITE"）——
        # str 混血 Enum 的 .value 直接当字符串用，s07 埋的设计
        return [{"tier": t.value, "calls": self.calls[t],
                 "promptTokens": self.prompt_tokens[t],
                 "completionTokens": self.completion_tokens[t],
                 "cost": f"{self.cost_by_tier()[t]:.6f}"} for t in ModelTier]

    def reset(self) -> None:
        self.prompt_tokens = {t: 0 for t in ModelTier}
        self.completion_tokens = {t: 0 for t in ModelTier}
        self.calls = {t: 0 for t in ModelTier}


# ═══════════════════════════════════════════════════════════════
# ModelRouter — 实现 Model 协议的路由器
# ═══════════════════════════════════════════════════════════════

class ModelRouter:
    """标签 → 具体模型 的解析与转发，附带按 tier 记账。

    用法（装配层）：
        router = ModelRouter(routes={ModelTier.LITE: cheap_model,
                                     ModelTier.DEFAULT: mid_model,
                                     ModelTier.CRAFT: strong_model})
        SidecarServer(model=router, ...)   # 实现 Model 协议，无感替换

    兜底语义：未知 agent 给 DEFAULT 是 fail-safe（中等能力，不至于砸
    也不至于浪费）——和权限层的 default.deny 相反：那边漏判的代价是
    安全，这边漏判的代价是多花几毛钱。
    """

    # generate（Model 协议入口）默认服务的 agent：主对话
    default_agent = "cli"

    def __init__(self, routes: dict[ModelTier, Any],
                 agents: Optional[dict[str, ModelTier]] = None) -> None:
        # 槽位完整性校验：三级缺一不可（缺槽的路由表是半成品配置，
        # 构造期就该炸，不是路由时才 KeyError）
        missing = [t for t in ModelTier if t not in routes]
        if missing:
            raise ValueError(f"router 缺少模型槽位: {missing}")
        self.routes = routes
        self.agents = agents if agents is not None else dict(DEFAULT_AGENT_ROUTES)
        self.tracker = CostTracker()


    def route(self, agent_name: str) -> Any:
        tier = self.agents.get(agent_name, ModelTier.DEFAULT)  # 未知 agent 兜底 default
        return self.routes[tier]

    def call(self, agent_name: str, messages: list[dict]) -> ModelReply:
        """路由 → 转发到真 Model → 按 tier 记账。

        记账诚实边界：离线模型 usage 为空 → 记 0 次 tokens 但 calls +1
        （调用确实发生了，钱是 0——不撒谎也不瞎编）。
        """
        model = self.route(agent_name)
        reply = model.generate(messages)
        usage = reply.usage or {}
        self.tracker.track(
            self.agents.get(agent_name, ModelTier.DEFAULT),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0))
        return reply



    def generate(self, messages: list[dict]) -> ModelReply:
        """Model 协议适配：默认服务主对话（default_agent → craft 槽）。

        这一行是本课的关节：有了它，router 就是一个 Model（结构子类型），
        run_agent(model=router) 零改动可用。
        """
        return self.call(self.default_agent, messages)

    def cost_summary(self) -> list[dict]:
        """给 sidecar/status 的 UI 安全成本表（duck typing 钩子——
        裸 FakeModel/RealModel 没有这个方法，status 就不带成本段）。
        """
        return self.tracker.summary()
