"""最小 Harness 的回归测试。"""

import unittest

from src.harness.main import run
from src.harness.models import ModelReply


class StubModel:
    """测试用模型：记录 Harness 传入的消息，并返回指定文本。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.received_messages: list[dict] | None = None

    def generate(self, messages: list[dict]) -> ModelReply:
        self.received_messages = messages
        return ModelReply(kind="final", text=self.response)


class HarnessRunTests(unittest.TestCase):
    def test_run_uses_the_default_fake_model(self) -> None:
        result = run("测试任务")

        self.assertTrue(result.run_id)
        self.assertEqual(result.task, "测试任务")
        self.assertEqual(result.output, "模拟模型回复：测试任务")

    def test_run_uses_the_injected_model(self) -> None:
        model = StubModel("来自测试模型的结果")

        result = run("自定义模型任务", model=model)

        self.assertEqual(model.received_messages[0]["content"], "自定义模型任务")
        self.assertEqual(result.output, "来自测试模型的结果")
