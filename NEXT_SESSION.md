# 给下一窗口助手的说明

你好，下一窗口的助手。请先阅读这个文件和项目现有代码，再继续带我学习。

## 我的目标

我正在从零学习并实现一个 Python LLM / Agent Harness。项目目录是：

`C:\wyw\wywd-harness`（2026-09-04 从 D:\React\wywd-harness 搬来，
.venv 已用 uv 重建：`uv venv --python 3.13` + `uv pip install requests chainlit`）

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

- `FINAL_ANSWER_PREFIX = "最终回答："`：离线阶段的终止约定（真实 API 会换成结构化的 stop\_reason / tool\_calls 字段）。

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
  由助手验收时修复；TODO 2\~5 的循环结构一次写对；我还顺带写出了生成器表达式。

- 14 条测试全部通过。

## 已完成：练习 06 状态字段（2026-08-27）

- `main.py`：RunResult 新增 `status: Literal["completed", "max_steps"] = "completed"`，
  run() 零改动。

- `agent.py`：保险丝从 raise RuntimeError 改为返回 status="max\_steps" 的 RunResult，
  output 用 last\_reply 记录最后一轮发言。

- 验收备注：TODO 1\~3 一次写对；两条测试 TODO 没填（"故意的红灯"亮了但没接），
  由助手验收时补齐；保险丝 return 的缩进顺带修成常规风格。

- 14 条测试全部通过。新增面试题：异常 vs 状态字段（interview-questions.md）。

## 已完成：练习 07 接入真实模型（2026-08-27）——离线主线全部毕业

- `src/harness/real_model.py`：RealModel 用 requests 调 DeepSeek /chat/completions
  （OpenAI 兼容格式）。密钥三段式：参数 → 环境变量 → 大声报错；
  SYSTEM\_PROMPT 教真模型字符串约定；网络异常指数退避重试（2、4 秒），
  最终 raise RuntimeError ... from error。

- `scripts/smoke_deepseek.py`：冒烟脚本（单发 generate + 完整 run\_agent）。

- 冒烟实测通过：真模型按约定输出"最终回答："，run\_agent 返回 status=completed。

- 依赖：requests 2.34.2。14 条离线测试保持全绿（real\_model 不被任何测试导入）。

- 已知边界（留作改进）：401（key 无效）也会被重试 3 次，严格实现应先判
  status\_code 立刻失败。

- 验收备注：TODO 1\~3 一次写对，用了 `or` 短路求值做密钥兜底。

## 已完成：练习 08 结构化迁移（2026-08-27，由助手写完）

- `models.py`：新增 `ToolCall`（call\_id / name / arguments 字典）与
  `ModelReply`（kind="final" | "tool\_calls"）；`Model` 协议升级为
  `generate(messages: list[dict]) -> ModelReply`；`FakeModel` / `ScriptedModel`
  迁移到新协议，剧本改为 ModelReply 对象。

- `agent.py`：循环不再做任何字符串前缀匹配，改看 `reply.kind`；一轮支持多个
  工具调用（逐个执行、逐个带 tool\_call\_id 回灌）；工具参数改用
  `registry.execute(name, **arguments)` 按名传递。
  **退役**：FINAL\_ANSWER\_PREFIX、TOOL\_REQUEST\_PREFIX、\_coerce\_arg、
  \_render\_messages、ROLE\_LABELS。

- `real_model.py`：适配新协议（收消息列表、返回 ModelReply）；SYSTEM\_PROMPT
  删掉全部暗号；原生 tools 参数留到练习 09。

- `main.py` 的 run()、`scripts/smoke_deepseek.py` 同步适配新协议。

- 测试全量迁移并扩到 15 条（新增"一轮多个工具调用"）。

- 迁移中抓到本课主题的活教材 bug：ScriptedModel 曾把消息列表按引用存进
  received\_inputs，循环继续追加导致所有"快照"都变成最终状态——
  已改为 list(messages) 浅拷贝快照。

- 15 条测试全部通过；DeepSeek 真实冒烟通过。

## 已完成：练习 09 真模型原生 tool\_calls（2026-08-27）——全项目毕业

- `tools.py`：`tool_to_schema` 用 inspect 反射 handler 签名自动生成 JSON Schema
  （TYPE\_MAP 做类型映射；inspect.Parameter.empty 哨兵判断必填参数）。

- `real_model.py`：构造器收 tools；`_to_wire_messages` 把方言历史翻译成 wire 格式
  （assistant 工具调用消息 content=None；arguments 用 json.dumps 变 JSON 字符串）；
  解析 finish\_reason=="tool\_calls" + message.tool\_calls，json.loads 参数，
  返回结构化 ModelReply。

- `scripts/smoke_deepseek.py`：真模型 + 真工具；工具故意返回"紫色雪花"数据，
  最终回答里出现它 = 工具往返实锤（真伪判别法）。

- 验收：18 条测试全绿；agent.py 一行未改；冒烟回答含紫色雪花、status=completed。

## 毕业时你从零建成的完整链条

run() → Model 协议（generate(messages) -> ModelReply）→ ToolRegistry + JSON Schema
→ Agent Loop（双出口、多工具调用、tool\_call\_id 回灌、消息历史）
→ RealModel（HTTP、密钥环境变量、超时重试、原生 tool\_calls）。
15 条离线测试（毫秒级）+ 1 个真实冒烟脚本。

## 还可以继续的方向（选修）

- real\_model 已知边界：401（key 无效）不应重试，应判 status\_code 立刻失败；

- 消息结构 TypedDict / dataclass 化，替代裸 dict；

- status 扩展出 failed；流式输出与日志；

- 实战：给 run\_agent 配上你自己的工具（查文件、调接口、操作数据库）。

## 已完成：练习 10 跨问题会话记忆（2026-08-27）

- 设计：Harness 无状态，应用层管会话。`run_agent` 收 `history` 参数
  （`list(history)` 防御性复制起步），两个出口都把完整对话放进
  `RunResult.messages` 返回；`chat.py` 用 `history = result.messages` 延续记忆，
  `/clear` 手动清空（成本刹车：历史全量重发按 token 计费）。

- 验收备注：我的 history 分支漏了冷启动 else——history 为 None 时 messages
  未赋值，10 条测试当场 UnboundLocalError（"不想写测试"的风险现场）；
  由助手补 else，并写 3 条测试（两问延续、max\_steps 带历史、
  history 防御性复制回归测试）。

- 21 条测试全绿；真实对话实测："那上海呢？"回答"上海今天**也是**下紫色雪花"
  ——一个"也是"字就是记忆生效的铁证。

## 已完成：练习 11 git init（2026-08-29）

- 仓库已初始化，`.gitignore` 挡住 .venv / .idea / __pycache__ / .env；

- 首次提交 `a94607f`：18 个文件、1466 行，即前 10 节课的全部成果。

- **工作节奏（2026-09-05 修订）**：改代码 → 跑测试 → 停下，等用户自己提交。
  **助手绝不主动执行 git add / git commit**——提交权和提交信息都归用户，
  哪怕一节练习做完了也不提交，最多提醒一句"可以提交了"。

## 进行中：练习 12 实战文件工具 + 沙箱安全（骨架已建，等我填代码）

设计：能力 = 风险，四道闸门——只读 / ALLOWED\_ROOT 沙箱（is\_relative\_to 检查）/
max\_chars 限额 / 违规走错误回灌不崩溃。pathlib 是本课新语法（/ 拼路径、resolve、
iterdir、stat）。

- `src/harness/file_tools.py`（新建）：TODO 1 `_resolve_safe` 沙箱检查；
  TODO 2 `list_dir`；TODO 3 `read_file`（截断 + 1MB 防爆）。

- `scripts/chat.py`：TODO 4 注册 list\_dir / read\_file 并下发 schema。

- `tests/test_file_tools.py`（新建）：TODO 5\~8（放行 / 越界 / 列目录 / 读+截断）。

当前测试状态：21 条通过，新 4 条报 NotImplementedError。

验收标准：25 条全绿 + chat 里问"src/harness 目录里有什么？"
"读一下 README.md 用一句话总结"能得到基于真实文件内容的回答。

## 已完成：练习 12 实战文件工具 + 沙箱安全（2026-08-29，测试由助手写）

- `src/harness/file_tools.py`：ALLOWED\_ROOT 沙箱（`_resolve_safe` 用 resolve +
  is\_relative\_to 检查）、list\_dir、read\_file（1MB 防爆 + utf-8 + max\_chars 截断）。
  四道闸门：只读 / 沙箱 / 限额 / 违规走错误回灌不崩溃。

- `scripts/chat.py`：注册 list\_dir / read\_file，四个工具 schema 全量下发。

- `tests/test_file_tools.py`：放行 / 越界 / 列目录 / 读+截断，全离线。

- 验收：25 条测试全绿；离线攻击演练（list\_dir('D:/React')）沙箱拦截成功；
  真实 chat 实测：模型真实调用工具列出 src/harness 的真实文件和字节数、
  基于 README 真实内容给出总结。

- 提交：6ffff8f。

## 进行中：练习 13 手写轨迹可视化页（骨架已建，等我填代码）

方向调研结论：业界两条路——Chainlit（Agent 聊天界面首选，工具步骤原生展示）
和 Langfuse（轨迹树平台，需自托管数据库）。我们选择先手写迷你轨迹页：
零依赖、复用 RunResult.messages，做完再上 Chainlit。

- `src/harness/trace.py`（新建）：TODO 1 `_escape`（html.escape）；
  TODO 2 `_render_message` 按 role 分派（四种形态 + 未知 role 灰卡）；
  TODO 3 `render_trace` 组装整页（PAGE\_TEMPLATE.format，CSS 大括号双写）；
  TODO 4 `save_trace`（时间戳文件名）；TODO 5 `show_trace`（as\_uri + webbrowser）。

- `scripts/demo_trace.py`：TODO 6 一行调用 show\_trace。

- `tests/test_trace.py`：TODO 7 四种 role 全渲染（JSON 期望值用 html.escape
  动态构造）；TODO 8 XSS 灵魂测试（\<script> 必须出现、原始 \<script> 绝不出现）。

当前测试状态：25 条通过，新 2 条报 NotImplementedError。

验收标准：27 条全绿 + demo\_trace 自动打开浏览器，
轨迹页含用户气泡 / 工具调用卡片（缩进 JSON 参数）/ 结果卡片 / 最终回答。

## 已完成：练习 13 手写轨迹可视化页（2026-08-29，由助手写完）

- `src/harness/trace.py`：render\_trace 纯函数（按 role 分派：user/assistant 气泡、
  tool\_calls 卡片带 indent=2 的 JSON 参数、tool 结果卡片、未知 role 灰卡）；
  所有文本过 html.escape 防 XSS；save\_trace（时间戳文件名 + resolve 成绝对路径，
  as\_uri 只认绝对路径）；show\_trace 用 webbrowser 打开。

- 转义铁律：LLM 输出是不可信输入，进 HTML 前必须转义；危险的是"真实标签"，
  不是长得像危险的词（onerror 转义后作为文本出现是安全的）。

- `scripts/demo_trace.py`：离线演示，生成 trace-时间戳.html 并自动打开。

- `tests/test_trace.py`：四 role 渲染 + XSS 双向断言（转义形态必须在、
  原始标签绝不在）。trace-\*.html 已加入 .gitignore。

- 27 条测试全绿。提交：3a337fa。

## 当前下一步

练习 17 候选（见下方「已完成：练习 16」之后的优先级清单），
从 wire 解析防御开始。

## 已完成：练习 14 Chainlit 网页聊天（2026-08-29，由助手写完）

- `scripts/toolbox.py`：共享工具箱（4 个工具 + build\_registry/build\_model），
  终端和网页两个入口共用，新增工具只改一处。

- `scripts/chainlit_app.py`：@cl.on\_chat\_start（建模型/注册表/清记忆）+
  @cl.on\_message（cl.make\_async 跑同步 run\_agent；回放式 Step 展示本轮新增的
  工具调用与结果，切片 result.messages\[prior\_len+1:] 防止旧步骤重复显示）。

- 关键坑：chainlit 用 spec\_from\_file\_location 加载应用文件，不会把项目根放进
  sys.path——文件开头自行 bootstrap PROJECT\_ROOT，否则 src.*/scripts.* 导入全挂。

- 验收：27 条测试全绿；chainlit 起服务器（HTTP 200）；浏览器实测真实对话——
  网页上出现"已使用 get\_weather"步骤卡片 + DeepSeek 真实 call\_id
  （call\_00\_ffdjI3AhgYvKBLsFET005618）+ 紫色雪花回答。

- 运行：`.\.venv\Scripts\chainlit.exe run scripts\chainlit_app.py`。

- 提交：5141847。

## 已完成：练习 15 事件钩子 on\_event（2026-08-29，实时直播）

- `agent.py`：run\_agent 新增 `on_event: Callable[[str, dict], None] | None`，
  内部 `emit(event, **data)` 在四个节点广播：round\_start（step）/ model\_reply
  （kind/text/tool\_names）/ tool\_start（call\_id/name/arguments）/
  tool\_end（call\_id/name/content）。不传钩子则零行为变化。

- `scripts/chat.py`：终端实时播报（⚙️ 轮次 / 🤖 模型动作 / 🔧 工具执行与返回）。

- `scripts/chainlit_app.py`：回放式步骤升级为直播——on\_event 回调发生在
  run\_agent 的工作线程，用 `asyncio.run_coroutine_threadsafe(coro, loop)`
  把 Step 卡片架桥回主事件循环实时创建。

- `tests/test_agent.py`：事件序列与载荷断言 + "无钩子时行为不变"回归。

- 验收：29 条测试全绿；终端管道实测逐行播报；浏览器实测
  "⚙️ 调用 get\_weather" / "✅ 结果" 直播卡片 + 真实回答。

- 提交：0f3fbe8。

## 已完成：练习 16 让失败成为一等公民（2026-08-29，代码由我填，助手收尾）

- A 组失败语义：RunResult.status 的 Literal 增加 "failed"；
  run\_agent 把 model.generate 包进 try/except Exception——失败落地成
  status="failed" 的 RunResult（output 写人话、messages 照常返回）；
  real\_model 新增 \_is\_permanent\_error 纯函数（4xx 除 429 全算永久错误），
  在 raise\_for\_status 之前先问策略，命中立刻 raise RuntimeError——
  RuntimeError 不在 RequestException 家族，从重试网里穿出去（401 秒失败）。

- B 组沙箱保镖：file\_tools 第五道闸门 FORBIDDEN\_PARTS = {".env", ".git"}，
  \_resolve\_safe 用 relative\_to + any(parts) 查整条动线——.git/config 的
  文件名是 config，撞禁区的是路径中段的 .git；src/../.env 也会被拦
  （resolve 先归一化再检查）。模块 docstring 升级为五道闸门。

- 显示层：chat 打印 ⚠️ 分支；chainlit 用 cl.Message 发 ⚠️ 卡片。
  我的踩坑：把 chat 的 print 分支原样复制进 chainlit——print 是终端的嘴，
  网页用户什么都看不见，而旧的 send() 还在照发普通回答。
  教训：两个入口共用 RunResult，不共用显示代码。

- 测试：爆炸模型（定义在测试方法内的局部类，满足协议即插即用）验证
  failed 出口；策略打表（401/403 True，429/500/200 False）；禁区两条
  （.env 不需真实存在——检查先于读取）。

- 验收：33 条测试全绿；离线攻击演练 .env / .git/config / src/../.env /
  list\_dir(.git) 全部拦截，README / src 正常放行。

- 待办：真实冒烟——故意设错 key 跑 chat，确认 401 一秒内出 ⚠️ 且终端
  不崩；Chainlit 网页里看到 ⚠️ 卡片。

- 提交：5659d68（含补记的练习 15 笔记）

## 已完成：练习 17 wire 解析防御（2026-08-30，代码由我填，助手收尾）

- \_parse\_reply(payload) 纯函数哨兵：缺 choices/message、说用工具没给
  清单、空清单、参数坏 JSON，一律 raise ValueError（消息带原始 payload
  便于排障，低级异常 raise ... from 翻译）；generate 里裸解析退役，
  一行 return \_parse\_reply(response.json())。

- 重试循环挂第二个 except ValueError：解析失败=暂时性（代理吐 HTML、
  模型偶发坏参数），同样退避重试，耗尽消息与网络失败区分开。
  except 顺序知识点：requests 的 JSONDecodeError 同时是 ValueError 和
  RequestException 的子类，ValueError 分支放前面先匹配，坏 JSON 拿到
  "解析失败"的准确消息而不是被误报成网络失败。

- 本课最大教训（验收现场）：\_parse\_reply 一度漏了 final 分支——普通
  回答走进不了 tool\_calls 的 if，函数静默 return None（没执行到 return
  就等于 return None），真实聊天每问必挂。33 条老测试抓不到（不 import
  real\_model），TODO 4 一上岗就用 'NoneType' object has no attribute
  'kind' 钉住它。结论：没被测试看过的代码不算写完；练习 16 的失败出口
  把崩溃变成体面失败，也把 bug 藏深了——测试是首道防线。

- 测试：正常 final/tool\_calls 往返 + 四条畸形打表（缺 choices /
  没给清单 / 空清单 / 坏参数；空清单放行会让 agent 空转到 max\_steps）。

- 验收：35 条测试全绿。

- 提交：2d988c0

## 已完成：练习 18 连接复用 + 成本可观测（2026-08-30，代码由我填，助手补账单循环）

- real\_model：__init__ 建 self.\_session = requests.Session()，generate 改用
  self.\_session.post——连接池复用 TCP/TLS，省掉每轮几百毫秒握手；
  \_parse\_reply 两条 return 都带 usage=payload.get("usage") or {}。

- models：ModelReply 加 usage 字段（默认空 dict——协议加字段，旧实现
  零改动）；main：RunResult 加 usage。

- agent：totals 跨轮按键求和（totals\[k] = totals.get(k, 0) + v），三个
  出口都带 usage。验收抓出的坑：骨架期 totals 声明和出口传参都写了，
  但循环里忘了累加——账单永远是 {}，测试一上岗就抓到
  （{} != {...250...}）。结论同练习 17：链路每一环都要有测试盯着。

- chat：每问打印"（本问 tokens：输入 X / 输出 Y）"。

- 真实运行热修（助手）：DeepSeek 的 usage 混着嵌套明细字典
  （prompt\_tokens\_details 等），记账循环第一次真实运行就炸出
  TypeError（0 + {...}）——离线测试的盲区（离线模型 usage 为空，
  真实数据一进门就现形）。修在信任边界：\_parse\_reply 清洗 usage
  只留 int（协议 dict\[str, int] 的约定从此成立），回归测试锁死。
  这是"解析处就是信任边界"原则的第三次上岗（17 番外篇）。

- 可观测三件套收齐：事件（15）/ 轨迹（13）/ 成本（18）。

- 验收：37 条测试全绿；待真实冒烟：chat 连问两轮，第二轮输入 tokens
  应明显大于第一轮（历史全量重发，记忆=钱）。

- 提交：aacba84 + 热修 b4f8834

## 已完成：练习 19 历史窗口截断（2026-08-31，由助手写完）

- src/harness/memory.py（新建）：trim\_history 纯函数——不超限返回
  防御性副本 / 超限负切片取最近 N 条 / 配对安全（窗口开头是孤儿 tool
  消息就丢弃，while 先判空再取 \[0]）。负切片的坑：history\[-0:] 等价
  于整个列表（-0 就是 0），max\_messages 为 0 必须显式返回空。

- toolbox：MAX\_HISTORY\_MESSAGES = 20（取舍常量，两个入口共用）；
  chat / chainlit：run\_agent 之前 if history: history = trim\_history(...)
  ——result.messages 以截断后历史为前缀，下一问自动延续瘦身的记忆
  （记忆策略归应用层，纯函数进库）。

- tests/test\_memory.py：防御性副本 / 保留最新 / 丢孤儿 tool / 配对完整。

- 验收：41 条测试全绿；待真实冒烟：chat 连问 5+ 轮，每问输入 tokens
  应稳定在某个范围（对比练习 18 的线性上涨——刹车生效）。

- 提交：e7eb2b5

## 当前下一步

方向调整（2026-08-31，用户定：先深入学习工具系统）。调研结论
（OpenAI Agents SDK / LangChain / PydanticAI / MCP / Anthropic）：
我们自建的内核（inspect 反射生成 schema + 注册表解耦 + 错误回灌）
正是各家共同核心，差距集中在四件事——给模型的说明书（参数描述）、
错误策略可配置、依赖注入、工具协议化。

工具深潜系列（练习 20\~25）：

- 练习 20 · docstring 驱动的 schema：手写简易 Google 风格 docstring
  解析器（仿 griffe 思路），参数描述自动进 JSON Schema——OpenAI SDK
  同款机制；

- 练习 21 · 输入校验 + 错误策略三档：参数先按 schema 校验（类型不对
  回灌可行动的错误文案——Anthropic 标准），Tool 加 on\_error：
  "backflow"（现状）/ "message"（自定义文案）/ "raise"（快速失败）；

- 练习 22 · 上下文注入：仿 PydanticAI RunContext——沙箱根从模块级
  全局改为注入的 ctx，注入参数不进 schema（模型可见面 vs 程序依赖面
  分离，安全课续集）；

- 练习 23 · 审批闸门：Tool 加 needs\_approval，循环暂停等人工确认
  （chat y/n、Chainlit 按钮），拒绝回灌"用户否决了这次调用"——
  MCP 规范 "SHOULD always have human in the loop" 的落地；

- 练习 24 · MCP 客户端（大件）：stdio 连外部 MCP server，tools/list
  动态发现 + tools/call 调用 + annotations 风险提示接进审批闸门；

- 贯穿作业 · 工具设计评审（先做它，成本最低见效最快）：拿 Anthropic
  《Writing effective tools for agents》的标准过现有 4 个工具——
  命名空间前缀、描述像 onboarding 文档、返回上下文相关、错误文案
  可行动。

原排期顺延：TypedDict 消息结构、GitHub Actions CI、摘要压缩（选做）。
参考（详见会话记录）：Anthropic《Writing effective tools for agents》
/ OpenAI Agents SDK Tools / LangChain Tools / PydanticAI Dependencies
/ MCP Tools 规范。

## 2026-09-04 起：以 learn-workbuddy 章节为练习单元（主路线）

用户定稿的学习契约：**把 learn-workbuddy 的代码整合进本项目，整合过程
就是练习。一个章节 = 一个练习，按依赖顺序推进，一次一小块。**

- 教材已放在项目内：`learn-workbuddy/`（24 章，只读参考，勿改它）；

- 已完成 s01（Agent Loop）/ s02（Tool Dispatch）/ s03（Deferred Loading）：
  远端提交 adb5072，延迟机制内建在 `src/harness/tools.py`（Tool 带
  input\_schema/defer 字段；ToolRegistry 带 search/load\_by\_name/
  defer\_execute/model\_schemas/token\_report/validate\_arguments），
  配套 `src/harness/std_tools.py`（calc/find\_text/tree\_dir）；

- 下一步：~~s04\_permission\_hooks~~ **已完成，见下方专节**；

- 教训：不再以"在大文件里零散埋 TODO"的方式做练习（RunContext 那批
  tools.py 注入改造已整体回滚）；不提交半成品；机制只保留一套实现。

## 验证命令

在 PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m src.harness.main
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
$env:DEEPSEEK_API_KEY = "sk-..."   # 冒烟前设置
.\.venv\Scripts\python.exe -X utf8 -m scripts.smoke_deepseek
.\.venv\Scripts\python.exe -X utf8 scripts/web_app.py   # 自定义网页 UI → http://127.0.0.1:8765
```

当前项目使用 Python 3.13（uv 管理的 .venv，requests；chainlit 已在 s07-c
退役，网页端换成自研 web\_app）。

## 已完成：s04\_permission\_hooks 分级信任 + 审批闸门（2026-09-04，由助手写完）

- 提交：21f2f8a。

- 环境热修：项目搬家后 .venv 丢失，用 uv 重建；learn-workbuddy/ 加入
  .gitignore（第三方教材，只读参考不进仓库）。

- permissions.py（工作区里原有的 A 批决策层 + B 批执行层，本次收尾）：

  - **PermissionRule 升级为 matches + explain 双调用子**（对齐教材）：
    matches 判命中，explain 只在命中后被调、从请求里提取证据拼人话
    理由——拒绝必须有据可查（测试钉死：理由里要出现 "sudo"）。
    evaluate() 命中返回 PermissionDecision、不命中返回 None，
    decide() first-match-wins + default.deny 兜底；

  - 新增 **path.write\_ask** 规则：界内写 -> ASK。排在 outside DENY
    之后——越界写硬拒、界内写才轮到审批（顺序即安全语义）；

  - build\_default\_policy 新增 safe\_tools 参数：末尾追加
    tool.allow\_safe 白名单规则；名单外的新工具依旧 default.deny
    （fail-closed 的落点：新工具必须显式配规则才有治理路径）。

- **write\_file**（file\_tools.py，Harness 里唯一改状态的工具）：

  - 复用 \_resolve\_safe 沙箱（越界/禁区同一套闸门）+
    MAX\_WRITE\_CHARS = 20000 内容限额；父目录不自动创建
    （FileNotFoundError 走错误回灌）；整文件覆盖，不是追加；

  - 分层分工：审批闸门在权限层（write\_ask），工具本体只管
    "被放行之后怎么安全地写"；

- toolbox：SAFE\_TOOLS 免审批白名单（8 个只读/沙箱内工具，write\_file
  刻意不在内）+ build\_policy()（WorkspaceScope(ALLOWED\_ROOT)——决策层
  预判和执行层 \_resolve\_safe 认同一个沙箱根，两层不说两家话）。
  已知边界：策略看见的是桥接工具 ToolSearch/DeferExecuteTool，穿透后
  的延迟工具（tree\_dir）管不到——它只读+沙箱内，风险可接受；

- agent.py：runner 分支统一改用 result.to\_protocol\_block()——
  "Error \[xxx]:" 前缀只有这一个真源，循环不再自己拼拦截文案；

- chat.py：GovernedToolRunner 接入（build\_policy + cli\_approver y/n

  - AuditTrail），run\_agent 传 runner。只有 ASK 分支会调审批员；
    chainlit 的按钮审批是另一个入口另一套 UI（待做）；

- 已知边界：write\_file(".env") 会先 ASK（scope 只管边界不管禁区），
  批准后执行层 \_resolve\_safe 硬拦、错误回灌——多问一次但绝不放行。
  禁区前移到策略层需要 permissions 认识 FORBIDDEN\_PARTS（跨模块
  机制合并，暂不做）；

- 测试（130 条全绿）：test\_permissions +7（write\_ask 4 / safe 2 /
  文档）、test\_file\_tools +5（mock 把 ALLOWED\_ROOT 换成 tmp 目录：
  写入/覆盖/越界/禁区/限额+无半成品）、test\_governed\_runner +2
  （agent-loop 集成：BLOCKED 回灌带 permission\_blocked 前缀；放行
  正常执行）。**坑：集成测试的 handler 必须带类型注解**——无注解
  参数被反射成 string，校验层会先于权限把 int 参数拒掉（s02 约定）。

- 下一步候选（由用户定）：① s05\_electron\_shell（契约顺位，桌面
  壳，重）；② Chainlit 按钮审批（把 s04 的网页入口补齐，轻）；
  ③ 工具设计评审贯穿作业（拿 Anthropic 标准过全部 9 个工具）。

## 已完成：s04-b Chainlit 按钮审批（2026-09-05，由助手写完）

- 缺口修复（本课真正的动因）：网页入口调 run\_agent 一直没传 runner——
  完全绕过 s04 闸门，write\_file 会直接执行（只剩执行层沙箱兜底）。
  本次接上 GovernedToolRunner，网页和终端从此过同一道闸门。

- 只改 scripts/chainlit\_app.py，permissions.py / agent.py / toolbox.py
  一行未动——approver 注入铁律的红利现场：CLI input() / 网页按钮 /
  测试 lambda，三入口同一治理内核。

- 接线三件套：

  - on\_chat\_start 存 policy + audit（会话级工件，每个聊天页一份，
    互不串账）；审批员不在这造——它必须闭包住 on\_message 的事件循环；

  - ask\_approval 协程：cl.AskActionMessage 画"✅ 允许 / ⛔ 拒绝"卡片。
    chainlit 2.12 的 API 事实（读源码确认）：Action(name, payload, label)
    ——payload 必填；send() 返回 TypedDict（含 name/label），超时返回
    None。判定 bool(response and response.get("name") == "approve")：
    不回应即拒绝，fail-closed；

  - web\_approver 同步闭包：run\_coroutine\_threadsafe +
    Future.result()——与练习 15 的 emit\_step 同一座桥、相反用法
    （播报 fire-and-forget，审批必须拿回程票）；except Exception
    一律 False：桥断/等待超时都当拒绝，审批路径永远 fail-closed。

- 环境坑（排障实录）：端口 8000 被练习 14 时代（8/29）的旧 chainlit
  进程占着，新服务器绑定失败退出、旧进程照常答 HTTP 200——测的是
  旧代码。排障法：Get-NetTCPConnection -LocalPort 8000 拿 PID →
  Stop-Process，再重启。

- 验收：141 条测试全绿（未碰 harness，此层按约定不配单测）；浏览器
  实测 10/10——请求写 hello.txt → 审批卡弹出（path.write\_ask +
  理由）→ 点"⛔ 拒绝"后模型收到 permission\_blocked 回灌并向用户
  解释；重试 → 点"✅ 允许" → 模型报告创建成功；磁盘验证 hello.txt
  内容"审批通过的紫色雪花"一字不差（验证后已清理）。卡片点完后
  自动显示"Selected: ..."是 chainlit 内建反馈。

- 下一步候选（由用户定）：① s05\_electron\_shell（契约顺位，重）；
  ② s06\_sidecar\_server（教材顺位）；③ 工具设计评审贯穿作业
  （Anthropic 标准过全部工具，轻、见效快）。

## 已完成：工具设计评审 + 命名重构（2026-09-05，用户三选全做）

- 评审标准：Anthropic《Writing effective tools for agents》五原则
  ——选对工具 / 命名空间 / 返回有意义上下文 / token 效率 / 描述工程。
  全文 + 社区拆解见会话记录；10 个工具逐一过矩阵，整体偏上
  （描述带路径约定/示例/副作用、返回可行动、token 限额齐），
  真正的问题只有 3 个：

1. **add 与 calc 重叠**（Anthropic "similar tools" 陷阱——模型会
   选错）。calc 是 add 的超集 → **删除 add 工具**（函数一并删）；
2. **文件工具缺命名空间前缀**（Anthropic 建议 jira\_search 式域前缀）
   → 四个工具改名：`list_dir→fs_list`、`read_file→fs_read`、
   `write_file→fs_write`、`find_text→fs\_find`（handler 函数名不动）；
3. **get\_weather 假数据 = 主动欺骗**（原则③"返回有意义上下文"）
   → 不接真 API（教学冒烟靠"紫色雪花"当真伪判别），改为**标注模拟
   数据**：docstring + 发给模型的 description 都写明"演示用模拟数据，
   不代表真实天气"——模型知道是假的，才不会拿它当真去回答用户。

- 连带改动：

  - toolbox.py：ALL\_TOOLS 删 add、四个 name 字段改名、get\_weather
    description 标注；SAFE\_TOOLS（删 "add"、改名）、READ\_TOOLS
    {"fs\_read","fs\_list"}、WRITE\_TOOLS {"fs\_write"}；

  - permissions.py：build\_default\_policy 默认 read/write 集合同步
    fs\_ 名（教学默认跟着真实命名走），文档示例同步；

  - tests/test\_permissions.py：请求工具名连带更新（走默认策略的
    请求名 = 默认集合名，一处不跟就 default.deny）。

- **排障实录（重要教训）**：并行发 3 个 Edit 改同一个文件发生写竞态
  ——只有最后一个 write\_file→fs\_write 替换落盘，list\_dir/read\_file
  两个"成功返回"但内容被覆盖丢失，跑出 5 个 default.deny 失败。
  → 改同一文件必须串行编辑，别并行。

- 验收：143 条测试全绿。三个遗留候选不变（s05\_electron\_shell /
  s06\_sidecar\_server / 更多工具评审），由用户定。

### 追加：新增 now 时钟工具（2026-09-05）

- 动机：Agent 上下文里没有时钟——模型不知道"今天几号"，这是所有
  Agent 的共同短板（用户从候选工具里只选了 now）。

- 实现：std\_tools.now() 一行 datetime.now().strftime("%Y-%m-%d %H:%M")，
  零状态纯函数；返回人类可读文本而非时间戳（"有意义上下文"样板）；
  toolbox 注册进 ALL\_TOOLS + SAFE\_TOOLS（免审批）。

- 测试：+2（strptime 可解析且与真实时钟偏差 <1h / 格式含日期不是只几点）。

- 验收：143 条全绿；冒烟 decide("now") -> tool.allow\_safe ALLOW、
  注册表含 now、execute 返回真实时间。

## 已完成：s05 Electron 桌面壳（2026-09-05~06，Python 模拟三进程架构）

- 用 multiprocessing 模拟 Electron 的 main/renderer/preload 三进程隔离；
  纯 Python，不引入 Node。UI 不卡、崩溃不连带、受限 API 桥。

- src/harness/electron.py（可测核心，14 个单测）：
  - IPC 协议常量 INCOMING\_RENDERER / OUTGOING\_MAIN（类型集合冻结）；
  - ElectronMain：route 路由（ping/session/list/agent/message），
    \_make\_approver 回程票闭包（request\_id 对齐审批请求与回执）；
  - PreloadBridge：收信分派环（result/pong 返回，approval/request 弹
    y/n 回执，event 消费，None 抛 PreloadBridgeClosed 抬走）；
  - 机制三件套：收信分派环 / 审批回程票 / 单线程顺序嵌套（写进注释）。

- scripts/electron\_shell.py（进程编排 + 终端 UI，不配单测）：
  - 模块级 main\_process / renderer\_process / choose\_model（无 key
    FakeModel，有 key RealModel）；Windows spawn 四守则落点；
  - 事件直播：run\_agent 的 on\_event 打包成 event 消息穿 IPC 到
    renderer 打印（⚠️ 坑：事件名放 "event" 键，data 里 name 是工具名，
    用 name 存事件名会被覆盖）。

- 验收：157 条测试全绿（+14 electron）；无 key 冒烟三进程跑通 +
  事件直播；有 key 真模型工具调用直播；实验 D（杀 main 子进程）
  renderer 存活不连带——同时暴露"无重连逻辑"边界（s06 的动机）。

## 已完成：s06 Sidecar Server（2026-09-06，真多进程 + JSON-RPC）

- "主进程不跑 agent，Sidecar 来跑"：把 agent 从壳里拆到独立子进程。
  壳（main）只负责路由，JSON-RPC over socket 通信。s05 的下一层。

- src/harness/sidecar.py（可测核心，23 个单测）：
  - RingBuffer：有界环形日志（bytearray + write\_pos 绕圈 + 锁），
    满了覆盖最旧——捕获 sidecar 日志，不是数据库（AuditTrail 才是）；
  - RPCConnection：newline-delimited JSON framing（send/recv 按 \n 切帧）；
  - SidecarServer：领域路由 sidecar/*、session/*、agent/*、tool/*；
    handle\_connection 里 per-connection 装配 GovernedToolRunner
    （approver 闭包住 conn——socket 是连接才知道的，不能构造期装配）；
    agent/send 接现有 run\_agent + 权限闸门，session 存 history 回写；
    on\_event 打包成 event 通知（"event" 键防 name 冲突）；
  - MainProcessClient：call + 收信分派环（method 帧就地消费：审批弹
    y/n 回 approval/response、event 打印；无 method 且 id 匹配返回）；
    ConnectionClosed 诚实失败（对端关闭 send/recv 都兜 OSError）。

- 协议要点（教学核心，写进注释）：
  - 外层请求用 id 配对（request id=1 → response id=1）；响应帧没有
    method，只能靠 id 认领；
  - 审批/事件用 id:null 通知 + params.request\_id 回程票——为什么不用
    id？响应无 method 靠 id 认领，若审批也用响应，main 和 sidecar 两套
    id 计数在一条线上会撞号，分派环分不清；通知有 method 天然分流；
  - 死锁四违约：①分派环把审批/事件当结果；②user\_prompt 重入 call；
    ③双线程同 recv（非线程安全）；④不消费 event（event 挡住 result）。

- scripts/sidecar\_shell.py（进程编排 + 终端 UI）：
  - socketpair 一端传 mp.Process args（Windows 官方 socket reducer
    支持 pickle，源码级验证过）；父进程 start() 后 srv.close()；
  - 交互 /status /sessions /logs + 提问；
  - 收尾顺序定死：call(shutdown) → close()（EOF 才让 sidecar 退出）
    → join(timeout=5) → terminate()。

- 冒烟：无 key 全链路（ping → session/create → 提问直播 → /status
  ringBuffer/handlers → q 干净退出无残留）；**已知边界：FakeModel 只读
  第一条消息，被 system 起步抢位会"回答系统提示"（无 key 冒烟现象，
  真模型 RealModel 正确区分 system/user，不受影响）**。

- 测试：180 条全绿（+23 sidecar）。提交：4d4d0d6。

## 已完成：s06.5 三壳归一 + 填 TODO（2026-09-06，代码由我填，助手验收改测试）

- 提交：378106e。

- 主题：把"起 sidecar 进程 + 连接 + 建会话 + 收尾"的编排从各入口抽出，
  收敛成 `scripts/shell.py` 的 **SidecarShell**（s06 建的 TODO 骨架，本次填完）。
  终端 / 网页 / 桌面三个 UI 从此共用同一个壳。

- scripts/shell.py（SidecarShell，+13 条测试在 tests/test_shell.py）：

  - start()：防重复启动；socketpair 一端传子进程；失败清半壳再抛
    （terminate + join + close，不留半个壳）；

  - **spawn 找 target 的两层修复（本课核心坑）**：chainlit 用 console
    shim 启动，加载 app 后会重置 sys.path，而 spawn 的 preparation data
    是 proc.start() 那一刻快照的 sys.path——子进程"全新解释器"反序列化
    时就要 import 本模块找 target，靠代码内注入是"鸡生蛋"。修复：
    ① 模块顶层注入 sys.path + 把项目根写进环境变量 PYTHONPATH
    （子进程解释器启动时就生效）；② start() 里 spawn 前一刻再插一次
    sys.path（对付 chainlit 的重置）；③ 红利规则：任何 mp target 都
    不要定义在 chainlit_app.py，target 一律住 scripts/shell.py；

  - stop() 幂等且有序：shutdown → close()（EOF 才让 sidecar 退出）→
    join(timeout=5) → 还活着才 terminate；stop 后 session_id 清空；

  - clear() = session/destroy + session/create，返回新 sid
    （/clear 免费获得——三个 UI 一起有）；

  - user_prompt()：agent/send 包一层， sidecar 已死时 ConnectionClosed
    诚实失败（不静默装没事）。

- scripts/sidecar_shell.py 瘦身（143 行改动，净减）：编排逻辑全删，
  只剩"终端展示"——show_event 直播打印 + main 交互循环（UI 归 UI），
  多了 /clear 命令。

- scripts/chainlit_app.py 重写为第三个 UI（终端 sidecar_shell、
  桌面 electron_shell、网页本文件）：不再直跑 run_agent，网页接线
  变成"审批按钮 → shell.user_prompt；事件 → 步骤卡片"。两处新知：

  - 审批签名零适配：sidecar 跨进程只传 rule_id + reason 两个字符串
    （_make_approver 拆好的），网页审批员直接收 (rule_id, reason)，
    不用造 PermissionDecision/ToolRequest——比 s04-b 的 web_approver 干净；

  - **chainlit 生命周期坑**：F5 刷新触发 on_chat_end（停壳）但
    on_chat_start 不重跑——会话里留下死壳。解法 \_ensure_shell() 惰性
    重建（is_alive 检查）。代价：刷新 = 失忆（新壳新 sid，旧历史随
    进程销毁），可接受边界。

- tests/test_shell.py：验收改了 3 处——stop 幂等测试补 alive=False
  （子进程已正常退出的场景，断言"不 terminate"才成立）；shutdown
  协议断言改成"shutdown 被调 + closed 标志"（close 不记 calls）；
  clear 的 calls 断言去掉多余的切片包装。

- 验收：189 条测试全绿（+9）。

- 当前下一步：s07_session_management（用户已定）。

## 进行中：s07 Session Management（骨架已建，等我填代码）

设计：s06 的 session 是字典里的一行，身份和运行时混在一起（destroy 一删
全没、崩溃后 status=running 僵尸、无恢复入口）。s07 拆成两类对象——
**SessionRecord**（逻辑会话：id/cwd/mode/transcript/runtime_generation，
可跨 runtime 存活，能进 Store）和 **SessionProcess**（一代运行时：turn
锁/abort 信号/turn_runner 执行入口，只能重建不能序列化）。恢复语义：
resume 不是复活旧进程，而是用旧记录造新运行时（generation+1）。
四操作：create（新 id+第 1 代）/ close（释放运行时留记录，幂等）/
resume（旧 id+generation+1，live 拒绝）/ forget（真删除，必须先 close）。
六态状态机 _ALLOWED_TRANSITIONS 唯一真源。

架构适配（与教材的差异）：不做 per-session HTTP listener——transport
只保留一套（s06 JSON-RPC sidecar）；运行时资源 = 锁+信号+注入的
turn_runner（(message, history) -> (output, messages)，session 层不
import run_agent，离线测试给假 runner）。"端口课"由 sidecar 进程/RPC
连接充当：resume 后一样全部重建、一样不进 Record。

- src/harness/session.py（新建）：TODO 1 _ALLOWED_TRANSITIONS 迁移表；
  TODO 2 SessionRecord 可变 dataclass + summary()；TODO 3
  InMemorySessionStore（RLock + deepcopy 存取边界）；TODO 4
  _transition/_publish/_commit 三件套（RLock 同线程重入的原因）；
  TODO 5 start（creating→idle）；TODO 6 run_turn（非阻塞抢锁拒绝并发
  turn + 晚到结果 _commit 拒收）；TODO 7 close（幂等）；TODO 8 计数器
  （rpartition 摸最大编号）+create；TODO 9 resume/close/forget/
  shutdown_all。

- tests/test_session.py（新建，测试由助手写）：28 条全离线——Record
  不含运行时资源 / str-mixin Enum（== "idle"、json 直出）/ store
  deepcopy 双向防渗透 / 状态机正反迁移 / 并发 turn 拒绝不排队 /
  close 竞态晚到结果拒收（线程+Event 精确编排，不靠 sleep）/ 四操作
  语义 / 僵尸 running 关掉后复活 / Manager 换代共享 store 续号 /
  cwd+mode 校验。

当前测试状态：189 条通过；新 28 条中 27 条报错（TODO 未填），
SessionStateTests 1 条绿（Enum 是现成代码）。

验收标准：217 条全绿 + 教材 README 五问能口头回答（①会话存在≠进程
活着 ②四操作各改什么 ③端口/线程/锁/client 不能序列化复活 ④
transcript≠memory ⑤running 不是存活证明，Manager._runtimes 才是权威）。

路线调整（2026-09-06，用户定）：**s09 持久化提前，排在 s08 之前**——
刚学完 resume，紧接着让 resume 扛住进程重启（s09 = JSONL 事件日志 +
重放重建，Claude Code 同款；它正好回答"resume 的新运行时从哪拿历史"）。
s08 模型路由与这两课无依赖，顺延到 s09 之后；s10~s12 不受影响。
顺序：s07 填完（217 全绿）→ s07-b sidecar 接线 → s09 持久化 → s08。

s07-b（接线）：sidecar.py 的 sessions dict → SessionManager，
session/destroy 语义修正为 close，新增 session/resume / session/forget
路由，shell UI 加 /resume /forget。

### s07 设计调研结论（2026-09-06，用户问"这样做是最好的吗"）

逐家对照真实系统，结论：**Record/Process 分离 + resume=重建 是业界共识，
s07 是它的微缩全景模型**。对应物：

- OpenAI Agents SDK：Session 就是"按 session_id 存取的历史 + 可插拔
  后端"（SQLite/Redis/SQLAlchemy/MongoDB），比我们更极端——**根本没有
  常驻运行时**，每轮 run 都重新拉历史、全新执行；"同一 session_id +
  同一后端换实例续跑"就是我们的 Manager 换代测试；SessionSettings
  (limit) 对应 trim_history；in-memory 后端进程结束即丢（同我们的边界）。
- LangGraph：thread_id 为主键，checkpointer 逐步存档（Memory/SQLite/
  Postgres），resume=同 thread_id 新调用；跨线程长期记忆是独立的
  Store 概念——transcript≠memory 原样成立。
- Claude Code：会话=项目目录下的 JSONL transcript（~/.claude/projects/）；
  --continue 接最近一次、--resume 按 id 挑，都是"新进程重放 transcript"；
  /clear 退出但不删 transcript（close≠forget 的产品化）；Agent SDK
  暴露的适配器接口就叫 **SessionStore**（和我们的端口同名同职）。
- Cloudflare Agents/Durable Objects：actor 模型，空闲即休眠（零成本）、
  消息到达即唤醒；休眠时**内存变量全丢、storage/attachment 存活**，
  唤醒=从 storage 重建——"记录活、运行时重建"的工业版；单实例写
  同一 session 由平台保证（我们的 resume 拒绝 live 是手动版）。
- Zed ACP：协议层就有 session/new 与 session/load（恢复）两个方法，
  loadSession 是能力协商项；教材"ACP-like"出处即此。
- Google ADK：SessionService 端口 + 三后端（InMemory 教学/Database/
  VertexAi 托管），InMemory"重启即丢"写进官方文档——我们只做
  InMemory 的教学定位与 Google 同款。

我们刻意简化（=教材练习①②的生产化补丁，设计本身不用改）：
① 只有 InMemory 后端（业界同款端口形态：Claude SessionStore adapter /
LangGraph checkpointer 后端 / ADK SessionService）；② 无 startup
reconciliation（僵尸 running 启动时清理——我们靠 close 第三步手动抹）；
③ 按 turn 提交 transcript，而 Claude Code/LangGraph 逐消息追加/存档，
崩溃丢最后一轮（教学规模可接受）。generation 的学术对应：分布式系统
的 epoch / fencing token（防僵尸执行器写旧状态）。

## 已完成：s07 填完（2026-09-07，代码由我填，助手验收）

- 提交：2be871a。会话生命周期全部 TODO 落地（Record/Process 分离、
  状态机、四操作、InMemory store），217 条测试全绿。

## 进行中：s10 工作区记忆（骨架已建，等我填代码）

设计：**transcript 负责忠实记录（s09），memory 负责有损选择（s10）**。
三个文件三种身份：daily/*.jsonl 只追加的证据；curated.json 机器真相
（tempfile + os.replace 原子替换——本课新机制）；MEMORY.md 派生视图
（随时从 canonical 重建）。蒸馏门槛刻意不让 LLM 决定：年龄 ≥30 天
AND 类型 ∈ {decision/convention/pitfall} AND（重要度 ≥4 OR 重复 ≥2）。

三个架构判断：

1. **keyed supersession（memory_key 冲突域 + 修订链）砍掉留作 s10-b
  候选**——教材近半代码在那里；本课走"内容寻键"路径（kind+规范化
  内容 = 稳定 key，同内容新证据只合并，无覆盖语义）。
2. **写路径是延迟工具**：memory_write 走 ToolSearch 发现（s03 复用），
   进 SAFE_TOOLS（只追加原始日志，晋升由蒸馏闸门管——"模型说重要就
   永久保存"是提示注入污染记忆的正门）。读路径 = 会话起步 history_seed
   注入有界视图（第二条 system；空记忆时 seed 与旧版完全一致）。
3. **/memory 命令本地直读**（不走 RPC）：记忆就是文件，任何进程都能
   读/蒸馏——本身是教学点。

- `src/harness/workspace_memory.py`（新建）：TODO 1 __init__（resolve +
  workspace_id=sha256 路径指纹前 16 位 + 布局）；TODO 2 append_daily_log
  （校验 + asdict + 一行追加 + fsync）；TODO 3 _read_log/read_all_facts
  （partial tail + scope 校验——s09 第三次上岗）；TODO 4
  _atomic_write_text（mkstemp 同目录 + fsync + os.replace + unlink
  missing_ok）；TODO 5 _render_memory（三节视图）；TODO 6 distill（年龄/
  类型/重要度重复三闸门 + processed_ids 幂等 + 证据合并）；TODO 7
  get_context_for_agent（MEMORY.md + 最近 6 条，预算截断）。
- `scripts/toolbox.py`：TODO 8a write_memory_fact（root 可注入）；TODO 8b
  build_history_seed（空记忆单 system，有记忆双 system）。memory_write
  延迟工具注册 + SAFE_TOOLS 已直接给。
- `scripts/shell.py`：TODO 9 history_seed 换 build_history_seed()（一行）。
- `scripts/sidecar_shell.py`：TODO 10 /memory [distill]（本地读 + 蒸馏）。

- 测试（助手写，16 条全离线 tmp；时间用注入 recorded_at/as_of 控制）：
  scope 隔离/串线炸；追加四重校验；读写往返；partial tail 双向；年龄/
  类型/重要度重复三闸门各一档；幂等（二次跑零新建）；规范化内容合并
  （大小写+空格）；双文件原子落盘无 .tmp 残骸；有界注入截断；重启
  恢复；write_memory_fact 落日志；seed 双形态；工具延迟+免审批注册。

当前测试状态：**301 条中 15 红（全钉 TODO），286 绿**。基线会随功能增长，
别再按 275 对——s07-c 用 test\_web\_app 换掉了旧的侧边栏测试，SSE 又补了
一批，一律以实测为准（上面这条 301 是 2026-09-10 实测）。

15 条红与 TODO 的对应（按依赖顺序填：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8a/8b）：

| 分组 | 条数 | 对应 TODO |
| --- | --- | --- |
| ScopeTests | 2 | TODO 1（\_\_init\_\_ + workspace\_id 路径指纹） |
| AppendTests | 3 | TODO 2（append\_daily\_log）、TODO 3（\_read\_log / read\_all\_facts） |
| DistillTests | 6 | TODO 6（distill 三闸门），依赖 TODO 4/5 |
| ContextTests | 2 | TODO 7（get\_context\_for\_agent 有界注入） |
| ToolboxIntegrationTests | 2 | TODO 8a（write\_memory\_fact）、8b（build\_history\_seed） |

**注意 TODO 9（shell.py 的 history\_seed 一行改动）和 TODO 10
（sidecar\_shell.py 的 /memory 命令）没有任何测试钉住**——前 8 个填完 15 红
会全绿，但 9/10 得靠冒烟验收，别以为红转绿就完事了。

验收标准：**301 全绿** + 冒烟——无 key 起终端问"记住我们用 uv 管环境"
（真模型走 memory_write 工具）→ /memory 看到原始事实 → 手工跑一次
带旧时间戳的 distill（或 API 直写旧事实 + /memory distill）→ MEMORY.md
出现该决策 → 重开会话起步历史里有记忆段。顺序：s10 填完 → s11/s12
（或先 s10-b keyed supersession，用户定）。

## 已完成：s08 模型路由（2026-09-09，代码由我填，助手验收修复+讲解）

- 三级路由上线：lite（粗筛）/ default（规划执行）/ craft（用户交互）
  槽位 + 按 tier 的成本记账。**Router 实现 Model 协议**（generate →
  craft 槽再转发）——run_agent/sidecar/turn_runner 零改动，装配只换
  build_model_router() 一处（s01 依赖注入在 s08 兑现成"无感插入"）。
  价格按输入/输出拆开（对上练习 18 usage 的两个键）；未知 agent 兜底
  DEFAULT（fail-safe：漏判代价是多花钱，与权限层 default.deny 方向
  相反）；离线模型 usage 空 → 记 0 tokens 但 calls+1（不撒谎）。

- 验收修复实录（三处）：
  ① default_agent 类属性被填 TODO 时覆盖 → generate AttributeError
    （连带 run_agent 变 failed——练习 16 的失败出口把异常吞成状态，
     教训：状态 failed 时先看 output 里的真异常）；
  ② /status 在 return 字典之前写 status["modelCost"]（变量未定义）
    + else 塞空表——空表违反 duck typing 契约（裸模型应完全不带
    modelCost 键，空表会误导 UI 以为挂了空路由器）；
  ③ summary() 用 t.name（"LITE"）不是 t.value（"lite"）——str 混血
    Enum 的 .value 才是当字符串用的那个值。
  另外 TODO 9 填了主体但占位 print 忘删（两条消息一起打）。

- 验收：259 全绿（含 run_agent 直接吃 router 的结构子类型实锤）；
  终端冒烟：聊天一轮 → /cost 出层级表（craft calls=1，tokens=0
  是 FakeModel 无 usage 的诚实零）→ /status 带 modelCost。

- 遗留：MemorySelectorRouter（零工具选择器）留给 s12（需要检索层
  供给候选）；lite/default 槽位预注册了 explore/planner/compact/
  title，等 s10/s14 接真调用方；真实分级换模型只改
  toolbox.build_model_router 的映射表。

## 下一步

s10 workspace memory（工作区记忆：日志追加、主题蒸馏、30 天保留）。
s11 user memory / s12 cloud memory 排其后。

> 补充（2026-09-10）：s07-c 自定义前端、前端缺陷修复与界面重做、事件直播
> 改 SSE 都已完成，专节在**本文档末尾**（不在这个位置）。s10 是当前唯一
> 的进行中项，骨架已建、15 条红测试钉着，等填。

## 已完成：网页侧边栏修复（2026-09-08，由助手修）

- 现象：网页侧边栏整个不出现。根因：custom_js 加载链路断在仓库外——
  链路 = .chainlit/config.toml 的 [UI] custom_js 指路 + 项目根 public/
  供文件（chainlit 2.12 源码核实：public_dir = APP_ROOT/public，**不是**
  .chainlit/public）；.chainlit/ 被 gitignore 整体忽略，config.toml 从没
  进过仓库，换棵树一跑 chainlit 就生成默认配置（无 custom_js）。
- 修复四件：① config.toml 进仓库（.gitignore 改"内容忽略 + config
  例外"）；② 侧边栏 JS 留在项目根 public/（2.12 的正确位置）；③ 面板
  后端 _collect_sessions 按 sid 去重——s09 后所有壳共享 .sessions/，
  每个壳的 session/list 都是同一份全量清单，不去重会按在线壳数重复
  N 遍；死壳错误行不炸请求；④ 前端 ＋/↻ 头部按钮接上 onOpClick
  （原只挂在 .sp-body，头部按钮被"不折叠"分支吞掉，纯摆设）。
- 验证：headless chainlit + 浏览器实测——JS 200、页面注入、面板渲染
  （sess_0001 ◀当前 / idle·live·gen）、关/活/删操作全链路生效（合成
  点击验证 DOM 事件；坐标自动化点击因 IAB 内部缩放偏移不可靠，非网页
  bug——真实浏览器光标命中不受影响）。248 条测试全绿（+5 面板测试）。
- 多 tab 已知边界（写进 sidecar_panel 模块头）：共享 .sessions/ 下，
  新 sidecar 启动清账会把别的 tab 活会话在证据里抹成 closed（对方下次
  save 写回，装饰性抖动）；跨进程 resume 可造出双运行时写同一证据；
  同时启动有计数器竞态。根治需跨进程文件锁，教学边界。

## 已完成：s09 JSONL 持久化（2026-09-08，代码由我填，助手验收修复+讲解）

- 提交待做。JsonlSessionStore（SessionStore 协议的 JSONL 落地）上线：
  每会话一个 .sessions/\<sid\>.jsonl，append-only 证据流（record 元数据
  迁移留痕 + messages_appended 增量），sequence 信封 + event_id 证据
  指针，flush+fsync 两连落盘；损坏策略：partial tail 放过并报告，
  完整坏行/跳号/前缀改写 raise TranscriptCorruptionError。
  sidecar 启动清账补上（s07 欠的账）：僵尸记录抹 closed，error 放过。

- 验收修复实录（教学现场，三处 bug + 一个结构事故）：
  ① **类头被删**：class JsonlSessionStore 一行连同 banner 误删，全部
     方法缩进进 SessionTranscript 类体——`-> SessionTranscript` 注解在
     类定义未完成时求值 → NameError → 整个模块导入失败（199 条 ≠ 243
     条的第一现场：两个测试文件加载失败）。
  ② **docstring 转义坑**：伪代码里的 `\\n`（渲染显示用）被照抄进代码，
     写进文件的是"反斜杠+n 两个字符"而不是换行符——行不分、partial
     tail 判定全歪。（骨架在 docstring 里写 \\n 是渲染需要，抄进代码
     必须是 \n——这个坑记住了。）
  ③ **异常张冠李戴**：append 的保留字防御写成 CorruptionError——
     Corruption 是"读证据发现证据坏了"，Validation 才是"写入方违反
     协议"，方向相反。
  ④ TODO 4a（create）和 TODO 8（shell 接线）漏填，由助手验收时补。

- 验收：243 条全绿；终端冒烟两段满分——第一段聊天退出，证据文件 8 行
  （creating→idle→system 播种→running→提交对话→idle，全程留痕）；
  第二段重启 sidecar：/sessions 显示旧会话（closed，启动清账抹过，
  证据第 9 行）+ 新会话续号 sess_0002，/resume 换代 gen=2。

- 顺序：s09 完成 → 下一步 s08 模型路由。s10~s12 不变。

## 已完成：s07-b Sidecar 会话接线（2026-09-08，代码由我填，助手写测试+验收）

- sessions 裸字典退役，SidecarServer 的会话控制面 = s07 的 SessionManager；
  session/destroy（一删全没）→ session/close（释放运行时、留记录、幂等）；
  新增 session/resume（旧 id + generation+1 新运行时，live 拒绝）与
  session/forget（真删除，必须先 close）；agent/send 改走 run_turn——
  并发拒绝、晚到结果拒收由状态机接管。SidecarShell 加三个薄包装，
  /clear 编舞升级 close→forget→create，终端 UI 加 /close /resume /forget。

- 两个接缝设计（实现注释里有完整版）：
  1. **TurnRunner 协议装不下 status** → turn_runner 闭包记
     `self._last_turn_status`（协议接缝记账）。安全前提：同一时刻至多
     一个 turn；跨连接并发 turn 时 status 可能串台（output 走返回值
     不受影响）——已知边界。
  2. **起步历史播两个副本**：runtime 工作副本（run_turn 用）+ store
     存档（resume 用），只写一个 = 另一条路丢 system 提示。

- 领域异常翻译约定：SessionLifecycleError 家族 / ValueError（mode 非法）/
  OSError（cwd 没了，resolve(strict=True)）→ {"error": 人话} 进 result；
  其他异常穿透给 handle_connection 兜底 → JSON-RPC error。

- 验收：225 条测试全绿（17 条红灯一次回绿，零修补）；终端冒烟剧本
  满分通过——提问 → /close（记录保留）→ send 报 "not found or closed"
  → /resume（generation 2）→ /sessions 显示 idle live=True gen=2 →
  live /forget 被拒 → close 后 forget 真删 → 清单空 → q 干净退出。

- 验收收尾：填完的 TODO 指令块已清（沿用 041bb94 惯例，设计要点收编
  进 docstring）；上一轮杂务顺带清了 s05/s06/s07 的 19 处旧 TODO 块
  （含 electron.py _make_approver 里骨架期遗留的一行 `...`）。

- 已知边界：跨 close 的记忆延续由离线测试盯着（FakeModel 只读第一条
  消息，终端展示不了这个语义）；/status 的 sessions 口径 = 记录总数
  （closed 未 forget 也计入）。

## 已完成：s07-c 自定义前端 + 事件直播改 SSE（2026-09-10）

- 提交：cb36c8b（C 方案本体，由我填）、b538b2f（前端修复 + 界面重做 +
  SSE）、b9e37ba（清 chainlit 遗留 + .gitignore）。
- 本节记两件事：s07-c 的架构决定，以及之后用户实测反馈"前端有好多 bug +
  页面不好看"引出的修复与 SSE 改造。**后端 6 个端点没动过架构，改的都是
  前端和事件传输。**

### C 方案：自定义前端彻底解耦 chainlit（cb36c8b）

- 为什么换掉：chainlit 的聊天区是它自己的 WS + React 状态，"会话切换 =
  服务端往聊天区推历史"只能靠 contextvars 快照跨线程搬运，效果不可靠
  （重放 fire-and-forget、失败全静默）。C 方案换标准做法——**会话是记录，
  聊天区是视图，切换 = 换 id + 读历史 + 自己渲染**。
- 新增 `scripts/web_app.py`（纯 stdlib，127.0.0.1:8765），三大机制：
  1. **历史 = 纯读**：GET /api/sessions/\<sid\>/messages 直接
     JsonlSessionStore.load(sid).messages（replay fold），不经过 sidecar
     RPC、不碰运行中的 turn、closed 会话秒开。零静默失败：成功 = 前端
     自己拿到数组并渲染。
  2. **直播/审批 = 事件环**（线程安全 deque + seq）+ threading.Event：
     on_event 与 user_prompt 都往同一条流里塞；审批用线程 Event 而不是
     asyncio Future——"loop 已死 / 上下文过期"这两个失败模式从根上不存在。
  3. **发消息带显式 sid**（`shell.send_to(sid, msg)`）：单壳单 sidecar 下
     多 tab 各看各的，不能依赖壳的"当前会话指针"。
- 关键架构决定：**单壳单 sidecar**。多 tab 只是同一个 backend 的多个浏览器
  视图——"每 tab 一个 sidecar 共享 .sessions/"时代的竞态（启动清账误伤、
  跨进程 resume 双运行时、计数器竞态）被架构性消灭，不是修好。
- 退役：chainlit_app / sidecar_panel / sessions_panel.js / .chainlit /
  test_sidecar_panel——桥、contextvars、reap 补丁家族一并清除。

### 前端修复 + 界面重做（b538b2f）

用户实测反馈后整体重写 `public/index.html` + `app.js`（仍零依赖 vanilla +
内联 SVG，不引任何 CDN）。修掉的 7 处缺陷病根相同——**从 DOM 反推状态**
和 **整体重建 innerHTML**：

1. 发送后输入框不清空——`send()` 里根本没有 `input.value = ""`。现在发出
   即清空，请求失败才把原文回填。
2. 已关闭会话照样能输入发送——`openSession()` 无条件 `setInputEnabled(true)`。
   现在按 live 锁定输入区，并给"复活会话"入口。
3. 状态从 DOM 徽章反推——`findRow()` 靠 `!querySelector('.badge.off')` 猜
   live，而"当前"徽章又覆盖了 live/closed，于是"当前且已关闭"被判成 live。
   现在 `state.sessions` 是唯一事实来源，live 只从数据读。
4. 5s 轮询整体重建列表——冲掉两段式删除的"确认?"、滚动位置与焦点。现在按
   sid 做 **keyed 增量更新**，只改文本与 class，不重建节点。
5. Esc 隐藏审批卡——后端干等满 300 秒且卡片永不重显（`shownApprovals` 已
   标记）。现在 Esc 不再响应，卡片带倒计时，**切换会话时自动拒绝未决审批**。
6. `esc()` 不转义引号——却被拼进 `data-sid="..."` / `title="..."`，标题含
   引号就会撑破属性。现在动态值一律走 `textContent`，HTML 只由内联模板产生。
7. 直播区只认 `tool_start`/`tool_end`，且 turn 结束即清空——现在四类事件都画
   （含 `round_start` / `model_reply`），并保留到下一轮。

另外两个自己踩出来的坑：

- `.lock` / `.pill` 这类**显式设了 `display`** 的组件会盖掉浏览器默认的
  `[hidden]{display:none}`，带 `hidden` 的元素照样显示（输入区上方曾多出
  一条空白圆角条）。补 `[hidden]{display:none !important}`。
- 切会话后侧栏高亮要等下一次 5s 轮询才画上（`openSession` 只设了
  `state.sid` 没刷选中态）。补 `paintSelection()` 立即生效。**这个是从
  验证探针里抓出来的，不是读代码看出来的。**

界面侧：真实空态（不再有游离的 "—"）、长系统提示与工具结果折进
`<details>`（之前打开会话会被一大段系统提示刷屏）、状态点取代文字徽章、
气泡/胶囊/审批弹窗/Toast 重画、emoji 图标换内联 SVG。

### 事件直播：轮询 → SSE

- `EventRing` 加订阅扇出：每个连接一个**有界** `queue.Queue`（满了丢这一条，
  慢客户端不会拖累发布方）。关键语义是**订阅者只收订阅之后的事件**——客户端
  因此不用再同步 seq 指针，天然不会重放上一轮的旧工具卡。
- 新端点 `GET /api/events/stream`：首帧 `ready` 给基线，静默期每 15 秒发一行
  `: ping` 心跳探活；断开/收工都是正常出口，`finally` 里 unsubscribe。
- **事件名放在 JSON 的 `"event"` 键，没用 SSE 的 `event:` 字段**——沿用
  sidecar 既有契约，前端一个 `onmessage` 就能分发全部类型；用 `event:` 字段
  就得在前端维护事件名注册表，后端加新事件会静默失联。
- `protocol_version` 改 HTTP/1.1（SSE 需要）。代价是**所有响应都必须给准
  Content-Length**，顺手修掉静态 404 分支缺 Content-Length 会在 HTTP/1.1 下
  把连接挂住的老问题（新增 `_send_empty`）。
- `GET /api/events` 快照端点保留供 curl 调试，并加了 `subscribers` 计数。
- 前端删掉 `pollEvents` / `lastSeq` / `pollTimer`，改常驻 `EventSource`
  （断线靠浏览器内置重连）；**审批事件不按 `posting` 门控**——安全动作任何
  tab 收到都该弹。

### 验收与边界

- 测试：**301 条**（s07-c 的 test_web_app.py 19 条 + SSE 的 11 条），15 红
  仍是 s10 骨架 TODO，非回归。SSE 部分新增 `EventRingSubscribeTests`（7）+
  `SseEndpointTests`（4，真起 ThreadingHTTPServer 走裸 socket 验响应头 /
  ready 帧 / 实时推送 / 不重放旧事件 / 断开回收）。
- 浏览器侧实证：`/api/events` 的 `subscribers` 走了 **0 → 1（真实 Chrome 的
  EventSource 连上并保持）→ 0（退出后自动回收）**；CDP 抓到的实际 DOM 里
  零 EXCEPTION、零 console error，关闭态会话 `inputDisabled=true` +
  `lockShown=true` + 胶囊"已关闭"。
- **已知副作用（下次一定撞上）**：依赖虚拟时间的无头工具
  （`--screenshot` / `--dump-dom`）在这个页面上会**无输出**——SSE 是"永远
  挂起"的请求，Chrome 的 `pauseIfNetworkFetchesPending` 策略会把虚拟时钟
  冻死。绕过办法：Chrome 加 `--remote-debugging-port`，用 Node 22 内置的
  `WebSocket` + `fetch` 直接讲 CDP（抓异常 / 断言 DOM / 截图 / `Browser.close`
  优雅关闭），脚本在 `.workbuddy/tools/cdp-probe.mjs`。
- 遗留：`.workbuddy/tmp/pc` 等 Chrome 配置目录需清理（批量删除守卫要求
  用户确认）；`smoke_tmp_clean.py` 已备份到 `.workbuddy/scratch/` 后从仓库
  根移走（它是会删 `.sessions/*.jsonl` 的一次性脚本，未跟踪、删了不可恢复）。

## 已完成：fs_find 扫描范围收窄（2026-09-11，由助手写完）

工具设计评审（`.workbuddy/research/fs-tools-review.md`）暴露的第一条硬伤：
`find_text` 的跳过名单只有 `FORBIDDEN_PARTS = {".env", ".git"}`，`os.walk`
要遍历全树 **15448 个文件 / 225.7 MB**（`.venv` 7157 + `learn-workbuddy`
8193 = 99%），而且**耗时取决于命中数量**——命中多就早退，命中少/不命中
就扫到底，反直觉且不可预期。实测最坏 42~54 秒一次工具调用。

- `src/harness/std_tools.py`：

  - 新增 **IGNORED_DIRS / IGNORED_SUFFIXES** 两张 frozenset 常量。语义与
    `FORBIDDEN_PARTS` 严格分开：后者是**安全**（必须拒，任何参数绕不过），
    前者是**效率**（看了也没用）。名单与项目根 `.gitignore` 同一份意图，
    但刻意硬编码兜底——`.gitignore` 被改坏也不会退化成全树扫描。

  - 新增两个纯函数（测试不用碰文件系统，练习 16 的 `_is_permanent_error`
    同款套路）：`_should_skip_file` 后缀判断（`Path.suffix` + `lower()`，
    防 `PHOTO.PNG` 漏网）；`_is_probably_binary` 前 4KB 探 NUL 字节。

  - `find_text` 三处改动：`dirnames[:]` 剪枝时并联 `IGNORED_DIRS`；文件级
    联查后缀；读文件从 `read_text` 改为 `read_bytes` → NUL 探测 → `decode`，
    二进制根本不进正则（旧写法会让 `errors="replace"` 把 PNG 静默变成
    一串 U+FFFD 乱码喂给模型）。

  - **导入修正**：`ALLOWED_ROOT` 从"按值 import"改为"按模块引用"
    （`file_tools.ALLOWED_ROOT`）——它是参与路径计算的可变配置，按值导入
    等于复制出第二份永不更新的副本，mock 换沙箱时只对 file_tools 生效、
    对 std_tools 失效（这类测试会静默测错东西）。`FORBIDDEN_PARTS` 是
    常量、不参与路径计算，按值导入没问题。

- `scripts/toolbox.py`：`fs_find` 的 description 补上可行动信息——写明
  自动跳过哪些目录，以及"要搜这些目录就把它作为 root 显式传入"。模型
  不知道这条就会以为内容不存在。

- **设计要点**：忽略名单只作用于"递归途中遇到的目录"，**显式指定的 root
  永远放行**——`find_text("x", root="learn-workbuddy")` 照样能搜教材。
  这是 ripgrep 的行为（*Files specified explicitly bypass most filters*），
  而且是 `os.walk(base)` 从 base **内部**开始剪枝这一结构白送的，不用
  额外写代码。

- 测试：`tests/test_std_tools.py` +10（SkipPredicateTests 6 条纯函数打表：
  后缀 / 大小写 / 多段后缀 / 无后缀 / NUL 探测 / 只探前 4KB；SearchScopeTests
  4 条临时沙箱集成：忽略目录被剪枝 / 后缀命中被跳过 / **显式 root 绕过
  忽略名单** / NUL 兜底）。**311 条测试，15 红仍是 s10 骨架 TODO，非回归。**

- 实测（同一批关键词，改前 → 改后）：

  | 关键词 | 改前 | 改后 |
  | --- | --- | --- |
  | `_parse_reply` | 44.84 s | **0.042 s** |
  | `no_such_symbol_xyzzy` | 42.06 s | **0.043 s** |
  | `subprocess.run` | 10.06 s | 0.055 s |
  | `import os` | 0.38 s | 0.051 s |

- **副产物：一次正确性修复。** `subprocess.run` 改前返回 9 条命中、改后
  未命中——那 9 条**全部来自 `learn-workbuddy/`**（第三方教材），不是本项目
  代码。旧实现把第三方库的命中当成项目代码返回给模型；现在要搜教材得
  显式 `root="learn-workbuddy"`（实测 25 ms）。

- 遗留（评审报告 P0 的其余几条，本次未动）：`fs_read` 无行号 / 无
  offset-limit（只能看到文件的 5%）、缺 `fs_edit`（≥2 万字符的文件完全
  改不动）、`fs_write` 非原子写、错误文案不可行动、`parse_docstring` 丢
  续行。`tree_dir` 还没接 `IGNORED_DIRS`（同一份名单可复用）。

- 提交：97feac7

## 已完成：fs_read 重做（行号 + offset/limit 分页）（2026-09-11，由助手写完）

评审报告 P0 第 2/3 条（读不完自己的源码），顺手带上第 7 条（二进制当文本）
与 P1 第 8 条（目录报成 PermissionError）。旧实现 `read_file(path,
max_chars=2000)` 从文件头截断、无行号、无 offset——NEXT_SESSION.md 40115
字符只能看到 2021 字符（5.0%），**文件的后 95% 在任何情况下都读不到**：
不是"要翻页"，是根本没有翻页这个动作。

- `src/harness/file_tools.py`：

  - 签名从 `(path, max_chars=2000)` 换成 **`(path, offset=1, limit=200)`**，
    对齐 Claude Code 的 `Read(file_path, offset, limit)` 与 Anthropic
    text_editor 的 `view_range`——按**行**分页，不是按字符。
  - 返回 **cat -n 格式**：右对齐行号（宽度按本页最大行号自适应）+ 制表符
    + 正文；末尾一行页码小结，三种形态——`（共 N 行，已全部显示）` /
    `（共 N 行，已显示 a-b 行；继续读用 offset=b+1）` / 空文件占位。
    **给模型的可行动信息就落在那句 "继续读用 offset=…" 上。**
  - 三道限额代替原来的单道：`DEFAULT_READ_LINES=200`（行数）、
    `MAX_LINE_CHARS=2000`（单行——压缩 JSON / minified JS 一行几十万
    字符，光限行数拦不住）、`MAX_READ_BYTES=1MB`（文件大小）。
  - `_truncate_line` 纯函数：超长行截断并**注明原长**，而不是默默丢弃
    （默默丢弃会让模型以为这一行就这么短）。
  - **目录显式拦下**：旧写法掉进 `read_text` 的 IsADirectoryError，在
    Windows 上被翻译成 `PermissionError: [Errno 13] Permission denied`
    ——模型读到"权限不足"会去猜怎么提权，方向完全错了。现在报
    `src 是目录不是文件——用 fs_list('src') 看它下面有什么`。
  - **二进制拒绝**（复用第一步的 NUL 探测）：`sse-ui.png` 以前被
    `errors="replace"` 静默解成一串 U+FFFD 乱码喂给模型，现在抛
    `看起来是二进制文件（34127 字节）……请换别的办法处理`。
  - 参数错误一律 `ValueError`（offset<1 / limit<1 / offset>文件末尾），
    和 fs_find 的坏正则走同一条回灌通道；offset 越界的消息里带合法区间。

- **结构改动**：`_is_probably_binary` 从 `std_tools.py` **迁到
  `file_tools.py`**——read 和 search 都要用它，而 std_tools 已经 import
  了 file_tools，放那边会绕成循环导入。它是"文件内容分类"，本来就属于
  文件工具这一层。对应的 3 条测试同步从 test_std_tools.py 迁到
  test_file_tools.py。

- `scripts/toolbox.py`：`fs_read` 的 description 换成"返回带行号的正文；
  大文件用 offset/limit 翻页（默认从第 1 行起、最多 200 行）"。

- 测试：**320 条，15 红仍是 s10 骨架 TODO，非回归。** 新增 ReadFileTests 5
  （首行带行号+TAB / 小文件标"已全部显示" / offset+limit 翻页与整篇读
  逐字一致 / offset 越界报合法区间 / 非正参数拒绝）、ReadFileGuardTests 5
  （目录报对类型 / 二进制拒绝 / 空文件占位 / 超长行截断不拖累邻行 /
  超大文件指向 fs_find）、BinaryProbeTests 3（迁入）；test_std_tools 里
  对应的 3 条迁出。

- 验收实测：NEXT_SESSION.md 1246 行——`offset=1&limit=200` 拿第一页、
  `offset=1200` 拿到末尾并报"已全部显示"，**覆盖率从 5.0% 变成 100%**。

- 遗留：`fs_edit` 还没做（所以 ≥2 万字符的文件仍然改不动——覆盖式
  `fs_write` 撞 `MAX_WRITE_CHARS`）；`fs_write` 仍是非原子写；错误文案
  只改了 read 这一路，`list_dir` / `write_file` 的还没跟上；
  `parse_docstring` 丢续行的 bug 仍在（本次新写的 Args 都刻意压在单行，
  绕开了它）。

- 提交：acae02f

## 已完成：fs_edit 编辑工具 + 换行符修复（2026-09-11，由助手写完）

评审报告 P0 第 4/5 条（缺 `fs_edit` → 大文件改不动）。执行过程中又发现并
修掉一个同级别的**新 bug**：换行符被写坏。

### A. fs_edit（本步主题）

- `file_tools.edit_file(path, old_text, new_text, replace_all=False)`：
  **只传改动片段**，代价与文件大小无关。对齐 Claude Code 的 `Edit`、
  Anthropic 的 `str_replace`、MCP 的 `edit_file`。
- 三条规矩（Anthropic 在 SWE-bench 工程博客里写过：试过多种编辑方案，
  字符串替换可靠性最高）：
  1. **逐字匹配**——匹配不到就报错，并提示最常见的踩坑：把 fs_read 的
     行号前缀一起复制进来了；
  2. **默认要求唯一**——出现 N 次就报出 N，让模型自己补上下文重试，
     或 `replace_all=true`。错误消息的**信息量**决定模型能不能自愈；
  3. **先读再改**——本层强制不了（工具是无状态函数，拿不到会话历史），
     靠约定与审批人把关。已知边界，写进 docstring。
- 返回值带 **片段对照 diff**（`- 旧` / `+ 新`，超 `DIFF_MAX_LINES=20`
  行截断并注明剩余）。这不只是好看：审批闸门弹 ASK 时人只看得到这份摘要，
  **没有 diff 的审批就是盲签**——s04 闸门第一次真正有了审批依据。
- `new_text` 只受 `MAX_WRITE_CHARS` 约束（"一次调用能改多少状态"），
  **不受文件大小约束**——这正是它存在的理由。

### B. 权限接线（漏一个方向就是安全洞）

- `toolbox.WRITE_TOOLS` 加进 `fs_edit`；**刻意不进 SAFE_TOOLS**。
  测试两头都钉住：漏进 WRITE_TOOLS → `default.deny`（fail-closed，表现为
  "工具坏了"）；误进 SAFE_TOOLS → 免审批直接放行（真洞）。断言
  `build_policy().decide(fs_edit)` 的 rule_id == `path.write_ask`。

### C. 换行符修复（执行中新发现的 bug，不修则 A 不可用）

- **症状一（写坏文件）**：读用 `read_bytes().decode()`（不翻译），写用
  `write_text()`（文本模式把 `\n` 翻译成 `os.linesep`，而 `\r` 原样保留）
  ——于是 `b'a\r\nb\r\n'` 写回变成 `b'a\r\r\nb\r\r\n'`，**CR 翻倍**。实测
  本仓库工作区大量是 CRLF（`file_tools.py` 340 处、`NEXT_SESSION.md` 1305
  处、`agent.py` 249 处，`git core.autocrlf=true`）——也就是说**旧的
  `fs_write` 一写 CRLF 文件就把它写坏**。
- **症状二（匹配不上）**：CRLF 文件的原样正文里是 `\r\n`，而模型的多行
  `old_text` 是用 `\n` 拼的——不归一化，`fs_edit` 对本仓库文件"永远找不到"。
- 三个新函数（都在 file_tools，读 / 写 / 编辑共用一套）：
  `_normalize_newlines(text) -> (统一到 \n 的正文, 原本的换行符)`；
  `_existing_newline(path)`（只探头 8KB）；`_write_text_bytes(path, text,
  newline)`（**字节写，绝不走文本模式**）。
- 规则：**读写对称——进去什么风格，出来什么风格**。覆盖已有文件时沿用它的
  换行风格，新建文件用 `\n`。这样"没动过的行一个字都没变"，git diff 里
  只会出现真正改动的那几行。
- 附注：本仓库工作区本来就是混的——`src` / `tests` / `scripts` 下的 .py
  文件里 **22 个 LF、24 个 CRLF**（`models.py` / `permissions.py` /
  `tools.py` / `std_tools.py` 这些一直是 LF，`file_tools.py` / `agent.py`
  是 CRLF）。`core.autocrlf=true` 下 git 层面对两者一视同仁，所以一直没
  暴露。新逻辑对两种风格都是"进去什么出来什么"，不会加剧这种混合。

### 验收（副本操作，不碰真文件）

拿 `NEXT_SESSION.md` 的副本（46051 字符 / 1305 行 / 1305 个 CRLF）：

```
fs_write 整文件重写 : ValueError: 写入内容过长（46051 字符，上限 20000），拒绝写入
fs_edit 改一行      : 21 ms，1 处替换（首处在第 299 行）
CRLF 1305 -> 1305（没翻倍），裸 CR 1305 -> 1305
变更行（0 基）：[298]  共 1 行
```

- 测试：**339 条，15 红仍是 s10 骨架 TODO，非回归。** 新增 EditFileTests 13
  条（唯一替换 / diff 供审批 / diff 截断 / 找不到的可行动提示 / 多处报次数 /
  replace_all / 空 new_text 删除 / 相同片段拒绝 / 空 old_text 拒绝 /
  new_text 超限 / 禁区 / **改得动 fs_write 拒绝的大文件** / 归一化纯函数
  打表）+ 4 条换行风格测试（CRLF 保风格不翻倍 / 多行锚点匹配 CRLF 文件 /
  新建用 LF / 覆盖沿用原风格）+ 2 条接线测试（注册且模型可见 / 走 ASK 不走
  白名单）。
- 探针：`.workbuddy/scratch/probe_fs_edit.py`（跑完自动清理副本）。

- 遗留：**非原子写还没做**（`fs_write` 与 `fs_edit` 都还是整文件覆盖落盘，
  下一步与错误文案一起统一）；错误文案只改了 read / edit 两路；
  `parse_docstring` 丢续行仍在；`tree_dir` 未接 `IGNORED_DIRS`。

- 提交：（待用户）

## 已完成：第四步 · 读总量闸 + fs_glob（2026-09-12，由助手写完）

用户定的切法：第四步 = **限额收口 + 补 glob**，原子写拆成第五步。（原计划
第四步是"原子写 + 错误文案"，拆开是因为 glob 与限额收口同属"把读这一侧
补完整"，而原子写是写那一侧的独立机制。）

### A. 第四道闸门：单次返回的字符总量

- 病灶：前三道限额各自都拦不住"行数 × 单行"的**组合**。实测
  `read_file("NEXT_SESSION.md", limit=5000)` 直接吐出 **54580 字符**；最坏
  情况 200 行 × 每行 2000 字符 = **40 万字符**，一次工具调用就把上下文
  撑爆。Claude Code 有这道闸（输出上限 30000 字符），我们漏了。
- `MAX_READ_CHARS = 30_000`：逐行累加"行号前缀 + 正文 + 换行"的真实占用，
  装不下就停在这一行。**第一行无论如何都留下**（`_truncate_line` 已保证
  单行不超过 `MAX_LINE_CHARS`，一定装得下），所以不会返回空正文。
- 撞上限时的小结单独一种措辞：`（共 N 行，已显示 a-b 行——本页触到单次
  30000 字符上限；继续读用 offset=b+1）`。**必须说清是撞了字符上限**，
  否则模型会以为自己传的 limit 没生效，然后把 limit 调得更大（更糟）。
- 实测：`limit=5000` → 30018 字符（原来 54580）；小结写"已显示 1-817 行
  ——本页触到单次 30000 字符上限；继续读用 offset=818"。

### B. fs_glob：补上"按名字找"那一格

- 认知框架（本步最值钱的一点）：**"读什么"是三个互不重叠的问题**——
  按内容搜（`fs_find`）/ 按名字找（**原来缺**）/ 按位置读（`fs_read`）。
  缺中间一格，模型只好拿内容去猜文件名、或者把 `tree_dir` 的输出当名单用。
- `glob_files(pattern, root=".", max_results=50)`：
  - `_glob_to_regex` 纯函数把 glob 编译成正则：`*` 不跨 `/`、`?` 单字符、
    **`**/` 匹配"零层或多层目录"**——最后这条是 glob 最反直觉也最要紧的
    地方，正因为允许零层，`**/*.py` 才能同时命中 `agent.py` 和
    `src/harness/agent.py`；
  - **为什么不借现成的**：`fnmatch` 的 `*` 会跨 `/`，于是 `**/*.py` 反过来
    匹配不到根目录的 .py（语义错，而且错得安静）；`Path.glob` 语义对，但
    绕不过忽略名单——实测扫全树 **1760 ms**，正是第一步刚修掉的那个毛病。
    自己翻译十几行，语义和性能就都在手里；
  - `re.escape` 把模式里的 `.` 当普通字符，否则 `test_*.py` 连 `test_aXpy`
    都命中；
  - 复用 `find_text` 的剪枝骨架（`dirnames[:]` + `FORBIDDEN_PARTS` +
    `IGNORED_DIRS`）；**不按后缀过滤二进制**——只看文件名、从不读内容，
    没有"读成乱码"的风险，模型要找 `**/*.png` 就该找到（与 fs_find 不同）；
  - pattern 按**相对 root** 解释（与 `Path.glob` 一致），报出的路径始终相对
    项目根（方便直接喂给 `fs_read`）：所以 `glob_files("*.md",
    root="learn-workbuddy")` 命中教材第一层的 .md，报出
    `learn-workbuddy/README.md`。
- `toolbox`：注册 `fs_glob`（即时工具），加进 `SAFE_TOOLS`（只读 + 沙箱内，
  与 `fs_find` 同款）；**刻意不进 `WRITE_TOOLS`**。

- 实测：`fs_glob("**/*.py")` 全量遍历 **5.8 ms**（对照 `fs_find` 未命中
  46 ms、`Path.glob` 1760 ms）；命中 46 个本项目 .py，`.venv` 与
  `learn-workbuddy` 一个都不出现；显式 root 时能翻教材。

- 测试：**355 条，15 红仍是 s10 骨架 TODO，非回归。** 新增 GlobToRegexTests 7
  （`*` 不跨 `/` / `**/` 零层与多层 / 中间的 `**` / 裸 `**` / `?` 单字符 /
  `.` 被转义 / 大小写不敏感）、GlobFilesTests 7（找到项目 .py / `**/` 同时
  够到嵌套与根目录 / 忽略名单生效 / 显式 root 绕过且路径相对项目根 / 未命中
  回显模式 / 截断提示 / 越界拒绝）、ReadFileGuardTests +2（单页遵守字符预算
  且说明原因 / 短行文件不受影响）。

- 遗留：**非原子写仍没做**（`fs_write` / `fs_edit` 都还是整文件覆盖落盘，
  下一步做）；`list_dir` / `write_file` 的错误文案还没跟上；
  `parse_docstring` 丢续行仍在；`tree_dir` 未接 `IGNORED_DIRS`。

- **新发现的缺口（本轮顺手记下，未修）**：策略层的 `path_arg()` 只认
  `arguments["path"]`，而搜索类工具用的是 `root`——实测 `fs_find` /
  `fs_glob` 传 `root="../.."` 时，决策层给的是 **allow [tool.allow_safe]**，
  而 `fs_read` / `fs_write` 同样越界是 **deny [path.outside_workspace]**。
  **不是安全洞**（执行层 `_resolve_safe` 仍然照拦，实测抛 PermissionError，
  不会真的越界），但"决策层预判、执行层兜底"两层就不说一家话了。修法是让
  `path_arg` 也认 `root`，并把两个搜索工具加进 `READ_TOOLS`——属于权限层的
  改动，值得单独一步。

- 提交：（待用户）

## 已完成：第五步 · 写侧收口（原子写 + 错误文案）（2026-09-12，由助手写完）

两件事：把两个写工具的落盘换成**原子替换**，再把剩下的错误文案统一成
**可行动的**（评审报告 P0-6 与 P1-9）。走的是 A 方案——在 file_tools 里新写
一份，不去动 `workspace_memory` 的 TODO 4（那属于 s10 主线，留给自己填）。

### A. 原子写（`_atomic_write_bytes`，机制只有一份）

- 病灶：旧写法是 `path.write_bytes()`——**先截断再写**。写一半崩溃就是半个
  文件顶着正式名字，原有内容已经被毁了。文件越大这个窗口越宽，而 `fs_edit`
  正是为改大文件而生的，所以它比 `fs_write` 更经不起这条。
- 四步（顺序不能换，注释里逐条写了为什么）：
  1. `tempfile.mkstemp(dir=path.parent)` —— 临时文件必须与目标**同目录**。
     `os.replace` 只保证"同一文件系统内"原子；跨盘会退化成"复制 + 删除"，
     那个窗口里文件是半截的。
  2. 写入 + `flush` + `os.fsync` —— fsync 把数据真正推给磁盘。少了它，
     `os.replace` 之后断电仍可能丢内容（改名是原子的，但数据可能还在操作
     系统的页缓存里）。
  3. `os.replace(tmp, path)` —— 原子改名：观察者要么看到旧的完整文件、要么
     看到新的完整文件，不存在中间态。
  4. `finally: tmp.unlink(missing_ok=True)` —— 成功时 tmp 已不存在
     （`missing_ok=True` 所以不炸），失败时清掉残骸。
- **落在 `_write_text_bytes` 里，所以 `write_file` 与 `fs_edit` 一起获得原子
  性**——机制只有一份，不会出现"一个原子、一个不原子"的分裂。

### B. 错误文案（P1-9 收口）

改前都是"陈述事实"，改后每条都带下一步。实测输出：

| 场景 | 现在的文案 |
| --- | --- |
| 列不存在的目录 | `没有这个路径：X——用 fs_list('.') 看项目根下有什么，或用 fs_glob('**/*名字片段*') 按名字找` |
| 把文件当目录列 | `README.md 是文件不是目录——用 fs_read('README.md') 读它的内容` |
| 路径越出沙箱 | `../../外面.txt 越出沙箱——沙箱根是 D:\React\wywd-harness。请改用相对项目根的路径，例如 'src/harness/agent.py'` |
| 碰禁区 | `禁区不可访问：.git/config（命中 .git）——.env 里的密钥、.git 里的版本库对工具永久关闭，换个文件吧` |
| 写一个目录 | `sub 是目录不是文件——要写哪个文件请把文件名补齐` |
| 父目录不存在 | `X 的父目录不存在——本工具不会自动创建目录，先用 fs_list 确认上一级` |
| 写超限 | `写入内容过长（20001 字符，上限 20000）——改已有文件请用 fs_edit（只传改动的那一段），或者拆成几次写` |

- 两处顺带修掉的结构问题：
  - `list_dir` 原来只检查 `is_dir()`，"路径不存在"和"这是个文件"会撞出同一条
    模糊消息（甚至掉到底层的 `FileNotFoundError` 原文）——现在分开报；
  - 禁区消息原来只说"拒绝访问"，现在把**命中的是哪个禁区段**报出来
    （`.git/config` 会显示"命中 .git"），模型才知道是路径中段撞的。

### C. 测试结构

- 抽出 `_SandboxedFileTest` 基类（临时沙箱 + `_seed` / `_read` / `_read_bytes`
  / `_write` / `_edit` / `_list` / `_temp_leftovers`），`EditFileTests` 与两个
  新类共用，删掉了重复的 setUp / tearDown。
- 新增 `AtomicWriteTests` 4 条：写成功不留 `.tmp` / 编辑成功不留 `.tmp` /
  **mock 掉 `os.replace` 后旧内容原封不动且无残骸**（`write_file` 与
  `edit_file` 各一条）。后两条是原子写唯一能被观测到的承诺。
- 新增 `ErrorMessageTests` 7 条：上表每条文案都钉住关键字。

- 验收实测（探针 `.workbuddy/scratch/probe_step5.py`）：

```
原子写失败（mock os.replace） : 内容 '原始内容\n' 完好，目录里没有 .tmp
非原子写同样失败（真做一次）   : 内容只剩 'x'，原来的 5 个字符没了
```

- 测试：**366 条，15 红仍是 s10 骨架 TODO，非回归。**

- 遗留：`fs_list` 仍无条数上限（P1-13）；`fs_find` 仍丢缩进 / 无上下文行 /
  大小写不敏感（P1-10~12）；`parse_docstring` 丢续行（P2-14）；`tree_dir` 未接
  `IGNORED_DIRS`；策略层 `path_arg()` 不认 `root`（见第四步节的缺口）。
- `workspace_memory._atomic_write_text` 当时还是 **TODO 4 骨架**——本次没有去
  实现它（那属于 s10 主线，是留给自己填的练习）。见下一节：它已经在 s10 里
  填完了。

- 提交：455acdc

## 进行中：s10 · TODO 1~4 已填（2026-09-12，由助手写完）

用户的切法：**一次填 1~4**（`__init__` / `append_daily_log` / `_read_log` +
`read_all_facts` / `_atomic_write_text`），红灯 **15 → 10**。

### 填了什么

- **TODO 1 `__init__`**：`resolve()` 先行（防相对路径 / 软链接给同一个项目
  造出两个 scope）→ `workspace_id = sha256(绝对路径)[:16]` → 布局一次定死
  + `daily/` 落地。
- **TODO 2 `append_daily_log`**：四重校验（空 / 超长 / importance 越界 /
  未知 kind，每条错误消息都带"可选值是什么"）+ 造 `MemoryFact` +
  **`"ab"` 二进制追加 + flush + fsync**。
- **TODO 3 `_read_log` + `read_all_facts`**：三种情况区别对待——partial tail
  放过、完整坏行炸、空行跳过；外加 UTF-8 解码失败也算损坏（**乱码当空行跳过
  等于悄悄丢事实**）；`workspace_id` 不匹配 → `MemoryScopeError`。
- **TODO 4 `_atomic_write_text`**：`mkstemp` 同目录 → 写 + `flush` +
  `fsync` → `os.replace` → `finally unlink(missing_ok=True)`。

### 三处对骨架说明的偏离（都写进注释了）

1. **`read_all_facts` 的排序只用 `recorded_at`，不拿 `fact_id` 当平局判据。**
   骨架写的是 `(recorded_at, fact_id)`，但 `fact_id` 是 `uuid4`——实测 2000
   次：按双键排只有 **49.8%** 保住插入顺序（等于掷硬币），单键排 **100%**。
   追加顺序是"同一秒里谁先写的"这个有意义的事实，不该被随机数洗掉；而
   `sorted` 本身是稳定排序，单键天然保住它。**测试 `test_append_read_roundtrip`
   断言的正是插入顺序**——照骨架写会变成一条 50% 概率挂的 flaky 测试。
2. **新增 `_collapse_whitespace`（只折叠空白，不 casefold）**，`_normal_form`
   改为复用它。原来只有一个 `_normal_form`，它带 casefold——拿它处理**存储**
   会把 `SQLite WAL` 存成 `sqlite wal`，那是对原文的篡改。存储只折叠空白，
   寻键才需要叠 casefold。**"存储"和"寻键"不该用同一种强度。**
3. **落盘用 `"ab"` / `"wb"` 而不是 `"a"` / `"w"`。** 文本模式会替我们翻译
   换行符（Windows 上 `\n` → `\r\n`），写出来的字节就不是要给的那一串——
   2026-09-12 在 file_tools 上刚栽过这个坑。

### 顺带补的一个骨架漏洞

`_render_memory`（TODO 5）**没有 `raise NotImplementedError`**，函数体只剩
docstring——调用它会**静默返回 `None`**，然后在很远的地方炸。这正是练习 17
记下的"没执行到 return 就等于 return None"。已补上 raise：**未实现的 TODO
必须大声失败**。

### 验收

- 测试：**366 条 / 10 红**（原 15 红）。转绿的正是 `ScopeTests` 2 +
  `AppendTests` 3；剩下 10 条是 `DistillTests` 6（TODO 6）、`ContextTests` 2
  （TODO 7）、`ToolboxIntegrationTests` 2（TODO 8a/8b）。
- 探针 `.workbuddy/scratch/probe_s10_todo1to4.py` 实测：四条校验全拦住、
  partial tail 放过（读回 2 条）、完整坏行抛 `MemoryCorruptionError`、串线抛
  `MemoryScopeError`、原子写失败后旧内容原封不动且无 `.tmp` 残骸。

### 已知待办

- `workspace_memory._atomic_write_text` 与 `file_tools._atomic_write_bytes`
  是**同一套机制的两份实现**（都在 2026-09-12 写）。先各留一份是为了保住
  s10 的教学点；等这一课过了，把其中一份改成调用另一份，收敛成单实现。
- TODO 5~8b 未填；TODO 9 / 10 没有任何测试钉住（只能靠冒烟验收）。

- 提交：（待用户）

## 进行中：s10 · TODO 5~6 已填（2026-09-12，由助手写完）

从 TODO 1~4 之后接着填。红灯 **10 → 3**。

### 填了什么

- **TODO 5 `_render_memory`**：三行页眉 + 按 decision / convention / pitfall
  分节。三个细节：**空节不输出**（只有标题没有内容，会让人以为"这里本该有
  东西但丢了"）；outcome 干脆不在节列表里——它从不晋升，curated 里根本没有
  它；条目内按 `(content, key)` 排序，**保证渲染结果逐字节可复现**——派生
  视图一旦不可复现，每次读都会判定"与 canonical 不一致"，于是每次读都白写
  一次原子替换。
- **TODO 6 `distill`**：三段式——筛（年龄线 + 剔除已处理）/ 分组（内容寻键）
  / 落地（新建或合并）。
  - **幂等的钥匙**是 `processed` 集合：晋升只建立"证据 -> 条目"的指针，日志
    一行不动，所以重复跑靠"这条事实已经指向某个条目了"跳过；
  - **代表事实**取 `(-importance, recorded_at, fact_id)` 最小——最重要优先、
    同重要度取最早措辞。挑法必须确定，否则每次渲染出来的 MEMORY.md 都可能
    不一样；
  - **没有变化就不落盘**（`if created or updated`），省掉两次原子替换。

### ⚠️ 发现的一处账目矛盾（骨架说明 ≠ 字段注释 ≠ 测试）

三处对 `skipped` 的说法互相打架：

| 出处 | 说法 |
| --- | --- |
| `DistillReport` 字段注释 | "被门槛拦下的事实数" |
| 骨架的 TODO 6 说明 | `skipped = len(aged) - len(candidates)` 起步，不合格组再 `+= len(facts)` |
| 测试 `test_importance_or_repetition_gate` | `created + skipped == scanned` |

**场景 D（同内容重复 2 次、重要度 2）**：两条事实都够格（重复闸门放行），
所以"被门槛拦下的事实数"= 0；但测试要求 `1 + skipped == 2`，即 skipped = 1。
**两者不可兼得。**

**根因是单位不一致**：`scanned` / `eligible` 数的是**事实**，`created` /
`updated` 数的是**条目**。一组 2 条事实只建出 1 条条目——另 1 条被折叠进
同一条记忆，它没有从账上消失。

**我的取法**：`skipped = scanned - created - updated` 收口（让测试的恒等式
永远成立），并把 `DistillReport` 的字段注释改成与之一致的说法。代价是它不再
等于"被拦下的事实数"，诊断价值下降。
**另一条路**：保留原语义，把测试断言改成 `eligible + skipped == scanned`
（这个恒等式在 A/B/C/D 四个场景里都成立）。选了前者是因为**不去改测试契约**；
若更看重诊断价值，随时可以换成后者。

另：`scanned` 的语义也按测试倒推调整成了"**过了年龄线且尚未处理过**的事实数"
——`test_distill_is_idempotent` 断言第二次跑的 `scanned == 0`；若按字面的
"过了年龄线的事实数"算，它永远是 1，看不出到底是幂等生效还是坏了。

### 验收

- 测试：**366 条 / 3 红**。转绿的是 `DistillTests` 6 条 + `ContextTests` 的
  `test_restart_recovers_state`（它只需要 5+6，不需要 TODO 7）。剩 3 条：
  `ContextTests.test_recent_facts_bounded_and_placeholder`（TODO 7）、
  `ToolboxIntegrationTests` 2 条（TODO 8a / 8b）。
- 探针 `.workbuddy/scratch/probe_s10_todo5to6.py`：四个场景分别打中四道闸门
  （年龄 / 类型 / 重要度 / 重复）；幂等二跑 `scanned=0`；内容寻键把
  `SQLite 必须 WAL` 与 `sqlite   必须  wal` 合成一条；渲染出的 MEMORY.md 三节
  齐全且 outcome 不在其中；原子写无残骸；`DistillPolicy(minimum_age_days=0)`
  能把刚写的事实当场晋升——**证明门槛是 harness 控制的参数，不是模型说了算**。

- 渲染产物（探针实测）：

```
# Workspace Memory

Derived from append-only project facts. Edit the source log or policy, not this view.

## Decisions
- 存储层用 SQLite WAL 模式 (seen 1x; evidence: 1)

## Conventions
- 路径必须相对项目根 (seen 1x; evidence: 1)

## Pitfalls
- 不要把这个坑忘了 (seen 1x; evidence: 1)
```

- 剩余：TODO 7（`get_context_for_agent`）、TODO 8a / 8b（`toolbox.py` 的
  `write_memory_fact` / `build_history_seed`）；TODO 9 / 10 靠冒烟。

- 提交：（待用户）

## 已完成：s10 工作区记忆全部填完（2026-09-12，由助手写完）—— 红灯清零

从 TODO 1~4 → 5~6 → 7 + 8a/8b → 9 + 10，分三轮填完。**366 条测试全绿**，
并且走完了完整冒烟。

### 最后一轮（TODO 7 / 8a / 8b / 9 / 10）

- **TODO 7 `get_context_for_agent`**：两段拼接——策展视图（`read_memory_md`，
  顺手修复陈旧的 MEMORY.md）+ 最近 N 条原始事实。`recent_limit <= 0` 时整段
  不要；**注意不能写成 `facts[-recent_limit:]`**，0 取负还是 0，`facts[-0:]`
  等于整个列表（练习 19 记过的坑）。空记忆返回哨兵文案。
- **TODO 8a `write_memory_fact`**：`WorkspaceMemory(root or ALLOWED_ROOT)`
  → `append_daily_log(source="agent")` → 返回"已记录 … 等待蒸馏策略裁决"。
  **措辞是安全设计的一部分**：说"已永久记住"会让模型以为它能操纵长期记忆。
- **TODO 8b `build_history_seed`**：`with_system([])` 打底（工具目录），
  有记忆时追加第二条 system。空记忆时与 s06.5 完全一致。
- **TODO 9 `scripts/shell.py`**：`history_seed=lambda: with_system([])` →
  `history_seed=build_history_seed`；`with_system` 随之在本文件失效，从 import
  里移除；填完的 TODO 指令块清掉、设计要点收编进 docstring。
- **TODO 10 `scripts/sidecar_shell.py`**：`/memory` 与 `/memory distill`。
  **本地直读、不走 RPC**——记忆就是 `<项目根>/.memory/` 下的文件，任何进程都
  能读、都能蒸馏（教学点：**记忆的所有权在文件系统，不在某个活着的进程**，
  所以 sidecar 崩了、会话换了一代，记忆都还在）。注意导入顺序：`src.harness.*`
  必须排在 `from scripts.shell import ...` **之后**——项目根的 sys.path 引导
  在 shell.py 里。

### 一处顺手的小收拾

`"(no workspace memory yet)"` 这个哨兵要在两个模块里对齐
（`get_context_for_agent` 返回它、`build_history_seed` 靠它判空），抽成
`workspace_memory.NO_MEMORY_PLACEHOLDER` 常量。跨模块比字符串字面量是"改一处
漏一处"的经典来源，而且漏了的症状很隐蔽：**记忆永远注入不进去**。

### 冒烟验收（不用 API key，脚本 `.workbuddy/scratch/smoke_s10.py`）

| 场景 | 实测结果 |
| --- | --- |
| 1 空记忆起步历史 | **1 条 system**（工具目录）——与 s06.5 行为零变化 |
| 2 写链路 | `memory_write` 落进 `.memory/daily/2026-09-12.jsonl` 共 1 行；读回 kind=decision / importance=5 |
| 3 注入链路 | 有记忆后起步历史变 **2 条 system**，第二条是 `# Recent Workspace Facts` / `- [decision] 这个项目用 uv 管虚拟环境 (2026-09-12)`——**在真 sidecar 子进程里验的**，证明 shell.py 的接线生效 |
| 4 蒸馏链路 | `DistillPolicy(minimum_age_days=0)` 把年龄线压到 0 → 扫描 1 / 晋升 1；MEMORY.md 出现 `- 这个项目用 uv 管虚拟环境 (seen 1x; evidence: 1)` |
| 5 幂等 | 第二次：扫描 0 / 晋升 0 / 合并 0 |

- 冒烟脚本自己踩了一个坑（本身有教学价值）：**顶层直接 `shell.start()` 会触发
  Windows spawn 递归起进程**——`An attempt has been made to start a new
  process before the current process has finished its bootstrapping phase`。
  加 `if __name__ == "__main__":` 守卫后正常。本项目所有入口都守这条守则。
- 冒烟跑完自动清理：`.memory/` 整个删除（跑之前不存在）、本次新建的
  `.sessions/sess_0154.jsonl` 与 `sess_0155.jsonl` 删除。**仓库里没留下任何
  验收痕迹。**

### 仍未验证的部分 → 已验证（2026-09-12 真实环境冒烟）

- 真模型路径已用脚本 `.workbuddy/scratch/smoke_s10_real.py` 验过。
  关键证据：
  - **场景 A**：让真模型调 `memory_write`——它**真的选了**那个工具
    （ToolSearch 拿到 schema → DeferExecuteTool 执行）。回复里没废话
    是好事：模型听话干活不嘴碎。
  - **场景 B**：`.memory/daily/2026-09-12.jsonl` 落了 1 行，
    `kind=convention importance=4 source=agent`。
  - **场景 C**：新会话（`sess_0155`）起步历史 2 条 system，第二条把
    那条事实注入了进来。
  - **场景 D**：默认 30 天年龄线拦下（合理），压到 0 后晋升 1 条，
    MEMORY.md 出现 `- 本项目…约定以 uv 管理 Python 虚拟环境 (seen 1x; evidence: 1)`。
- 跑完自动清场：`.memory/` 与本次新开的两个 session 文件全删。仓库干净。

至此 s10 整条链路（离线 + 真模型 + 离线回归）全部端到端通过。

### 遗留

- `workspace_memory._atomic_write_text` 与 `file_tools._atomic_write_bytes`
  仍是同一套机制的**两份实现**，待收敛成一份。
- 评审报告剩下的 P1/P2 工具小项；权限层 `path_arg()` 不认 `root`。
- shell 工具那条线（用户说以后再做）——建议先做 `run_tests` 这类零注入面的
  专用工具。

- 提交：（待用户）

## 2026-09-16 ~ 09-17：长任务三连修 + bash 工具上架

> ⚠️ **先说一件影响记录方式的事**：`.workbuddy/memory/` 下的文件（含 MEMORY.md）
> 在**会话之间会被宿主记忆服务的缓存覆盖** —— 09-16 那天写进去的 push 记录、
> 探针、长任务修复全没了，而同时期写进 `scratch/` 的脚本都在。
> 所以：**要跨会话留下的事，写在这个文件里**（git 跟踪，不会被覆盖），
> 别只写进 `.workbuddy/memory/`。

### 一、修「完成不了长任务」（09-16）

**根因**：`run_agent` 的签名默认 `max_steps=5`，而**所有入口都不传它**
（`sidecar.py` 的 turn_runner、`chat.py` 都没给）→ 5 成了线上真实上限。

**证据**（`.workbuddy/scratch/probe_step_limit.py`，ScriptedModel 离线可复现）：
同一个「7 轮工具 + 第 8 轮给答案」的剧本，只改 max_steps：

```
max_steps= 5  status=max_steps  实际执行工具轮数=5
max_steps= 8  status=completed  实际执行工具轮数=7
max_steps=10  status=completed  实际执行工具轮数=7
```

**改动**
- `scripts/toolbox.py`：`MAX_AGENT_STEPS_DEFAULT = 30` + `resolve_max_agent_steps()`
  （读 `WYWD_MAX_STEPS`，非法值喊一声退回默认）；**把步数预算写进 system prompt**
  （业界做法：光有 enforced ceiling 不够，还要有 advertised budget，模型才会
  自我规划、接近上限时收尾）；`build_system_prompt` / `with_system` /
  `build_history_seed` 都多了一个 `max_steps` 参数（默认 None，无参调用照旧）。
  30 的依据：OpenAI Agents SDK 10 / LangGraph 25 / CrewAI 25 / smolagents 20 /
  Vercel 20，**注意单位不可直接搬**（LangGraph 数 super-step，我们数 model.generate）。
- `src/harness/sidecar.py`：构造器加 `max_steps: int = 30`，turn_runner 显式传，
  `sidecar/status` 加 `maxSteps`（行为参数要能在运维面板看见）。
- `scripts/shell.py` / `scripts/chat.py`：装配处显式传；chat 撞上限时走新的 `⏸` 分支。
- `src/harness/agent.py`：熔断出口的 output 从 `"请求调用工具：now"`（对模型是它
  刚说的话、对用户是天书）改成说清三件事——用了几轮 / 未完成 / **可续跑**。
- 前端 `store.js`（优先显示后端那句人话）、`drawer.js`（显示单轮步数上限）。

**关键语义**：撞上限是**暂停**不是失败。`messages` 照常返回，调用方把它当 history
再发一句「继续」就能从断点接着做（`verify_long_task.py` 验证：中断时 5 条消息，
续跑时模型收到 6 条，role 序列完整，能走到 completed）。

### 二、bash 工具上架（09-17）

**背景**：`permissions.build_default_policy` 里早就有两条专属于 bash 的规则
（`bash.hard_deny` / `bash.requires_approval`），一直没有对应工具，规则在空转。
所以工具名必须**逐字叫 `bash`**：规则按名字匹配，换名字就落进 `default.deny`。

- `src/harness/std_tools.py`：新增 `run_bash()` + `_clean_env()`（按名字剥
  KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL 环境变量——`env` 本来能把 API key 打出来，
  而工具输出要回灌给模型**并随 transcript 落盘**）+ `_decode_output()`（utf-8 →
  gbk → replace）+ `_clip_output()`（超长截断并**注明**）。cwd=沙箱根；
  stdout+stderr 合并；`shell=True` 平台默认；超时钳到 [1,300]。
- `scripts/toolbox.py`：`bash_enabled()`（`WYWD_DISABLE_BASH=1` 关闭，**默认启用**
  ——它的安全靠"每次都要审批"，开了闸门却不上架等于白做）；`SAFE_TOOLS` 注释里
  写死「绝不能加 bash」。
- **顺手修掉一个潜藏 bug**：`permissions.is_hard_deny` 里
  `command.strip().split()[0]` 在空命令上会 `IndexError`——那条规则在 bash 存在
  之前**永远不会被调用**，所以从没暴露，bash 一上架就会被 `{"command": ""}` 踩到。
- 新增 14 条测试（`tests/test_std_tools.py` 的 `BashToolTests`）+ 更新
  `tests/test_workspace.py` 的注册名单期望值。

**边界（必须明说）**：bash 是本项目**唯一跑得出沙箱**的能力——fs_* 的路径被
`_resolve_safe` 拦住，命令拦不住（`cd /` 之后想去哪去哪）。它的边界是**审批**，
不是沙箱。DANGEROUS 那份是**首词黑名单**，属心理安全（`find . -delete` 绕过 `rm`）。

### 三、能力探针 `scripts/smoke_capability.py`（10 层）

测「agent 能做什么、卡在哪一层」。`--max-steps N` 做对照实验是核心用法：

```bash
python -X utf8 scripts/smoke_capability.py                 # 现状
python -X utf8 scripts/smoke_capability.py --max-steps 20  # 对照
python -X utf8 scripts/smoke_capability.py --only L4 L7
python -X utf8 scripts/smoke_capability.py --offline       # 只验管线，不烧额度
```

L1 单工具 / L2 多轮串联 / L3 多工具协作（含能否识别输出被截断）/ L4 延迟工具发现 /
L5 失败后诚实 / L6 写审批 / L7 长任务（对照主角）/ L8 跨轮记忆 / L9 没有的能力会不会编 /
L10 用 bash 干正事（含审批闸门）。

### 验收

- **492 条测试全绿（2 skip）**
- `.workbuddy/scratch/demo_bash.py`：四场景端到端（批准 / 拒绝 / 危险命令 / 空命令），
  其中危险命令那条**审批环节根本没触发**（approver 一次都没被调用）——「DENY 不可
  审批覆盖」的实证。
- `.workbuddy/scratch/verify_long_task.py`：长任务改动 5 个检查点全 OK。
- `.workbuddy/scratch/probe_find_time.py`：只读工具耗时实测（fs_read 0.96ms /
  fs_find 早停 17ms / 全量 40ms）→ 结论「工具之间并行不值得做」。

### 仍未做

- `README.md` 还停在 chainlit 时代（46 条测试 / `chainlit_app.py` 都过时了）。
- 长任务只解决了一半：历史窗口仍是 20 条 ≈ 6 轮，跑长了会被 `trim_history` 截掉。
  真正的长任务要教材 s13（输出外化）+ s14（四层压缩）。
- bash 的输出上限 8000 字符只是止损（截断+说明），s13 才是解法。

### 四、修「工具像在返回假数据」的真因：换代时工作区静默复位（09-17）

**现场**（用户提供的对话记录）：同一会话里，`fs_list` 列出了某个 Java/Vue 项目
（`backend/ frontend/ docs/ sql/ .m2/ .maven-settings.xml` …）的完整结构，
`fs_read README.md` 也读到了它的 README（DocNest 企业知识库）；但紧接着
`fs_read docs/RAG评估与消融实验报告.md` 报
`[WinError 3] 系统找不到指定的路径: 'D:\React\wywd-harness-v2\docs\...'`，
`fs_glob **/*.java` 也返回空。**当时的 agent 判定「工具在返回虚假数据」——这个结论错了。**

**先排除"假数据"**：`list_dir` 的输出格式是 `名字  (文件, N 字节)` / `名字/  (目录)`，
与现场逐字一致；`fs_find` 读到的 `.gitignore` 前 15 行与我们 v2 的真实 `.gitignore`
一字不差。**输出是真的，只是两次调用用了不同的根。**

**真因**：**工作区是 sidecar 的进程级状态，而 `revive_shell()` 换代时没有把它带过去。**
1. 用户把工作区打开成磁盘上那个项目目录（browse/open）→ 那一刻 root 就是它，
   所以 `fs_list` / `fs_read README` 都对；
2. sidecar 因环境原因死掉（本机高频：宿主"安全删除"拦 unlink，见 09-15 事故）；
3. 下一次请求触发 `revive_shell()` → 新壳的 `initial_workspace` 是**启动态 =
   项目根 v2**（`scripts/shell.py` 装配时写死的那份）；
4. 于是后续所有工具都按 v2 解析路径 → `docs/` 不存在、没有 `.java`；
5. 前端只在**用户主动操作工作区**时才刷 `refreshWorkspace()`（`start()` 里刷一次），
   所以界面上可能还显示着旧路径 —— 现象看上去就像"数据是假的"。

`workspaces/` 目录是空的 → 那个项目**不是 zip 上传的**，是「打开目录」指过去的
真实磁盘目录。

**修复**（`scripts/web_app.py`）
- 新增 `self._workspace_root`：凡成功切换工作区就记一笔（`_remember_workspace`，
  挂在 `set_workspace` 的**统一出口**上，zip 上传也一并覆盖）；
- `revive_shell()` 起完新壳后调 `_restore_workspace()` 把原目录设回去；
- **只在明确知道结果时更新记忆**：`kind=default` → 清空（用户主动复位，别把
  复位也"恢复"掉）、有 `root` → 更新、结构缺失 → 保持原样（"猜"会擦掉一份
  好记忆，而擦掉就再也恢复不了了）；
- 恢复失败不拖垮自愈，但**喊一声**（不静默）；
- **为什么记忆必须在 WebApp 这一侧**：revive 的触发条件就是旧壳已经死了——
  问不到"你刚才在哪个目录"。

**验收**
- **495 条测试全绿**（492 + 3 条新测试：恢复成功 / 复位后不恢复 / 恢复失败清记忆）
- `.workbuddy/scratch/verify_workspace_revive.py`：**真 sidecar** 端到端——
  spawn 真子进程 → 切工作区到临时目录 → 杀掉 → 自愈 → 读回的工作区**确实**是那个
  目录（`恢复成功 : True`）。

**顺带踩的坑**：写这个验证脚本时忘了 Windows spawn 守则（顶层直接 `shell.start()`），
子进程一 spawn 就崩 `An attempt has been made to start a new process before…`。
**所有副作用必须进 `main()`，顶层只留 `if __name__ == "__main__"`** ——
s10 的冒烟脚本踩过同一个坑，这次是我自己踩。


