/* 数据层：一个模块单例 store（state + 动作 + 事件流订阅）。

   为什么不需要 Pinia/Vuex：ES 模块的求值是单例——本文件顶层的 reactive()
   只跑一次，所有 import 它的组件拿到的是同一个对象。这就是官方文档里
   "简单场景用 reactive + 模块导出即可"的形态，够这个项目用一辈子。

   数据所有权（对应 C 方案的三大机制）：
     - 历史：GET /api/sessions/<sid>/messages 直读证据文件，谁都不缓存副本。
       state.messages 只是"当前视图正在显示的那一份"，切会话就整体换掉。
     - 直播：state.live 是纯瞬态——turn 开始清空、切会话清空，绝不落盘。
       历史永远以 messages API 为准（写进边界的教学取舍）。
     - 通知：state.notice 是"这一轮的结局"（failed / max_steps），不是
       transcript 的一部分——结局不落账。但事件环在**服务端内存**里跨 F5
       活着，打开会话时按 sessionId 从环里把最近一轮的结局重建回来
       （restoreNotice）；环转出去（500 条上限）就算了。真持久化得让
       session 记录带上它（归后端那一步）。

   事件流：一条 app 级的 SSE 长连，替代了早先"发消息期间每 500ms 轮询
   /api/events"。游标由浏览器管（Last-Event-ID），断线重连也是内建的。
*/

import { computed, reactive } from "vue";
import * as api from "./api.js";
import { ui } from "./ui.js";

const LS_CURRENT = "wywd_current_sid";
const LIST_INTERVAL = 5000;    // 会话清单轮询：live/closed 在别处也会变
const LIVE_MAX = 60;           // 直播行上限：turn 再长也不让 DOM 无界增长

// ═══════════════════════════════════════════════════════════
// 状态
// ═══════════════════════════════════════════════════════════

export const state = reactive({
  sid: null,          // 当前选中的会话（本 tab 的视图指针）
  sessions: [],       // 已整形的清单行（见 normalizeRow）
  messages: [],       // 当前视图的历史
  live: [],           // 直播行：{ id, kind, name, text }
  notice: null,       // { level, text } —— 本轮结局，非历史的一部分
  chatError: "",      // 历史拉取失败（显示在聊天区）
  listError: "",      // 清单拉取失败（显示在侧边栏）
  listLoading: true,  // 清单在途（2026-09-23：区分"还没拉到"和"真的没有"——
                      // turn 阻塞期间清单 GET 排队，不能显示"还没有会话"撒谎）
  posting: false,     // 正在等一轮 turn 结束
  stopping: false,    // 已请求停止、等 turn 收场（按钮变"停止中…"）
  pausing: false,     // 已请求暂停、等 turn 收场（按钮变"暂停中…"）
  locked: false,      // 当前会话被关闭：历史还能看，但不能再发
  streamUp: null,     // SSE 状态：null=还没连过 / true=连着 / false=断了
});

export const approval = reactive({
  ticket: null,       // null = 没有卡
  ruleId: "",
  reason: "",
  submitting: false,  // 已回执、等 turn 收尾：按钮锁住
});

export const toast = reactive({ text: "" });

// 成本账（s08 的模型路由 + 练习 18 的 usage）：一个真源是 sidecar/status
// 的 modelCost 段，另一个是每轮 agent/send 带回来的 usage。
export const cost = reactive({
  rows: null,     // per-tier 表；null = 模型没挂路由器（裸 FakeModel/RealModel）
  usage: {},      // 上一轮的 token 账；离线模型是空字典
});

// 运维面板的数据：sidecar 自述状态 + RingBuffer 日志尾巴。和 cost 分开是
// 因为它们刷新节奏不同——成本每轮变一次，日志只有抽屉开着才值得拉。
export const ops = reactive({
  status: null,   // sidecar/status 原样；{"error": …} 表示拉不到
  logs: "",
  at: 0,          // 上次刷新时间戳（面板上显示"刚更新"）
  loading: false,
});

const costTotal = computed(() => {
  if (!cost.rows) return null;
  // cost 是后端**已经格式化好的字符串**（model_router 的 summary() 输出
  // "0.000000"，终端 /cost 直接拼个 $ 就打印）。前端要求总和，得先转数字：
  // 直接 sum 会变成字符串拼接（"0.000000" + "0.000123" …），
  // 最后 toFixed 当场抛 "total.toFixed is not a function"。
  return cost.rows.reduce((sum, row) => sum + (Number(row.cost) || 0), 0);
});

function fmtInt(n) {
  return n == null ? "?" : Number(n).toLocaleString("en-US");
}

/** 顶栏那行账。离线模型 usage 为空 → 这一节直接不出现（不编造 0）。 */
export const costLine = computed(() => {
  const parts = [];
  const u = cost.usage || {};
  if (u.prompt_tokens != null || u.completion_tokens != null) {
    parts.push("本问 输入 " + fmtInt(u.prompt_tokens) +
               " / 输出 " + fmtInt(u.completion_tokens));
  }
  const total = costTotal.value;
  if (total != null) parts.push("累计 $" + total.toFixed(6));
  return parts.join("   ·   ");
});

/** 悬停看层级明细（per-tier 表在顶栏塞不下，先做成 title） */
export const costTitle = computed(() => {
  if (!cost.rows || !cost.rows.length) return "";
  return cost.rows.map((row) =>
    row.tier + ": " + row.calls + " 次 · 输入 " + fmtInt(row.promptTokens) +
    " / 输出 " + fmtInt(row.completionTokens) + " · $" + row.cost).join("\n");
});

// 能不能发消息：一个 computed 吃掉 vanilla 版散落各处的 setInputEnabled。
// 注意**不再要求 state.sid**：会话改成懒建之后，"刚打开还没有任何会话就
// 直接打字"是合法操作，send() 会先替你把会话开出来（见 ensureSession）。
export const canSend = computed(
  () => !state.posting && !state.chatError && !state.locked
);

// ═══════════════════════════════════════════════════════════
// 小工具
// ═══════════════════════════════════════════════════════════

let toastTimer = null;

/** 一闪而过的提示。导出给组件用（复制成功、抽屉刷新失败…）——提示是
    跨组件的公共设施，不该每个组件自己塞一个。 */
export function notify(text) {
  toast.text = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.text = ""; }, 2500);
}

/** 把后端原始行整形好，视图只负责画——整形是数据层的活。 */
function normalizeRow(raw) {
  if (raw && raw.error) return { id: "", error: raw.error };
  const id = raw.id || raw.sessionId || "?";
  return {
    id,
    title: raw.title && raw.title !== id ? raw.title : id,
    live: raw.live === true,
    status: raw.status || "?",
    generation: raw.runtimeGeneration || 1,
    count: raw.messages || 0,
    cwd: raw.cwd || "",          // 侧栏底部的"项目"标识
  };
}

/** 按 id 查清单行——vanilla 版靠 querySelector 反查 live，那是拿视图当数据源。 */
export function sessionById(sid) {
  return state.sessions.find((row) => row.id === sid) || null;
}

// ═══════════════════════════════════════════════════════════
// 动作：清单 / 生命周期
// ═══════════════════════════════════════════════════════════

let listing = false;    // 防重入：上一趟清单还没回来就不发新的一趟
let listRetry = false;  // 被跳过的那趟是"数据可能已过期"的信号，回来后补一刷

export async function refreshSessions() {
  // **2026-09-23 修（审批回执发不出去的真正元凶）**：清单 GET 走 RPC，
  // turn 期间被 _rpc_lock 堵死。老代码 setInterval 每 5 秒**照发不误**，
  // 每条在途 GET 各占一条浏览器连接（HTTP/1.1 同源上限 6：SSE 1 + 发消息
  // POST 1 + 堆 4 条就满）。池一满，用户按"允许"的回执 POST 在浏览器里
  // **排队发不出去**（fetch 不报错、一直 pending）→ 票 60 秒超时 →
  // permission_blocked。前 1~2 次审批能过、三四次之后全失灵，就是池
  // 被堆满的时间点。防重入后任何时刻最多 1 条清单 GET 占连接，回执
  // 永远有路可走。
  if (listing) { listRetry = true; return; }
  listing = true;
  state.listLoading = true;
  try {
    const data = await api.listSessions();
    // 壳把 sidecar 的报错翻译成 {"error": 人话} 直通了——不接住的话
    // data.sessions 是 undefined，界面会显示"无会话"，那是撒谎
    if (data && data.error) { state.listError = data.error; return; }
    state.sessions = (data.sessions || []).map(normalizeRow);
    state.listError = "";
  } catch (err) {
    state.listError = "后端没起来（127.0.0.1:8765）";
  } finally {
    listing = false;
    state.listLoading = false;
    // 在途期间有人想刷（手动操作触发的）→ 现在补上，最多晚一趟、不丢
    if (listRetry) { listRetry = false; refreshSessions(); }
  }
}

/** 拉 sidecar 状态（成本表也在里面）。includeLogs 只有抽屉开着才传 true——
    日志是几 KB 的文本，闲着就拉是白花带宽。

    失败不弹错：这是旁路观测，不是主链路；拉不到就在面板上留一行说明。 */
let statusing = false;  // 防重入：与 refreshSessions 同理（抽屉开着时每 3 秒
                        // 一发 status+logs，turn 阻塞期间不防重入会把浏览器
                        // 连接池更快堆满——2026-09-23 审批回执发不出去的共犯）

export async function refreshStatus(includeLogs = false) {
  if (statusing) return;
  statusing = true;
  try {
    const data = await api.fetchStatus();
    // 用 Array.isArray 而不是 `|| null`：错误响应 {"error": …} 也是 truthy，
    // 直接赋值会让 costLine 拿着一个不是数组的东西去算总和
    cost.rows = Array.isArray(data.modelCost) ? data.modelCost : null;
    ops.status = data;
    if (includeLogs) ops.logs = (await api.fetchLogs()).logs || "";
    ops.at = Date.now();
  } catch (err) {
    ops.status = { error: "取不到 sidecar 状态（后端没起来？）" };
  } finally {
    statusing = false;
  }
}

export async function createSession() {
  try {
    const res = await api.sessionAction("create");
    if (res && res.ok) {
      await openSession(res.sessionId);
      refreshSessions();
    } else {
      notify((res && res.detail) || "新建失败");
    }
  } catch (err) {
    notify("后端没响应");
  }
}

/** 行操作：close / resume / forget。close 当前会话时锁住输入框（记录还在）。 */
export async function runAction(op, sid) {
  try {
    const res = await api.sessionAction(op, sid);
    if (res && res.ok) {
      notify(res.detail || "完成");
      if (op === "close" && sid === state.sid) state.locked = true;
    } else {
      notify((res && res.detail) || "操作失败");
    }
  } catch (err) {
    notify("后端没响应");
  }
  refreshSessions();
}

// ═══════════════════════════════════════════════════════════
// 动作：打开会话（换 sid + 拉历史 + 全量换掉视图）
// ═══════════════════════════════════════════════════════════

export async function openSession(sid) {
  state.sid = sid;
  state.locked = false;
  state.chatError = "";
  state.notice = null;
  state.messages = [];
  state.live = [];
  approval.ticket = null;
  seenApprovals.clear();
  try { localStorage.setItem(LS_CURRENT, sid); } catch (err) { /* 隐私模式：忽略 */ }

  try {
    const data = await api.fetchMessages(sid);
    if (data && data.error) { state.chatError = data.error; return; }
    state.messages = (data && data.messages) || [];
  } catch (err) {
    state.chatError = "拉取历史失败";
  }
  await restoreNotice(sid);
}

/** 打开会话时从事件环重建"上一轮结局"（并发第 3 步）。

    结局（failed / max_steps 的人话）不进 transcript，F5 后历史里没
    痕迹、notice 也随页面没了——用户看到"问了一句，什么都没发生"。
    修复：事件环在服务端内存里跨 F5 活着，最后一轮的 turn_end 还在环
    里——按 sessionId 找最近一条，非 completed 就把 notice 立回去。
    环转出去（500 条上限）就算了：notice 本就是补充材料（教学取舍）。
*/
async function restoreNotice(sid) {
  let res;
  try {
    res = await api.fetchEvents();
  } catch (err) {
    return;                                   // 环拿不到就算了
  }
  const events = (res && res.events) || [];
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.event !== "turn_end" || e.sessionId !== sid) continue;
    if (e.status && e.status !== "completed") {
      state.notice = e.error
        ? { level: "error", text: e.error }
        : { level: "warn", text: outcomeText(e.status, e.output) };
    }
    return;   // 只看最近一条：它 completed 就别拿更早的结局吓人
  }
}

/** 点清单：live 的直接打开；closed 的先复活（resume）再打开。 */
export async function selectSession(sid) {
  const row = sessionById(sid);
  if (row === null || row.live) { await openSession(sid); return; }
  try {
    const res = await api.sessionAction("resume", sid);
    if (res && res.ok) {
      await openSession(sid);
      refreshSessions();          // live 标志变了，立刻刷列表
    } else {
      notify((res && res.detail) || "复活失败");
    }
  } catch (err) {
    notify("后端没响应");
  }
}

// ═══════════════════════════════════════════════════════════
// 动作：发一轮
// ═══════════════════════════════════════════════════════════

/** 懒建的兜底口：还没有会话就开一个（复用 ＋ 按钮那条路）。
    成功 = state.sid 变非空 —— createSession 内部已经 openSession 过。 */
async function ensureSession() {
  if (state.sid) return true;
  await createSession();
  return Boolean(state.sid);
}

/** 发消息前确认会话有 live 运行时，没有就先 resume。

    为什么需要：空闲回收会在后台把闲置的会话 close 掉（运行时释放，记录还在）。
    这时用户什么都没做错，却会在界面上吃到一句"会话已关闭"—— 那是回收的
    后果，不该由用户承担。resume 是幂等且廉价的（造一代新运行时），
    所以发送路径上顺手做掉，"点一下就能继续"变成"根本不用点"。

    口径与 selectSession 里那条"closed 先 resume 再打开"一致，只是把它
    挪到了发送路径。注意：别的 tab 主动关掉的会话也会被这里复活——
    可接受（不破坏任何数据），换来的是回收对用户完全无感。
    当前会话被**本 tab** 关掉时 canSend 已经是 false，send 根本不会走到这。
*/
async function ensureLive(sid) {
  const row = sessionById(sid);
  if (row === null || row.live) return true;   // 清单里没有 / 本来就活着
  try {
    const res = await api.sessionAction("resume", sid);
    if (!res || !res.ok) {
      notify((res && res.detail) || "复活会话失败");
      return false;
    }
    refreshSessions();
    return true;
  } catch (err) {
    notify("后端没响应");
    return false;
  }
}

export async function send(text) {
  const body = String(text || "").trim();
  if (!body || !canSend.value) return;

  // 懒建：启动时不再预建会话，所以"第一句话"要自己把会话开出来。
  // 放在 posting 之前：建会话失败就直接返回，不留一个卡住的 posting 旗。
  if (!(await ensureSession())) return;
  // 后台空闲回收可能已经把它 close 了：先复活，别让用户吃"会话已关闭"。
  if (!(await ensureLive(state.sid))) return;

  state.posting = true;
  state.stopping = false;
  state.pausing = false;
  state.live = [];
  state.notice = null;
  seenApprovals.clear();

  let res;
  try {
    res = await api.sendMessage(state.sid, body);
  } catch (err) {
    finishPosting();
    state.notice = { level: "error", text: "后端没响应" };
    return;
  }

  // 提交被拒（并发被拒 / 会话已关）：后端回的是人话，摆到眼前而不是
  // 一闪而过的 toast。没有 turn 在跑，posting 当场落旗。
  if (res && res.error) {
    finishPosting();
    state.notice = { level: "error", text: res.error };
    return;
  }

  // 同步模式（WYWD_SYNC_SEND=1 / 旧版后端）：响应本身就是结局，老打法
  // 原样走完。
  if (res && res.output != null) {
    finishPosting();
    noteOutcome(res);
    refreshSessions();
    refreshStatus(ui.drawerOpen);
    // turn 结束：以历史为准重拉全量渲染（含本次新对话）
    const data = await api.fetchMessages(state.sid);
    if (data && !data.error) state.messages = data.messages || [];
    return;
  }

  // 202 提交即返回（并发第 3 步）：结局由 SSE 的 turn_end 事件送达
  // （handleTurnEnd 落旗 + 重拉历史 + 刷清单/状态）。turnId 记下来做
  // 配对——上一轮的晚到结局灌不进这一轮的等待（A/B 串台防的是它）。
  activeTurnId = (res && res.turnId) || null;
  if (!state.streamUp) {
    // 流没连上（断线重连窗口）：turn 已提交，结局可能延迟——诚实喊一声。
    // EventSource 会自动重连续传（Last-Event-ID），多数情况照常收到。
    state.notice = { level: "warn",
      text: "事件流没连上（正在重连）——结局可能延迟到达，长时间没动静请刷新页面。" };
  }
}

/** 请求停止在跑的 turn（2026-09-27）。

    受理即回——turn 在后端 run_agent 的循环检查点收场（最迟等完当前的
    模型调用 / 工具 / 审批），真正收尾由 handleTurnEnd 做：stopping 旗
    在那里落。这里只负责把按钮变成"停止中…"并诚实转述被拒。 */
export async function stopTurn() {
  if (!state.posting || state.stopping) return;
  state.stopping = true;
  try {
    const res = await api.stopTurn(state.sid);
    if (res && res.error) {
      state.stopping = false;
      state.notice = { level: "error", text: res.error };
    }
  } catch (err) {
    state.stopping = false;
    state.notice = { level: "error", text: "停止请求没送到后端" };
  }
}

/** 请求暂停在跑的 turn（2026-09-27）：与 stopTurn 同一条纪律——受理
    即回，turn 在后端循环检查点收场，handleTurnEnd 落 pausing 旗。 */
export async function pauseTurn() {
  if (!state.posting || state.pausing) return;
  state.pausing = true;
  try {
    const res = await api.pauseTurn(state.sid);
    if (res && res.error) {
      state.pausing = false;
      state.notice = { level: "error", text: res.error };
    }
  } catch (err) {
    state.pausing = false;
    state.notice = { level: "error", text: "暂停请求没送到后端" };
  }
}

function finishPosting() {
  state.posting = false;
  // 直播**不在这里清**：刚跑完的一轮正是最该回看的东西。清掉之后只剩
  // 历史里那几条 role=tool 的结果，"第几轮、模型决定了什么"全没了。
  // 清空时机改成"下一次发送"和"切换会话"。
}

/** 把 agent/send 的 status 摆到台面上。

    status 是 RunResult 的一等公民（练习 16），但前端一直只看了 error
    字段——于是 status="failed" 的 turn 在界面上表现为"用户问了一句、
    什么都没发生"（失败时 transcript 里只有 user 那条，模型那句人话在
    output 里，而 output 不落 transcript，重拉历史拿不到）。
*/
function noteOutcome(res) {
  // 每轮 token 账进直播（终端那边一直有"（本问 tokens：输入 X / 输出 Y）"）。
  // 离线模型 usage 是空字典 → 这一行不出现，不编造 0。
  cost.usage = (res && res.usage) || {};
  const u = cost.usage;
  if (u.prompt_tokens != null || u.completion_tokens != null) {
    pushLive("cost", "本问 tokens：" + fmtInt(u.prompt_tokens) +
             " 输入 / " + fmtInt(u.completion_tokens) + " 输出");
  }

  const status = (res && res.status) || "completed";
  if (status === "completed") {
    pushLive("end", "✓ 本轮完成");           // 单色 glyph，不用彩色 emoji
    return;
  }
  const text = outcomeText(status, res && res.output);
  state.notice = { level: "warn", text };
  pushLive("warn", "⚠︎ " + text);            // U+26A0 + U+FE0E：强制文字形态
}

/** 非 completed 结局的人话。output 优先（后端给的 output 已经把该说的
    说清了——用了几轮 / 任务没做完 / 直接说「继续」就能接着做）；兜底
    文案保留「继续」这个暗示：对用户来说那是唯一能据此行动的信息。 */
function outcomeText(status, output) {
  return output || (status === "failed"
    ? "本轮失败"
    : "达到步数上限，本轮暂停（任务还没做完）；说「继续」可以接着做。");
}

// ═══════════════════════════════════════════════════════════
// 事件流：一条 app 级 SSE 长连（替代 500ms 轮询）
// ═══════════════════════════════════════════════════════════

let stream = null;
const seenApprovals = new Set();   // ticket → 已弹过（重连可能重放同一帧）
let liveSeq = 0;                   // 直播行的稳定 key（v-for 需要）
let activeTurnId = null;           // 本 tab 在途 turn 的 id（202 响应给的）——
                                   // turn_end 按它配对，晚到的旧结局灌不进来

export function startEventStream() {
  if (stream) return;
  stream = api.openEventStream();

  stream.onopen = () => { state.streamUp = true; };
  stream.onerror = () => {
    // 浏览器自己会重连（readyState 回到 CONNECTING）并带上 Last-Event-ID，
    // 这里只更新顶栏那颗灯——重试退避和游标都不用我们操心。
    state.streamUp = false;
  };
  stream.onmessage = (e) => {
    let ev = null;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    handleEvent(ev);
  };
}

function pushLive(kind, text, name = "", extra = null) {
  // extra：个别行要带额外字段（思考行的 summary/lines 给折叠块用）。
  // 用展开而不是给每行都塞两个空键——绝大多数行不需要它们。
  const row = { id: ++liveSeq, kind, name, text };
  if (extra) Object.assign(row, extra);
  state.live.push(row);
  // 上限：事件环 500 条管的是服务端内存，这一条管的是浏览器内存
  if (state.live.length > LIVE_MAX) {
    state.live.splice(0, state.live.length - LIVE_MAX);
  }
}

function handleEvent(ev) {
  // turn_end 是收尾事件（并发第 3 步）：posting 的结局全靠它，先于直播
  // 闸门处理——它自带 sessionId，在 handleTurnEnd 里分流。
  if (ev.event === "turn_end") { handleTurnEnd(ev); return; }

  // 直播只画"本 tab 视图会话、当前正在跑的这一轮"：
  // - sessionId 过滤（第 3 步起中途事件也带）：别的会话 / 别的 tab 的
  //   turn 画不进来——旧版只有 posting 旗，两个 tab 同时跑会串台；
  // - posting 闸：不在 turn 里就不画，顺手免疫"连上就把旧事件整批
  //   重放"（服务端还有 after=latest 兜底）。
  // 代价是 turn 末尾那一两帧有极小概率赶在 posting 落旗之后到达、丢一行
  // 直播——直播本就是补充材料，历史才是真相（写进边界的教学取舍）。
  if (!state.posting) return;
  if (ev.sessionId && ev.sessionId !== state.sid) return;

  if (ev.event === "round_start") {
    pushLive("round", "第 " + ((ev.step || 0) + 1) + " 轮");
  } else if (ev.event === "model_reply") {
    // 思考模式（s16 之后）：reasoning_content 单独展示——它**不是回答**，
    // 只是"模型刚才在想什么"的痕迹。
    // 2026-09-24：**不再截断**。旧版截 160 字符 + 单行省略（理由是思考链
    // 动辄几千字会把轨迹区撑烂）——但那样"看全文"就永远不可能了。现在改成
    // 折叠块：全文留在行里，撑烂的问题交给**收起**解决（和工具结果同一套
    // 手法），摘要只取首行 90 字符给收起时看。换行也保留：思考是有段落的，
    // 压成一行反而读不出结构。
    if (ev.reasoning) {
      const thought = String(ev.reasoning).trim();
      if (thought) {
        const firstLine = thought.split("\n")[0] || thought;
        const summary = firstLine.length > 90
          ? firstLine.slice(0, 90) + "…" : firstLine;
        pushLive("think", thought, "", {
          summary, lines: thought.split("\n").length,
        });
      }
    }
    if (ev.kind === "tool_calls") {
      pushLive("model", "决定调用：" + ((ev.tool_names || []).join("、") || "?"));
    } else {
      pushLive("model", "给出最终回答");
    }
  } else if (ev.event === "tool_start") {
    let args = "";
    try { args = JSON.stringify(ev.arguments); } catch (err) { args = ""; }
    if (args && args.length > 120) args = args.slice(0, 120) + "…";
    // 命令说明（2026-09-24）：bash 的 arguments 里可能有 explain——那是模型
    // 写给**人**看的一句话（"这条命令干嘛的"）。用户读不懂 shell，所以把它
    // 单独存下来，让直播行能在命令上面先显示这句说明。
    const argObj = (ev.arguments && typeof ev.arguments === "object")
      ? ev.arguments : null;
    const explain = argObj ? String(argObj.explain || "") : "";
    pushLive("call", args, ev.name || "tool", { explain });
  } else if (ev.event === "tool_end") {
    let out = String(ev.content == null ? "" : ev.content);
    // 审批已经出结果了（被拒 / 超时）→ **把还挂着的卡片收掉**。
    // 后端在 `ApprovalBoard.request` 超时后就不等了、直接放行 BLOCKED，
    // 但它**不会告诉前端"这张票作废了"**——于是卡片一直显示"需要批准"，
    // 用户看到的就还是"卡住"（2026-09-23 第二次报的就是这个：工具明明
    // 已经返回 permission_blocked，卡片却还在）。
    if (approval.ticket && out.includes("permission_blocked")) {
      approval.ticket = null;
      approval.submitting = false;
      pushLive("warn", "审批已结束（被拒或超时），这条没执行");
    }
    // 计划工具（plan-and-execute 第 1 步）：计划全文单独成卡——它是要
    // 盯着看的进度，不进 240 字符截断的普通结果行。剥掉运行时包的
    // "工具 plan_write 返回："前缀，卡片只呈现计划本身。
    if (ev.name === "plan_write") {
      const prefix = "工具 plan_write 返回：";
      const text = out.startsWith(prefix) ? out.slice(prefix.length) : out;
      const firstLine = text.split("\n")[0] || text;
      pushLive("plan", text, "计划", {
        summary: firstLine,
        lines: text.split("\n").length,
      });
    } else {
      if (out.length > 240) out = out.slice(0, 240) + "…";
      pushLive("out", out);
    }
  } else if (ev.event === "approval_request") {
    const ticket = ev.request_id;
    if (!seenApprovals.has(ticket)) {
      seenApprovals.add(ticket);
      approval.ticket = ticket;
      approval.ruleId = ev.rule_id || "";
      approval.reason = ev.reason || "";
      approval.submitting = false;
    }
  }
}

/** turn_end 事件 = 本轮结局（并发第 3 步）：posting 的收尾由事件驱动。

    202 之后 POST 响应里没有结局了，收尾三件套全搬到这里：
    落 posting 旗 → 重拉历史（以证据为准）→ 刷清单/状态（成本表随
    usage 变）。结局本身：error（被状态机拒绝 / 内部异常）→ 红字摆到
    台面；否则 noteOutcome——{status, output, usage} 和同步响应同一
    形状，一套代码两条通道。
*/
function handleTurnEnd(ev) {
  // 多 tab / 跨会话：不是本 tab 视图那一轮的不收（壳断连合成的
  // turn_end 也带 sessionId——那是 sidecar 死掉时的诚实收场事件）。
  if (ev.sessionId && ev.sessionId !== state.sid) return;
  if (!state.posting || !activeTurnId) return;
  // turnId 配对：A 的晚到结局灌给 B 的等待这种事，宁可白防不可不防。
  if (ev.turnId && ev.turnId !== activeTurnId) return;
  activeTurnId = null;

  if (ev.error) {
    state.notice = { level: "error", text: ev.error };
    pushLive("warn", "⚠︎ " + ev.error);
  } else if (ev.status === "aborted") {
    // 主动停止（2026-09-27）：不是失败——历史照常落账，说「继续」接着跑。
    state.notice = { level: "warn", text: "本轮已停止；说「继续」可以接着做。" };
    pushLive("warn", "■ 本轮已停止");
    if (ev.output) pushLive("out", ev.output);
  } else if (ev.status === "paused") {
    // 主动暂停（2026-09-27）：与停止共用恢复路径——「继续」接着做。
    state.notice = { level: "warn", text: "本轮已暂停；说「继续」可以接着做。" };
    pushLive("warn", "⏸︎ 本轮已暂停");
    if (ev.output) pushLive("out", ev.output);
  } else {
    noteOutcome(ev);
  }
  state.stopping = false;
  state.pausing = false;
  finishPosting();
  refreshSessions();
  // 每轮 token 会让成本表变，顺手刷；抽屉开着就一起把日志带了
  refreshStatus(ui.drawerOpen);
  // turn 结束：以历史为准重拉全量渲染（含本次新对话）。失败不打断
  // 收尾——历史下次切会话还能拿到。
  api.fetchMessages(state.sid).then((data) => {
    if (data && !data.error) state.messages = data.messages || [];
  }).catch(() => { /* 拉取失败：保持现状，不炸收尾 */ });
}

// ═══════════════════════════════════════════════════════════
// 审批
// ═══════════════════════════════════════════════════════════

export async function answerApproval(approved) {
  if (!approval.ticket || approval.submitting) return;
  const ticket = approval.ticket;
  approval.submitting = true;
  try {
    // **2026-09-23：回执加 10 秒超时。** 以前 fetch 挂多久都不吭声——
    // 连接池被占满时它会永远 pending（浏览器排队不发），票在服务端
    // 60 秒超时，用户全程只看到"按了没反应"。现在至少把"没送到"喊出来。
    const res = await api.respondApproval(ticket, approved,
                                          AbortSignal.timeout(10000));
    // 回执失败**必须说出来**（2026-09-23 修）：以前这里是空 catch，
    // 点了"允许"没任何反应，只能干等超时——现场完全看不出发生了什么。
    if (res && res.ok === false) notify(res.detail || "回执没送到");
  } catch (err) {
    const msg = (err && err.name === "TimeoutError")
      ? "10 秒没送到（连接被占用或后端卡住）"
      : (err && err.message ? err.message : err);
    notify("回执失败：" + msg);
  }
  // 点完**立刻收卡**。旧实现是"等 turn 收尾自己消失"，只靠一个 1.2 秒的
  // 定时器兜底；但只要这期间又来了一个审批（**同一轮里 agent 连调两次 bash
  // 就会**），`approval.ticket === ticket` 永远不成立 → 卡片挂着不收，
  // submitting 还停在 true → 再点被 `if (approval.submitting) return`
  // 直接挡掉。用户看到的"按了没反应"就是这个。
  approval.ticket = null;
  approval.submitting = false;
}

export function dismissApproval() {
  approval.ticket = null;
}

// ═══════════════════════════════════════════════════════════
// 动作：工作区（上传目录）
// ═══════════════════════════════════════════════════════════

/** 工作区是 sidecar 的**边界**（工具闭包 + 权限作用域都绑它）：换掉之后
    下一轮就走新沙箱。已建会话的历史不动——旧历史里的路径在新沙箱里可能
    不存在，所以切换成功后提示"建议新建会话"。 */
export const workspace = reactive({
  info: null,        // {kind, id?, root?}；null = 读不到
  error: "",         // 读不到时的人话（与 info 互斥）
  busy: "",          // 非空 = 正在做某个动作，内容是给用户看的进度短语
});

export async function refreshWorkspace() {
  try {
    // 工作区是**会话级**的：读的是当前这个会话的目录
    const data = await api.fetchWorkspace(state.sid || "");
    const info = (data && data.workspace) || null;
    workspace.info = info;
    workspace.error = info ? "" : "工作区不可读";
  } catch (err) {
    workspace.info = null;
    workspace.error = "后端没响应";
  }
}

/** 四个动作的公共收尾。返回原始 res，让组件决定要不要接着弹手输框。

    browse 的三条出路都在这儿收：成功 / 取消 / 弹不出框降级成手输。
    取消不是错误（不该弹红字），但也**要说一声**——静默的后果是用户分不清
    "点了没反应"和"我取消了"，他只会觉得按钮坏了（真实反馈里出现过）。
*/
function applyWorkspaceResult(res) {
  if (res && res.cancelled) {
    notify("已取消选择，工作区没变");
    refreshWorkspace();          // 进度文案还挂在栏上，刷回后端的事实
    return res;
  }
  if (res && res.fallback === "manual") {
    notify(res.detail || "改成手输路径");
    return res;                  // 组件收到这个再弹手输框
  }
  if (res && res.ok) {
    if (res.workspace) workspace.info = res.workspace;
    notify((res.detail || "工作区已切换") + "，建议新建会话再聊");
  } else {
    notify((res && res.detail) || "切换失败");
    refreshWorkspace();          // 失败就把显示恢复成后端的事实
  }
  return res;
}

/** 跑一个工作区动作。busyText 只是给用户看的进度短语。 */
export async function runWorkspace(action, opts = {}, busyText = "") {
  workspace.busy = busyText;
  try {
    // 换的是**当前会话**的目录（没会话时后端会明确回绝，不静默失败）
    const res = await api.workspaceAction(action, opts, state.sid || "");
    return applyWorkspaceResult(res);
  } catch (err) {
    notify("后端没响应");
    await refreshWorkspace();
    return null;
  } finally {
    workspace.busy = "";
  }
}

/** 上传一个 zip 当工作区。为什么读成 base64：后端要的是 JSON 里的字符串
    （不走 multipart——那要手写边界解析，不值当；见 web_app._upload_zip）。
    FileReader 是唯一能把 File 变成 base64 的浏览器 API。 */
export async function uploadWorkspaceZip(file) {
  if (!file) return null;
  const kb = Math.max(1, Math.round((file.size || 0) / 1024));
  workspace.busy = `上传中… ${kb} KB`;
  let data = "";
  try {
    data = await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || ""));
      fr.onerror = () => reject(new Error("读不出这个文件"));
      fr.readAsDataURL(file);
    });
    // readAsDataURL 给的是 "data:<mime>;base64,<载荷>"，砍掉头
    data = data.slice(data.indexOf(",") + 1);
  } catch (err) {
    workspace.busy = "";
    notify(err.message || "读文件失败");
    return null;
  }
  return runWorkspace("upload", { filename: file.name, data },
                      `上传中… ${kb} KB`);
}

// ═══════════════════════════════════════════════════════════
// 动作：目录选择器（选工作区用）
// ═══════════════════════════════════════════════════════════

/** 目录清单是服务端数据（跟 approval 同一类），所以进 store 而不是组件。
    它是瞬态的——弹层关掉就丢，丢的成本只是重新点一下。 */
export const fsPicker = reactive({
  open: false,
  path: null,        // 当前浏览到的目录；null = 起点态（还没挑盘）
  parent: null,      // 上一级目录；null = 到底了（盘符根）
  roots: [],         // 起点态的入口：盘符（Windows）或 /
  entries: [],       // 当前目录下的子目录
  error: "",
  loading: false,
});

export async function openFsPicker() {
  fsPicker.open = true;
  await loadFsList("");        // 起点态：先挑盘
}

export function closeFsPicker() {
  fsPicker.open = false;
}

/** Windows 的盘符根以 \ 结尾，别再补一个分隔符；其余目录补 / 即可。
    混用分隔符 Windows 也认，交给后端的 resolve 一步归一。 */
function joinPath(dir, name) {
  const sep = /[\\/]$/.test(dir) ? "" : "/";
  return dir + sep + name;
}

async function loadFsList(path) {
  fsPicker.loading = true;
  fsPicker.error = "";
  try {
    const data = await api.fetchFsList(path);
    if (data && data.error) {
      fsPicker.error = data.error;
      return;
    }
    fsPicker.path = data.path || null;
    fsPicker.parent = data.parent || null;
    fsPicker.roots = data.roots || [];
    fsPicker.entries = data.entries || [];
  } catch (err) {
    fsPicker.error = "后端没响应";
  } finally {
    fsPicker.loading = false;
  }
}

export function fsEnterDir(name) {
  if (fsPicker.loading || !fsPicker.path) return;
  loadFsList(joinPath(fsPicker.path, name));
}

export function fsGoUp() {
  if (fsPicker.loading || !fsPicker.parent) return;
  loadFsList(fsPicker.parent);
}

export function fsEnterRoot(root) {
  if (fsPicker.loading) return;
  loadFsList(root);
}

/** 把当前浏览到的目录设为工作区。关弹层在先：切换失败的话，
    工作区面板会自己刷回后端的事实，别让两层 UI 同时挂着。 */
export async function pickFsDir() {
  if (fsPicker.loading || !fsPicker.path) return;
  const path = fsPicker.path;
  closeFsPicker();
  await runWorkspace("open", { path }, "切换中…");
}

// ═══════════════════════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════════════════════

let listTimer = null;

export function start() {
  refreshSessions();
  refreshStatus();
  refreshWorkspace();
  listTimer = setInterval(refreshSessions, LIST_INTERVAL);
  startEventStream();

  // F5 / 重开 tab：把上次的会话视图拉回来（只读 GET，closed 也能看）
  let last = null;
  try { last = localStorage.getItem(LS_CURRENT); } catch (err) { /* 忽略 */ }
  if (last) openSession(last);
}
