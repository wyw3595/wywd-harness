"""手写轨迹可视化：把 RunResult.messages 渲染成一张 HTML 时间线页。

📚 为什么需要它
  chat 里模型的工具调用是个黑盒。render_trace 把完整消息历史变成
  人能看的页面：用户气泡、助手气泡、工具调用卡片（含格式化参数）、
  工具结果卡片——Langfuse 那类"轨迹树"的迷你版。

📚 本课核心
  - 纯函数与副作用分离：render_trace 只做"数据 -> HTML 字符串"，
    可离线测试；save_trace / show_trace 才碰文件和浏览器。
  - XSS 转义是本课的灵魂：LLM 输出是不可信输入，任何文本进 HTML
    之前必须 html.escape()——否则模型说一句 <script>...</script>，
    就在你的浏览器里执行了。
  - str.format 的模板里，CSS 的字面大括号要写成 {{ }}，
    因为单个 { 会被当成插值占位符。

完成标志：tests/test_trace.py 全过，scripts/demo_trace 打开浏览器能看到
完整轨迹页（含紫色雪花工具卡片）。
"""

import html
import json
import webbrowser
from datetime import datetime
from pathlib import Path

# 页面骨架模板：只有 {title} 和 {body} 两个插值点。
# 内联 CSS、零外部资源——离线双击就能看。注意 CSS 的大括号都是双写的。
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; background: #f4f5f7;
          margin: 0; padding: 24px; }}
  h1 {{ font-size: 18px; color: #333; }}
  .bubble {{ max-width: 70%; margin: 8px 0; padding: 10px 14px;
             border-radius: 12px; white-space: pre-wrap; word-break: break-word;
             line-height: 1.6; }}
  .user      {{ background: #d8eaff; margin-left: auto; }}
  .assistant {{ background: #ffffff; }}
  .card   {{ background: #fff8e1; border: 1px solid #f0d98c; max-width: 80%;
             margin: 8px 0; padding: 10px 14px; border-radius: 8px; font-size: 14px; }}
  .card pre {{ background: #fff; padding: 8px; border-radius: 6px; overflow-x: auto; }}
  .role-tag {{ font-size: 12px; color: #888; display: block; margin-bottom: 4px; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""


def _escape(text: str) -> str:
    """HTML 转义：& < > " ' 全部变成实体。任何进 HTML 的文本都要过这一关。"""

    return html.escape(text)


def _render_message(message: dict) -> str:
    """把一条消息渲染成一个 HTML 片段。"""

    role = message["role"]

    # 系统提示：会话开局注入的工具目录。低调小卡片，标灰信息
    # （s03 的目录是常驻工件，不该和用户对话同权重）。
    if role == "system":
        return (
            '<div class="card" style="background:#f0f0f2;border:1px solid #ddd;">'
            '<span class="role-tag">系统提示（工具目录）</span>'
            f'{_escape(message["content"])}'
            "</div>"
        )

    if role == "user":
        return (
            '<div class="bubble user">'
            f'<span class="role-tag">用户</span>{_escape(message["content"])}'
            "</div>"
        )

    # 带 tool_calls 的 assistant 必须在普通 assistant 之前判断：
    # 两者的区分依据就是有没有这个键。
    if role == "assistant" and "tool_calls" in message:
        cards = []
        for call in message["tool_calls"]:
            pretty = json.dumps(call["arguments"], indent=2, ensure_ascii=False)
            cards.append(
                '<div class="card">'
                f'<span class="role-tag">工具调用：{_escape(call["name"])}</span>'
                f"<pre>{_escape(pretty)}</pre>"
                "</div>"
            )
        return "\n".join(cards)

    if role == "assistant":
        return (
            '<div class="bubble assistant">'
            f'<span class="role-tag">助手</span>{_escape(message["content"])}'
            "</div>"
        )

    if role == "tool":
        return (
            '<div class="card">'
            '<span class="role-tag">'
            f"工具结果（{_escape(message['tool_call_id'])}）"
            "</span>"
            f'{_escape(message["content"])}'
            "</div>"
        )

    # 未知 role：可见地降级，绝不静默消失。
    return (
        '<div class="card">'
        f'<span class="role-tag">未知消息类型：{_escape(role)}</span>'
        "</div>"
    )


def render_trace(messages: list[dict]) -> str:
    """把消息历史渲染成完整 HTML 页面字符串（纯函数，可离线测试）。"""

    body = "\n".join(_render_message(m) for m in messages)
    return PAGE_TEMPLATE.format(title="Agent 轨迹", body=body)


def save_trace(messages: list[dict], directory: str | Path = ".") -> Path:
    """渲染并写入带时间戳的 trace-xxx.html，返回文件路径。"""

    filename = datetime.now().strftime("trace-%Y%m%d-%H%M%S.html")
    # resolve() 转成绝对路径：show_trace 里的 as_uri() 只认绝对路径。
    path = (Path(directory) / filename).resolve()
    path.write_text(render_trace(messages), encoding="utf-8")
    return path


def show_trace(messages: list[dict], directory: str | Path = ".") -> Path:
    """生成轨迹页并用默认浏览器打开——所有副作用都收在这一层。"""

    path = save_trace(messages, directory)
    webbrowser.open(path.as_uri())
    return path
