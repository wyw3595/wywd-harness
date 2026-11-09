"""s18 Experts System 的离线单元测试：专家包 × 索引 × 会话级激活。

三条线：
  - 契约：ExpertPackage / ToolsConfig 构造期校验（fail-closed）；
  - 内核：specialization 渲染（叠加不覆盖）、select_tools（只能收窄）、
    ExpertIndex 目录/加载/坏包容错；
  - sidecar 接线：session/create 的 expertId 激活、转录即证据
    （close→resume 人格还在）、未知专家人话报错、expert/list。

全部离线：临时目录 + 直调 handler（同 test_sidecar 的纪律）。
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.harness.experts import (
    ExpertError,
    ExpertIndex,
    ExpertPackage,
    ToolsConfig,
    expert_step_limit,
    max_steps_for_expert,
    render_specialization,
    select_tools,
)
from src.harness.models import ModelReply, ToolCall
from src.harness.permissions import build_default_policy
from src.harness.sidecar import SidecarServer
from src.harness.tools import Tool, ToolRegistry


def _package(**overrides) -> ExpertPackage:
    payload = {
        "expert_id": "architect",
        "name": "软件架构师",
        "system_prompt": "你是资深软件架构师，先考虑可扩展性与演进成本。",
        "category": "软件开发",
        "description": "架构设计、技术选型",
        "guidelines": ["先量化再决策", "优先有文档的模式"],
        "tools_config": {"preferred_tools": ["fs_read"],
                         "disabled_tools": ["bash"]},
        "skill_bundles": ["code-review"],
    }
    payload.update(overrides)
    return ExpertPackage.from_dict(payload)


def _write_package(root: Path, payload: dict) -> None:
    experts = root / ".workbuddy" / "experts"
    experts.mkdir(parents=True, exist_ok=True)
    (experts / f"{payload['expert_id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# 一、契约：非法包在构造期炸掉
# ═══════════════════════════════════════════════════════════════

class PackageContractTests(unittest.TestCase):

    def test_missing_required_fields(self) -> None:
        with self.assertRaises(ExpertError) as ctx:
            ExpertPackage.from_dict({"expert_id": "x", "name": "y"})
        self.assertIn("system_prompt", str(ctx.exception))

    def test_bad_expert_ids_rejected(self) -> None:
        # id 进文件名：路径穿越与平台怪名字在门口挡掉。
        for bad in ("../evil", "a b", ".hidden", "", "a/b"):
            with self.assertRaises(ExpertError):
                _package(expert_id=bad)

    def test_empty_fields_rejected(self) -> None:
        with self.assertRaises(ExpertError):
            _package(name="  ")
        with self.assertRaises(ExpertError):
            _package(system_prompt="")

    def test_normalization(self) -> None:
        package = _package(
            category="  ",
            guidelines=["先量化", "  ", "再决策"],
            skill_bundles=[" code-review ", ""])
        self.assertEqual(package.category, "通用")   # 空分类回落
        self.assertEqual(package.guidelines, ("先量化", "再决策"))
        self.assertEqual(package.skill_bundles, ("code-review",))

    def test_tools_config_fail_closed(self) -> None:
        with self.assertRaises(ExpertError) as ctx:
            ToolsConfig.from_dict({"prefered_tools": ["fs_read"]})  # 拼错键
        self.assertIn("prefered_tools", str(ctx.exception))
        with self.assertRaises(ExpertError):
            ToolsConfig.from_dict(["fs_read"])      # 不是对象

    def test_summary_has_no_body(self) -> None:
        summary = _package().summary()
        self.assertEqual(summary["expert_id"], "architect")
        self.assertNotIn("system_prompt", summary)  # 正文很贵，清单不带
        self.assertEqual(summary["disabled_tools"], ["bash"])
        self.assertFalse(summary["read_only"])      # 只摘 bash 不算只读

    def test_summary_flags_full_read_only(self) -> None:
        summary = _package(tools_config={
            "disabled_tools": ["bash", "fs_write", "fs_edit", "memory_write"],
        }).summary()
        self.assertTrue(summary["read_only"])
        self.assertEqual(summary["disabled_tools"],
                         ["bash", "fs_edit", "fs_write", "memory_write"])


# ═══════════════════════════════════════════════════════════════
# 二、specialization：叠加，不覆盖
# ═══════════════════════════════════════════════════════════════

class SpecializationTests(unittest.TestCase):

    def test_block_shape(self) -> None:
        block = render_specialization(_package())
        self.assertTrue(block.startswith("<expert_specialization>"))
        self.assertTrue(block.endswith("</expert_specialization>"))
        self.assertIn("「软件架构师」", block)
        self.assertIn("资深软件架构师", block)
        self.assertIn("<behavior_guidelines>", block)
        self.assertIn("- 先量化再决策", block)
        self.assertIn("code-review", block)          # 技能束提示

    def test_no_guidelines_no_empty_block(self) -> None:
        block = render_specialization(_package(guidelines=[], skill_bundles=[]))
        self.assertNotIn("<behavior_guidelines>", block)
        self.assertNotIn("技能束", block)


# ═══════════════════════════════════════════════════════════════
# 三、select_tools：工具倾向只能收窄
# ═══════════════════════════════════════════════════════════════

class SelectToolsTests(unittest.TestCase):

    def test_disabled_removed_and_reported(self) -> None:
        config = ToolsConfig(disabled=frozenset({"bash"}))
        kept, disabled = select_tools(
            ["fs_read", "bash", "calc"], config)
        self.assertEqual(kept, ("fs_read", "calc"))
        self.assertEqual(disabled, ("bash",))

    def test_preferred_reorders_but_never_adds(self) -> None:
        # preferred 只调排序：名单里没有的名字是配置噪音，静默忽略——
        # 专家包永远不能比基础装配多出一个工具。
        config = ToolsConfig(preferred=frozenset({"calc", "ghost"}))
        kept, _ = select_tools(["fs_read", "calc", "fs_list"], config)
        self.assertEqual(kept, ("calc", "fs_read", "fs_list"))

    def test_empty_config_keeps_everything(self) -> None:
        kept, disabled = select_tools(["a", "b"], ToolsConfig())
        self.assertEqual(kept, ("a", "b"))
        self.assertEqual(disabled, ())


# ═══════════════════════════════════════════════════════════════
# 四、索引：目录常驻、整包按需、坏包容错
# ═══════════════════════════════════════════════════════════════

class ExpertIndexTests(unittest.TestCase):

    def test_missing_directory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = ExpertIndex(Path(tmp))
            self.assertEqual(index.list(), [])
            self.assertEqual(index.render_directory(), "")

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。",
                "category": "软件开发", "description": "架构与选型",
            })
            index = ExpertIndex(root)
            self.assertEqual(len(index.list()), 1)
            package = index.load("architect")
            self.assertEqual(package.name, "软件架构师")
            self.assertIn("架构师", index.render_directory())

    def test_load_failures_are_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = ExpertIndex(root)
            with self.assertRaises(ExpertError) as ctx:
                index.load("no-such-expert")
            self.assertIn("没有这个专家包", str(ctx.exception))

            _write_package(root, {
                "expert_id": "broken", "name": "x",
                "system_prompt": "y"})
            (root / ".workbuddy" / "experts" / "broken.json").write_text(
                "{这不是 json", encoding="utf-8")
            with self.assertRaises(ExpertError):
                index.load("broken")

    def test_corrupted_package_does_not_break_listing(self) -> None:
        # 坏包标着"（包损坏）"列出来，加载时才炸——目录不整体失效。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "good", "name": "好专家",
                "system_prompt": "正文"})
            (root / ".workbuddy" / "experts").mkdir(parents=True, exist_ok=True)
            (root / ".workbuddy" / "experts" / "bad.json").write_text(
                "{坏", encoding="utf-8")
            listing = ExpertIndex(root).list()
            names = {entry["expert_id"]: entry["name"] for entry in listing}
            self.assertEqual(names["good"], "好专家")
            self.assertEqual(names["bad"], "（包损坏）")


# ═══════════════════════════════════════════════════════════════
# 五、sidecar 接线：会话级激活，转录即证据
# ═══════════════════════════════════════════════════════════════

class SidecarExpertTests(unittest.TestCase):

    def _server(self, root: Path | None = None) -> SidecarServer:
        return SidecarServer(
            model=None, registry=ToolRegistry(),
            policy=build_default_policy(),
            history_seed=lambda: [{"role": "system", "content": "基础提示"}],
            expert_index=ExpertIndex(root) if root is not None else None)

    def test_activation_appends_specialization_after_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。"})
            server = self._server(root)
            sid = server._handle_session_create(
                {"expertId": "architect"})["sessionId"]

        messages = server._manager.get_session(sid).record.messages
        self.assertEqual(messages[0]["content"], "基础提示")  # 核心提示还在
        self.assertEqual(messages[-1]["role"], "system")      # 叠加在末尾
        self.assertIn("<expert_specialization>", messages[-1]["content"])
        self.assertIn("资深架构师", messages[-1]["content"])

    def test_expert_id_is_persisted_and_survives_resume(self) -> None:
        """s18：expert_id 进 Record，resume 后才能按它收窄工具。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。"})
            server = self._server(root)
            sid = server._handle_session_create(
                {"expertId": "architect"})["sessionId"]
            self.assertEqual(server._manager.load_record(sid).expert_id,
                             "architect")
            server._handle_session_close({"sessionId": sid})
            server._handle_session_resume({"sessionId": sid})

        self.assertEqual(server._manager.load_record(sid).expert_id,
                         "architect")

    def test_expert_narrows_registry(self) -> None:
        """2026-11-08 补的 s18 另一半：专家包必须在运行时真的摘工具。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。",
                "tools_config": {"preferred_tools": ["fs_read"],
                                 "disabled_tools": ["bash"]}})
            server = self._server(root)
            base = ToolRegistry()
            for name in ("bash", "fs_read", "fs_write"):
                base.register(Tool(name=name, description=name,
                                   handler=lambda **kwargs: ""))
            narrowed = server._expert_registry(base, "architect")

        self.assertEqual(narrowed.names(), ["fs_read", "fs_write"])

    def test_expert_registry_fails_closed(self) -> None:
        """索引缺失 / 未知专家：宁可禁用全部工具，也不能退回全量。"""

        base = ToolRegistry()
        base.register(Tool(name="bash", description="bash",
                           handler=lambda **kwargs: ""))
        self.assertEqual(
            self._server()._expert_registry(base, "architect").names(), [])
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            self.assertEqual(
                server._expert_registry(base, "ghost").names(), [])

    def test_unknown_expert_does_not_create_orphan_session(self) -> None:
        """先验专家再建会话：坏 expertId 不该留下一个半成品会话。"""

        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            with self.assertRaises(ValueError):
                server._handle_session_create({"expertId": "ghost"})
            self.assertEqual(server._manager.list_sessions(), [])

    def test_no_expert_id_keeps_legacy_shape(self) -> None:
        # 不带 expertId 的创建与改造前完全一致（老调用零感知）。
        server = self._server()
        sid = server._handle_session_create({})["sessionId"]
        messages = server._manager.get_session(sid).record.messages
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "基础提示")

    def test_specialization_survives_close_and_resume(self) -> None:
        # 转录即证据：close → resume 后人格还在（教科书式持久化，
        # 不靠额外字段，靠 transcript 重放）。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。"})
            server = self._server(root)
            sid = server._handle_session_create(
                {"expertId": "architect"})["sessionId"]
            server._handle_session_close({"sessionId": sid})
            resumed = server._handle_session_resume({"sessionId": sid})

        self.assertIn("sessionId", resumed)
        messages = server._manager.get_session(sid).record.messages
        self.assertTrue(any("<expert_specialization>" in m.get("content", "")
                            for m in messages))

    def test_unknown_expert_is_actionable_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            with self.assertRaises(ValueError) as ctx:
                server._handle_session_create({"expertId": "ghost"})
            self.assertIn("没有这个专家包", str(ctx.exception))

    def test_expert_id_without_index_fails_loudly(self) -> None:
        # 老装配（不带专家索引）传了 expertId：大声报错，不静默装没看见。
        server = self._server()
        with self.assertRaises(ValueError):
            server._handle_session_create({"expertId": "architect"})

    def test_expert_list_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package(root, {
                "expert_id": "architect", "name": "软件架构师",
                "system_prompt": "你是资深架构师。",
                "description": "架构与选型"})
            server = self._server(root)
            listing = server._handle_expert_list({})
            self.assertEqual(listing["experts"][0]["expert_id"], "architect")
            self.assertNotIn("system_prompt", listing["experts"][0])

        # 没有索引的装配：空清单（不是报错——UI 可以常驻这个口）。
        self.assertEqual(self._server()._handle_expert_list({}),
                         {"experts": []})


class ExpertStepLimitTests(unittest.TestCase):
    """专家级成本封顶：reviewer 8 步，只收窄不放宽。"""

    def test_reviewer_gets_its_own_limit(self) -> None:
        self.assertEqual(expert_step_limit("reviewer"), 8)
        self.assertEqual(max_steps_for_expert("reviewer", 60), 8)

    def test_unknown_expert_keeps_global_default(self) -> None:
        self.assertIsNone(expert_step_limit("architect"))
        self.assertEqual(max_steps_for_expert("architect", 60), 60)
        self.assertEqual(max_steps_for_expert("", 30), 30)

    def test_expert_limit_never_widens_global_default(self) -> None:
        # 用户把全局调到比专家上限还低：专家的限额是"最多花多少"，
        # 不是"至少给我多少"。
        self.assertEqual(max_steps_for_expert("reviewer", 3), 3)


class _EndlessToolModel:
    """永远请求工具的模型：用来把 turn 推到步数上限（不碰网络）。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: list[dict],
                 tools: list[dict] | None = None) -> ModelReply:
        self.calls += 1
        return ModelReply(
            kind="tool_calls",
            tool_calls=[ToolCall(f"c{self.calls}", "echo", {"text": "x"})],
        )


class SidecarStepCapTests(unittest.TestCase):
    """sidecar 接线：按 expert_id 选步数，撞顶后用 finalizer 收尾。

    评审会话的"读几个文件就该下结论"是业务约束；这里钉三件事：
    步数真的按专家收窄、撞顶真的调定稿、定稿结论真的进证据。
    """

    def _server(self, root: Path, model, *, max_steps: int = 30,
                finalizer=None) -> SidecarServer:
        _write_package(root, {
            "expert_id": "reviewer", "name": "代码审查员",
            "system_prompt": "你是只读代码审查员。"})
        registry = ToolRegistry()
        registry.register(Tool(name="echo", description="t",
                               handler=lambda text="": f"回声：{text}"))
        return SidecarServer(
            model=model, registry=registry,
            policy=build_default_policy(safe_tools=("echo",)),
            expert_index=ExpertIndex(root),
            max_steps=max_steps,
            finalizer=finalizer,
        )

    def _run(self, server: SidecarServer, expert_id: str = "reviewer",
             message: str = "审查这个 diff") -> dict:
        events: list[dict] = []
        server._on_event = lambda event, data: events.append(
            {"event": event, **data})
        sid = server._handle_session_create(
            {"expertId": expert_id} if expert_id else {})["sessionId"]
        server._handle_agent_send({"sessionId": sid, "message": message})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for event in events:
                if event.get("event") == "turn_end":
                    return {"sid": sid, **event}
            time.sleep(0.02)
        raise AssertionError("5 秒内没等到 turn_end")

    def test_reviewer_stops_at_eight_steps_and_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = _EndlessToolModel()
            seen: list[list[dict]] = []

            def finalizer(messages: list[dict]) -> str:
                seen.append(list(messages))
                return "审查结论：发现 1 个 blocker。"

            server = self._server(Path(tmp), model, finalizer=finalizer)
            outcome = self._run(server)

        self.assertEqual(model.calls, 8)              # 不是全局的 30
        self.assertEqual(outcome["status"], "max_steps")   # 仍诚实报截断
        self.assertEqual(outcome["output"], "审查结论：发现 1 个 blocker。")
        self.assertEqual(len(seen), 1)                # 只定稿一次
        self.assertTrue(any(message.get("role") == "tool"
                            for message in seen[0]))
        # 定稿进了证据：历史渲染不能只靠 turn_end 的直播。
        # 且**只落一份**——turn_end 的 output 不进 messages，追加一次即可
        # （2026-11-09 钩子报过"重复落盘"的 blocker，用计数钉死是假阳性）。
        record = server._manager.get_session(outcome["sid"]).record
        self.assertEqual(record.messages[-1],
                         {"role": "assistant",
                          "content": "审查结论：发现 1 个 blocker。"})
        self.assertEqual(
            len([m for m in record.messages
                 if m.get("content") == "审查结论：发现 1 个 blocker。"]), 1)

    def test_other_expert_keeps_global_limit_and_no_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = _EndlessToolModel()
            called: list[int] = []

            def finalizer(messages: list[dict]) -> str:
                called.append(1)
                return "不该出现"

            server = self._server(Path(tmp), model, max_steps=3,
                                  finalizer=finalizer)
            # 用 architect（无专家限额）验证：全局 3 步、不定稿。
            _write_package(Path(tmp), {
                "expert_id": "architect", "name": "架构师",
                "system_prompt": "你是架构师。"})
            outcome = self._run(server, expert_id="architect")

        self.assertEqual(model.calls, 3)
        self.assertEqual(called, [])
        self.assertEqual(outcome["status"], "max_steps")
        self.assertIn("步数用尽", outcome["output"])

    def test_finalizer_failure_keeps_honest_output(self) -> None:
        """定稿是一次模型调用，会失败；失败不该炸 turn，也不该吞掉真相。"""

        with tempfile.TemporaryDirectory() as tmp:
            model = _EndlessToolModel()

            def finalizer(messages: list[dict]) -> str:
                raise RuntimeError("网关 502")

            server = self._server(Path(tmp), model, finalizer=finalizer)
            outcome = self._run(server)

        self.assertEqual(outcome["status"], "max_steps")
        self.assertIn("步数用尽", outcome["output"])


if __name__ == "__main__":
    unittest.main()
