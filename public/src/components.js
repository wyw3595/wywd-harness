/* 视图层：四个组件。免构建路线拿不到 .vue 单文件组件，所以模板写成
   字符串——代价是编辑器不给高亮，收益是浏览器直接跑、零工具链。

   一条铁律：所有 LLM 输出都走 {{ }} 插值，绝不用 v-html。
   练习 13 手写的 html.escape 在这里由框架代劳（Vue 的模板插值默认转义
   HTML），但"危险的是真实标签"这条判断没有变——一旦哪天图省事写了
   v-html，XSS 就直接进门。
*/

import { computed, h, nextTick, ref, watch } from "vue";
import { parseMarkdown } from "./markdown.js";
import { WorkspacePanel } from "./workspace.js";
import {
  approval,
  answerApproval,
  canSend,
  costLine,
  costTitle,
  createSession,
  notify,
  refreshSessions,
  runAction,
  selectSession,
  send,
  state,
  toast,
} from "./store.js";
import { filterRows, toggleDrawer, toggleSidebar, ui } from "./ui.js";

// ═══════════════════════════════════════════════════════════
// 复制到剪贴板（markdown 代码块 / 消息都要用）
// ═══════════════════════════════════════════════════════════

/** navigator.clipboard 只在**安全上下文**里存在（localhost / 127.0.0.1 算，
    局域网 IP 访问就不算了）。拿不到就诚实说一声——点了按钮以为复制成功是
    最讨厌的静默失败。 */
async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    notify("已复制" + what);
  } catch (err) {
    notify("复制失败：这个来源不给用剪贴板");
  }
}

// ═══════════════════════════════════════════════════════════
// Markdown — 数据树 → VNode（薄到只剩映射表）
// ═══════════════════════════════════════════════════════════
//
// 为什么用 render() 手写 VNode 而不是模板：数据树是递归的，模板表达递归
// 要么靠自引用组件、要么靠 v-for 套娃，都比一个 map 难读。而且这条路径上
// **没有 innerHTML 可写**——注入面不存在，不需要"记得转义"。

function inlineVNodes(tokens) {
  return tokens.map((t, i) => {
    if (t.type === "code") return h("code", { key: i, class: "md-code" }, t.text);
    if (t.type === "strong") return h("strong", { key: i }, inlineVNodes(t.inline));
    if (t.type === "em") return h("em", { key: i }, inlineVNodes(t.inline));
    if (t.type === "del") return h("del", { key: i }, inlineVNodes(t.inline));
    if (t.type === "br") return h("br", { key: i });
    if (t.type === "link") {
      return h("a", { key: i, href: t.href, target: "_blank",
                      rel: "noopener noreferrer" }, inlineVNodes(t.inline));
    }
    return t.text == null ? "" : t.text;
  });
}

function blockVNodes(blocks) {
  return blocks.map((b, i) => {
    if (b.type === "code") {
      // 代码块套一层容器放"复制"按钮——按钮不进 <pre>（那样会被一起复制走，
      // 而且 pre 里不允许交互内容）
      return h("div", { key: i, class: "md-pre-wrap" }, [
        h("button", {
          class: "md-copy", type: "button", title: "复制这段代码",
          "aria-label": "复制这段代码",
          onClick: () => copyText(b.text, "代码"),
        }, "复制"),
        h("pre", { class: "md-pre" },
          [h("code", { class: b.lang ? "language-" + b.lang : "" }, b.text)]),
      ]);
    }
    if (b.type === "heading") {
      return h("h" + b.level, { key: i, class: "md-h" }, inlineVNodes(b.inline));
    }
    if (b.type === "hr") return h("hr", { key: i, class: "md-hr" });
    if (b.type === "quote") {
      return h("blockquote", { key: i, class: "md-quote" }, blockVNodes(b.blocks));
    }
    if (b.type === "list") {
      return h(b.ordered ? "ol" : "ul", { key: i, class: "md-list" },
               b.items.map((item, j) => h("li", { key: j }, blockVNodes(item.blocks))));
    }
    return h("p", { key: i, class: "md-p" }, inlineVNodes(b.inline));
  });
}

export const Markdown = {
  name: "Markdown",
  props: { text: { type: String, default: "" } },
  render() { return blockVNodes(parseMarkdown(this.text)); },
};

// ═══════════════════════════════════════════════════════════
// SessionList — 侧边栏（清单 + 行操作）
// ═══════════════════════════════════════════════════════════

export const SessionList = {
  name: "SessionList",

  // 工作区面板挂在侧栏里：它是"模型在哪个目录里干活"的显示与切换处，
  // 和会话清单一上一下、共用同一条侧栏（Codex 也是把空间信息放侧栏）。
  components: { WorkspacePanel },

  setup() {
    // armed 是纯 UI 状态（"这一行正在等待二次确认"），归组件自己管，
    // 不进 store——数据层只放服务端有对应物的东西。
    const armed = ref("");
    let armTimer = null;

    // 过滤后的行：搜索词在 ui 里（本 tab 的视图状态），过滤是纯函数
    const rows = computed(() => filterRows(state.sessions, ui.search));

    // 底部"项目"标识 = 当前会话的工作目录（Codex 把项目空间挂在侧栏底部）
    const cwd = computed(() => {
      const row = state.sessions.find((r) => r.id === state.sid) ||
                  state.sessions[0];
      return (row && row.cwd) || "";
    });

    function onForget(row) {
      if (armed.value !== row.id) {          // 第一下：武装
        armed.value = row.id;
        clearTimeout(armTimer);
        armTimer = setTimeout(() => { armed.value = ""; }, 3000);
        return;
      }
      armed.value = "";                      // 第二下：真删
      runAction("forget", row.id);
    }

    return { state, ui, rows, cwd, armed, onForget, createSession,
             refreshSessions, runAction, selectSession, toggleSidebar,
             toggleDrawer };
  },

  template: `
    <aside id="sidebar" :class="{ collapsed: ui.sidebarCollapsed }">
      <div class="sb-head">
        <button id="btn-sidebar" type="button" class="icon-btn"
                :title="ui.sidebarCollapsed ? '展开侧栏' : '折叠侧栏'"
                :aria-label="ui.sidebarCollapsed ? '展开侧栏' : '折叠侧栏'"
                :aria-expanded="ui.sidebarCollapsed ? 'false' : 'true'"
                @click="toggleSidebar">{{ ui.sidebarCollapsed ? '&#187;' : '&#171;' }}</button>
        <span class="sb-name">会话</span>
        <span class="flex"></span>
        <button id="btn-create" type="button" class="icon-btn" title="新建会话"
                aria-label="新建会话" @click="createSession">&#65291;</button>
        <button id="btn-refresh" type="button" class="icon-btn" title="刷新清单"
                aria-label="刷新清单" @click="refreshSessions">&#8635;</button>
      </div>
      <div class="sb-search" v-if="!ui.sidebarCollapsed">
        <input id="search" v-model="ui.search" type="search" autocomplete="off"
               placeholder="搜索会话" aria-label="按 id 或状态过滤会话">
      </div>
      <div id="session-list" role="list">
        <div v-if="state.listError" class="err">{{ state.listError }}</div>
        <div v-else-if="!rows.length" class="empty">{{ ui.search ? '没有匹配的会话' : '还没有会话，点 ＋ 开始' }}</div>
        <template v-for="row in rows" :key="row.id || row.error">
          <div v-if="row.error" class="err">{{ row.error }}</div>
          <div v-else class="row" role="listitem" :class="{ cur: row.id === state.sid }" @click="selectSession(row.id)">
            <div class="r-title">
              <span class="r-tt" :title="row.title">{{ row.title }}</span>
              <span v-if="row.id === state.sid" class="badge">当前</span>
              <span v-else-if="row.live" class="badge">live</span>
              <span v-else class="badge off">closed</span>
            </div>
            <div class="r-meta">{{ row.id }} &#183; {{ row.status }} &#183; gen={{ row.generation }} &#183; {{ row.count }} 条</div>
            <div class="r-ops" @click.stop>
              <button v-if="row.live" type="button" :aria-label="'关闭会话 ' + row.id" @click="runAction('close', row.id)">关</button>
              <button v-else type="button" :aria-label="'复活会话 ' + row.id" @click="runAction('resume', row.id)">活</button>
              <button class="del" type="button" :class="{ arm: armed === row.id }"
                      :aria-label="'删除会话 ' + row.id + '（点两次确认）'"
                      @click="onForget(row)">{{ armed === row.id ? '确认?' : '删' }}</button>
            </div>
          </div>
        </template>
      </div>
      <WorkspacePanel />
      <div class="sb-foot" v-if="!ui.sidebarCollapsed" :title="cwd">{{ cwd || "—" }}</div>
    </aside>
  `,
};

// ═══════════════════════════════════════════════════════════
// ChatArea — 聊天区（历史 + 直播 + 输入栏）
// ═══════════════════════════════════════════════════════════

/** 把一条原始消息整形为"画哪张卡"的决定——视图里不该有 if/else 七连。 */
function bubble(m) {
  const role = m.role || "?";
  let text = (m.content || "").trim();
  if (!text && m.tool_calls && m.tool_calls.length) {
    text = "🤖 调用工具：" + m.tool_calls.map((t) => t.name || "?").join(", ");
  }
  if (!text) return { cls: "system", body: "（空消息）" };
  if (role === "user") return { cls: "user", body: text };
  // 只有模型/系统的发言过 markdown：用户输入原样显示（别把用户打的字符
  // 当语法解释），工具结果是机器输出（缩进敏感，原样更准）
  if (role === "assistant") return { cls: "assistant", body: text, md: true };
  // 系统提示折起来。它是 transcript 的第一条（历史=真相，不能藏），但每段
  // 对话一进来先看到一大段系统提示是纯噪音——Codex/ChatGPT 都不展示它。
  if (role === "system") return { cls: "system", body: text, sys: true };
  if (role === "tool") {
    return { cls: "tool", label: "工具结果",
             body: text.length > 400 ? text.slice(0, 400) + "…" : text };
  }
  return { cls: "system", body: "[" + role + "] " + text };
}

export const ChatArea = {
  name: "ChatArea",

  components: { Markdown },

  setup() {
    const draft = ref("");
    const box = ref(null);
    const inputEl = ref(null);
    const atBottom = ref(true);      // 用户是不是正贴着底部看

    const rendered = computed(() => state.messages.map(bubble));

    function toBottom() {
      nextTick(() => {
        const el = box.value;
        if (el) el.scrollTop = el.scrollHeight;
        atBottom.value = true;
      });
    }

    /** 滚动位置 → "贴底"布尔。48px 容差：scrollTop 有亚像素误差，
        卡在 0 会让按钮永远亮着（明明已经到底了）。 */
    function onScroll() {
      const el = box.value;
      if (!el) return;
      atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    }

    /** composer 自动长高：先归零再按内容量高度，否则只会越撑越高。
        上限 200px 与 CSS 的 max-height 一致（超出后由 textarea 自己滚）。 */
    function autoGrow() {
      const el = inputEl.value;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }

    // 新内容到来时**只在用户本来就贴着底**才自动滚。上翻看历史时被强行
    // 拽回底部很烦人——那种情况改为亮出"回到最新"，把决定权交回去。
    watch([() => state.messages.length, () => state.live.length,
           () => state.chatError, () => state.notice],
          () => { if (atBottom.value) toBottom(); });

    function submit() {
      const text = draft.value;
      if (!text.trim() || !canSend.value) return;
      draft.value = "";                       // 发完清空
      nextTick(autoGrow);                     // 高度跟着收回一行
      send(text);
    }

    return { state, ui, canSend, costLine, costTitle, draft, box, inputEl,
             rendered, atBottom, onScroll, toBottom, autoGrow, submit,
             toggleDrawer };
  },

  template: `
    <main id="main">
      <header id="chat-head">
        <span id="stream-dot" :class="state.streamUp === true ? 'on' : (state.streamUp === false ? 'off' : 'idle')"
              :title="state.streamUp === true ? '事件流已连接'
                    : (state.streamUp === false ? '事件流断开，浏览器重连中' : '事件流连接中')"></span>
        <template v-if="state.sid">会话 <b id="chat-sid">{{ state.sid }}</b></template>
        <span v-else class="tb-ghost">未选择会话</span>
        <span id="cost-line" v-if="costLine" :title="costTitle">{{ costLine }}</span>
        <button id="btn-drawer" type="button" class="icon-btn"
                :aria-expanded="ui.drawerOpen ? 'true' : 'false'"
                aria-controls="drawer" title="运维面板：状态 / 成本 / 日志"
                aria-label="运维面板" @click="toggleDrawer">&#9881;</button>
      </header>
      <div id="messages-wrap">
        <div id="messages" ref="box" role="log" aria-live="polite"
             aria-label="对话记录" @scroll="onScroll">
          <div class="thread">
            <template v-for="(b, i) in rendered" :key="i">
              <details v-if="b.sys" class="msg system">
                <summary>系统提示</summary>
                <div class="sys-body">{{ b.body }}</div>
              </details>
              <div v-else class="msg" :class="[b.cls, { 'md-body': b.md }]">
                <span v-if="b.label" class="t-name">{{ b.label }}</span>
                <Markdown v-if="b.md" :text="b.body" />
                <template v-else>{{ b.body }}</template>
              </div>
            </template>
            <div v-if="!rendered.length && !state.chatError" class="thread-empty">
              <svg class="te-mark" viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
                <path d="M5.5 5.5h6v6h-6zM12.5 12.5h6v6h-6z" fill="none"
                      stroke="currentColor" stroke-width="1.4"/>
              </svg>
              <p class="te-title">Harness Agent</p>
              <p class="te-sub">从左侧选一个会话，或点 ＋ 新建；然后在下面描述你的任务。</p>
            </div>
            <div v-if="state.chatError" class="msg notice lv-error">
              <span class="n-tag">读不到历史</span>
              <span class="n-text">{{ state.chatError }}</span>
            </div>
            <div v-if="state.notice" class="msg notice" :class="'lv-' + state.notice.level">
              <span class="n-tag">{{ state.notice.level === 'error' ? '本轮没能开始' : '本轮结局' }}</span>
              <span class="n-text">{{ state.notice.text }}</span>
            </div>
            <div id="live-feed" :class="{ 'lf-hidden': !state.live.length && !state.posting }"
                 aria-live="polite" aria-label="本轮执行轨迹">
              <div v-for="line in state.live" :key="line.id" class="lf-line" :class="'lf-' + line.kind">
                <template v-if="line.kind === 'call'">调用 <span class="t-name">{{ line.name }}</span> {{ line.text }}</template>
                <template v-else-if="line.kind === 'out'"><span class="lf-arrow">↳</span> {{ line.text }}</template>
                <template v-else-if="line.kind === 'round'">── {{ line.text }} ──</template>
                <template v-else-if="line.kind === 'model'">{{ line.text }}</template>
                <template v-else>{{ line.text }}</template>
              </div>
              <div v-if="!state.live.length && state.posting" class="lf-empty">思考中…</div>
            </div>
          </div>
        </div>
        <button id="jump-bottom" v-if="!atBottom" type="button" title="回到最新"
                aria-label="回到最新一条" @click="toBottom">↓ 回到最新</button>
      </div>
      <footer id="input-bar">
        <div id="stream-warn" v-if="state.streamUp === false" role="status">
          与后端的事件流断了，浏览器正在自动重连——历史照常可读，期间的执行轨迹会缺一段。
        </div>
        <div class="cp-box">
          <textarea id="input" ref="inputEl" v-model="draft" rows="1"
                    placeholder="给 agent 布置任务" aria-label="输入消息"
                    :disabled="!canSend" @input="autoGrow"
                    @keydown.enter.exact.prevent="submit"></textarea>
          <button id="btn-send" type="button" :disabled="!canSend"
                  title="发送（Enter）" aria-label="发送" @click="submit">&#8593;</button>
        </div>
        <div class="cp-hint">Enter 发送 · Shift + Enter 换行</div>
      </footer>
    </main>
  `,
};

// ═══════════════════════════════════════════════════════════
// ApprovalCard — 底部审批浮层
// ═══════════════════════════════════════════════════════════

export const ApprovalCard = {
  name: "ApprovalCard",

  setup() {
    const card = ref(null);

    // 卡片出现时把焦点移到卡片上（tabindex=-1），**不是**移到"允许"按钮：
    // 审批是安全闸门，误按一下 Enter 就放行一个写操作是不能接受的默认动作。
    // 聚焦卡片让读屏软件念出规则和理由，人要动手就得自己 Tab 过去。
    watch(() => approval.ticket, async (ticket) => {
      if (!ticket) return;
      await nextTick();
      const el = card.value;
      if (el) el.focus();
    });

    return { approval, answerApproval, card };
  },

  template: `
    <div id="approval-overlay" :style="{ display: approval.ticket ? 'block' : 'none' }">
      <div id="approval-card" ref="card" tabindex="-1" role="alertdialog"
           aria-labelledby="a-rule" aria-describedby="a-reason">
        <div class="a-rule" id="a-rule">需要批准 {{ approval.ruleId }}</div>
        <div class="a-reason" id="a-reason">{{ approval.reason }}</div>
        <div class="a-ops">
          <button id="a-reject" type="button" :disabled="approval.submitting"
                  @click="answerApproval(false)">拒绝</button>
          <button id="a-allow" type="button" :disabled="approval.submitting"
                  @click="answerApproval(true)">允许</button>
        </div>
      </div>
    </div>
  `,
};

// ═══════════════════════════════════════════════════════════
// Toast — 一闪而过的提示
// ═══════════════════════════════════════════════════════════

export const Toast = {
  name: "Toast",

  setup() {
    return { toast };
  },

  template: `
    <div id="toast" :style="{ display: toast.text ? 'block' : 'none' }">{{ toast.text }}</div>
  `,
};
