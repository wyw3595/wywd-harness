/* 网络层：后端契约的唯一出口（五个端点 + 一条 SSE 长连）。

   为什么单独一个文件：vanilla 版把 fetch 散在 415 行里，谁在什么时候打
   哪个端点要靠全文搜。这里集中成具名函数——组件里看不到任何 URL 字符串，
   后端加端点只改这一处。

   这一层没有业务状态：前六个是请求-响应（返回 Promise），最后一个
   openEventStream 交出一个长连句柄（由调用方负责关）。换框架时这个文件
   可以原样搬走——接缝的价值就在这。
*/

const JSON_HEADERS = { "Content-Type": "application/json" };

async function get(path) {
  const res = await fetch(path);
  return res.json();
}

async function post(path, body, headers = {}, signal = undefined) {
  const res = await fetch(path, {
    method: "POST",
    // X-Wywd-Ui 对**所有** /api POST 生效（2026-09-25）：跨站"简单请求"
    // 发不出自定义头（要发就得先过 CORS 预检，而本服务不答预检），所以
    // 这一个头就是 CSRF 闸——后端守卫中间件全量校验，缺头 403。
    headers: { ...JSON_HEADERS, "X-Wywd-Ui": "1", ...headers },
    body: JSON.stringify(body),
    signal,
  });
  return res.json();
}

function messagesPath(sid) {
  return "/api/sessions/" + encodeURIComponent(sid) + "/messages";
}

// ── 会话清单 / 生命周期操作 ──────────────────────────────

export function listSessions() {
  return get("/api/sessions");
}

/** action ∈ create | close | resume | forget */
export function sessionAction(action, sid = "") {
  return post("/api/sessions", { action, sid });
}

// ── 历史（纯读，不经过 sidecar RPC）──────────────────────

export function fetchMessages(sid) {
  return get(messagesPath(sid));
}

// ── 发一轮（同步等 turn 完成）───────────────────────────

// ── 发一轮（默认 202 提交即返回，结局走 SSE 的 turn_end 事件；
//    WYWD_SYNC_SEND=1 时后端切回同步等结局的老契约）──────

export function sendMessage(sid, message) {
  return post(messagesPath(sid), { message });
}

// ── 事件环一次性读取（GET /api/events）──────────────────

/** 从头翻旧账的入口：打开会话时按 sessionId 找最近一条 turn_end，
    把上一轮结局的 notice 立回去（结局不进 transcript，F5 后只能从
    环里找——环是服务端内存，转没了就算了）。SSE 仍是唯一的推送通道。 */
export function fetchEvents() {
  return get("/api/events?after=0");
}

// ── 审批回执 ────────────────────────────────────────────

export function respondApproval(ticket, approved, signal) {
  return post("/api/approvals/" + encodeURIComponent(ticket), { approved },
              {}, signal);
}

// ── sidecar 自述状态 / 日志（成本表挂在状态里）──────────

export function fetchStatus() {
  return get("/api/status");
}

/** sidecar 的 RingBuffer 日志尾巴。失败也被壳翻译成一行说明文本，
    所以这里拿到的永远是 {"logs": "…"}——不会是个 error 对象。 */
export function fetchLogs() {
  return get("/api/logs");
}

// ── 工作区（让 agent 在哪个目录里干活）───────────────────

/** 读**这个会话**的工作目录（工作区是会话级的，读谁的要说明白）；
    壳不认识 workspace/get 时后端会如实降级为"默认"。 */
export function fetchWorkspace(sid = "") {
  const query = sid ? `?sid=${encodeURIComponent(sid)}` : "";
  return get(`/api/workspace${query}`);
}

/** 工作区操作。action ∈ browse | open | upload | reset。

    X-Wywd-Ui 现在由 post() 统一带（所有 /api POST 都过 CSRF 闸），
    这里不用再单独塞；browse 会在用户桌面上弹系统对话框，是这道闸
    最早的保护对象。
*/
export function workspaceAction(action, opts = {}, sid = "") {
  return post("/api/workspace", { action, ...opts, sid });
}

// ── 文件系统浏览（工作区选择器的数据源）─────────────────

/** 列一个目录的子目录。path 为空 = 起点态（后端回盘符 / 根目录）。

    只回**目录名**，不回文件内容 —— 纯读 GET；跨站读响应被同源策略挡住
    （本服务不回任何 CORS 头），DNS rebinding 由后端的 Host 校验挡住，
    所以 GET 不需要 X-Wywd-Ui（那道闸留给会改状态的 POST）。
*/
export function fetchFsList(path = "") {
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  return get("/api/fs/list" + q);
}

// ── SSE 事件长连（替代原来的 500ms 轮询）─────────────────

/** 打开事件流。两个要点：

    - `after=latest`：只在"现在"之后推。事件环是常驻的，不带这个参数
      首次连接会把环里最多 500 条旧事件整批重放（F5 后满屏旧卡片）。
      断线重连时浏览器会把 Last-Event-ID 头带上来，服务端优先用它——
      续传游标因此完全不用前端管。
    - 断线重连是 EventSource 的内建行为，不必自己写退避重试。
*/
export function openEventStream() {
  return new EventSource("/api/events/stream?after=latest");
}
