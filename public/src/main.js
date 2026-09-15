/* 根组件 + 挂载点。

   四层到齐：api.js（网络）→ store.js（服务端数据）/ ui.js（视图偏好）
   → components.js + drawer.js（视图）→ 本文件（装配）。

   对照 vanilla 版：那边 init() 里既绑事件、又拉清单、又开定时器，全在
   一个函数里；这里拆成组件生命周期（onMounted / onUnmounted）——挂载即
   开始、卸载即清理，不用靠"记得写清理代码"。
*/

import { createApp, onMounted, onUnmounted } from "vue";
import { ApprovalCard, ChatArea, SessionList, Toast } from "./components.js";
import { OpsDrawer } from "./drawer.js";
import { approval, dismissApproval, notify, start } from "./store.js";
import { closeDrawer } from "./ui.js";

const App = {
  name: "App",

  components: { SessionList, ChatArea, OpsDrawer, ApprovalCard, Toast },

  setup() {
    function onKey(e) {
      if (e.key !== "Escape") return;
      // Esc 的两级语义：先收最上面的浮层。审批卡只**隐藏**，绝不等于拒绝——
      // 拒绝必须是一次明确动作（等待超时才是 fail-closed 的默认拒绝，
      // 误按 Esc 就当成拒绝会让用户莫名其妙地丢一次操作）。
      if (approval.ticket) { dismissApproval(); return; }
      closeDrawer();
    }

    onMounted(() => {
      document.addEventListener("keydown", onKey);
      start();                       // 拉清单 + 起事件流 + 恢复上次会话
    });

    onUnmounted(() => {
      document.removeEventListener("keydown", onKey);
    });
  },

  template: `
    <SessionList />
    <ChatArea />
    <OpsDrawer />
    <ApprovalCard />
    <Toast />
  `,
};

const app = createApp(App);

// 兜底：组件里抛异常时 Vue 默认只在 console 里吼一声，页面留一块空白，
// 用户只看到"点了没反应"。这里至少把它变成一句看得见的话。
app.config.errorHandler = (err, _instance, info) => {
  console.error("[vue]", info, err);
  notify("界面出错：" + ((err && err.message) || String(err)));
};

app.mount("#app");
