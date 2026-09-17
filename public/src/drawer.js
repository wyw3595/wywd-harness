/* 运维抽屉：sidecar 自述状态 / 成本表 / 日志尾。

   为什么单独一个文件：它是"旁路观测"——和聊天主链路没有任何耦合，
   关掉它整个应用照常工作。放一起会让 components.js 混进一堆只在这里
   用得到的表格样式和刷新节奏。

   刷新节奏归它自己管（3 秒一次，且只在开着的时候）：谁需要数据谁负责
   拉，store 只提供"拉一次"的能力。反过来把定时器放 store 里，就得让
   数据层知道"有没有人在看"——那是视图的事。
*/

import { computed, onUnmounted, ref, watch } from "vue";
import { cost, ops, refreshStatus } from "./store.js";
import { closeDrawer, ui } from "./ui.js";

const REFRESH_MS = 3000;

export const OpsDrawer = {
  name: "OpsDrawer",

  setup() {
    const timer = ref(null);

    async function refresh() {
      await refreshStatus(true);
    }

    watch(() => ui.drawerOpen, (open) => {
      if (timer.value) { clearInterval(timer.value); timer.value = null; }
      if (!open) return;
      refresh();
      timer.value = setInterval(refresh, REFRESH_MS);
    }, { immediate: true });

    onUnmounted(() => { if (timer.value) clearInterval(timer.value); });

    const atText = computed(() => {
      if (!ops.at) return "";
      return new Date(ops.at).toTimeString().slice(0, 8);
    });

    const ring = computed(() => {
      const s = ops.status;
      if (!s || s.error || s.ringBufferTotal == null) return null;
      return s.ringBufferUsed + " / " + s.ringBufferTotal +
             (s.ringBufferFull ? "（已满，最旧的被覆盖）" : "");
    });

    const logLines = computed(() => ops.logs || "（暂无日志）");

    return { ui, ops, cost, atText, ring, logLines, refresh, closeDrawer };
  },

  template: `
    <aside id="drawer" :class="{ open: ui.drawerOpen }" aria-label="运维面板">
      <div class="dw-head">
        <span class="dw-title">运维</span>
        <span class="dw-at" v-if="atText" :title="'上次刷新 ' + atText">{{ atText }}</span>
        <button type="button" class="icon-btn" title="立即刷新" aria-label="立即刷新"
                :disabled="ops.loading" @click="refresh">&#8635;</button>
        <button type="button" class="icon-btn" title="收起（Esc）" aria-label="收起运维面板"
                @click="closeDrawer">&#10005;</button>
      </div>
      <div class="dw-body">
        <section class="dw-sec">
          <h3>sidecar</h3>
          <div v-if="ops.status && ops.status.error" class="dw-err">{{ ops.status.error }}</div>
          <div v-else-if="ops.status" class="dw-kv">
            <div><span>会话记录</span><b>{{ ops.status.sessions }}</b></div>
            <div><span>RingBuffer</span><b>{{ ring || "—" }}</b></div>
            <div><span>RPC handlers</span><b>{{ ops.status.handlers }}</b></div>
            <div><span>单轮步数上限</span><b>{{ ops.status.maxSteps || "—" }}</b></div>
          </div>
          <div v-else class="dw-dim">读取中…</div>
        </section>

        <section class="dw-sec">
          <h3>成本</h3>
          <table class="dw-table" v-if="cost.rows && cost.rows.length">
            <thead><tr><th>tier</th><th>calls</th><th>输入</th><th>输出</th><th>$</th></tr></thead>
            <tbody>
              <tr v-for="row in cost.rows" :key="row.tier">
                <td>{{ row.tier }}</td><td>{{ row.calls }}</td>
                <td>{{ row.promptTokens }}</td><td>{{ row.completionTokens }}</td>
                <td>{{ row.cost }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="dw-dim">模型没挂路由器，无成本表</div>
        </section>

        <section class="dw-sec dw-grow">
          <h3>sidecar 日志</h3>
          <pre class="dw-logs">{{ logLines }}</pre>
        </section>
      </div>
    </aside>
  `,
};
