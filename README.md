# wywd-harness

从零实现一个 Python LLM Harness 的学习项目。

## 当前能力

- `run(task)` 会创建一次运行结果。
- `FakeModel` 在离线环境下生成可预测的回复。
- `run(task, model=...)` 可注入任意实现 `generate(task) -> str` 的模型。
- `Tool` 描述可执行工具，`ToolRegistry` 负责注册、查找和调度工具。
- `run_agent(task, model=..., registry=..., max_steps=...)` 用循环驱动模型多轮执行，双出口：`status="completed"` / `"max_steps"`。
- 模型协议是结构化的：`generate(messages) -> ModelReply`（`kind="final"` 或 `"tool_calls"`），没有任何字符串暗号；一轮可携带多个 `ToolCall`。
- 循环维护 `{"role": "user"/"assistant"/"tool", "content": ...}` 消息历史并直接传给模型；工具结果带 `tool_call_id` 回灌。
- `RunResult` 带 `status` 字段：可预期的结局用状态表达，不靠异常。
- `RealModel` 通过 HTTP 调用 DeepSeek（OpenAI 兼容格式）：密钥走环境变量，网络异常指数退避重试；原生 `tool_calls` 通道——`tool_to_schema` 自动生成 JSON Schema，解析 `finish_reason`，方言 ↔ wire 双向翻译。
- 离线模型：`FakeModel` 固定回复；`ScriptedModel` 按剧本回 `ModelReply` 并记录每轮收到的消息快照。

## 目录

- `src/harness/main.py`：Harness 入口与运行结果。
- `src/harness/models.py`：模型协议、`ModelReply` / `ToolCall` 与离线模型（`FakeModel` / `ScriptedModel`）。
- `src/harness/agent.py`：Agent Loop（结构化双出口、工具回灌、消息历史）。
- `src/harness/tools.py`：工具描述、工具注册表与 `tool_to_schema`（自动生成 JSON Schema）。
- `src/harness/real_model.py`：真实模型（DeepSeek，需要 `DEEPSEEK_API_KEY` 环境变量）。
- `scripts/smoke_deepseek.py`：真实调用冒烟脚本（不进单元测试）。
- `scripts/chat.py`：交互式对话入口——和你的 Harness 真实聊天。
- `tests/test_main.py`：核心行为测试。
- `tests/test_agent.py`：Agent Loop 与工具回灌测试。
- `tests/test_tools.py`：工具系统测试。
- `NEXT_SESSION.md`：下一窗口继续学习时的上下文说明。

怎么运行（PowerShell）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.harness.main          # 最小运行
.\.venv\Scripts\python.exe -X utf8 -m src.harness.agent         # 离线 Agent 演示
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v   # 18 条测试
setx DEEPSEEK_API_KEY "sk-..."   # 一次性永久设置（写注册表，新开窗口生效）
.\.venv\Scripts\python.exe -X utf8 -m scripts.chat              # 交互式对话（产品入口）
.\.venv\Scripts\python.exe -X utf8 -m scripts.smoke_deepseek    # 冒烟脚本
```
