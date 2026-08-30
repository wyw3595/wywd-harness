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


# 重试策略（练习 16）："该不该重试"抽成纯函数——不发网络，测试能离线打表。
# 4xx（除 429）＝请求本身有问题（key 错、参数错），重试一万次还是它；
# 429 限流＝"你没错，只是太快，歇会儿再来"；5xx 和网络异常＝暂时性故障，
# 交给下面的重试循环。
def _is_permanent_error(status_code: int) -> bool:
    """判断一个 HTTP 状态码是否属于"重试也没用"的永久性错误。"""

    if status_code == 429:
        return False
    if 400 <= status_code < 500:
        return True
    return False


# wire 解析哨兵（练习 17）：网络层交进来的 payload 是不可信输入，任何畸形
# ——缺 choices/message、说用工具没给清单、空清单、参数坏 JSON——一律
# raise ValueError，上层当"暂时性失败"退避重试。低级异常（KeyError/
# IndexError/TypeError）在此翻译成人话，raise ... from 保留案发现场。
def _parse_reply(payload: dict) -> ModelReply:
    """把 API 返回的 JSON 字典解析成 ModelReply；畸形一律 ValueError。"""
    try:
        choices = payload["choices"]
        choice = choices[0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(
            f"响应畸形：缺少 choices/message 结构。原始 payload：{payload}"
        ) from error

    if choice.get("finish_reason") == "tool_calls":
        try:
            items = message["tool_calls"]
            if not items:
                raise ValueError(f"响应畸形：tool_calls 但没有工具调用。原始 message：{message}")
            return ModelReply(
                kind="tool_calls",
                tool_calls=[
                    ToolCall(
                        call_id=item["id"],
                        name=item["function"]["name"],
                        arguments=_parse_tool_arguments(item),
                    )
                    for item in items
                ],
            )

        except (KeyError, TypeError) as error:
            raise ValueError(
                f"响应畸形：说是 tool_calls 却没给可用清单。原始 message：{message}"
            ) from error

    # 走到这里说明 finish_reason 不是 "tool_calls"（"stop"/"length" 等），
    # 一律按最终回答处理。这行就是验收抓出的 None bug 的补丁：
    # 函数没走到 return，就等于 return None。
    return ModelReply(kind="final", text=message.get("content") or "")


def _parse_tool_arguments(item: dict) -> dict:
    try:
        return json.loads(item["function"]["arguments"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"响应畸形：工具参数不是 JSON。原始 item：{item}"
        ) from error




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

        # 永久性错误快速失败（练习 16）：401 这类错误重试一万次也是它，
        # 所以先判策略再 raise_for_status。RuntimeError 不在 RequestException
        # 家族，这个 raise 会从 except 的网里穿出去——绝不重试。
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    DEEPSEEK_URL,
                    json=payload,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if _is_permanent_error(response.status_code):
                    raise RuntimeError(
                        f"DeepSeek 拒绝请求（HTTP {response.status_code}）："
                        f"永久性错误，不重试。{response.text}"
                    )
                response.raise_for_status()
                # 解析交给哨兵（练习 17）：json() 抛的 JSONDecodeError 也是
                # ValueError 家族，和 payload 畸形共用同一个 except 分支。
                return _parse_reply(response.json())
            except ValueError as error:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"响应无法解析，已重试 {MAX_RETRIES} 次。") from error
                time.sleep(2 ** attempt)

            except requests.RequestException as error:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"调用 DeepSeek API 失败，已重试 {MAX_RETRIES} 次。") from error
                time.sleep(2 ** attempt)



            # TODO 3（练习 17）：再挂一个 except ValueError 分支——解析失败
            # （坏 JSON / 缺字段 / 坏参数）算"暂时性失败"，走同样的退避重试；
            # 耗尽后 raise RuntimeError，消息要和网络失败区分开（说清是
            # "响应无法解析"）。思考：为什么它可重试，401 却不可重试？

