/* s07-c 网页会话面板：左侧会话栏（custom_js 注入，零框架）。

   通过 scripts/sidecar_panel.py 的进程内 HTTP 服务（127.0.0.1:8765）拉取
   sidecar 会话清单、执行 新建/切换/关闭/删除。

   交互口径（trea 风格会话列表）：
     - 面板固定**左侧**，半透明深色，可折叠（点标题条）；展开时给 body 加
       padding-left 让位——不再盖住聊天区，折叠即还原全宽；
     - **点击一行 = 切换会话**（closed 行 resume 到它，generation+1 新运行时，
       之后主聊天区发消息就是给这个会话）；当前行高亮 + 「当前」徽标；
     - 「活着的但不是本页当前」= 运行时在别的 tab（后端聚合 live + liveThread）
       → 只读 + 「其他页」徽标，防止跨进程 resume 造出双运行时；
     - 行内小按钮：关（close）/ 活（resume）/ 删（forget，**两段式确认**——
       第一次点变红「确认?」，3 秒内再点才真删，防手滑丢历史）；
     - 每 5 秒轮询一次清单 + 手动 ↻；轮询带三重防抖：
       ① 序号守卫（过期响应直接丢弃）② 字符串 diff（内容没变不重建 DOM，
       不闪不丢 hover）③ 鼠标悬停时推迟换 DOM（防止点击被重建打断）。

   切换的呈现（s07-c）：resume 成功后，**后端桥**（chainlit_app 注册的
   _bridges[thread]）会清空聊天区并把目标会话的完整历史重放进去——
   "点会话，主聊天区直接变成那个会话的对话"。重放只是呈现（sidecar
   record 的投影）；chainlit 自己的 thread 状态没换，发下一条消息就是
   接着新会话聊（run_turn 从 record 拿历史）。前端这里只弹横幅给即时
   反馈，不跟桥抢活。

   本页锚点（ownerThread）的认定顺序：
     1. sessionStorage 里本 tab 记住的 thread 还在线（threads 名单校验）→
        继续用（F5 刷新回来不断线——chainlit 的 on_chat_start 刷新后不重跑，
        这是锚点最主要的恢复路径）；
     2. 否则认领 defaultThread（最近注册的壳；新 tab 的 on_chat_start 刚
        register 过，最近的就是自己）。
   已知边界：多 tab 同时开新页时存在认错锚点的竞态（教学口径，单 tab 无此问题）。
*/

(function () {
  "use strict";

  var API = "http://127.0.0.1:8765";
  var PANEL_ID = "s07-session-panel";
  var PANEL_W = 232;
  var listEl = null;
  var collapsed = false;
  var ownerThread = null;   // 本页自己的线程：所有 GET/POST 都带它，
                            // 后端按它判定 current/own 并精确路由操作 ——
                            // 多 tab 各管各的，切会话不再错位。

  // 轮询防抖三件套的状态
  var fetchSeq = 0;         // ① 序号：只有最新一次 refresh 的响应允许落地
  var lastListHtml = null;  // ② diff：清单 HTML 没变就不碰 DOM
  var pendingListHtml = null; // ③ 悬停推迟：鼠标在面板上时攒着，离开再换
  var panelHover = false;

  var opBusy = false;       // 操作互斥：runOp 期间拒绝新操作（防双击连环触发）

  var SS_THREAD_KEY = "wywd_owner_thread";  // 本 tab 的锚点（sessionStorage
                                            // = 每 tab 一份，正是不串台的关键）

  var STYLE = [
    "#" + PANEL_ID + "{position:fixed;left:0;width:" + PANEL_W + "px;z-index:9999;",
    "font-family:Segoe UI,system-ui,sans-serif;font-size:12px;color:#d7dde4;",
    "background:rgba(16,20,28,.95);border-right:1px solid rgba(255,255,255,.1);",
    "display:flex;flex-direction:column;overflow:hidden;}",
    "body.sp-panel-open{padding-left:" + (PANEL_W + 1) + "px;}", // 展开时让位：整页右移，不盖聊天区
    "#" + PANEL_ID + " .sp-head{display:flex;align-items:center;gap:6px;padding:8px 10px;",
    "cursor:pointer;user-select:none;background:rgba(255,255,255,.06);font-weight:600;",
    "flex:0 0 auto;}",
    "#" + PANEL_ID + " .sp-head button{border:0;background:rgba(255,255,255,.12);",
    "color:inherit;border-radius:5px;padding:2px 8px;cursor:pointer;font-size:12px;}",
    "#" + PANEL_ID + " .sp-head button:hover{background:rgba(255,255,255,.25);}",
    "#" + PANEL_ID + " .sp-now{padding:6px 10px;font-size:11px;color:#8ec0ff;",
    "background:rgba(88,166,255,.08);border-bottom:1px solid rgba(255,255,255,.08);",
    "flex:0 0 auto;word-break:break-all;}",
    "#" + PANEL_ID + " .sp-body{flex:1 1 auto;overflow-y:auto;padding:4px 6px;}",
    "#" + PANEL_ID + " .sp-row{padding:6px 8px;border-radius:6px;margin-bottom:2px;",
    "cursor:pointer;border:1px solid transparent;}",
    "#" + PANEL_ID + " .sp-row:hover{background:rgba(255,255,255,.07);}",
    "#" + PANEL_ID + " .sp-row.sp-cur-row{background:rgba(88,166,255,.16);",
    "border-color:rgba(88,166,255,.45);}",  // 当前会话：蓝色高亮
    "#" + PANEL_ID + " .sp-row.sp-foreign{opacity:.55;cursor:not-allowed;}",
    "#" + PANEL_ID + " .sp-row .sp-title{display:flex;align-items:center;gap:6px;",
    "overflow:hidden;}",                    // 标题行：超长省略号，不撑破面板
    "#" + PANEL_ID + " .sp-tt{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;",
    "white-space:nowrap;}",
    "#" + PANEL_ID + " .sp-badge{font-size:10px;padding:1px 5px;border-radius:8px;",
    "background:rgba(88,166,255,.25);color:#8ec0ff;flex:0 0 auto;}",
    "#" + PANEL_ID + " .sp-badge.sp-off{background:rgba(255,255,255,.12);color:#8b949e;}",
    "#" + PANEL_ID + " .sp-meta{color:#8b949e;font-size:11px;margin-top:2px;",
    "word-break:break-all;}",
    "#" + PANEL_ID + " .sp-ops{margin-top:4px;gap:4px;display:flex;}",
    "#" + PANEL_ID + " .sp-ops button{border:0;background:rgba(255,255,255,.12);",
    "color:inherit;border-radius:4px;padding:1px 7px;cursor:pointer;font-size:11px;}",
    "#" + PANEL_ID + " .sp-ops button:hover{background:rgba(255,255,255,.25);}",
    "#" + PANEL_ID + " .sp-ops .sp-del:hover{background:rgba(255,107,107,.35);}",
    "#" + PANEL_ID + " .sp-ops button.sp-arm{background:rgba(255,107,107,.55);", // 删除确认态
    "font-weight:600;}",
    // 操作进行中：按钮半透明 + 不可点（防双击的视觉面）
    "#" + PANEL_ID + ".sp-busy .sp-head button," +
      "#" + PANEL_ID + ".sp-busy .sp-ops button{opacity:.45;pointer-events:none;}",
    "#" + PANEL_ID + " .sp-empty{color:#8b949e;padding:12px 6px;text-align:center;}",
    "#" + PANEL_ID + " .sp-err{color:#ff7b72;padding:8px 6px;}",
    "#" + PANEL_ID + " .sp-toast{position:absolute;left:8px;right:8px;bottom:8px;",
    "background:rgba(30,36,48,.98);border-radius:6px;padding:6px 8px;font-size:11px;",
    "border:1px solid rgba(255,255,255,.12);}",
    // 折叠 = 真折叠：只剩标题条（当前条/清单全部收起）
    "#" + PANEL_ID + ".sp-collapsed{height:auto;}",
    "#" + PANEL_ID + ".sp-collapsed .sp-now," +
      "#" + PANEL_ID + ".sp-collapsed .sp-body{display:none;}",
    ""
  ].join("\n");

  function injectStyle() {
    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  // 动态量 chainlit 顶栏高度（代替硬编码 56px；量不到就退回旧值）
  function headerTop() {
    var h = document.querySelector("#root header") ||
            document.querySelector("header");
    return h && h.offsetHeight ? h.offsetHeight : 56;
  }

  function buildPanel() {
    var panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.top = headerTop() + "px";
    panel.innerHTML =
      '<div class="sp-head">' +
      '<span>🗂 会话</span>' +
      '<span style="flex:1"></span>' +
      '<button data-op="create" title="新建会话">＋</button>' +
      '<button data-op="refresh" title="刷新">↻</button>' +
      "</div>" +
      '<div class="sp-now">当前：-</div>' +
      '<div class="sp-body"></div>';
    document.body.appendChild(panel);
    document.body.classList.add("sp-panel-open"); // 展开 = 让位
    listEl = panel.querySelector(".sp-body");
    panel.querySelector(".sp-head").addEventListener("click", function (e) {
      // ＋/↻ 走统一的 onOpClick；点空白标题条才折叠
      if (e.target.tagName === "BUTTON") { onOpClick(e); return; }
      collapsed = !collapsed;
      panel.classList.toggle("sp-collapsed", collapsed);
      document.body.classList.toggle("sp-panel-open", !collapsed);
    });
    listEl.addEventListener("click", onListClick);
    // 悬停推迟的落点：鼠标离开面板时，把攒着的新清单一次性换上
    panel.addEventListener("mouseenter", function () { panelHover = true; });
    panel.addEventListener("mouseleave", function () {
      panelHover = false;
      if (pendingListHtml !== null && pendingListHtml !== lastListHtml) {
        listEl.innerHTML = pendingListHtml;
        lastListHtml = pendingListHtml;
      }
      pendingListHtml = null;
    });
    return panel;
  }

  // 招呼：window.postMessage → chainlit 前端转发 → 服务端 on_window_message
  // 钩子（在**当前连接**的 ws 上下文里跑）刷新面板桥的上下文锚点。F5 后
  // on_chat_start 不重跑，桥里还是旧连接的上下文——不招呼一声，点会话的
  // 清屏重放会静默发到已死的连接上。发两次兜底（React 挂载时序）。
  function hello() {
    try {
      window.postMessage({ source: "s07-session-panel", hello: 1 }, "*");
    } catch (e) { /* 旧浏览器/被策略拦：忽略 */ }
  }

  function init() {
    var guard = 0;
    var timer = setInterval(function () {
      if (document.body && document.body.appendChild) {
        clearInterval(timer);
        injectStyle();
        buildPanel();
        // 串行化：先 refresh 拿到本页锚点（ownerThread），再恢复上次会话。
        // 并发会让 restore 在 ownerThread 为空时按全局默认壳走——多 tab 下
        // 会把会话"接走"到别的页面，这是玩家错位的真 bug 之一。
        refresh().then(function () {
          hello();
          setTimeout(hello, 2000);   // 兜底第二声（等 React 挂稳监听）
          restoreLastSession();
          gcOrphanSessions();  // 无条件清历史 closed 空孤儿（restore 可能 skip）
        });
        setInterval(refresh, 5000); // 轮询：侧边栏跟着会话列表走
      } else if (++guard > 100) {   // 10 秒没就绪，放弃（页面异常）
        clearInterval(timer);
      }
    }, 100);
  }

  // ── 锚点认定：sessionStorage 优先（F5 恢复），defaultThread 兜底 ──────
  function adoptOwnerThread(data) {
    var threads = data.threads || [];
    var remembered = null;
    try { remembered = sessionStorage.getItem(SS_THREAD_KEY); } catch (e) { /* 隐私模式忽略 */ }
    if (ownerThread && threads.indexOf(ownerThread) !== -1) {
      return;  // 当前锚点还在线，不动
    }
    if (remembered && threads.indexOf(remembered) !== -1) {
      ownerThread = remembered;             // F5 回来：本 tab 上次的壳还在
    } else {
      ownerThread = data.defaultThread || null;  // 新 tab / 壳已被 reap
    }
    if (ownerThread) {
      try { sessionStorage.setItem(SS_THREAD_KEY, ownerThread); } catch (e) { /* ignore */ }
    }
  }

  async function refresh() {
    var body = listEl;
    if (!body) return;
    var seq = ++fetchSeq;   // ① 序号守卫：并发 refresh 只有最新者能落地
    var data;
    try {
      var res = await fetch(API + "/api/sessions?thread=" +
        encodeURIComponent(ownerThread || ""));
      data = await res.json();
    } catch (e) {
      if (seq === fetchSeq) {
        body.innerHTML = '<div class="sp-err">面板后端没起来（127.0.0.1:8765）</div>';
      }
      return;
    }
    if (seq !== fetchSeq) return;  // 过期响应：期间已有更新的 refresh，丢弃
    adoptOwnerThread(data);
    // 常驻"当前会话"行：textContent 赋值不重建 DOM，悬停中也安全
    var spNow = document.getElementById(PANEL_ID).querySelector(".sp-now");
    var curRow = (data.sessions || []).find(function (s) { return s.current; });
    if (spNow) {
      spNow.textContent = curRow
        ? "当前：" + curRow.id + " · " + (curRow.status || "?") +
          " · gen=" + (curRow.runtimeGeneration || 1)
        : "当前：-";
    }
    // ② diff + ③ 悬停推迟：内容没变不重建；鼠标正悬在面板上就攒着
    var rows = (data.sessions || []).map(renderRow).join("");
    var html = rows || '<div class="sp-empty">（无会话，点 ＋ 新建）</div>';
    if (html !== lastListHtml) {
      if (panelHover) {
        pendingListHtml = html;
      } else {
        body.innerHTML = html;
        lastListHtml = html;
        pendingListHtml = null;
      }
    }
    rememberCurrent(data);   // 每次刷新同步"当前会话"，刷新恢复的锚点
  }

  function renderRow(s) {
    if (s.error) {
      return '<div class="sp-err">' + escapeHtml(s.error) + "</div>";
    }
    var id = s.id || s.sessionId || "?";
    var title = s.title && s.title !== id ? s.title : id;
    var live = !!s.live;               // 后端已聚合：任何一个壳的运行时有它就算活
    var status = s.status || "?";
    var gen = s.runtimeGeneration || 1;
    var isCur = !!s.current;
    // 别页的活会话：运行时不在本页（liveThread 指向别的 thread）→ 只读。
    // 注意不能只看 live && !isCur——resume 不关旧当前，本页旧当前也是
    // live 且非 current，但 liveThread 是自己，照样可操作。
    var isForeign = live && !isCur && !!s.liveThread && s.liveThread !== ownerThread;
    var cls = ["sp-row"];
    if (isCur) cls.push("sp-cur-row");
    if (isForeign) cls.push("sp-foreign");
    var badge = isCur
      ? '<span class="sp-badge">当前</span>'
      : (live ? '<span class="sp-badge">live</span>'
              : '<span class="sp-badge sp-off">closed</span>');
    var meta = escapeHtml(id + " · " + status + " · gen=" + gen +
      (isForeign ? " · 其他页" : "") +
      (s.thread ? " · " + shortThread(s.thread) : ""));
    // 行尾操作按钮：别页的活会话只读（关/活/删都不给，防跨进程误伤）
    var ops = isForeign ? "" : [
      live ? '<button data-op="close" data-sid="' + escapeAttr(id) + '">关</button>' : "",
      (!live ? '<button data-op="resume" data-sid="' + escapeAttr(id) + '">活</button>' : ""),
      '<button class="sp-del" data-op="forget" data-sid="' + escapeAttr(id) + '">删</button>'
    ].join("");
    return (
      '<div class="' + cls.join(" ") + '" data-sid="' + escapeAttr(id) +
      '" data-thread="' + escapeAttr(s.thread || "") + '" data-live="' + live + '">' +
      '<div class="sp-title">' +
      '<span class="sp-tt" title="' + escapeAttr(title) + '">' + escapeHtml(title) + "</span>" + badge +
      "</div>" +
      '<div class="sp-meta">' + meta + "</div>" +
      (ops ? '<div class="sp-ops">' + ops + "</div>" : "") +
      "</div>"
    );
  }

  function shortThread(thread) {
    return thread ? thread.slice(0, 2) + "…" : "";
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text);
    return div.innerHTML;   // & < > 转义（文本上下文够用）
  }

  function escapeAttr(text) {
    // 属性上下文必须连引号一起转义（innerHTML 序列化不转义引号）
    return String(text).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
      .replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // 列表点击：按钮 → 操作；行本身 → 切换会话
  function onListClick(e) {
    var btn = e.target.closest("button");
    if (btn) {
      onOpClick(e);
      return;
    }
    var row = e.target.closest(".sp-row");
    if (row) switchSession(row);
  }

  // 切换会话：closed 行 resume（换代）成当前；本页 live 当前行/其他页行是禁区
  async function switchSession(row) {
    var sid = row.getAttribute("data-sid");
    var live = row.getAttribute("data-live") === "true";
    if (row.classList.contains("sp-foreign")) {
      toast("这是另一个页面的会话，去那个页面操作");
      return;
    }
    var isCur = row.classList.contains("sp-cur-row");
    if (isCur) { toast("已在此会话"); return; }
    if (live) { toast("该会话在运行中，先关闭再切换"); return; }
    var result = await runOp("resume", sid);
    if (result && result.ok) {
      // 聊天区的清屏重放由后端桥负责（chainlit_app._make_chat_bridge）；
      // 前端只弹横幅给即时反馈，不跟桥抢活
      sessionBanner(
        "已切换到 <b>" + escapeHtml(sid) + "</b>，聊天区正在重放它的历史"
      );
    }
  }

  // 顶部横幅：不依赖 chainlit，纯 DOM——保证切换反馈一定看得到。
  // 位置跟着面板状态走（折叠让位/展开避开），不再硬编码。
  function sessionBanner(html) {
    var el = document.createElement("div");
    el.style.cssText =
      "position:fixed;top:" + (headerTop() + 8) + "px;" +
      "left:" + (collapsed ? 12 : PANEL_W + 13) + "px;right:12px;z-index:10001;" +
      "padding:10px 14px;border-radius:8px;font-size:12px;line-height:1.6;" +
      "background:rgba(30,48,80,.96);border:1px solid rgba(88,166,255,.5);" +
      "color:#cfe3ff;box-shadow:0 4px 16px rgba(0,0,0,.35);";
    el.innerHTML = html;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }

  function onOpClick(e) {
    var btn = e.target.closest("button");
    if (!btn) return;
    var op = btn.getAttribute("data-op");
    var sid = btn.getAttribute("data-sid") || "";
    if (op === "refresh") { refresh(); return; }
    // 删除是不可逆操作：两段式确认。第一次点 → 按钮变红「确认?」，
    // 3 秒内再点才真删；超时或轮询重建 DOM 都会自然复位。
    if (op === "forget" && btn.getAttribute("data-armed") !== "1") {
      btn.setAttribute("data-armed", "1");
      btn.dataset.orig = btn.textContent;
      btn.textContent = "确认?";
      btn.classList.add("sp-arm");
      setTimeout(function () {
        if (btn.isConnected) {
          btn.removeAttribute("data-armed");
          btn.textContent = btn.dataset.orig || "删";
          btn.classList.remove("sp-arm");
        }
      }, 3000);
      return;
    }
    runOp(op, sid);
  }

  // runOp：所有变更操作的咽喉。互斥（防双击）+ silent 模式（GC 批量清理
  // 时不开 toast 不逐次刷新，避免连环轰炸）。
  async function runOp(op, sid, opts) {
    var silent = !!(opts && opts.silent);
    if (opBusy) {
      if (!silent) toast("上一个操作还没完成");
      return null;
    }
    opBusy = true;
    document.getElementById(PANEL_ID).classList.add("sp-busy");
    var res, data;
    try {
      try {
        res = await fetch(API + "/api/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: op, sid: sid, thread: ownerThread || "" })
        });
        data = await res.json();
      } catch (e) {
        if (!silent) toast("面板后端没响应，稍后再试");
        return null;
      }
      if (!silent) {
        toast(data.detail || (data.ok ? "完成" : "失败"));
        refresh(); // 操作完立刻刷一遍，别等轮询
      }
      return data;
    } finally {
      opBusy = false;
      document.getElementById(PANEL_ID).classList.remove("sp-busy");
    }
  }

  // ── 刷新恢复：F5 后 chainlit 会重跑 on_chat_start 建新壳新 sid，这里把
  //   上次会话接回来。**localStorage 按 thread 分 key**——多 tab 各记各的
  //   「上次会话」，不再互相覆盖（全局一个 key 时 F5 可能接走别页的会话）。
  var LS_KEY = "wywd_last_sid";

  function lastSidKey() {
    return ownerThread ? LS_KEY + ":" + ownerThread : "";
  }

  function rememberCurrent(data) {
    var key = lastSidKey();
    if (!key) return;
    var cur = (data.sessions || []).find(function (s) { return s.current; });
    if (cur) {
      try { localStorage.setItem(key, cur.id); } catch (e) { /* 隐私模式忽略 */ }
    }
  }

  async function restoreLastSession() {
    var key = lastSidKey();
    if (!key) return;
    var last;
    try { last = localStorage.getItem(key); } catch (e) { return; }
    if (!last) return;
    var data = await fetchSessions();
    if (!data) return;
    var cur = (data.sessions || []).find(function (s) { return s.current; });
    if (!cur || cur.id === last) return;   // 本来就接上了，别折腾
    var result = await runOp("resume", last);
    if (result && result.ok) {
      toast("已接回上次会话 " + last);
      // 本次加载刚 create 的孤儿（restore 前的 current，live 空会话）：
      // close + forget 二连走掉——forget 拒绝 live，必须先 close。
      // 只关本页壳自己的会话（cur 就是这个壳的 current），不碰别的 tab。
      if ((cur.messages || 0) <= 1) {
        await runOp("close", cur.id, { silent: true });
        await runOp("forget", cur.id, { silent: true });
      }
      refresh();
      gcOrphanSessions();  // 顺带清历史 closed 空孤儿
    }
  }

  // 孤儿清扫：chainlit 每次页面加载都会 on_chat_start → create 一个空会话。
  // 刷新一旦发生，这些只剩 system 播种（messages==1 且 closed）的会话就成群
  // 结队。此函数把它们 forget 掉，列表回到干净。保守口径：只清 closed 且
  // 消息 <=1（没有过 user 对话）的；当前/上次会话一律不动。
  // 清理全程 silent：N 个孤儿 = N 次静默 forget + 1 次 toast + 1 次刷新。
  async function gcOrphanSessions() {
    var key = lastSidKey();
    if (!key) return;
    var data = await fetchSessions();
    if (!data) return;
    var last = "";
    try { last = localStorage.getItem(key) || ""; } catch (e) { /* ignore */ }
    var cur = (data.sessions || []).find(function (s) { return s.current; });
    var curId = cur ? cur.id : "";
    var doomed = (data.sessions || []).filter(function (s) {
      return s.id !== last && s.id !== curId &&
        !s.live && s.status === "closed" && (s.messages || 0) <= 1;
    });
    var cleaned = 0;
    for (var i = 0; i < doomed.length; i++) {
      var result = await runOp("forget", doomed[i].id, { silent: true });
      if (result && result.ok) cleaned++;
    }
    if (cleaned) {
      toast("已清理 " + cleaned + " 个空会话");
      refresh();
    }
  }

  async function fetchSessions() {
    try {
      var res = await fetch(API + "/api/sessions?thread=" +
        encodeURIComponent(ownerThread || ""));
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  var toastTimer = null;
  function toast(text) {
    var panel = document.getElementById(PANEL_ID);
    if (!panel) return;
    var el = panel.querySelector(".sp-toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "sp-toast";
      panel.appendChild(el);
    }
    el.textContent = text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.remove(); }, 2500);
  }

  init();
})();
