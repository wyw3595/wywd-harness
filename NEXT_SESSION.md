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

- **从现在起的工作节奏**：改代码 → 跑测试 → `git add -A && git commit -m "..."` 小步提交。
  每完成一节练习都应该有一次提交，提交信息写清"做了什么"。

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
```

当前项目使用 Python 3.13（uv 管理的 .venv，requests + chainlit）。

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
  刻意不在内）+ build\_policy()（WorkspaceScope(ALLOWED_ROOT)——决策层
  预判和执行层 \_resolve\_safe 认同一个沙箱根，两层不说两家话）。
  已知边界：策略看见的是桥接工具 ToolSearch/DeferExecuteTool，穿透后
  的延迟工具（tree\_dir）管不到——它只读+沙箱内，风险可接受；

- agent.py：runner 分支统一改用 result.to\_protocol\_block()——
  "Error [xxx]:" 前缀只有这一个真源，循环不再自己拼拦截文案；

- chat.py：GovernedToolRunner 接入（build\_policy + cli\_approver y/n
  + AuditTrail），run\_agent 传 runner。只有 ASK 分支会调审批员；
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
