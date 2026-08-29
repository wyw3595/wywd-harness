# 给下一窗口助手的说明

你好，下一窗口的助手。请先阅读这个文件和项目现有代码，再继续带我学习。

## 我的目标

我正在从零学习并实现一个 Python LLM / Agent Harness。项目目录是：

`D:\React\wywd-harness`

我希望理解每一步，而不是只拿到最终代码。

## 已完成内容

### 1. 最小 Harness

文件：`src/harness/main.py`

- `RunResult` 保存一次运行的 `run_id`、`task` 和 `output`。
- `run(task, model=None)` 负责创建运行编号、调用模型并返回结果。
- 如果没有传入模型，默认使用 `FakeModel`。

### 2. 模型抽象

文件：`src/harness/models.py`

- `Model(Protocol)` 约定模型需要提供 `generate(task) -> str`。
- `FakeModel` 提供离线、可预测的模拟回复。
- `run()` 支持通过 `model=...` 注入自定义模型。

### 3. 工具系统

文件：`src/harness/tools.py`

- `Tool` 保存工具名称、说明和真正执行的函数。
- `ToolRegistry` 负责注册、查找和执行工具。
- 已处理重复工具名和未知工具名的异常。

测试文件：

- `tests/test_main.py`
- `tests/test_tools.py`
- `tests/test_agent.py`

### 4. Agent Loop 最小版本（练习 03）

文件：`src/harness/agent.py`、`src/harness/models.py`

- `FINAL_ANSWER_PREFIX = "最终回答："`：离线阶段的终止约定（真实 API 会换成结构化的 stop_reason / tool_calls 字段）。
- `run_agent(task, model=None, max_steps=5)`：while + 步骤计数器驱动模型多轮执行；两个退出条件——模型交出最终回答时正常返回 RunResult，步数用尽时抛 RuntimeError。
- `ScriptedModel`：按剧本依次回复，台词只剩最后一条时一直重复，这样"永不结束"的测试测到的才是循环的保险丝而不是模型自己的异常。
- 进度备注：我自己写了 FakeModel 兜底、计数器和循环开头，剩余部分由助手在 2026-08-27 补完；10 条测试全部通过。

面试题记录在：`interview-questions.md`。

## 我的学习方式

请严格遵守下面的节奏：

1. 先解释这一步要解决什么问题。
2. 解释涉及的 Python 语法，并给简单例子。
3. 解释为什么采用当前设计，以及它和 Harness 的关系。
4. 再修改或创建文件。
5. 运行测试并解释测试结果。
6. 每次只推进一个小模块，不要一次写完整个项目。
7. 如果我说“你来写”，可以由你直接写代码；但仍然必须解释代码目的、语法和设计原因。
8. 如果我说“让我来写”，只创建带 TODO 的练习骨架，不要直接填答案。
9. 代码里出现的每一个新 Python 语法——包括 `startswith`、`removeprefix`
   这类很小的字符串方法——都必须单独讲解：“一句话说明它是干什么的 +
   一个可运行的小例子 + 在本项目里它出现在哪一行、为什么用它”。
   不要默认我已经认识，也不要把它们淹没在设计讲解里。

不要只说“已完成”，也不要跳过解释。

## 已完成：练习 04 工具接入循环（2026-08-27，由助手写完）

- `agent.py`：新增 `TOOL_REQUEST_PREFIX = "调用工具："`；`run_agent` 增加 `registry` 参数；
  循环维护 `records` 运行记录，每轮把记录 `"\n".join` 后喂给模型（回灌）；
  工具请求分支：解析 → `_coerce_arg` 转参 → `registry.execute(名字, *args)` →
  结果或错误信息追加进 records；工具出错用 `except Exception` 兜住，循环不崩。
- `models.py`：`ScriptedModel` 新增 `received_inputs`，记录每轮实际收到的输入。
- `tests/test_agent.py`：新增 `AgentToolLoopTests`（工具成功回灌 / 未知工具错误回灌）。
- 12 条测试全部通过；`python -m src.harness.agent` 演示天气工具往返成功。

## 已完成：练习 05 多轮对话历史（2026-08-27）

- `agent.py`：`records` 升级为 `messages: list[dict]`，三种角色 user / assistant / tool；
  每轮发言先入历史再判断；`_render_messages` 用 `ROLE_LABELS` 把消息渲染成带署名文本
  喂给模型（存取分离，接真实 API 时删渲染、直传结构）。
- 验收备注：我填的渲染函数把 `m['role']` 写成了 `m['user']`（KeyError）且冒号用了半角，
  由助手验收时修复；TODO 2~5 的循环结构一次写对；我还顺带写出了生成器表达式。
- 14 条测试全部通过。

## 已完成：练习 06 状态字段（2026-08-27）

- `main.py`：RunResult 新增 `status: Literal["completed", "max_steps"] = "completed"`，
  run() 零改动。
- `agent.py`：保险丝从 raise RuntimeError 改为返回 status="max_steps" 的 RunResult，
  output 用 last_reply 记录最后一轮发言。
- 验收备注：TODO 1~3 一次写对；两条测试 TODO 没填（"故意的红灯"亮了但没接），
  由助手验收时补齐；保险丝 return 的缩进顺带修成常规风格。
- 14 条测试全部通过。新增面试题：异常 vs 状态字段（interview-questions.md）。

## 已完成：练习 07 接入真实模型（2026-08-27）——离线主线全部毕业

- `src/harness/real_model.py`：RealModel 用 requests 调 DeepSeek /chat/completions
  （OpenAI 兼容格式）。密钥三段式：参数 → 环境变量 → 大声报错；
  SYSTEM_PROMPT 教真模型字符串约定；网络异常指数退避重试（2、4 秒），
  最终 raise RuntimeError ... from error。
- `scripts/smoke_deepseek.py`：冒烟脚本（单发 generate + 完整 run_agent）。
- 冒烟实测通过：真模型按约定输出"最终回答："，run_agent 返回 status=completed。
- 依赖：requests 2.34.2。14 条离线测试保持全绿（real_model 不被任何测试导入）。
- 已知边界（留作改进）：401（key 无效）也会被重试 3 次，严格实现应先判
  status_code 立刻失败。
- 验收备注：TODO 1~3 一次写对，用了 `or` 短路求值做密钥兜底。

## 已完成：练习 08 结构化迁移（2026-08-27，由助手写完）

- `models.py`：新增 `ToolCall`（call_id / name / arguments 字典）与
  `ModelReply`（kind="final" | "tool_calls"）；`Model` 协议升级为
  `generate(messages: list[dict]) -> ModelReply`；`FakeModel` / `ScriptedModel`
  迁移到新协议，剧本改为 ModelReply 对象。
- `agent.py`：循环不再做任何字符串前缀匹配，改看 `reply.kind`；一轮支持多个
  工具调用（逐个执行、逐个带 tool_call_id 回灌）；工具参数改用
  `registry.execute(name, **arguments)` 按名传递。
  **退役**：FINAL_ANSWER_PREFIX、TOOL_REQUEST_PREFIX、_coerce_arg、
  _render_messages、ROLE_LABELS。
- `real_model.py`：适配新协议（收消息列表、返回 ModelReply）；SYSTEM_PROMPT
  删掉全部暗号；原生 tools 参数留到练习 09。
- `main.py` 的 run()、`scripts/smoke_deepseek.py` 同步适配新协议。
- 测试全量迁移并扩到 15 条（新增"一轮多个工具调用"）。
- 迁移中抓到本课主题的活教材 bug：ScriptedModel 曾把消息列表按引用存进
  received_inputs，循环继续追加导致所有"快照"都变成最终状态——
  已改为 list(messages) 浅拷贝快照。
- 15 条测试全部通过；DeepSeek 真实冒烟通过。

## 已完成：练习 09 真模型原生 tool_calls（2026-08-27）——全项目毕业

- `tools.py`：`tool_to_schema` 用 inspect 反射 handler 签名自动生成 JSON Schema
  （TYPE_MAP 做类型映射；inspect.Parameter.empty 哨兵判断必填参数）。
- `real_model.py`：构造器收 tools；`_to_wire_messages` 把方言历史翻译成 wire 格式
  （assistant 工具调用消息 content=None；arguments 用 json.dumps 变 JSON 字符串）；
  解析 finish_reason=="tool_calls" + message.tool_calls，json.loads 参数，
  返回结构化 ModelReply。
- `scripts/smoke_deepseek.py`：真模型 + 真工具；工具故意返回"紫色雪花"数据，
  最终回答里出现它 = 工具往返实锤（真伪判别法）。
- 验收：18 条测试全绿；agent.py 一行未改；冒烟回答含紫色雪花、status=completed。

## 毕业时你从零建成的完整链条

run() → Model 协议（generate(messages) -> ModelReply）→ ToolRegistry + JSON Schema
→ Agent Loop（双出口、多工具调用、tool_call_id 回灌、消息历史）
→ RealModel（HTTP、密钥环境变量、超时重试、原生 tool_calls）。
15 条离线测试（毫秒级）+ 1 个真实冒烟脚本。

## 还可以继续的方向（选修）

- real_model 已知边界：401（key 无效）不应重试，应判 status_code 立刻失败；
- 消息结构 TypedDict / dataclass 化，替代裸 dict；
- status 扩展出 failed；流式输出与日志；
- 实战：给 run_agent 配上你自己的工具（查文件、调接口、操作数据库）。

## 已完成：练习 10 跨问题会话记忆（2026-08-27）

- 设计：Harness 无状态，应用层管会话。`run_agent` 收 `history` 参数
  （`list(history)` 防御性复制起步），两个出口都把完整对话放进
  `RunResult.messages` 返回；`chat.py` 用 `history = result.messages` 延续记忆，
  `/clear` 手动清空（成本刹车：历史全量重发按 token 计费）。
- 验收备注：我的 history 分支漏了冷启动 else——history 为 None 时 messages
  未赋值，10 条测试当场 UnboundLocalError（"不想写测试"的风险现场）；
  由助手补 else，并写 3 条测试（两问延续、max_steps 带历史、
  history 防御性复制回归测试）。
- 21 条测试全绿；真实对话实测："那上海呢？"回答"上海今天**也是**下紫色雪花"
  ——一个"也是"字就是记忆生效的铁证。

## 已完成：练习 11 git init（2026-08-29）

- 仓库已初始化，`.gitignore` 挡住 .venv / .idea / __pycache__ / .env；
- 首次提交 `a94607f`：18 个文件、1466 行，即前 10 节课的全部成果。
- **从现在起的工作节奏**：改代码 → 跑测试 → `git add -A && git commit -m "..."` 小步提交。
  每完成一节练习都应该有一次提交，提交信息写清"做了什么"。

## 进行中：练习 12 实战文件工具 + 沙箱安全（骨架已建，等我填代码）

设计：能力 = 风险，四道闸门——只读 / ALLOWED_ROOT 沙箱（is_relative_to 检查）/
max_chars 限额 / 违规走错误回灌不崩溃。pathlib 是本课新语法（/ 拼路径、resolve、
iterdir、stat）。

- `src/harness/file_tools.py`（新建）：TODO 1 `_resolve_safe` 沙箱检查；
  TODO 2 `list_dir`；TODO 3 `read_file`（截断 + 1MB 防爆）。
- `scripts/chat.py`：TODO 4 注册 list_dir / read_file 并下发 schema。
- `tests/test_file_tools.py`（新建）：TODO 5~8（放行 / 越界 / 列目录 / 读+截断）。

当前测试状态：21 条通过，新 4 条报 NotImplementedError。

验收标准：25 条全绿 + chat 里问"src/harness 目录里有什么？"
"读一下 README.md 用一句话总结"能得到基于真实文件内容的回答。

## 验证命令

在 PowerShell 中运行：

```powershell
.\\.venv\\Scripts\\python.exe -X utf8 -m src.harness.main
.\\.venv\\Scripts\\python.exe -X utf8 -m unittest discover -s tests -v
$env:DEEPSEEK_API_KEY = "sk-..."   # 冒烟前设置
.\\.venv\\Scripts\\python.exe -X utf8 -m scripts.smoke_deepseek
```

当前项目使用 Python 3.13.9 和 `.venv` 虚拟环境（requests 2.34.2）。
