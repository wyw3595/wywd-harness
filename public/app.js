/* C 方案前端：会话浏览器 + 聊天 UI（vanilla，零依赖）。

   架构（对照 chainlit 版）：
     - 列表 = GET /api/sessions（5s 轮询）
     - 点击会话 = 换 currentSid → GET /api/sessions/<sid>/messages 拿全量
       历史 → 自己渲染 DOM。清屏重放根本没有"进度"问题：拿不到就不渲染，
       拿到就渲染，currentSid 变了就是变了。
     - 发送 = POST /api/sessions/<sid>/messages（同步等 turn 完成），等待
       期间 500ms 轮询 /api/events 画工具直播卡 + 审批卡。
     - 审批 = events 里的 approval_request → 底部卡片 → POST
       /api/approvals/<ticket>。fail-closed：不回应 = 300s 后拒绝。

   没有锚点、没有会话存储、没有"桥"——HTTP 无状态，F5 / 多 tab 天然正确。
   "当前会话"只存一个 localStorage 变量（每 tab 自己的视图）。
*/

(function () {
  "use strict";

  var LS_CURRENT = "wywd_current_sid";

  var state = {
    sid: null,            // 当前选中的会话（本 tab 的视图指针）
    lastSeq: 0,
    posting: false,
    pollTimer: null,
    shownApprovals: {},   // ticket → true（审批卡去重）
    listTimer: null,
  };

  var $ = function (id) { return document.getElementById(id); };

  // ── 工具函数 ─────────────────────────────────────────────

  function api(path, opts) {
    return fetch(path, opts).then(function (r) { return r.json(); });
  }

  function toast(text) {
    var el = $("toast");
    el.textContent = text;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.style.display = "none"; }, 2500);
  }

  function esc(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  function shortId(id) { return String(id).slice(0, 8); }

  // ── 会话列表 ─────────────────────────────────────────────

  function loadSessions() {
    api("/api/sessions").then(function (data) {
      renderList(data.sessions || []);
    }).catch(function () {
      $("session-list").innerHTML =
        '<div class="err">后端没起来（127.0.0.1:8765）</div>';
    });
  }

  function renderList(rows) {
    var html = rows.map(function (s) {
      if (s.error) return '<div class="err">' + esc(s.error) + "</div>";
      var id = s.id || s.sessionId || "?";
      var live = s.live === true;
      var isCur = id === state.sid;
      var title = (s.title && s.title !== id) ? s.title : id;
      var gen = s.runtimeGeneration || 1;
      var cls = ["row"];
      if (isCur) cls.push("cur");
      var badge = isCur ? '<span class="badge">当前</span>'
        : (live ? '<span class="badge">live</span>'
                : '<span class="badge off">closed</span>');
      var ops = [
        live ? '<button data-op="close" data-sid="' + esc(id) + '">关</button>' : "",
        (!live ? '<button data-op="resume" data-sid="' + esc(id) + '">活</button>' : ""),
        '<button class="del" data-op="forget" data-sid="' + esc(id) + '">删</button>'
      ].join("");
      return (
        '<div class="' + cls.join(" ") + '" data-sid="' + esc(id) + '">' +
          '<div class="r-title"><span class="r-tt" title="' + esc(title) + '">' +
            esc(title) + "</span>" + badge + "</div>" +
          '<div class="r-meta">' + esc(id + " · " + (s.status || "?") +
            " · gen=" + gen + " · " + (s.messages || 0) + " 条") + "</div>" +
          (ops ? '<div class="r-ops">' + ops + "</div>" : "") +
        "</div>"
      );
    }).join("");
    $("session-list").innerHTML =
      html || '<div class="empty">（无会话，点 ＋ 新建）</div>';
  }

  function onListClick(e) {
    var btn = e.target.closest("button");
    if (btn) { onOpClick(btn); return; }
    var row = e.target.closest(".row");
    if (row) selectSession(row.getAttribute("data-sid"));
  }

  // 切换：closed 的先复活（resume），live 的直接打开——历史由 GET 拉取
  function selectSession(sid) {
    var row = findRow(sid);
    var live = row && row.live === true;
    if (live || row === null) { openSession(sid); return; }
    api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "resume", sid: sid })
    }).then(function (res) {
      if (res && res.ok) {
        openSession(sid);
        loadSessions();       // live 标志变了，立刻刷列表
      } else {
        toast((res && res.detail) || "复活失败");
      }
    }).catch(function () { toast("后端没响应"); });
  }

  function findRow(sid) {
    var rows = ($("session-list").querySelectorAll(".row"));
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-sid") === sid) {
        return {
          live: !rows[i].querySelector('.badge.off'),
          el: rows[i]
        };
      }
    }
    return null;
  }

  // ── 打开会话：换 currentSid + 拉历史 + 全量渲染 ────────────

  function openSession(sid) {
    state.sid = sid;
    try { localStorage.setItem(LS_CURRENT, sid); } catch (e) { /* ignore */ }
    clearChat();
    // 把事件指针同步到服务端当前 seq：事件环是常驻的，F5 后 lastSeq 从
    // 0 开始会把上一轮的老工具卡重放出来——无视历史，直播只管当下。
    api("/api/events").then(function (data) {
      state.lastSeq = data.latest || 0;
    }).catch(function () { /* 同步失败：最多错过一张旧卡 */ });
    $("chat-head").innerHTML = "会话 <b>" + esc(sid) + "</b>";
    api("/api/sessions/" + encodeURIComponent(sid) + "/messages")
      .then(function (data) {
        if (data.error) {
          $("messages").innerHTML = '<div class="msg system">⚠ ' +
            esc(data.error) + "</div>";
          setInputEnabled(false);
          return;
        }
        renderMessages(data.messages || []);
        setInputEnabled(true);
      })
      .catch(function () {
        $("messages").innerHTML = '<div class="msg system">⚠ 拉取历史失败</div>';
      });
  }

  function renderMessages(msgs) {
    var boxes = [];
    msgs.forEach(function (m) {
      boxes.push(messageEl(m));
    });
    $("messages").innerHTML = boxes.join("");
    scrollBottom();
  }

  function messageEl(m) {
    var role = m.role || "?";
    var text = (m.content || "").trim();
    if (!text && m.tool_calls && m.tool_calls.length) {
      text = "🤖 调用工具：" + m.tool_calls
        .map(function (t) { return t.name || "?"; }).join(", ");
    }
    if (!text) return '<div class="msg system">（空消息）</div>';
    if (role === "user") {
      return '<div class="msg user">' + esc(text) + "</div>";
    }
    if (role === "assistant") {
      return '<div class="msg assistant">' + esc(text) + "</div>";
    }
    if (role === "system") {
      return '<div class="msg system">⚙ ' + esc(text) + "</div>";
    }
    if (role === "tool") {
      var snippet = text.length > 400 ? text.slice(0, 400) + "…" : text;
      return '<div class="msg tool"><span class="t-name">🔧 工具结果</span> ' +
        esc(snippet) + "</div>";
    }
    return '<div class="msg system">[' + esc(role) + "] " + esc(text) + "</div>";
  }

  function clearChat() {
    $("messages").innerHTML = "";
    $("live-feed").innerHTML = '<div class="lf-empty">—</div>';
    state.shownApprovals = {};
    hideApproval();
  }

  // ── 发送 ─────────────────────────────────────────────────

  function send() {
    var input = $("input");
    var text = input.value.trim();
    if (!text || state.posting || !state.sid) return;
    state.posting = true;
    setInputEnabled(false);
    showThinking(true);

    // 等待期间轮询 events：工具直播 + 审批卡
    state.pollTimer = setInterval(pollEvents, 500);

    api("/api/sessions/" + encodeURIComponent(state.sid) + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text })
    }).then(function (res) {
      finishPosting();
      if (res.error) {
        toast("⚠ " + res.error);
      } else {
        loadSessions();
      }
      // turn 结束：以历史为准重拉全量渲染（含本次新对话）
      return api("/api/sessions/" + encodeURIComponent(state.sid) + "/messages");
    }).then(function (data) {
      if (data && !data.error) renderMessages(data.messages || []);
      setInputEnabled(true);
    }).catch(function () {
      finishPosting();
      setInputEnabled(true);
      toast("后端没响应");
    });
  }

  function finishPosting() {
    state.posting = false;
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    showThinking(false);
  }

  function setInputEnabled(on) {
    $("input").disabled = !on;
    $("btn-send").disabled = !on;
  }

  function showThinking(on) {
    var feed = $("live-feed");
    feed.innerHTML = on
      ? '<div class="lf-empty">🤖 思考中…</div>'
      : '<div class="lf-empty">—</div>';
    if (!on) state.shownApprovals = {};
  }

  // ── 事件轮询：工具直播 + 审批卡 ──────────────────────────

  function pollEvents() {
    api("/api/events?after=" + state.lastSeq).then(function (data) {
      (data.events || []).forEach(handleEvent);
      state.lastSeq = data.latest || state.lastSeq;
    }).catch(function () { /* 轮询断一下没关系，下次再试 */ });
  }

  function handleEvent(ev) {
    if (ev.event === "tool_start") {
      var args = "";
      try { args = JSON.stringify(ev.arguments); } catch (e) { /* ignore */ }
      if (args && args.length > 120) args = args.slice(0, 120) + "…";
      appendLive("🔧 调用 <span class='t-name'>" + esc(ev.name || "tool") +
        "</span> " + esc(args));
    } else if (ev.event === "tool_end") {
      var out = String(ev.content == null ? "" : ev.content);
      if (out.length > 240) out = out.slice(0, 240) + "…";
      appendLive("   ↳ " + esc(out));
    } else if (ev.event === "approval_request") {
      var ticket = ev.request_id;
      if (!state.shownApprovals[ticket]) {
        state.shownApprovals[ticket] = true;
        showApproval(ticket, ev.rule_id, ev.reason);
      }
    }
  }

  function appendLive(html) {
    var feed = $("live-feed");
    feed.innerHTML = feed.innerHTML.replace(
      /^\s*<div class="lf-empty">/,
      '<div class="lf-empty" style="display:none">');
    var div = document.createElement("div");
    div.className = "lf-line";
    div.style.cssText = "font-size:12px;color:#8b949e;padding:2px 0;line-height:1.5;" +
      "word-break:break-word";
    div.innerHTML = html;
    feed.appendChild(div);
    scrollBottom();
  }

  // ── 审批卡 ───────────────────────────────────────────────

  function showApproval(ticket, ruleId, reason) {
    $("approval-card").querySelector(".a-rule").textContent =
      "⚠️ 需要审批 [" + ruleId + "]";
    $("approval-card").querySelector(".a-reason").textContent = reason || "";
    $("approval-overlay").style.display = "block";
    var allow = $("a-allow"), reject = $("a-reject");
    allow.disabled = reject.disabled = false;
    allow.onclick = function () { respond(ticket, true, allow, reject); };
    reject.onclick = function () { respond(ticket, false, allow, reject); };
  }

  function respond(ticket, approved, allowBtn, rejectBtn) {
    // 点了之后立刻禁用按钮：等 turn 结束（后端 sidecar 决定放行与否）
    allowBtn.disabled = rejectBtn.disabled = true;
    api("/api/approvals/" + encodeURIComponent(ticket), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved: approved })
    }).catch(function () { /* 回执失败：等待超时 fail-closed */ });
    // 不立刻关卡——给用户"已提交"的反馈，直到 turn 结束才整体消失
    setTimeout(function () { $("approval-overlay").style.display = "none"; },
               1200);
  }

  function hideApproval() {
    $("approval-overlay").style.display = "none";
  }

  // ── 行操作：新建 / 刷新 / 关 / 活 / 删（两段式确认）────────

  function createSession() {
    api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "create" })
    }).then(function (res) {
      if (res && res.ok) {
        openSession(res.sessionId);
        loadSessions();
      } else {
        toast((res && res.detail) || "新建失败");
      }
    }).catch(function () { toast("后端没响应"); });
  }

  function onOpClick(btn) {
    var op = btn.getAttribute("data-op");
    var sid = btn.getAttribute("data-sid") || "";
    if (op === "refresh") { loadSessions(); return; }
    if (op === "forget" && btn.getAttribute("data-armed") !== "1") {
      btn.setAttribute("data-armed", "1");
      btn.dataset.orig = btn.textContent;
      btn.textContent = "确认?";
      btn.classList.add("arm");
      setTimeout(function () {
        if (btn.isConnected) {
          btn.removeAttribute("data-armed");
          btn.textContent = btn.dataset.orig || "删";
          btn.classList.remove("arm");
        }
      }, 3000);
      return;
    }
    api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: op, sid: sid })
    }).then(function (res) {
      if (res && res.ok) {
        toast(res.detail || "完成");
        if (op === "close" && sid === state.sid) {
          // 当前会话被关：视图保留历史（记录还在），只是不能再发
          setInputEnabled(false);
        }
      } else {
        toast((res && res.detail) || "操作失败");
      }
      loadSessions();
    }).catch(function () { toast("后端没响应"); });
  }

  // ── 杂项 ─────────────────────────────────────────────────

  function scrollBottom() {
    var m = $("messages");
    m.scrollTop = m.scrollHeight;
  }

  function init() {
    $("session-list").addEventListener("click", onListClick);
    $("btn-create").addEventListener("click", createSession);
    $("btn-refresh").addEventListener("click", loadSessions);
    $("input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    $("btn-send").addEventListener("click", send);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") hideApproval();
    });

    loadSessions();
    state.listTimer = setInterval(loadSessions, 5000);

    // F5 / 重开 tab：把上次的会话视图读回来（只读 GET，closed 也能看）
    var last = null;
    try { last = localStorage.getItem(LS_CURRENT); } catch (e) { /* ignore */ }
    if (last) openSession(last);
  }

  init();
})();