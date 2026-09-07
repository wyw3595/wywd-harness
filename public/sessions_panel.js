/* s07-b 网页会话面板：真·侧边栏（custom_js 注入，零框架）。

   通过 scripts/sidecar_panel.py 的进程内 HTTP 服务（127.0.0.1:8765）拉取
   sidecar 会话清单、执行 新建/关闭/复活/删除。只依赖浏览器 fetch，纯 vanilla。

   设计口径：
     - 面板固定右侧，半透明，可折叠（点标题条），不挡聊天输入框；
     - 每 5 秒轮询一次清单 + 手动 ↻；操作按钮用事件委托（列表整体一个监听）；
     - custom_js 在页面加载时运行，body 可能还没就绪，用重试窗口等它。
*/

(function () {
  "use strict";

  var API = "http://127.0.0.1:8765";
  var PANEL_ID = "s07-session-panel";
  var listEl = null;
  var collapsed = false;

  var STYLE = [
    "#" + PANEL_ID + "{position:fixed;top:70px;right:12px;width:260px;z-index:9999;",
    "font-family:Segoe UI,system-ui,sans-serif;font-size:12px;color:#d7dde4;",
    "background:rgba(20,24,32,.92);border:1px solid rgba(255,255,255,.12);",
    "border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.35);overflow:hidden;}",
    "#" + PANEL_ID + " .sp-head{display:flex;align-items:center;gap:6px;padding:8px 10px;",
    "cursor:pointer;user-select:none;background:rgba(255,255,255,.06);font-weight:600;}",
    "#" + PANEL_ID + " .sp-head button{border:0;background:rgba(255,255,255,.12);",
    "color:inherit;border-radius:5px;padding:2px 7px;cursor:pointer;font-size:12px;}",
    "#" + PANEL_ID + " .sp-head button:hover{background:rgba(255,255,255,.25);}",
    "#" + PANEL_ID + " .sp-body{max-height:52vh;overflow-y:auto;padding:6px;}",
    "#" + PANEL_ID + " .sp-row{border-bottom:1px solid rgba(255,255,255,.07);padding:5px 2px;}",
    "#" + PANEL_ID + " .sp-id{font-family:Consolas,monospace;word-break:break-all;}",
    "#" + PANEL_ID + " .sp-cur{color:#7ee787;}",
    "#" + PANEL_ID + " .sp-closed{color:#f2cc60;}",
    "#" + PANEL_ID + " .sp-meta{color:#8b949e;font-size:11px;margin:2px 0 4px;}",
    "#" + PANEL_ID + " .sp-ops button{border:0;background:rgba(255,255,255,.12);color:inherit;",
    "border-radius:4px;padding:1px 6px;margin-right:4px;cursor:pointer;font-size:11px;}",
    "#" + PANEL_ID + " .sp-empty{color:#8b949e;padding:8px 2px;text-align:center;}",
    "#" + PANEL_ID + " .sp-err{color:#ff7b72;padding:8px 2px;}",
    "#" + PANEL_ID + " .sp-toast{position:absolute;left:6px;right:6px;bottom:6px;",
    "background:rgba(255,255,255,.12);border-radius:6px;padding:6px 8px;font-size:11px;}",
    "#" + PANEL_ID + ".sp-collapsed .sp-body{display:none;}",
    ""
  ].join("\n");

  function injectStyle() {
    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);
  }

  function buildPanel() {
    var panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.innerHTML =
      '<div class="sp-head">' +
      '<span>🗂 会话</span>' +
      '<span style="flex:1"></span>' +
      '<button data-op="create" title="新建会话">＋</button>' +
      '<button data-op="refresh" title="刷新">↻</button>' +
      "</div>" +
      '<div class="sp-body"></div>';
    document.body.appendChild(panel);
    listEl = panel.querySelector(".sp-body");
    panel.querySelector(".sp-head").addEventListener("click", function (e) {
      // 点了按钮不折叠；点空白标题条才折叠
      if (e.target.tagName === "BUTTON") return;
      collapsed = !collapsed;
      panel.classList.toggle("sp-collapsed", collapsed);
    });
    panel.querySelector(".sp-body").addEventListener("click", onOpClick);
    return panel;
  }

  function init() {
    var guard = 0;
    var timer = setInterval(function () {
      if (document.body && document.body.appendChild) {
        clearInterval(timer);
        injectStyle();
        buildPanel();
        refresh();
        setInterval(refresh, 5000); // 轮询：侧边栏跟着会话列表走
      } else if (++guard > 100) {   // 10 秒没就绪，放弃（页面异常）
        clearInterval(timer);
      }
    }, 100);
  }

  async function refresh() {
    var body = listEl;
    if (!body) return;
    var data;
    try {
      var res = await fetch(API + "/api/sessions");
      data = await res.json();
    } catch (e) {
      body.innerHTML = '<div class="sp-err">面板后端没起来（127.0.0.1:8765）</div>';
      return;
    }
    var rows = (data.sessions || []).map(renderRow).join("");
    body.innerHTML = rows ||
      '<div class="sp-empty">（无会话，点 ＋ 新建）</div>';
  }

  function renderRow(s) {
    if (s.error) {
      return '<div class="sp-err">' + escapeHtml(s.error) + "</div>";
    }
    var id = s.id || s.sessionId || "?";
    var live = s.live !== false && s.status !== "closed";
    var cls = s.current ? "sp-id sp-cur" : "sp-id";
    var status = s.status || "?";
    var gen = s.runtimeGeneration || 1;
    // 行尾操作：当前 live 的给"关"，closed 的给"活"，都给"删"；全表再带"＋ 新"
    var ops = [
      live ? '<button data-op="close" data-sid="' + id + '">关</button>' : "",
      (!live ? '<button data-op="resume" data-sid="' + id + '">活</button>' : ""),
      '<button data-op="forget" data-sid="' + id + '">删</button>'
    ].join("");
    return (
      '<div class="sp-row">' +
      '<div class="' + cls + '">' + escapeHtml(id) +
      (s.current ? " ◀当前" : "") + "</div>" +
      '<div class="sp-meta">' + escapeHtml(status) + " · live=" +
      live + " · gen=" + gen + "</div>" +
      '<div class="sp-ops">' + ops + "</div>" +
      "</div>"
    );
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text);
    return div.innerHTML;
  }

  function onOpClick(e) {
    var btn = e.target.closest("button");
    if (!btn) return;
    var op = btn.getAttribute("data-op");
    var sid = btn.getAttribute("data-sid") || "";
    if (op === "refresh") { refresh(); return; }
    runOp(op, sid);
  }

  async function runOp(op, sid) {
    var res, data;
    try {
      res = await fetch(API + "/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: op, sid: sid })
      });
      data = await res.json();
    } catch (e) {
      toast("面板后端没响应，稍后再试");
      return;
    }
    toast(data.detail || (data.ok ? "完成" : "失败"));
    refresh(); // 操作完立刻刷一遍，别等轮询
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