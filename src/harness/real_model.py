"""
============================================================
Harness 练习 07 + 09：真实模型（DeepSeek）
============================================================

📚 为什么需要它
  FakeModel / ScriptedModel 只会背台词。RealModel 第一次让 Harness
  真正"思考"：generate 发 HTTP 请求到 DeepSeek，解析 JSON 拿回文本。
  它实现同一个 Model 协议，所以循环、工具、历史、状态零改动。

📚 本课核心
  - 环境变量管理密钥：os.environ.get，绝不硬编码进代码。
  - requests 发 POST：URL、Bearer 令牌请求头、JSON 请求体、timeout。
  - 响应是 OpenAI 兼容格式，文本藏在 choices[0].message.content。
  - 健壮性：网络异常按"指数退避"重试；密钥缺失是"真意外"，
    立刻大声报错，不能被重试掩盖。
  - 依赖隔离：真模型单独一个文件，14 条离线测试不需要网络库。

📚 面试要点：超时和重试是网络调用的基本礼仪；密钥进代码 = 泄漏倒计时。

完成标志：python -m scripts.smoke_deepseek 冒烟成功，打印真实回复。
============================================================
"""

import json
import os
import time

import requests

from src.harness.models import ModelReply, ToolCall

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 30  # 秒。网络调用必须设超时，否则可能永远卡住。
MAX_RETRIES = 3

# 原生 tool_calls 时代：提示词只负责人设，不再承载任何格式约定。
SYSTEM_PROMPT = "你是一个任务执行者。简洁、直接地完成任务；不确定时如实说明。"


def _to_wire_messages(messages: list[dict]) -> list[dict]:
    """把循环维护的"方言"历史翻译成 DeepSeek 接受的 wire 格式。

    唯一需要翻译的是 assistant 的工具调用消息。我们的方言：
        {"role": "assistant", "content": "",
         "tool_calls": [{"call_id": "call_1", "name": "add",
                         "arguments": {"a": 2, "b": 3}}]}
    wire 格式要求：
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "add",
                                      "arguments": "<JSON 字符串>"}}]}
    注意 arguments 在 wire 上是 JSON 字符串（用 json.dumps 生成），不是字典。

    其余消息（user / 普通 assistant / tool 结果）练习 05/08 设计时
    就对齐了 wire 格式，原样放行即可。
    """

    wire_messages: list[dict] = []
    for message in messages:
        if message["role"] == "assistant" and "tool_calls" in message:
            wire_messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call["call_id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call["arguments"], ensure_ascii=False
                                ),
                            },
                        }
                        for call in message["tool_calls"]
                    ],
                }
            )
        else:
            wire_messages.append(message)

    return wire_messages


class RealModel:
    """调用 DeepSeek /chat/completions 的真实模型，实现 Model 协议。

    需要环境变量 DEEPSEEK_API_KEY，或在构造时传入 api_key。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEEPSEEK_MODEL,
        tools: list[dict] | None = None,
    ) -> None:
        # 参数优先、环境变量兜底；都没有就立刻失败——密钥缺失不能被重试掩盖。
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "缺少 DeepSeek API 密钥。请前往 platform.deepseek.com 申请，"
                "并将其设置为环境变量 DEEPSEEK_API_KEY，或在初始化时直接传入。"
            )
        self.model = model

        # 没传 tools 就是"手无寸铁"的模型；传了说明书它才知道能调用什么。
        self.tools = tools

    def generate(self, messages: list[dict]) -> ModelReply:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *_to_wire_messages(messages),
            ],
        }
        if self.tools:
            payload["tools"] = self.tools

        # 已知边界：raise_for_status 抛的 HTTPError 属于 RequestException 家族，
        # 所以 401（key 无效）也会被重试 3 次——严格说它属于"真意外"，
        # 应先判 response.status_code == 401 立刻抛错。v1 保持简单，留作改进点。
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    DEEPSEEK_URL,
                    json=payload,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                choice = response.json()["choices"][0]
                message = choice["message"]

                if choice["finish_reason"] == "tool_calls":
                    return ModelReply(
                        kind="tool_calls",
                        tool_calls=[
                            ToolCall(
                                call_id=item["id"],
                                name=item["function"]["name"],
                                arguments=json.loads(item["function"]["arguments"]),
                            )
                            for item in message["tool_calls"]
                        ],
                    )

                return ModelReply(kind="final", text=message.get("content") or "")

            except requests.RequestException as error:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"调用 DeepSeek API 失败，已重试 {MAX_RETRIES} 次。") from error
                time.sleep(2 ** attempt)

