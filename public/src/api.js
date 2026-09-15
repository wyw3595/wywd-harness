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

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
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

export function sendMessage(sid, message) {
  return post(messagesPath(sid), { message });
}

// ── 审批回执 ────────────────────────────────────────────

export function respondApproval(ticket, approved) {
  return post("/api/approvals/" + encodeURIComponent(ticket), { approved });
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
