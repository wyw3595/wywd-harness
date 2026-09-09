"""s08 模型路由的离线单测：路由表 / 拆价记账 / Model 协议适配 / 接线。

三个槽位用三个不同的 ScriptedModel 占位，回复文本各带槽位标记——
"回的是哪个槽"用回复内容判别（真伪判别法，比断言内部状态诚实）。
"""

import unittest

from src.harness.agent import run_agent
from src.harness.model_router import (
    DEFAULT_AGENT_ROUTES,
    PRICES,
    CostTracker,
    ModelRouter,
    ModelTier,
)
from src.harness.models import FakeModel, ModelReply, ScriptedModel
from src.harness.sidecar import SidecarServer
from src.harness.tools import ToolRegistry


def _reply(text: str, usage: dict | None = None) -> ModelReply:
    return ModelReply(kind="final", text=text, usage=usage or {})


def _router() -> tuple[ModelRouter, ScriptedModel, ScriptedModel, ScriptedModel]:
    """三槽三模型：lite/default/craft 各回自己的槽位标记。"""

    lite = ScriptedModel([_reply("来自lite")])
    default = ScriptedModel([_reply("来自default")])
    craft = ScriptedModel([_reply("来自craft")])
    router = ModelRouter(routes={
        ModelTier.LITE: lite,
        ModelTier.DEFAULT: default,
        ModelTier.CRAFT: craft,
    })
    return router, lite, default, craft


class RoutingTests(unittest.TestCase):
    """路由表：已知 agent 按表走；未知 agent 兜底 default；缺槽构造期就炸。"""

    def test_routes_known_agents_by_table(self) -> None:
        router, lite, default, craft = _router()
        self.assertIs(router.route("cli"), craft)         # 主对话 → craft
        self.assertIs(router.route("planner"), default)   # 规划 → default
        self.assertIs(router.route("explore"), lite)      # 粗筛 → lite
        self.assertEqual(DEFAULT_AGENT_ROUTES["cli"], ModelTier.CRAFT)

    def test_unknown_agent_falls_back_to_default(self) -> None:
        """fail-safe 兜底：未知 agent 给中等能力（漏判代价是多花钱，不是砸）。"""

        router, _, default, _ = _router()
        self.assertIs(router.route("no-such-agent"), default)

    def test_constructor_rejects_missing_tiers(self) -> None:
        """半成品配置构造期就炸，不是路由时才 KeyError。"""

        with self.assertRaises(ValueError):
            ModelRouter(routes={ModelTier.LITE: FakeModel(),
                                ModelTier.DEFAULT: FakeModel()})


class CallTests(unittest.TestCase):
    """call：转发到正确槽位 + 按 tier 记 usage（练习 18 的账单数据流）。"""

    def test_call_forwards_to_routed_model(self) -> None:
        router, *_ = _router()
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(router.call("explore", msgs).text, "来自lite")
        self.assertEqual(router.call("planner", msgs).text, "来自default")
        self.assertEqual(router.call("cli", msgs).text, "来自craft")

    def test_call_tracks_usage_by_tier(self) -> None:
        lite = ScriptedModel([_reply("x", usage={
            "prompt_tokens": 100, "completion_tokens": 50})])
        router = ModelRouter(routes={
            ModelTier.LITE: lite,
            ModelTier.DEFAULT: FakeModel(),
            ModelTier.CRAFT: FakeModel(),   # usage 空：记 0 次 tokens
        })
        router.call("explore", [{"role": "user", "content": "q"}])
        router.generate([{"role": "user", "content": "q"}])   # 走 craft
        self.assertEqual(router.tracker.calls[ModelTier.LITE], 1)
        self.assertEqual(router.tracker.prompt_tokens[ModelTier.LITE], 100)
        self.assertEqual(router.tracker.completion_tokens[ModelTier.LITE], 50)
        self.assertEqual(router.tracker.calls[ModelTier.CRAFT], 1)
        # 离线模型 usage 为空 → tokens 记 0（调用发生了，钱是 0，不撒谎）
        self.assertEqual(router.tracker.prompt_tokens[ModelTier.CRAFT], 0)


class ProtocolTests(unittest.TestCase):
    """generate 满足 Model 协议——run_agent 零改动接入（本课的关节）。"""

    def test_generate_routes_to_craft_slot(self) -> None:
        router, *_ = _router()
        reply = router.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, "来自craft")   # default_agent="cli"→craft

    def test_run_agent_accepts_router_as_model(self) -> None:
        """结构子类型实锤：router 就是一个 Model，主循环无感使用。"""

        router, *_ = _router()
        result = run_agent("你好", model=router, registry=ToolRegistry())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output, "来自craft")


class CostTrackerTests(unittest.TestCase):
    """拆价计算 + 按 tier 聚合 + reset。"""

    def test_price_table_split_pricing(self) -> None:
        lite = PRICES[ModelTier.LITE]
        self.assertEqual(lite.cost(1_000_000, 0), 0.25)    # 纯输入
        self.assertEqual(lite.cost(0, 1_000_000), 1.25)    # 纯输出
        craft = PRICES[ModelTier.CRAFT]
        self.assertAlmostEqual(craft.cost(2000, 1000), 0.105)  # 混合

    def test_aggregates_by_tier_and_resets(self) -> None:
        tracker = CostTracker()
        tracker.track(ModelTier.LITE, 1_000, 500)
        tracker.track(ModelTier.LITE, 1_000, 500)
        tracker.track(ModelTier.CRAFT, 2_000, 1_000)
        by_tier = tracker.cost_by_tier()
        self.assertAlmostEqual(by_tier[ModelTier.LITE], 0.00175)  # 2×(250+625)/1e6
        self.assertAlmostEqual(by_tier[ModelTier.CRAFT], 0.105)
        self.assertAlmostEqual(tracker.total_cost(), 0.10675)
        rows = tracker.summary()
        self.assertEqual([r["tier"] for r in rows], ["lite", "default", "craft"])
        lite_row = rows[0]
        self.assertEqual(lite_row["calls"], 2)
        self.assertEqual(lite_row["promptTokens"], 2_000)
        self.assertEqual(lite_row["completionTokens"], 1_000)
        tracker.reset()
        self.assertEqual(tracker.total_cost(), 0.0)
        self.assertEqual(tracker.calls[ModelTier.LITE], 0)


class SidecarIntegrationTests(unittest.TestCase):
    """/status 的 duck typing 挂钩：路由器带成本表，裸模型不带。"""

    def test_status_carries_cost_table_when_model_is_router(self) -> None:
        from src.harness.permissions import build_default_policy

        router, *_ = _router()
        router.generate([{"role": "user", "content": "hi"}])   # 记一笔
        server = SidecarServer(model=router, registry=ToolRegistry(),
                               policy=build_default_policy())
        status = server._handle_status({})
        rows = status["modelCost"]
        craft_row = next(r for r in rows if r["tier"] == "craft")
        self.assertEqual(craft_row["calls"], 1)

    def test_status_omits_cost_table_for_plain_model(self) -> None:
        from src.harness.permissions import build_default_policy

        server = SidecarServer(model=FakeModel(), registry=ToolRegistry(),
                               policy=build_default_policy())
        self.assertNotIn("modelCost", server._handle_status({}))


if __name__ == "__main__":
    unittest.main()
