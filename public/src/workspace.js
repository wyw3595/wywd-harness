/* 工作区面板：显示"模型现在在哪个目录里干活"，并给出换目录的四个入口。

   为什么单独一个文件：它和会话列表一样是侧栏的一块，但数据来源与动作都
   不相干（会话 = sidecar 的会话生命周期，工作区 = 沙箱的边界）。塞进
   components.js 会让那个文件混进一套完全不同的按钮语义。

   四条入口的差别（照搬 vanilla 版 `public/app.js` 里已经定好的决策）：
     browse —— 让**后端**弹系统目录选择框。为什么不是前端弹：浏览器拿不到
               选中目录的绝对路径（规范层面 File 只有 name/size/type），
               所以"前端弹框 → 把路径交给后端"这条路根本不存在。而服务
               就跑在本机，后端有能力弹真框。
     open   —— 手输路径。现在是 browse 的**降级**路径（后端没图形环境时）。
     upload —— 传一个 zip 当工作区（后端解压到 workspaces/<id>/）。
     reset  —— 回默认工作区。前几条是"出去"，这条是"回来"：出去的按钮有了、
               回来的没有，用户就被困在上传的那个目录里了。

   换工作区只是换了下一次发消息的沙箱边界，已建会话的历史不动——旧历史
   里的路径在新沙箱里可能不存在，所以成功后提示"建议新建会话"。
*/

import { computed, ref } from "vue";
import {
  refreshWorkspace,
  runWorkspace,
  uploadWorkspaceZip,
  workspace,
} from "./store.js";

/** 从绝对路径取最后一段（Windows 的反斜杠和 Unix 的斜杠都要认）。
    只在后端没给 id 时兜底——"我选的是哪个目录"要看得见。 */
function lastSegment(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "";
}

export const WorkspacePanel = {
  name: "WorkspacePanel",

  setup() {
    const fileEl = ref(null);

    /** 已经在默认工作区就没得回——按钮藏着，别给一个点了没反应的按钮。 */
    const isDefault = computed(() =>
      Boolean(workspace.info) && workspace.info.kind === "default");

    /** 名字要说清"这是哪种来源"，别一律叫"工作区"——用户刚选完目录，
        第一眼要知道"我刚才选的那个进去了没有"。 */
    const nameText = computed(() => {
      if (workspace.error) return workspace.error;
      const ws = workspace.info;
      if (!ws) return "—";
      if (ws.kind === "default") return "默认：项目根";
      if (ws.kind === "dir") return "目录：" + (ws.id || lastSegment(ws.root));
      return "上传：" + (ws.id || "");
    });

    /** 默认工作区可能不回传 root——那就写"项目根"，别留一段空白让人以为
        没读到（vanilla 版踩过这个）。 */
    const pathText = computed(() => {
      const ws = workspace.info;
      if (!ws) return "";
      return ws.root || (ws.kind === "default" ? "项目根" : "");
    });

    /** 忙的时候这一行显示进度短语（"等你在系统窗口里选目录…"），
        而不是把上半句留着让人以为卡住了。 */
    const lineText = computed(() => workspace.busy || nameText.value);

    async function browse() {
      const res = await runWorkspace("browse", {}, "等你在系统窗口里选目录…");
      if (res && res.fallback === "manual") await byPath();
    }

    /** 降级路径：后端弹不出框（服务跑在没图形环境的机器上）时才手输。
        把它当死路是错的——那是环境限制，不是用户做错了什么。 */
    async function byPath() {
      const path = window.prompt(
        "让 Agent 在哪个目录里工作？（本机绝对路径，直接引用不复制）", "");
      if (!path || !path.trim()) return;
      await runWorkspace("open", { path: path.trim() }, "切换中…");
    }

    function pickZip() {
      if (fileEl.value) fileEl.value.click();
    }

    async function onZip(e) {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";          // 置空：允许连续两次选同一个文件
      await uploadWorkspaceZip(file);
    }

    async function reset() {
      // 空路径：复位不需要坐标，"默认"是 sidecar 的启动态。浏览器拿不到
      // 项目根路径，这也正是必须由后端说"回哪儿"的原因。
      await runWorkspace("reset", {}, "切换中…");
    }

    return {
      workspace, isDefault, lineText, pathText, fileEl,
      browse, byPath, pickZip, onZip, reset, refreshWorkspace,
    };
  },

  template: `
    <div class="ws">
      <div class="ws-head">
        <span class="ws-title">工作区</span>
        <span class="flex"></span>
        <button type="button" class="icon-btn" :disabled="!!workspace.busy"
                title="选择目录（弹系统窗口）" aria-label="选择目录"
                @click="browse">&#30446;</button>
        <button type="button" class="icon-btn" :disabled="!!workspace.busy"
                title="上传 zip 当工作区" aria-label="上传 zip 当工作区"
                @click="pickZip">&#20256;</button>
        <button type="button" class="icon-btn" :disabled="!!workspace.busy"
                title="手输本机绝对路径" aria-label="手输路径"
                @click="byPath">&#36755;</button>
        <button v-if="!isDefault" type="button" class="icon-btn"
                :disabled="!!workspace.busy"
                title="回到默认工作区（项目根）" aria-label="回到默认工作区"
                @click="reset">&#22238;</button>
        <button type="button" class="icon-btn" :disabled="!!workspace.busy"
                title="重新读一次工作区" aria-label="刷新工作区"
                @click="refreshWorkspace">&#8635;</button>
      </div>
      <div class="ws-body">
        <div id="ws-name" class="ws-name">{{ lineText }}</div>
        <div id="ws-path" class="ws-path" :title="pathText">{{ pathText }}</div>
      </div>
      <input ref="fileEl" type="file" accept=".zip,application/zip"
             hidden @change="onZip">
    </div>
  `,
};
