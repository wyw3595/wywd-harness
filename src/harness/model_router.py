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


# ═══════════════════════════════════════════════════════════════
# 真实模型计价（2026-11-08）：按**模型**而不是按档位算钱
# ═══════════════════════════════════════════════════════════════
#
# 为什么要有这张表：上面的 PRICES 是"档位假想价"（教材的 cost matrix），
# 而本项目的三个档位在有 key 时全部映射到 DeepSeek——按 craft 档的
# $15/$75 记账，一次 FizzBuzz 会显示 $1.44，实际不到 1 分钱（2026-10-26
# 真机验收抓到的 P0）。档位价继续留着做教学对照，但**账单**必须按实际
# 模型算。
#
# 价格来源：DeepSeek 官方 pricing（per 1M tokens）。这里取**峰值价**
# （off-peak 是它的一半），并在 UI 上标"估算"——宁可高估，不要低报。

PRICING_NOTE = "按官方峰值价估算（缓存命中/未命中分开计）"


@dataclass(frozen=True)
class ModelPricing:
    """一个真实模型的计价：缓存命中、未命中、输出三个价（USD / 1M）。"""

    model_id: str
    cache_hit_per_million: float
    cache_miss_per_million: float
    output_per_million: float

    def cost(self, prompt_tokens: int, completion_tokens: int,
             cache_hit_tokens: int = 0) -> float:
        hit = max(0, min(int(cache_hit_tokens), int(prompt_tokens)))
        miss = max(0, int(prompt_tokens) - hit)
        return (hit * self.cache_hit_per_million
                + miss * self.cache_miss_per_million
                + int(completion_tokens) * self.output_per_million) / 1_000_000


# deepseek-chat 是 deepseek-flash 的非思考兼容别名，同价（见 real_model.py）。
MODEL_PRICING: dict[str, ModelPricing] = {
    "deepseek-chat": ModelPricing("deepseek-chat", 0.006, 0.3, 1.2),
    "deepseek-flash": ModelPricing("deepseek-flash", 0.006, 0.3, 1.2),
    "deepseek-v4-flash": ModelPricing("deepseek-v4-flash", 0.006, 0.3, 1.2),
    "deepseek-v4-pro": ModelPricing("deepseek-v4-pro", 0.044, 1.32, 3.96),
}


def pricing_for(model_id: str) -> Optional[ModelPricing]:
    """按模型名找计价；找不到（FakeModel / 自建端点）返回 None。"""

    key = str(model_id or "").strip().lower()
    return MODEL_PRICING.get(key)

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

    2026-11-08：每档额外记住**实际模型**与**缓存命中 tokens**。有已知
    计价就按模型算（缓存命中/未命中分开），没有就退回档位教学价——账单
    宁可高估，不能像之前那样把 craft 假想价套在 deepseek-chat 上。
    假设一个档位只映射一个模型（build_model_router 的现状）；同档多模型
    时按最后一次的模型计价，属于已知近似。
    """

    def __init__(self) -> None:
        # 三级各三本账（键用 ModelTier 成员——s07 _ALLOWED_TRANSITIONS 同款）
        self.prompt_tokens = {t: 0 for t in ModelTier}
        self.completion_tokens = {t: 0 for t in ModelTier}
        self.calls = {t: 0 for t in ModelTier}
        self.cache_hit_tokens = {t: 0 for t in ModelTier}
        self.model_ids = {t: "" for t in ModelTier}


    def track(self, tier: ModelTier, prompt_tokens: int, completion_tokens: int,
              *, model_id: str = "", cache_hit_tokens: int = 0) -> None:
        self.prompt_tokens[tier] += prompt_tokens
        self.completion_tokens[tier] += completion_tokens
        self.calls[tier] += 1
        self.cache_hit_tokens[tier] += max(0, int(cache_hit_tokens))
        if model_id:
            self.model_ids[tier] = str(model_id)

    def cost_by_tier(self) -> dict[ModelTier, float]:
        result = {}
        for tier in ModelTier:
            pricing = pricing_for(self.model_ids[tier])
            if pricing is not None:
                result[tier] = pricing.cost(
                    self.prompt_tokens[tier], self.completion_tokens[tier],
                    self.cache_hit_tokens[tier])
            else:   # 未知模型 / 离线模型：退回档位教学价（离线 tokens 为 0 → 0）
                result[tier] = PRICES[tier].cost(
                    self.prompt_tokens[tier], self.completion_tokens[tier])
        return result

    def total_cost(self) -> float:
        return sum(self.cost_by_tier().values())

    def summary(self) -> list[dict]:
        # t.value 才是小写字符串（"lite"）；t.name 是成员名（"LITE"）——
        # str 混血 Enum 的 .value 直接当字符串用，s07 埋的设计
        costs = self.cost_by_tier()
        rows = []
        for t in ModelTier:
            model_id = self.model_ids[t]
            known = pricing_for(model_id) is not None
            rows.append({
                "tier": t.value,
                "model": model_id or PRICES[t].name,
                "calls": self.calls[t],
                "promptTokens": self.prompt_tokens[t],
                "completionTokens": self.completion_tokens[t],
                "cacheHitTokens": self.cache_hit_tokens[t],
                "cost": f"{costs[t]:.6f}",
                "estimate": known,
                "pricingNote": PRICING_NOTE,
            })
        return rows

    def reset(self) -> None:
        self.prompt_tokens = {t: 0 for t in ModelTier}
        self.completion_tokens = {t: 0 for t in ModelTier}
        self.calls = {t: 0 for t in ModelTier}
        self.cache_hit_tokens = {t: 0 for t in ModelTier}
        self.model_ids = {t: "" for t in ModelTier}


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

    def call(self, agent_name: str, messages: list[dict],
             tools: list[dict] | None = None) -> ModelReply:
        """路由 → 转发到真 Model → 按 tier 记账。

        记账诚实边界：离线模型 usage 为空 → 记 0 次 tokens 但 calls +1
        （调用确实发生了，钱是 0——不撒谎也不瞎编）。
        tools 是本轮可用工具（会话级专家收窄后）；None = 模型默认清单。
        """
        model = self.route(agent_name)
        reply = model.generate(messages, tools=tools)
        usage = reply.usage or {}
        # 记账用**实际模型**与缓存命中量：真实厂商的输入价分命中/未命中，
        # 而 deepseek-chat 与 craft 档的假想价差两个数量级（2026-10-26 验收）。
        model_id = (getattr(model, "model", "")
                    or getattr(model, "name", "")
                    or type(model).__name__.lower())
        self.tracker.track(
            self.agents.get(agent_name, ModelTier.DEFAULT),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            model_id=str(model_id),
            cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0) or 0)
        return reply



    def generate(self, messages: list[dict],
                 tools: list[dict] | None = None) -> ModelReply:
        """Model 协议适配：默认服务主对话（default_agent → craft 槽）。

        这一行是本课的关节：有了它，router 就是一个 Model（结构子类型），
        run_agent(model=router) 零改动可用。
        """
        return self.call(self.default_agent, messages, tools=tools)

    def cost_summary(self) -> list[dict]:
        """给 sidecar/status 的 UI 安全成本表（duck typing 钩子——
        裸 FakeModel/RealModel 没有这个方法，status 就不带成本段）。
        """
        return self.tracker.summary()
