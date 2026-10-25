# wywd-harness

从零实现一个生产级 Python LLM / Agent Harness 的学习项目。
内核自建：模型协议、Agent Loop、工具系统、权限治理、真实模型接入、会话管理、
上下文压缩、多级记忆、技能系统、成本可观测；运输层走 FastAPI + 自研 Vue 前端。

## 当前能力

- **模型协议**：结构化 `Model` 协议 —— `generate(messages) -> ModelReply`
  （`kind="final" | "tool_calls"`），无字符串暗号；`ModelRouter` 三档路由
  （lite 粗筛 / default 执行 / craft 主对话），Router 自己实现 Model 协议所以零改动插入。
- **真实模型**：`RealModel` 走 OpenAI 兼容 HTTP（DeepSeek）——连接池、指数退避重试、
  4xx 永久错误快速失败、wire 翻译只在这一个文件。
- **Agent Loop**：`run_agent` 有界多轮循环；工具结果/错误按 `tool_call_id` 回灌、
  多工具互不拖累；`RunResult.status` 把 `completed / max_steps / failed` 做成可预期结局。
- **工具系统**：`ToolRegistry` + `inspect` 反射自动生成 JSON Schema；沙箱文件工具
  （只读根 + 限额 + `.env`/`.git` 禁区）；bash 工具（进程树击杀、后台任务出口、
  explain 给人看的命令说明）；延迟工具（schema 不常驻，ToolSearch 发现再加载）。
- **权限治理**：有序规则 first-match-wins + 默认拒绝；`ALLOW / ASK / DENY` 三种
  治理语义，DENY 不可被审批覆盖；界内写放行 + 只读 bash 白名单（治理审批疲劳）；
  approver 是注入的回调（终端 y/n / 网页卡片 / 测试假实现同一接口）。
- **权限免审批分级**：`path.write_allow`（界内写）、`bash.read_only_allow`
  （只读命令白名单，无 shell 元字符 + 整词命中 + 只读三前提）。
- **会话管理**：逻辑会话（SessionRecord，可持久化）与运行时（SessionProcess，
  turn 锁 / abort 信号，绝不序列化）分离；resume = 旧 id + generation+1 的新运行时；
  **会话级沙箱**——目录跟会话记录走，"一个会话一个目录"。
- **持久化**：JSONL 证据文件 append-only（transcript 是证据，MEMORY.md 是派生）；
  工具大输出外化到磁盘，上下文只留指针。
- **上下文压缩**：四层管线（截大输出 → 去重读 → 修剪 → 生成式摘要），每层记账；
  已确认事实走无损旁路，永不交给摘要；压缩产物不落盘（模型看压缩视图，证据记原始历史）。
- **记忆**：工作区记忆（从 daily 日志蒸馏，模型只能提名、门槛由 harness 控制）、
  用户记忆（Profile + Preference，语义 key 去重）、session transcript 三层分账。
- **历史召回**：五段召回链 normalize → candidate → score → stable rank →
  render（词法覆盖率 + 新近度可解释评分，total/覆盖率/新近度并列时按显式
  键破序）；`recall_memory` 工具按主题搜 daily log 全量原始证据，纯读不写回；
  跨用户 scope 防线在引擎；RecallResult 经桥接器进 s15 选择管线吃真分数。
- **MCP 连接器**：外部工具经标准协议接入——MCPServer 协议（tools_list /
  tools_call）、两道门（用户信任 TrustStore 持久化 + 项目级 grant 只能
  收窄，网络位与门）、命名空间化 `mcp__连接器__工具`、可见即注册成
  **真延迟工具**骑 ToolSearch / DeferExecute 轨道；执行期双检（权限层
  ASK + handler 撤权复查）防 TOCTOU；无 mcp.json 的默认工作区零开销。
- **专家系统**：领域专家整包加载——人格（system_prompt）+ 行为准则 +
  工具倾向（preferred 调序、disabled 摘除，**只能收窄**）+ 技能束；
  会话创建时激活（expertId），specialization 作为 system 消息叠加在
  核心提示之后（不覆盖），**转录即证据**：close/resume 人格自然回来；
  工具倾向进 registry 的接缝已备（select_tools 纯函数）。
- **可视化**：两步协议——`read_widget_guide`（五模块设计指引 × 明暗
  主题）→ 模型按指引生成 SVG/HTML → `show_widget` 服务端校验（viewBox
  约定 / 片段无文档标签 / 尺寸上限）→ 前端 **sandbox iframe** 原地
  渲染（allow-scripts 不带 allow-same-origin）；校验失败人话回灌；
  [widget] 注入协议两端钉死。
- **成果交付**：`present_files` 唯一交付入口——顺序即观看优先级
  （第一个标主交付 ★），每文件一张 artifact 卡（类型图标/名字/大小/
  存在性）；只交付**新生成**的产物；沙箱外路径拒绝建卡（存在性是
  信息不是展示）；缺失文件如实标"未找到"；[files] JSON 协议合同测试
  钉死。
- **技能系统**：SKILL.md 先列目录、触发词命中再展开全文；manifest 只能收窄权限。
- **提示词组装**：记忆候选选择管线（scope 门 → 置信度门 → 权限三层 →
  精确去重 → 冲突裁决 → top-k/双预算装箱，每条候选一个稳定原因码）+
  系统提示片段规划器（required fail-closed、optional 按价值竞争、
  展示序与预算序分离）；起步历史三块按预算装箱，双决策日志。
- **RPC 并发**：客户端"读线程 + Future 多路复用"——任意线程可并发 call，
  事件/审批走独立分派线程（2026-09-26 起，全局 _rpc_lock 已退役）。
- **Web 运输层**：FastAPI + uvicorn + SSE；守卫中间件（Host 校验 / X-Wywd-Ui 全量
  POST / 可选 WYWD_API_TOKEN / 请求体上限早退）；sidecar 死了自动换新。
- **前端**：Vue 3（vendor ESM，零构建）；SSE 直播 + 断线续传；审批卡；运维抽屉
  （状态 / 成本表 / 日志）。

## 目录

```
src/harness/        内核（不认识任何 UI 和具体模型）
  main.py             RunResult（状态契约的唯一出处）
  models.py           Model 协议 / ModelReply / 离线模型
  agent.py            Agent Loop（循环、回灌、事件钩子）
  tools.py            工具描述、注册表、tool_to_schema
  file_tools.py       沙箱文件工具
  std_tools.py        calc / find_text / glob / tree / bash 等
  permissions.py      权限决策层 + GovernedToolRunner + AuditTrail
  real_model.py       真实模型（OpenAI 兼容 wire 翻译）
  model_router.py     三档模型路由
  memory.py           历史窗口截断
  compact.py          四层上下文压缩 + 无损旁路
  artifact.py         工具输出外化
  prompt_assembly.py  记忆候选选择管线 + 提示片段规划器（两层预算）
  recall.py           历史召回链（normalize / score / stable rank / render）
  mcp.py              MCP 连接器（信任 / grant / 命名空间化 / dispatch）
  experts.py          领域专家包（索引 / specialization / 工具倾向）
  widgets.py          可视化（设计指引 / 校验 / [widget] 注入协议）
  presentation.py     成果交付（artifact 卡 / 沙箱边界 / [files] 协议）
  session.py          SessionRecord / SessionProcess / SessionManager
  jsonl_store.py      JSONL 证据持久化
  sidecar.py          Sidecar 服务端 + RPC 客户端（读线程 + Future）
  workspace.py        上传目录 / zip 工作区
  workspace_memory.py 项目级记忆（蒸馏门槛 harness 控制）
  user_memory.py      用户级记忆（Profile / Preference）
  skills.py           技能索引与按需展开
  electron.py         多进程 IPC 教学（桌面壳一课）
  trace.py            HTML 轨迹可视化
scripts/            壳层与入口
  web_app.py          Web 后端（FastAPI 运输层 + 守卫 + SSE）
  shell.py            SidecarShell（终端/网页共用的编排件）
  chat.py             终端聊天入口
  toolbox.py          装配层：工具/模型/记忆/策略的组装
  native_dialog.py    系统目录选择框（降级机制）
  smoke_*.py          冒烟脚本（真实链路自验，不进单测）
public/             前端（Vue 3 ESM vendor，零构建）
tests/              826 条离线单元测试（unittest，毫秒级，不碰网络）
```

## 快速开始

前置：Python 3.11+，用 uv 管依赖：

```powershell
uv sync --extra dev                                   # 装依赖（dev extra 带 httpx）
uv run python -X utf8 -m unittest discover -s tests   # 跑全量测试
```

⚠ `uv add/remove` 会把环境重同步回默认依赖集、剪掉 dev extra——调整依赖后
记得 `uv sync --extra dev`，否则 test_web_app 导入即炸。

**网页入口**（推荐）：

```powershell
uv run python -X utf8 scripts/web_app.py     # → 浏览器开 http://127.0.0.1:8765
```

**终端入口**：

```powershell
uv run python -X utf8 -m scripts.chat
```

**真实模型**（不设密钥时自动用离线 FakeModel，全链路可离线跑）：

```powershell
setx DEEPSEEK_API_KEY "sk-..."
```

环境变量：`WYWD_HOST` / `WYWD_PORT`（监听地址，默认 127.0.0.1:8765）、
`WYWD_API_TOKEN`（设了则所有 /api POST 需带 x-wywd-token，给 curl/局域网用）、
`WYWD_MODEL` / `WYWD_API_BASE`（换 OpenAI 兼容网关）、`WYWD_MAX_STEPS`（单轮步数，
默认 60）、`WYWD_APPROVAL_TIMEOUT`（审批超时秒数，默认 60）、
`WYWD_IDLE_REAP_SECONDS`（空闲回收，默认关）、`WYWD_DISABLE_BASH`（下架 bash）、
`WYWD_TURN_WORKERS`（并行 turn 上限，默认 2）、`WYWD_SYNC_SEND`（发消息切回
"同步等结局"的老契约——POST messages 现在默认 202 提交即返回，结局走 SSE）。

## 测试与冒烟

`tests/` 全部离线、毫秒级、不碰网络；CI 在每个 push/PR 跑 3.11~3.13 矩阵。
真实链路的自验在 `scripts/smoke_*`（需密钥或图形环境，不进 CI）：

```powershell
uv run python -X utf8 -m unittest discover -s tests -v   # 全量单测
uv run python -X utf8 scripts/smoke_web_workspace.py     # 上传→解压→读回 全链路
uv run python -X utf8 scripts/smoke_session_ops.py       # 会话四操作全链路
node scripts/smoke_frontend.cjs                          # 真 Chromium 前端冒烟（需服务在 8765）
```

## 设计说明

一句话：**协议与实现分离**。`Model` 协议让离线 fake 与真实 DeepSeek 共用同一份循环；
`RunResult.status` 让失败/截断/超步都是可预期的结果；`tool_call_id` 回灌让工具结果
与调用严格配对；wire 翻译只发生在 `real_model.py` 一个文件；权限的决策（纯函数）
与执行（GovernedToolRunner）分开；RPC 客户端的读线程按 id 配对、通知走独立分派。

学习轨迹与规划见 `NEXT_SESSION.md`；面试题积累见 `interview-questions.md`。
