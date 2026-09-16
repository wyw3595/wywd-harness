/* 目录选择弹层：后端列目录、前端自己画 —— 不依赖"服务进程能把窗口画到
   用户桌面上"，所以远程 / 容器 / 沙箱里照样能用（Jupyter / code-server 那套）。

   为什么不用系统原生框：原生框要求服务进程能把窗口画到**用户桌面**上，
   那个前提在沙箱化 shell / 远程 / 容器里必死（native_dialog 的注释记过实测），
   而且弹不出来时要等 900 秒才降级。这里自己画，把"选目录"变成纯 HTTP 读。

   交互：
     起点态       先挑盘（Windows 给盘符，其他平台给 /）
     点目录       进去（后端列它的子目录）
     上一级       往上走，盘符根没有上一级
     选这个目录   把当前浏览到的目录设为工作区

   树不做成"一次性整棵拉下来"：一个目录一次请求，按需展开。本机磁盘
   一次请求毫秒级，而整棵树在 C:\ 这种地方是灾难。
*/

import { computed } from "vue";
import {
  closeFsPicker,
  fsEnterDir,
  fsEnterRoot,
  fsGoUp,
  fsPicker,
  pickFsDir,
} from "./store.js";

export const FsPicker = {
  name: "FsPicker",

  setup() {
    const closeLabel = "&times;";

    /** 起点态没有 path，"选这个目录"无从谈起。 */
    const canPick = computed(() => Boolean(fsPicker.path));

    return {
      fsPicker, fsEnterDir, fsEnterRoot, fsGoUp, pickFsDir, closeFsPicker,
      closeLabel, canPick,
    };
  },

  template: `
    <div id="fs-overlay" :style="{ display: fsPicker.open ? 'block' : 'none' }">
      <div id="fs-picker" role="dialog" aria-label="选择工作目录">
        <div class="fp-head">
          <span class="fp-title">选择工作目录</span>
          <span class="flex"></span>
          <button type="button" class="icon-btn" aria-label="关闭"
                  :disabled="fsPicker.loading" @click="closeFsPicker"
                  v-html="closeLabel"></button>
        </div>

        <div class="fp-path" :title="fsPicker.path || ''">
          {{ fsPicker.path || "先选一个盘（或根目录）" }}
        </div>

        <div class="fp-list" aria-live="polite">
          <div v-if="fsPicker.loading" class="fp-empty">读取中…</div>
          <div v-else-if="fsPicker.error" class="err">{{ fsPicker.error }}</div>

          <template v-else-if="fsPicker.roots.length">
            <div class="fp-hint">选一个盘</div>
            <div v-for="r in fsPicker.roots" :key="r" class="fp-row"
                 role="listitem" @click="fsEnterRoot(r)">
              <span class="fp-ico"></span><span>{{ r }}</span>
            </div>
          </template>

          <template v-else>
            <div v-if="!fsPicker.entries.length" class="fp-empty">
              这个目录下面没有子目录
            </div>
            <div v-for="e in fsPicker.entries" :key="e.name" class="fp-row"
                 role="listitem" @click="fsEnterDir(e.name)">
              <span class="fp-ico"></span><span>{{ e.name }}</span>
            </div>
          </template>
        </div>

        <div class="fp-ops">
          <button type="button" :disabled="!fsPicker.parent || fsPicker.loading"
                  @click="fsGoUp">上一级</button>
          <span class="flex"></span>
          <button type="button" class="fp-primary"
                  :disabled="!canPick || fsPicker.loading" @click="pickFsDir">
            选这个目录
          </button>
        </div>
      </div>
    </div>
  `,
};
