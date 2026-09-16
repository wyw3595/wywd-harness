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

async function post(path, body, headers = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { ...JSON_HEADERS, ...headers },
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

// ── 工作区（让 agent 在哪个目录里干活）───────────────────

/** 读当前工作区。壳不认识 workspace/get 时后端会如实降级为"默认"。 */
export function fetchWorkspace() {
  return get("/api/workspace");
}

/** 工作区操作。action ∈ browse | open | upload | reset。

    `X-Wywd-Ui` 不是装饰：browse 会在**用户桌面上弹一个系统对话框**，
    本地服务不该让任意网页触发它。跨站表单发不出自定义头（要发就得先过
    CORS 预检，而本服务不答预检），所以这一个头就把它挡在门外了。
    后端只对 browse 校验这个头（见 web_app 的 do_POST）。
*/
export function workspaceAction(action, opts = {}) {
  return post("/api/workspace", { action, ...opts }, { "X-Wywd-Ui": "1" });
}

// ── 文件系统浏览（工作区选择器的数据源）─────────────────

/** 列一个目录的子目录。path 为空 = 起点态（后端回盘符 / 根目录）。

    只回**目录名**，不回文件内容 —— 纯读、且本服务只听 127.0.0.1，
    同源策略挡住了跨站读响应，所以这个端点不需要 X-Wywd-Ui 那道闸
    （那道闸留给会改状态的 POST）。
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
