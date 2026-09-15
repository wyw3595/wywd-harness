/* 视图偏好：只属于这一个浏览器的东西（侧栏折叠、抽屉开合、列表搜索词）。

   为什么不塞进 store：store 放的是**服务端有对应物**的数据（会话、历史、
   成本账）；这几个是本 tab 的视图状态，刷新后要么该记住、要么该从零开始。
   混在一起的下场是"改个侧栏宽度要不要通知后端"这种问题开始出现。

   持久化只做折叠和抽屉：用户调过一次的布局，下次开页面不该再调一次。
   搜索词不记——它是一次性的视线，不是偏好（记住反而会让下次开页面
   看到一份被过滤过的空列表，像坏了）。
*/

import { reactive } from "vue";

const LS_KEY = "wywd_ui";

function load() {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {};
  } catch (err) {
    return {};                 // 隐私模式 / 脏数据：当没存过
  }
}

export const ui = reactive({
  sidebarCollapsed: false,
  drawerOpen: false,
  search: "",
});

const saved = load();
// 没存过偏好时按屏宽给默认：窄屏上侧栏是整屏浮层，默认展开会一进来就盖住
// 对话区（Codex 桌面版在手机上也是收起态）。
ui.sidebarCollapsed = saved.sidebarCollapsed === true ||
  (saved.sidebarCollapsed === undefined && window.innerWidth <= 700);
ui.drawerOpen = saved.drawerOpen === true;

function persist() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      sidebarCollapsed: ui.sidebarCollapsed,
      drawerOpen: ui.drawerOpen,
    }));
  } catch (err) { /* 记不住就算了：偏好丢了不影响能用 */ }
}

export function toggleSidebar() {
  ui.sidebarCollapsed = !ui.sidebarCollapsed;
  persist();
}

export function toggleDrawer() {
  ui.drawerOpen = !ui.drawerOpen;
  persist();
}

export function closeDrawer() {
  if (ui.drawerOpen) { ui.drawerOpen = false; persist(); }
}

/** 用搜索词过滤会话行（纯函数，视图只管调）。空词返回原数组。 */
export function filterRows(rows, keyword) {
  const k = String(keyword || "").trim().toLowerCase();
  if (!k) return rows;
  return rows.filter((row) =>
    !row.error && (String(row.id).toLowerCase().includes(k) ||
                   String(row.title).toLowerCase().includes(k) ||
                   String(row.status).toLowerCase().includes(k)));
}
