"""历史窗口：会话记忆的成本刹车（练习 19）。

📚 为什么需要它
  历史全量重发（练习 10 的设计），账单随会话长度线性上涨（练习 18
  让你亲眼看到了数字），最终还会撑爆模型的上下文窗口。trim_history
  是最简单的刹车：只保留最近 max_messages 条。

📚 本课核心
  - 记忆策略归应用层（练习 10 铁律），但纯函数放库里有测试，
    终端和网页两个入口共用。
  - 切口安全（本课的灵魂）：assistant(tool_calls) 和 tool 消息是配对
    关系，切口若落在配对中间，API 会拒绝"孤儿 tool 消息"——tool 必须
    紧跟它的调用者。规则：窗口开头若是一条 tool 消息（它的配对方已在
    切口之外），把它丢掉，直到开头不再是 tool 为止。
  - 丢弃 vs 摘要：截断零依赖、可离线测试；摘要要调用模型本身
    （成本 + 延迟 + 不确定性），留作后续选做。

完成标志：tests/test_memory.py 全过，chat 连问多轮账单不再线性上涨。
"""

def trim_history(history: list[dict], max_messages: int) -> list[dict]:
    """把历史截断到最近 max_messages 条，且不拆散工具配对。"""

    if len(history) <= max_messages:
        # 不超限也复制：调用方的列表永远不该被我们原地改动（练习 10 礼数）。
        return list(history)

    # 负切片的坑：history[-0:] 等价于 history[0:]——整个列表！因为 -0
    # 就是 0。max_messages 为 0 时必须显式给空，不能指望负切片。
    if max_messages <= 0:
        return []

    # 取最近 max_messages 条（[-n:] = 倒数第 n 个到结尾）。
    window = history[-max_messages:]

    # 配对安全：assistant(tool_calls) 与 tool 是配对关系，切口若落在
    # 配对中间，窗口开头会是"孤儿 tool 消息"（调用方已被切掉），API
    # 直接拒绝。孤儿结果模型本来也看不到——丢弃直到开头不是 tool。
    # 先判空再取 [0]：and 短路保证空窗口不会越界。
    while window and window[0]["role"] == "tool":
        window = window[1:]

    return window
