/* 数据层：一个模块单例 store（state + 动作 + 事件流订阅）。

   为什么不需要 Pinia/Vuex：ES 模块的求值是单例——本文件顶层的 reactive()
   只跑一次，所有 import 它的组件拿到的是同一个对象。这就是官方文档里
   "简单场景用 reactive + 模块导出即可"的形态，够这个项目用一辈子。

   数据所有权（对应 C 方案的三大机制）：
     - 历史：GET /api/sessions/<sid>/messages 直读证据文件，谁都不缓存副本。
       state.messages 只是"当前视图正在显示的那一份"，切会话就整体换掉。
     - 直播：state.live 是纯瞬态——turn 开始清空、切会话清空，绝不落盘。
       历史永远以 messages API 为准（写进边界的教学取舍）。
     - 通知：state.notice 是"这一轮的结局"（failed / max_steps），也不是
       transcript 的一部分——status 没有落账，F5 后它就没了。这是已知边界，
       要持久化得让 session 记录带上它（归后端那一步）。

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
  posting: false,     // 正在等一轮 turn 结束
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

// 能不能发消息：一个 computed 吃掉 vanilla 版散落各处的 setInputEnabled
export const canSend = computed(
  () => Boolean(state.sid) && !state.posting && !state.chatError && !state.locked
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

export async function refreshSessions() {
  try {
    const data = await api.listSessions();
    // 壳把 sidecar 的报错翻译成 {"error": 人话} 直通了——不接住的话
    // data.sessions 是 undefined，界面会显示"无会话"，那是撒谎
    if (data && data.error) { state.listError = data.error; return; }
    state.sessions = (data.sessions || []).map(normalizeRow);
    state.listError = "";
  } catch (err) {
    state.listError = "后端没起来（127.0.0.1:8765）";
  }
}

/** 拉 sidecar 状态（成本表也在里面）。includeLogs 只有抽屉开着才传 true——
    日志是几 KB 的文本，闲着就拉是白花带宽。

    失败不弹错：这是旁路观测，不是主链路；拉不到就在面板上留一行说明。 */
export async function refreshStatus(includeLogs = false) {
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

export async function send(text) {
  const body = String(text || "").trim();
  if (!body || !canSend.value) return;

  state.posting = true;
  state.live = [];
  state.notice = null;
  seenApprovals.clear();

  try {
    const res = await api.sendMessage(state.sid, body);
    finishPosting();
    if (res && res.error) {
      // 并发被拒 / 会话已关：后端回的是人话，摆到眼前而不是一闪而过的 toast
      state.notice = { level: "error", text: res.error };
    } else {
      noteOutcome(res);
      refreshSessions();
      // 每轮 token 会让成本表变，顺手刷；抽屉开着就一起把日志带了
      refreshStatus(ui.drawerOpen);
    }
    // turn 结束：以历史为准重拉全量渲染（含本次新对话）
    const data = await api.fetchMessages(state.sid);
    if (data && !data.error) state.messages = data.messages || [];
  } catch (err) {
    finishPosting();
    state.notice = { level: "error", text: "后端没响应" };
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
  const text = status === "failed"
    ? (res && res.output) || "本轮失败"
    : "达到步数上限：模型没在限定轮数内给出最终回答，本轮被截断";
  state.notice = { level: "warn", text };
  pushLive("warn", "⚠︎ " + text);            // U+26A0 + U+FE0E：强制文字形态
}

// ═══════════════════════════════════════════════════════════
// 事件流：一条 app 级 SSE 长连（替代 500ms 轮询）
// ═══════════════════════════════════════════════════════════

let stream = null;
const seenApprovals = new Set();   // ticket → 已弹过（重连可能重放同一帧）
let liveSeq = 0;                   // 直播行的稳定 key（v-for 需要）

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

function pushLive(kind, text, name = "") {
  state.live.push({ id: ++liveSeq, kind, name, text });
  // 上限：事件环 500 条管的是服务端内存，这一条管的是浏览器内存
  if (state.live.length > LIVE_MAX) {
    state.live.splice(0, state.live.length - LIVE_MAX);
  }
}

function handleEvent(ev) {
  // 事件环是常驻且跨会话的，直播只关心"当前这一轮"。不在 turn 里就不画：
  // ① 多 tab 时不会把别人那轮的卡片画到自己这儿；
  // ② 顺手免疫"连上就把旧事件整批重放"（服务端还有 after=latest 兜底）。
  // 代价是 turn 末尾那一两帧有极小概率赶在 posting 落旗之后到达、丢一行
  // 直播——直播本就是补充材料，历史才是真相（写进边界的教学取舍）。
  if (!state.posting) return;

  if (ev.event === "round_start") {
    pushLive("round", "第 " + ((ev.step || 0) + 1) + " 轮");
  } else if (ev.event === "model_reply") {
    if (ev.kind === "tool_calls") {
      pushLive("model", "决定调用：" + ((ev.tool_names || []).join("、") || "?"));
    } else {
      pushLive("model", "给出最终回答");
    }
  } else if (ev.event === "tool_start") {
    let args = "";
    try { args = JSON.stringify(ev.arguments); } catch (err) { args = ""; }
    if (args && args.length > 120) args = args.slice(0, 120) + "…";
    pushLive("call", args, ev.name || "tool");
  } else if (ev.event === "tool_end") {
    let out = String(ev.content == null ? "" : ev.content);
    if (out.length > 240) out = out.slice(0, 240) + "…";
    pushLive("out", out);
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

// ═══════════════════════════════════════════════════════════
// 审批
// ═══════════════════════════════════════════════════════════

export async function answerApproval(approved) {
  if (!approval.ticket || approval.submitting) return;
  const ticket = approval.ticket;
  approval.submitting = true;     // 点了立刻锁按钮：等 turn 收尾
  try {
    await api.respondApproval(ticket, approved);
  } catch (err) { /* 回执失败：等待超时 fail-closed */ }
  // 不立刻关卡——先给"已提交"的反馈，turn 结束才整体消失
  setTimeout(() => { if (approval.ticket === ticket) approval.ticket = null; }, 1200);
}

export function dismissApproval() {
  approval.ticket = null;
}

// ═══════════════════════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════════════════════

let listTimer = null;

export function start() {
  refreshSessions();
  refreshStatus();
  listTimer = setInterval(refreshSessions, LIST_INTERVAL);
  startEventStream();

  // F5 / 重开 tab：把上次的会话视图拉回来（只读 GET，closed 也能看）
  let last = null;
  try { last = localStorage.getItem(LS_CURRENT); } catch (err) { /* 忽略 */ }
  if (last) openSession(last);
}
