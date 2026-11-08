# 变更请求 CR-2026-11-08：会话验收修复 + reviewer 审查能力

| 项 | 内容 |
|---|---|
| CR 编号 | CR-2026-11-08 |
| 标题 | 会话验收修复 + reviewer 审查能力（MVP） |
| 日期 | 2026-11-08 |
| 范围 | `e9b6dcb..HEAD`（14 个提交，24 个文件，+1473 / -101） |
| 状态 | 待评审（Draft for review） |
| 变更类型 | 缺陷修复（P0/P1/P2）+ 新能力（reviewer MVP）+ 文档 |
| 测试基线 | 全量 874 条 OK（含 2 skip） |

## 1. 摘要

本批变更来自一次真机验收：把服务跑起来、让 coding agent 做真任务（写
FizzBuzz + 单测 + 跑测试 + 交付），再对结果做代码审查。验收过程中修掉
三个 P0（启动崩溃、bash 环境泄漏、成本口径失真）、两个 P2（交付卡片
不可信输入、会话 id 契约不一致），并落地 reviewer 审查能力的最小闭环
（专家包 + 激活通道 + 只读 CLI + 历史 bug 回放评测）。

评测同时发现并修复了审计链的两个并发/持久化缺陷（append 无锁、
锚点非原子写），以及一条 `list()` 与 `_transcript()` 判定口径不一致的
启动崩溃回归。

## 2. 问题与动机

| # | 问题 | 影响 | 来源 |
|---|---|---|---|
| 1 | `.sessions/audit.jsonl` 被会话扫描 `*.jsonl` 当成 transcript | **服务重启即崩**（只要跑过一次工具） | 真机验收 |
| 2 | bash 工具把 `PYTHONHOME`（uv venv）与 harness 注入的 `PYTHONPATH` 漏给用户命令 | 用户机器上的 Python 一 import 就崩；agent 白跑 7 轮 | 真机验收 |
| 3 | 成本按 craft 档假想价 $15/$75 记账，且不计缓存折扣 | 一次 FizzBuzz 显示 $1.44（实际约 $0.006），误导用户 | 真机验收 |
| 4 | 交付卡片的 `[files]` 解析信任任意工具输出，URL 未做 scheme 白名单 | `javascript:` 可进 `<a href>`（Chrome 下被 target=_blank 挡住，属“靠巧合安全”） | 代码审查 |
| 5 | `create()` 允许非 `sess_<数字>` id，`list()` 只认 `sess_*`；两处判定不一致 | 自己建的对象自己看不见；残留 `sess_abc.jsonl` 会让 `list()` 崩 | 代码审查 + reviewer 评测 |
| 6 | `AuditChain.append` 无锁、锚点非原子写 | 并发 turn 会重复 seq/分叉链；锚点截断让 verify 抛异常 | reviewer 评测 |

## 3. 变更内容

### 3.1 启动与会话存储

- `JsonlSessionStore.list()` 只收 `sess_*.jsonl`（跳过 audit 等邻居），
  新增 `_is_session_id()` 单一判定，`list()`/`_transcript()` 共用；
- `_transcript()` 只收 `sess_<ASCII 数字>`，读写删三个方向同口径；
- 回归测试：`test_list_ignores_other_evidence_files`、
  `test_non_session_namespace_rejected`、`test_list_skips_stems_that_are_not_session_ids`。

### 3.2 bash 环境隔离

- `_clean_env()` 剥掉 `PYTHONHOME`（仅当等于 `sys.base_prefix`，即 uv shim
  注入的那份），并把 `PYTHONPATH` 中等于 harness 根的**绝对路径**条目去掉；
- bash 工具描述补充：“stdout/stderr 已合并，跑测试不用 `2>&1`；带
  `; | & > <` 元字符的命令走审批”；
- 测试：`test_interpreter_env_vars_do_not_leak`、
  `test_user_pythonhome_is_preserved`。

### 3.3 成本口径

- 新增 `ModelPricing` / `MODEL_PRICING`（deepseek-flash 与 deepseek-chat
  同价；v4-pro 另一档），`CostTracker` 记 `model_id` + `cache_hit_tokens`，
  有已知计价按模型算，未知模型退回档位教学价并标 `estimate=false`；
- `ModelRouter.call` 记录实际模型与 `prompt_cache_hit_tokens`；
- `summary()` 增加 `model / cacheHitTokens / estimate / pricingNote`；
- 前端顶栏 `累计 $` → `累计 ≈$`，title 附模型、命中量、峰值价说明。

### 3.4 交付卡片（present_files）

- 新增 `public/src/files-cards.js` 纯函数：数组形状校验、path 非空、
  只放行 http(s)、50 条封顶、主交付唯一；
- `store.js` 改用该解析器；`components.js` 的 `<a :href>` 不再直接信
  `card.path`；
- 冒烟脚本新增 0b 段 8 条纯函数断言。

### 3.5 reviewer 审查能力（MVP）

- `reviewer` 专家包（本地 `.workbuddy/experts/`）：只读人设 + 四条准则
  + 摘除 `bash/fs_write/fs_edit/memory_write` + 技能束 `code-review`；
- `expertId` 通道：`SidecarShell.new_session(expert_id)` →
  `WebApp.action(..., expert_id)` → `POST /api/sessions {expertId}`；
- `scripts/review.py`：git diff → 只读注册表 → `run_agent` → 结论；
  步数用尽时用已读上下文强制定稿；`--config-dir` 支持 worktree 评测；
- `scripts/eval_review.py`：4 个历史 bug 的 worktree 回放评测。

### 3.6 审计链

- `AuditChain.append` 用 `RLock` 包住「读链尾→算 hash→写日志→写锚点」；
- `_write_head` 改 `mkstemp + os.replace` 原子写；verify 对坏锚点返回
  「锚点文件损坏」而不是抛异常；`entries`/`verify` 同锁；
- 测试：4 线程并发 40 条仍连续、锚点原子/坏 JSON。

## 4. 验证证据

| 项 | 结果 |
|---|---|
| 全量单测 | **874 条 OK（2 skip）** |
| 真机编码任务 | FizzBuzz 12 轮完成，5 tests OK；独立复跑通过 |
| 审批拒绝路径 | 两条 bash 被拒 → `permission_blocked` → 模型如实汇报 |
| close → resume | 30 条历史完整，上下文问答正确 |
| 重启（audit 存在） | 6 个会话全部恢复 |
| 成本实测 | 4,304/2 tokens → `deepseek-flash · 0.001294`（旧口径 0.064710） |
| 交付卡片浏览器复验 | 24 项断言全过（真 payload + 合成三卡） |
| reviewer 评测 | 历史 bug 回放 **2/4** 召回；4 次约 $0.16、12 分钟；另抓到上述审计链缺陷 |
| 评测脚本自检 | `--score-only` 已修正首轮关键词过松导致的误判 |

## 5. 风险与缓解

| 风险 | 级别 | 缓解 |
|---|---|---|
| 成本是“峰值价估算”，峰谷价差未动态计算 | 中 | UI 标 `≈$` + `pricingNote`；一个档位只按最后使用的模型计价 |
| reviewer 只读保障只在 CLI 路径成立：sidecar 的专家 `tools_config` 未接线 | 高 | 暂不暴露 UI 入口；下一步接 `select_tools` 到会话运行时 |
| reviewer 集成类 bug 召回弱（seeded 0/2） | 中 | 保持 CLI“第二双眼”定位，不进 UI；先加集成检查清单并重跑失败 case |
| 审计跨进程写仍未防（教学边界） | 低 | 文档已声明；本批只解决同进程并发与锚点原子性 |
| `expertId` 新参数 | 低 | 空串不传字段，老行为零变化；有单测 |
| 成本数字显著变小，用户可能误以为不计费 | 低 | 顶栏 `≈$` + title 说明“按官方峰值价估算” |
| reviewer 配置在 `.workbuddy/`（ignore） | 中 | 换机器需带 `reviewer.json` + `code-review` 技能；CLI 缺配置报人话退出 |
| `TEST_REPORT-2026-10-26.md` 未跟踪 | 低 | 由用户决定入库或删除 |

## 6. 回滚方案

- 全部为独立提交，可逐笔 `git revert`；无数据库迁移、无会话数据删除、
  无协议破坏（`expertId` 可选；JSONL/审计文件格式不变）；
- 若需整批回滚：`git revert --no-commit e9b6dcb..HEAD` 后统一提交；
- 数据兼容：旧 `.sessions/` 可直接打开；`list()` 现在跳过非 `sess_<数字>`
  文件（旧行为是崩溃），不会丢会话数据。

## 7. 未包含 / 后续

- 专家 `tools_config` 接入 sidecar 会话运行时（网页 reviewer 只读的前提）；
- reviewer 集成检查清单 + 失败 case 重跑（目标 ≥3/4）；
- files-cards 的 5 条边界（首个 `[files]` 标记、note 多行、summary 计数、
  `isUrl+exists` 矛盾、暗色主交付凹陷）；
- audit 的 O(n²) 链尾读取、`call_id` 落链；
- `test_cmd` / `auto_test` / 外部裁判 / CI 前端冒烟；
- 成本峰谷动态计价。

## 8. 评审检查清单

- [ ] 启动路径：`.sessions/` 同时存在 `audit.jsonl`、`sess_abc.jsonl`、
      正常会话时，服务能起且列表正确；
- [ ] `bash` 在用户机器上跑 `python -m unittest` 不再受 `PYTHONHOME` 影响；
- [ ] 成本数字与官方价数量级一致（≈$0.00x，而非 $1.4x）；
- [ ] 交付卡片对 `javascript:`/非 http(s) 不产生可点链接；
- [ ] `POST /api/sessions {expertId}` 向后兼容（不带该字段行为不变）；
- [ ] 审计链并发追加 40 条后 `verify()` 通过；
- [ ] 全量 `python -X utf8 -m unittest discover -s tests` 为 874 OK。

## 附录 A：提交清单（`e9b6dcb..HEAD`）

```
f0db847 present_files 交付卡片：主交付徽标、URL 可点、不可信输入白名单
297ea6a 修重启崩溃：会话扫描只收 sess_*.jsonl
deab242 修 bash 环境泄漏：剥离 PYTHONHOME 与 harness 注入的 PYTHONPATH
b29d667 专家激活通道：create 透传 expertId（reviewer MVP 第一步）
4c8b39f docs：NEXT_SESSION/README 跟上（859 测试；审查修复与专家通道）
44692e3 reviewer CLI：diff → 只读审查 → 强制定稿
223eadd 修 list() 与 _transcript 判定不一致（reviewer 首跑抓到）
cd86f3e docs：记录 reviewer CLI 首跑与抓到的 list 崩溃（867 测试）
2d291a2 收两条审查 P3：环境变量判定收紧
da22d1a 成本按实际模型计价：缓存命中/未命中分开，顶栏改 ≈$
975f524 reviewer 评测：worktree 回放历史 bug，2/4 召回
8c0ac16 修审计链并发写与锚点原子性（reviewer 评测抓到）
b268239 docs：记录 reviewer 评测（2/4）与审计链并发修复（874 测试）
```

## 附录 B：如何转成 GitHub PR

当前 `origin/master` 落后本地 25 个提交（`00fd1dd`），且本会话没有可用的
GitHub token / `gh`。若要开 PR：

```bash
# 1) 推送本会话的基点（s24 收官）与变更分支
git push origin e9b6dcb:refs/heads/session-base-s24
git push origin HEAD:refs/heads/change/2026-11-08-session

# 2) 打开 compare 页，base 选 session-base-s24，head 选 change/2026-11-08-session
#    https://github.com/wyw3595/wywd-harness/compare/session-base-s24...change/2026-11-08-session
```

注意：直接以 `master` 为 base 会把 `e9b6dcb` 之前的 s20–s24 提交一起算进去
（`origin/master` 还停在 `00fd1dd`）。
