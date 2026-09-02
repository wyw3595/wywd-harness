# wywd-harness

从零实现一个生产级 Python LLM / Agent Harness 的学习项目。
内核自建：模型协议、Agent Loop、工具系统、真实模型接入、沙箱安全、会话记忆、轨迹可视化、成本可观测。

## 当前能力

- **模型协议**：结构化 `Model` 协议 —— `generate(messages) -> ModelReply`（`kind="final" | "tool_calls"`），没有任何字符串暗号；一轮可携带多个 `ToolCall`。
- **离线双模型**：`FakeModel` 固定回复；`ScriptedModel` 按剧本回 `ModelReply` 并记录每轮收到的消息快照。
- **Agent Loop**：`run_agent(task, model, registry, max_steps, history, on_event)` —— 有界循环、工具结果/错误按 `tool_call_id` 回灌、多工具互不拖累、跨问题会话记忆（Harness 无状态，记忆归应用层）。
- **状态契约**：`RunResult.status` 是可预期的结局 —— `completed` / `max_steps` / `failed`（失败是一等公民，不靠异常）。
- **工具系统**：`ToolRegistry` 注册/查找/调度；`tool_to_schema` 用 `inspect` 反射自动生成 JSON Schema；docstring 参数描述自动入 schema。
- **真实模型**：`RealModel` 走 OpenAI 兼容 HTTP 调 DeepSeek —— 密钥环境变量、连接池复用、指数退避重试、403/401 类永久错误快速失败、wire 解析哨兵（畸形响应一律 ValueError）。
- **沙箱文件工具**：只读 + 沙箱根 + 大小限额 + 禁区（`.env`/`.git`），违规走错误回灌不崩溃。
- **可观测三件套**：事件钩子（`round_start`/`model_reply`/`tool_start`/`tool_end` 实时直播）、HTML 轨迹页（防 XSS）、token 用量记账。
- **成本刹车**：`trim_history` 历史窗口截断，且不拆散 assistant(tool_calls)/tool 的配对。

## 目录

```
src/harness/
  main.py          一次最小运行与 RunResult（状态契约的唯一出处）
  models.py        模型协议、ModelReply/ToolCall、离线模型
  agent.py         Agent Loop（循环、回灌、历史、事件钩子、跨轮记账）
  tools.py         工具描述、注册表、tool_to_schema、docstring 解析
  real_model.py    真实模型（DeepSeek，OpenAI 兼容 wire 翻译）
  file_tools.py    沙箱文件工具（只读 + 沙箱 + 限额 + 禁区）
  memory.py        trim_history 历史窗口截断
  trace.py         轨迹可视化（纯函数渲染 + XSS 转义）
scripts/
  chat.py          交互式终端入口
  chainlit_app.py  网页聊天入口（Chainlit）
  toolbox.py       共享工具箱（终端/网页两个入口共用同一套工具与模型配置）
  demo_trace.py    离线演示：跑一次工具往返 + 生成轨迹页
  smoke_deepseek.py 真实调用冒烟脚本（不进单元测试，需密钥）
tests/             46 条离线单元测试（unittest，毫秒级，不碰网络）
pyproject.toml     依赖与打包声明（src 布局）
```

## 快速开始

前置：Python 3.11+ 虚拟环境，`pip install requests`（网页入口另需 chainlit）。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v   # 46 条测试
.\.venv\Scripts\python.exe -X utf8 -m src.harness.agent              # 离线 Agent 演示
.\.venv\Scripts\python.exe -X utf8 -m scripts.demo_trace             # 轨迹页演示
```

**真实对话**（需密钥，PowerShell 一次性设置）：

```powershell
setx DEEPSEEK_API_KEY "sk-..."          # 永久设置（写注册表，新开窗口生效）
.\.venv\Scripts\python.exe -X utf8 -m scripts.chat                   # 终端聊天
.\.venv\Scripts\chainlit.exe run scripts\chainlit_app.py             # 网页聊天
```

环境变量可覆盖（进程级，任选其一）：

```powershell
$env:WYWD_API_BASE = "https://网关地址/v1/chat/completions"   # 换 OpenAI 兼容网关
$env:WYWD_MODEL    = "moonshot-v1-8k"                        # 换模型档位
```

## 测试

`tests/` 下共 7 个文件、46 条断言，全部离线、不碰网络、毫秒级。
CI（GitHub Actions）在每个 push/PR 跑全量测试，版本矩阵 3.11 ~ 3.13。

```
unittest discover -s tests -v
```

## 设计说明

一句话：**协议与实现分离**。`Model` 协议让离线 fake 与真实 DeepSeek 共用同一份循环代码；
`RunResult.status` 让失败/截断/超步都是可预期的结果；`tool_call_id` 回灌让工具结果与调用严格配对；
消息历史是纯逻辑方言，wire 翻译只发生在 `real_model.py` 一个文件里（摘掉 provider 就能换）。

学习轨迹与规划见 `NEXT_SESSION.md`；面试题积累见 `interview-questions.md`。