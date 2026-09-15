/* 前端冒烟脚本：真 Chromium headless + CDP，零安装（用系统自带 Edge/Chrome）。

   为什么存在：前端换了框架之后，"页面能不能起来"没有单测盯着——
   290 多条 Python 测试一行都碰不到 public/。这个脚本补的就是那段空白：
   浏览器真打开、真点、真发消息，跑完给出 PASS/FAIL 清单。

   与 smoke_deepseek.py 同位置同理：真实环境的冒烟，不进单元测试。

   前置：node 18+；web_app 已在 127.0.0.1:8765 上跑着。零 API key 也行
   （FakeModel 离线跑一轮，够验证整条前端链路）。

   用法：
     node scripts/smoke_frontend.cjs
     node scripts/smoke_frontend.cjs --shot preview.png    # 顺手截图
     APP_URL=http://127.0.0.1:9999 node scripts/smoke_frontend.cjs

   两个设计点（都是被坑出来的）：
     - evaluate 必须查 exceptionDetails：页面里 Promise 抛异常时 CDP 本身
       不报错，只看 result.value 会拿到 {} 然后"假通过"——练习 17 的教训
       在 CDP 这一层的翻版。
     - 断言用 DOM/API 双向对照（清单行数 == /api/sessions 条数），
       只数 DOM 的话模板渲染错了也看不出来。
*/

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const APP = process.env.APP_URL || "http://127.0.0.1:8765/";
const DEBUG_PORT = 9333;
const BROWSERS = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/google-chrome",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];

const shotAt = process.argv.indexOf("--shot");
const shotPath = shotAt > -1 ? process.argv[shotAt + 1] : null;
// --dark：用 CDP 模拟 prefers-color-scheme: dark，把整套断言和截图在深色下
// 再跑一遍。深浅只是同一套 token 的两组取值，但"没跑过就等于没写"。
const darkMode = process.argv.includes("--dark");

const bin = BROWSERS.find((p) => fs.existsSync(p));
if (!bin) {
  console.error("找不到 Chrome / Edge");
  process.exit(2);
}

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "wywd-frontend-"));
const child = spawn(bin, [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  // 真实桌面尺寸：默认的 750px 窗口会让"内容居中成列"这条断言退化成
  // 恒真（列宽被拉满，左右间隙都是 0），截图也不像真实使用场景
  "--window-size=1280,860",
  `--remote-debugging-port=${DEBUG_PORT}`, `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ═══════════════════════════════════════════════════════════
// 0. 纯函数自检（不进浏览器）
// ═══════════════════════════════════════════════════════════
//
// 这个项目没有 JS 单测框架，冒烟脚本就是唯一的 JS 试验台。markdown 解析器
// 是纯函数、零 import，用 data: URL 直接 import 进来就能跑——快两个数量级，
// 而且能把它单独钉死（浏览器里只能间接看渲染结果）。

let pureFailed = 0;

function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  const ok = g === w;
  if (!ok) pureFailed++;
  console.log((ok ? "  PASS  " : "  FAIL  ") + name +
    (ok ? "" : "\n        得到 " + g + "\n        期望 " + w));
}

function shapes(blocks) { return blocks.map((b) => b.type); }

function allTokenTypes(blocks, acc = new Set()) {
  for (const b of blocks) {
    acc.add(b.type);
    for (const t of b.inline || []) {
      acc.add(t.type);
      for (const x of t.inline || []) acc.add(x.type);
    }
    for (const q of b.blocks || []) allTokenTypes([q], acc);
    for (const it of b.items || []) allTokenTypes(it.blocks, acc);
  }
  return acc;
}

async function pureSection() {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "public", "src", "markdown.js"), "utf8");
  // 零 import 的文件才能这么进（有 bare specifier 就解析不了）
  const mod = await import("data:text/javascript;base64," +
    Buffer.from(src).toString("base64"));
  const { parseMarkdown, safeHref } = mod;

  console.log("== 0. markdown 解析器（纯函数，data: URL 直跑）==");

  eq("段落软换行 → br",
     parseMarkdown("甲\n乙"),
     [{ type: "p", inline: [{ type: "text", text: "甲" },
                            { type: "br" },
                            { type: "text", text: "乙" }] }]);

  eq("围栏代码块（保语言，内容原样）",
     parseMarkdown("```python\nprint(1)\n```"),
     [{ type: "code", lang: "python", text: "print(1)" }]);

  eq("围栏没闭合也照样闭合（模型被截断的常事）",
     parseMarkdown("前\n```js\nlet a = 1;"),
     [{ type: "p", inline: [{ type: "text", text: "前" }] },
      { type: "code", lang: "js", text: "let a = 1;" }]);

  eq("标题 + 分隔线",
     shapes(parseMarkdown("# 甲\n\n---\n\n## 乙")),
     ["heading", "hr", "heading"]);

  eq("无序列表两项",
     parseMarkdown("- 甲\n- 乙")[0].items.map((it) => it.blocks[0].inline[0].text),
     ["甲", "乙"]);

  eq("有序列表标记成 ordered",
     (() => { const l = parseMarkdown("1. 甲\n2. 乙")[0];
              return [l.type, l.ordered, l.items.length]; })(),
     ["list", true, 2]);

  eq("缩进续行长成嵌套列表",
     (() => { const l = parseMarkdown("- 甲\n  - 子\n- 乙")[0];
              return [l.items.length, l.items[0].blocks.map((b) => b.type)]; })(),
     [2, ["p", "list"]]);

  eq("引用块",
     shapes(parseMarkdown("> 甲\n> 乙")),
     ["quote"]);

  eq("行内：代码/粗/斜/删除",
     parseMarkdown("a `x` **b** *c* ~~d~~")[0].inline.map((t) => t.type),
     ["text", "code", "text", "strong", "text", "em", "text", "del"]);

  eq("粗斜体 ***x*** 是 strong 套 em",
     parseMarkdown("***x***")[0].inline[0],
     { type: "strong", inline: [{ type: "em",
       inline: [{ type: "text", text: "x" }] }] });

  eq("链接白名单：https/相对/锚点放行，javascript/data 归零",
     [safeHref("https://a.com"), safeHref("javascript:alert(1)"),
      safeHref("data:text/html,x"), safeHref("/api/x"), safeHref("#a")],
     ["https://a.com", null, null, "/api/x", "#a"]);

  // ── 安全铁律：解析器只产出结构 token，"HTML"这种类型根本不存在，
  //    渲染层因此没有任何 innerHTML 可写（练习 13 的转义纪律升级成物理隔离）
  const evil = parseMarkdown('<img src=x onerror=alert(1)>\n\n<script>alert(2)</script>');
  eq("HTML 输入只变成 text token",
     [...allTokenTypes(evil)].sort(), ["p", "text"]);
  eq("HTML 原样躺在 text 里（由 Vue 插值转义）",
     evil[0].inline[0].text, "<img src=x onerror=alert(1)>");

  const evilLink = parseMarkdown("[点我](javascript:alert(1))");
  eq("javascript: 链接不产生 link token",
     [...allTokenTypes(evilLink)].sort(), ["p", "text"]);

  eq("正常链接才产生 link token",
     parseMarkdown("[点我](https://a.com)")[0].inline[0],
     { type: "link", href: "https://a.com",
       inline: [{ type: "text", text: "点我" }] });
}

async function findPage() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (err) { /* 浏览器还没起来 */ }
    await sleep(250);
  }
  throw new Error("CDP 端口没起来");
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let id = 0;
    const pending = new Map();
    const listeners = [];
    const api = {
      send(method, params = {}) {
        const myId = ++id;
        ws.send(JSON.stringify({ id: myId, method, params }));
        return new Promise((res, rej) => pending.set(myId, { res, rej }));
      },
      on(fn) { listeners.push(fn); },
      close() { ws.close(); },
    };
    ws.onopen = () => resolve(api);
    ws.onerror = (e) => reject(new Error("WS 错误: " + e.message));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
      } else {
        listeners.forEach((fn) => fn(msg));
      }
    };
  });
}

(async () => {
  await pureSection();

  const page = await findPage();
  const cdp = await connect(page.webSocketDebuggerUrl);

  const problems = [];
  const requests = [];   // CDP 抓的真实请求：EventSource 是长连，永远不"完成"，
                         // performance resource timing 里查不到它——只有这一层
                         // 能看到"到底有没有在轮询"。
  cdp.on((msg) => {
    if (msg.method === "Network.requestWillBeSent") {
      requests.push(msg.params.request.url);
    }
    if (msg.method === "Runtime.exceptionThrown") {
      problems.push("异常: " + (msg.params.exceptionDetails.exception?.description
        || msg.params.exceptionDetails.text));
    }
    if (msg.method === "Runtime.consoleAPICalled" &&
        ["error", "warning"].includes(msg.params.type)) {
      problems.push(msg.params.type + ": " +
        msg.params.args.map((a) => a.value ?? a.description).join(" "));
    }
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      problems.push("log: " + msg.params.entry.text);
    }
  });

  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Network.enable");
  await cdp.send("Page.enable");
  if (darkMode) {
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "dark" }] });
  }
  await cdp.send("Page.navigate", { url: APP });
  await sleep(2500);

  let failed = 0;

  const check = async (name, expr) => {
    const r = await cdp.send("Runtime.evaluate", {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) {
      console.log("  FAIL  " + name + "  →  页面异常: " +
        (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
      failed++;
      return false;
    }
    const val = r.result.value;
    if (val === undefined) {
      console.log("  FAIL  " + name + "  →  表达式没有返回值（选择器落空？）");
      failed++;
      return false;
    }
    const ok = val === true || (typeof val === "object" && val !== null && val.ok !== false);
    if (!ok) failed++;
    console.log((ok ? "  PASS  " : "  FAIL  ") + name + "  →  " +
      String(JSON.stringify(val)).slice(0, 220));
    return ok;
  };

  console.log("== 1. 挂载与骨架 ==");
  await check("Vue 挂载（#app 非空 + 各根节点齐）", `(() => {
    const ids = ['sidebar','main','session-list','messages','live-feed','input-bar','approval-overlay','toast'];
    const missing = ids.filter((i) => !document.getElementById(i));
    return { ok: missing.length === 0 && document.getElementById('app').children.length > 0,
             missing, appChildren: document.getElementById('app').children.length };
  })()`);
  await check("配色跟随系统（浅色/深色同一套 token 的两种取值）", `(() => {
    const rgb = getComputedStyle(document.body).backgroundColor;
    const m = rgb.match(/\\d+/g).map(Number);
    const lum = (m[0] * 0.299 + m[1] * 0.587 + m[2] * 0.114) / 255;
    return { ok: (lum < 0.5) === ${darkMode}, bg: rgb, lum: +lum.toFixed(3) };
  })()`);

  console.log("== 2. 会话清单（fetch → reactive → v-for）==");
  await check("清单行数 == /api/sessions 条数", `(async () => {
    const rows = document.querySelectorAll('#session-list .row').length;
    const api = await (await fetch('/api/sessions')).json();
    return { ok: rows === (api.sessions||[]).length, rows, api: (api.sessions||[]).length,
             listError: !!document.querySelector('#session-list .err') };
  })()`);
  await check("每行有徽标 + 行操作按钮", `(() => {
    const row = document.querySelector('#session-list .row');
    if (!row) return { ok: true, why: '无会话，跳过' };
    return { ok: !!row.querySelector('.badge') && row.querySelectorAll('.r-ops button').length >= 2,
             badge: row.querySelector('.badge').textContent,
             ops: [...row.querySelectorAll('.r-ops button')].map(b => b.textContent) };
  })()`);

  console.log("== 3. 点会话 → 拉历史 → 渲染气泡 ==");
  await check("点第一行后出现历史气泡", `(async () => {
    const row = document.querySelector('#session-list .row');
    if (!row) return { ok: true, why: '无会话，跳过' };
    row.click();
    await new Promise(r => setTimeout(r, 900));
    const head = document.getElementById('chat-sid').textContent;
    return { ok: /^sess_/.test(head), head,
             bubbles: document.querySelectorAll('#messages .msg').length,
             classes: [...document.querySelectorAll('#messages .msg')].map(m => m.className) };
  })()`);
  await check("输入框启用（canSend computed 生效）", `(() => {
    const i = document.getElementById('input'), b = document.getElementById('btn-send');
    return { ok: !i.disabled && !b.disabled, input: i.disabled, send: b.disabled };
  })()`);

  if (shotPath) {
    const cap = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(shotPath, Buffer.from(cap.data, "base64"));
    console.log("  ----  截图已存: " + shotPath);
  }

  console.log("== 4. 审批卡、toast、SSE 灯与布局 ==");
  await check("审批卡与 toast 默认隐藏", `(() => {
    const a = getComputedStyle(document.getElementById('approval-overlay')).display;
    const t = getComputedStyle(document.getElementById('toast')).display;
    return { ok: a === 'none' && t === 'none', approval: a, toast: t };
  })()`);
  await check("SSE 长连已连上（顶栏灯变绿）", `(() => {
    const dot = document.getElementById('stream-dot');
    return { ok: !!dot && dot.classList.contains('on'), cls: dot && dot.className,
             title: dot && dot.title };
  })()`);
  await check("/api/status 通，且顶栏那行账与数据一致（没数据就不编）", `(async () => {
    const st = await (await fetch('/api/status')).json();
    const line = document.getElementById('cost-line');
    const has = Array.isArray(st.modelCost);
    return { ok: typeof st.handlers === 'number' && !!line === has,
             handlers: st.handlers, hasModelCost: has, lineShown: !!line,
             lineText: line ? line.textContent.trim().slice(0, 40) : null };
  })()`);
  await check("内容居中成列、两侧不横向溢出", `(() => {
    const el = (i) => document.getElementById(i);
    const over = { doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                   main: el('main').scrollWidth - el('main').clientWidth,
                   msgs: el('messages').scrollWidth - el('messages').clientWidth };
    const thread = document.querySelector('#messages .thread');
    if (!thread) return { ok: false, why: '没有 .thread 内容列' };
    const tr = thread.getBoundingClientRect();
    const ms = el('messages').getBoundingClientRect();
    const gapL = Math.round(tr.left - ms.left);
    const gapR = Math.round(ms.right - tr.right);
    const user = document.querySelector('.msg.user');
    const userW = user ? Math.round(user.getBoundingClientRect().width) : 0;
    const colW = Math.round(tr.width);
    return { ok: over.doc <= 0 && over.main <= 0 && over.msgs <= 0
             && colW <= 800 && Math.abs(gapL - gapR) < 14
             && userW > 0 && userW <= colW,
             colW, gapL, gapR, userW, sidebar: Math.round(el('sidebar').getBoundingClientRect().width) };
  })()`);

  console.log("== 5. 发送链路（新建 → v-model → POST → 重拉历史）==");
  await check("点 ＋ 新建出会话，输入框可用", `(async () => {
    const btn = document.getElementById('btn-create');
    if (!btn) return { ok: false, why: '没有 #btn-create' };
    btn.click();
    await new Promise(r => setTimeout(r, 1200));
    const head = document.getElementById('chat-sid').textContent;
    return { ok: /^sess_/.test(head) && !document.getElementById('input').disabled, head };
  })()`);
  await check("发送后气泡增加、输入清空、输入栏回弹", `(async () => {
    const input = document.getElementById('input');
    // 故意带 HTML：用户输入必须原样显示（不是"转义后显示"，是压根没被当标签）
    input.value = '<b>加粗?</b> 报个时间';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    const modelSynced = input.value.includes('加粗');
    document.getElementById('btn-send').click();
    await new Promise(r => setTimeout(r, 2500));
    return { ok: modelSynced && document.querySelectorAll('#messages .msg').length > 0
             && !document.getElementById('input').disabled,
             modelSynced, inputCleared: input.value === '',
             bubbles: document.querySelectorAll('#messages .msg').length };
  })()`);
  await check("markdown 真的渲染了（模型回复里的缩进列表 → ul>li）", `(() => {
    const lis = [...document.querySelectorAll('#messages .msg.assistant ul.md-list li')];
    return { ok: lis.length >= 2, count: lis.length,
             first: lis[0] ? lis[0].textContent.trim().slice(0, 24) : null };
  })()`);
  await check("用户输入的 HTML 原样躺着（没有变成标签）", `(() => {
    const user = document.querySelector('#messages .msg.user');
    const text = user ? user.textContent : '';
    return { ok: text.includes('<b>加粗?</b>') && user.querySelector('b') === null,
             text: text.slice(0, 40), boldTags: user.querySelectorAll('b').length };
  })()`);
  await check("聊天区没有注入出来的可执行标签", `(() => {
    const box = document.getElementById('messages');
    const bad = box.querySelectorAll('script,img,iframe,object,embed,style');
    const handlers = [...box.querySelectorAll('*')]
      .filter((el) => [...el.attributes].some((a) => a.name.startsWith('on')));
    return { ok: bad.length === 0 && handlers.length === 0,
             tags: bad.length, onAttrs: handlers.length };
  })()`);
  await check("直播走 SSE 且一轮跑完后**不被抹掉**", `(() => {
    const lines = [...document.querySelectorAll('#live-feed .lf-line')];
    const text = lines.map(l => l.textContent.trim());
    const kinds = lines.map(l => l.className.replace('lf-line lf-', ''));
    return { ok: text.some(t => t.includes('第 1 轮'))
             && text.some(t => t.includes('本轮完成')),
             kinds, text };
  })()`);
  await check("结局卡正常时不出现（completed 不吓唬人）", `(() => {
    return { ok: document.querySelector('#messages .msg.notice') === null,
             notice: !!document.querySelector('#messages .msg.notice') };
  })()`);
  if (shotPath) {
    // 第二张：跑完一轮后的状态（历史 + 直播轨迹都在），这是信息量最大的一帧
    const cap = await cdp.send("Page.captureScreenshot", { format: "png" });
    const live = shotPath.replace(/(\.png)?$/i, "-live$1");
    fs.writeFileSync(live, Buffer.from(cap.data, "base64"));
    console.log("  ----  截图已存: " + live);
  }
  await check("事件走 SSE 长连，不再有轮询请求", `(() => {
    const hits = ${JSON.stringify(requests)}.filter((u) => u.includes('/api/events'));
    const streams = hits.filter((u) => u.includes('/stream'));
    const polls = hits.filter((u) => !u.includes('/stream'));
    return { ok: streams.length > 0 && polls.length === 0,
             streams: streams.length, polls: polls.length, sample: streams[0] || null };
  })()`);
  await check("关掉当前会话 → 输入栏锁住、历史仍可读", `(async () => {
    const cur = document.getElementById('chat-sid').textContent;
    const row = [...document.querySelectorAll('#session-list .row')]
      .find((r) => r.textContent.includes(cur));
    const closeBtn = row && [...row.querySelectorAll('button')]
      .find((b) => b.textContent.trim() === '关');
    if (!closeBtn) return { ok: false, why: '没找到"关"按钮' };
    closeBtn.click();
    await new Promise(r => setTimeout(r, 1200));
    return { ok: document.getElementById('btn-send').disabled
             && document.querySelectorAll('#messages .msg').length > 0,
             disabled: document.getElementById('btn-send').disabled };
  })()`);
  await check("清理：close → forget 测试会话", `(async () => {
    const cur = document.getElementById('chat-sid').textContent;
    const post = (body) => fetch('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then((r) => r.json());
    const close = await post({ action: 'close', sid: cur });
    const forget = await post({ action: 'forget', sid: cur });
    return { ok: close.ok === true && forget.ok === true, sid: cur,
             close: close.detail, forget: forget.detail };
  })()`);

  console.log("== 6. 报错可见性（后端拒绝这一轮时用户看得见）==");
  await check("给已关闭的会话发消息 → 结局卡出现并可读", `(async () => {
    const post = (body) => fetch('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then((r) => r.json());

    document.getElementById('btn-create').click();
    await new Promise(r => setTimeout(r, 1200));
    const sid = document.getElementById('chat-sid').textContent;

    // 绕开界面把它关掉：本 tab 还以为能发，于是 POST 会拿到后端的拒绝
    await post({ action: 'close', sid: sid });

    const input = document.getElementById('input');
    input.value = '这条应该发不出去';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('btn-send').click();
    await new Promise(r => setTimeout(r, 1500));

    const card = document.querySelector('#messages .msg.notice');
    const text = card ? card.textContent.trim() : '';
    const feed = document.getElementById('live-feed').textContent.trim();
    return { ok: !!card && card.classList.contains('lv-error') && text.includes('closed'),
             cls: card ? card.className : null, text: text.slice(0, 80),
             stillCanSend: !document.getElementById('btn-send').disabled };
  })()`);
  await check("清理：close → forget 这个会话", `(async () => {
    const sid = document.getElementById('chat-sid').textContent;
    const post = (body) => fetch('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) }).then((r) => r.json());
    await post({ action: 'close', sid: sid });
    const forget = await post({ action: 'forget', sid: sid });
    return { ok: forget.ok === true, sid, forget: forget.detail };
  })()`);

  console.log("== 7. 交互完整性（折叠 / 搜索 / 抽屉 / 回到最新 / 复制 / 审批 a11y）==");
  await check("侧栏折叠成轨、再展开（宽度真的变）", `(async () => {
    const sb = document.getElementById('sidebar');
    const wide = sb.getBoundingClientRect().width;
    document.getElementById('btn-sidebar').click();
    await new Promise(r => setTimeout(r, 300));
    const narrow = sb.getBoundingClientRect().width;
    const collapsed = sb.classList.contains('collapsed');
    document.getElementById('btn-sidebar').click();
    await new Promise(r => setTimeout(r, 300));
    return { ok: collapsed && narrow < wide && sb.getBoundingClientRect().width === wide,
             wide, narrow, backTo: sb.getBoundingClientRect().width };
  })()`);
  await check("搜索过滤会话 + 无匹配时给空态文案", `(async () => {
    const input = document.getElementById('search');
    const set = (v) => { input.value = v;
      input.dispatchEvent(new Event('input', { bubbles: true })); };
    const rows = () => document.querySelectorAll('#session-list .row').length;

    // 断言要自洽：拿 DOM 自己的前后数量比。之前拿 /api/sessions 的数字比，
    // 结果 5 秒轮询在检查过程中刷了一次，DOM 从 5 变 6，测试自己判自己失败。
    const before = rows();
    set('sess_0001');
    await new Promise(r => setTimeout(r, 80));
    const hit = rows();

    set('zzz-no-such');
    await new Promise(r => setTimeout(r, 80));
    const empty = document.querySelector('#session-list .empty');
    const emptyText = empty ? empty.textContent : '';
    const zero = rows();

    set('');
    await new Promise(r => setTimeout(r, 80));
    const restored = rows();
    return { ok: before >= 1 && hit === 1 && hit < before && zero === 0
             && /没有匹配/.test(emptyText) && restored === before,
             before, hit, zero, emptyText, restored };
  })()`);
  await check("运维抽屉：状态 / 成本表 / 日志三块都有内容", `(async () => {
    document.getElementById('btn-drawer').click();
    await new Promise(r => setTimeout(r, 900));
    const dw = document.getElementById('drawer');
    const kv = [...dw.querySelectorAll('.dw-kv div')].map(d => d.textContent.trim());
    const costRows = dw.querySelectorAll('.dw-table tbody tr').length;
    const logText = (dw.querySelector('.dw-logs') || {}).textContent || '';
    return { ok: dw.classList.contains('open') && dw.getBoundingClientRect().width > 100
             && kv.length >= 3 && costRows >= 1 && logText.length > 0,
             width: Math.round(dw.getBoundingClientRect().width), kv,
             costRows, logsHead: logText.slice(0, 40) };
  })()`);
  await check("Esc 收起抽屉", `(async () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await new Promise(r => setTimeout(r, 300));
    const dw = document.getElementById('drawer');
    return { ok: !dw.classList.contains('open'), width: Math.round(dw.getBoundingClientRect().width) };
  })()`);
  if (shotPath) {
    // 第三张：抽屉打开着的样子（状态 / 成本表 / 日志）。
    // 注意点击必须走 CDP——这里是 Node 进程，没有 document。
    await cdp.send("Runtime.evaluate", {
      expression: "document.getElementById('btn-drawer').click()" });
    await sleep(900);
    const cap = await cdp.send("Page.captureScreenshot", { format: "png" });
    const dw = shotPath.replace(/(\.png)?$/i, "-drawer$1");
    fs.writeFileSync(dw, Buffer.from(cap.data, "base64"));
    console.log("  ----  截图已存: " + dw);
    await check("重新打开后 Esc 依然能收起抽屉", `(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(r => setTimeout(r, 300));
      return { ok: !document.getElementById('drawer').classList.contains('open') };
    })()`);
  }
  await check("上翻历史时亮出'回到最新'，点了回到底部", `(async () => {
    // 内容不够长就滚不动，"按钮该不该出现"根本测不到。用探针把消息撑长，
    // 让这条断言与"当前会话刚好有多少条"解耦——否则它会随会话内容飘。
    const { state } = await import('/src/store.js');
    const saved = state.messages.slice();
    state.messages = saved.concat(Array.from({ length: 40 },
      (_, i) => ({ role: 'assistant', content: '探针第 ' + i + ' 条，用来把滚动条撑出来。' })));
    await new Promise(r => setTimeout(r, 200));

    const box = document.getElementById('messages');
    const overflow = box.scrollHeight - box.clientHeight;
    box.scrollTop = 0;
    box.dispatchEvent(new Event('scroll', { bubbles: true }));
    await new Promise(r => setTimeout(r, 150));

    const btn = document.getElementById('jump-bottom');
    const appeared = !!btn;
    if (btn) btn.click();
    await new Promise(r => setTimeout(r, 300));
    const gone = document.getElementById('jump-bottom') === null;
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 48;

    state.messages = saved;                 // 探针用完立刻收走
    return { ok: overflow > 60 && appeared && gone && atEnd,
             overflow, appeared, gone, atEnd };
  })()`);
  await check("代码块渲染出复制按钮，点击把源码交给剪贴板", `(async () => {
    // 在真页面里隔离挂载一个 Markdown 组件——模型离线时不产代码块，
    // 靠罐头数据造不出来，只有这一招能测到"渲染 + 复制"这条完整路径
    const { Markdown } = await import('/src/components.js');
    const { createApp, h } = await import('vue');

    let copied = null;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t) => { copied = t; return Promise.resolve(); } },
    });

    const host = document.createElement('div');
    document.body.appendChild(host);
    // 围栏用 charCode 拼反引号：这段表达式本身躺在 Node 的模板字符串里，
    // 直接手写反引号会把模板字符串提前闭合（刚踩过）
    const F = String.fromCharCode(96, 96, 96);
    const src = '看代码：\\n\\n' + F + 'js\\nlet a = 1;\\n' + F + '\\n';
    const app = createApp({ render: () => h(Markdown, { text: src }) });
    app.mount(host);
    await new Promise(r => setTimeout(r, 80));

    const code = host.querySelector('pre.md-pre code');
    const btn = host.querySelector('button.md-copy');
    const codeText = code ? code.textContent : null;
    if (btn) btn.click();
    await new Promise(r => setTimeout(r, 80));

    app.unmount();
    host.remove();
    delete navigator.clipboard;
    return { ok: !!btn && codeText === 'let a = 1;' && copied === codeText,
             hasButton: !!btn, codeText, copied };
  })()`);
  await check("审批卡：role=alertdialog、自动聚焦卡片、Esc 只隐藏不拒绝", `(async () => {
    const { approval } = await import('/src/store.js');
    approval.ticket = 'probe-ticket';
    approval.ruleId = 'path.write_ask';
    approval.reason = '探针理由：界内写需要审批';
    await new Promise(r => setTimeout(r, 150));

    const card = document.getElementById('approval-card');
    const shown = getComputedStyle(document.getElementById('approval-overlay')).display;
    const role = card.getAttribute('role');
    const focusedCard = document.activeElement === card;
    const allowFocused = document.activeElement === document.getElementById('a-allow');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await new Promise(r => setTimeout(r, 150));
    const hidden = getComputedStyle(document.getElementById('approval-overlay')).display;
    const cleared = approval.ticket === null;
    const notRejected = approval.submitting === false;   // Esc ≠ 拒绝
    return { ok: shown === 'block' && role === 'alertdialog' && focusedCard
             && !allowFocused && hidden === 'none' && cleared && notRejected,
             role, focusedCard, allowFocused, cleared, notRejected };
  })()`);

  console.log("== 8. console 干净 ==");
  await check("无 error / warn / 未捕获异常", `({ ok: ${problems.length === 0} })`);
  problems.forEach((p) => console.log("      ! " + p));

  cdp.close();
  child.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (err) { /* ignore */ }
  const bad = failed + pureFailed;
  console.log(bad ? `\n${bad} 项失败` : "\n全部通过");
  process.exit(bad ? 1 : 0);
})().catch((err) => {
  console.error("冒烟失败:", err.message);
  child.kill();
  process.exit(1);
});
