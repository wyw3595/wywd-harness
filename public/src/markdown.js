/* markdown 解析器：文本 → 数据树（纯函数，零 import）。

   为什么要从零写：模型吐出来的都是 markdown，早先只有 white-space:pre-wrap
   扛着——`**加粗**`、列表、代码块全是裸的。但"渲染 HTML"这条路在这个项目里
   是封死的（练习 13 的铁律：LLM 输出是不可信输入，进 HTML 前必须转义）。

   于是走另一条：**解析成数据树，渲染层用 Vue 的 h() 建 VNode**。
   全程没有 innerHTML / v-html 可写，注入面在结构上就不存在——
   比"记得转义"可靠得多（转义是纪律，这是物理隔离）。

   分层：本文件只管"文本 → 数据"，不认识 Vue；VNode 的活在 components.js。
   好处是这个文件零 import，任何 JS 运行时都能直接跑（Node 里用 data: URL
   就能 import 进来做单测——见 scripts/smoke_frontend.cjs 的纯函数自检段）。

   支持子集（按模型实际输出频率取舍）：
     块级：围栏代码块 / 标题 / 引用 / 有序无序列表（含嵌套）/ 分隔线 / 段落
     行内：行内代码 / 链接 / 粗体 / 斜体 / 粗斜体 / 删除线
   不做：表格 / 脚注 / HTML 内联 / 行内公式（用不着，且都是注入风险的入口）。
   已知取舍：不支持"惰性续行"（不缩进的续行会被当成新段落）——模型输出里很少见。
*/

// ── 块级 ────────────────────────────────────────────────

const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?/;
const ULIST = /^(\s*)([-*+])\s+(.*)$/;
const OLIST = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;

function indentOf(line) {
  const m = /^[ \t]*/.exec(line)[0];
  return m.replace(/\t/g, "    ").length;
}

/** 解析整篇：返回块数组。递归用于列表项与引用的内部。 */
export function parseMarkdown(text) {
  return parseBlocks(String(text == null ? "" : text).replace(/\r\n?/g, "\n")
    .replace(/\t/g, "    ").split("\n"));
}

function parseBlocks(lines) {
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // 围栏代码块：内容原样保留（不解析行内），收尾栅栏缺失也照样闭合——
    // 模型被 max_tokens 截断在代码块中间是常事，不能因此吞掉后面全文。
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1][0];
      const closer = new RegExp("^\\s*" + marker + "{3,}\\s*$");
      const buf = [];
      i++;
      while (i < lines.length && !closer.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;                                    // 跳过收尾栅栏（没有也算）
      blocks.push({ type: "code", lang: fence[2] || "", text: buf.join("\n") });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length,
                    inline: parseInlineAll([heading[2]]) });
      i++;
      continue;
    }

    if (HR.test(line)) { blocks.push({ type: "hr" }); i++; continue; }

    if (QUOTE.test(line)) {
      const buf = [];
      while (i < lines.length && (QUOTE.test(lines[i]) || !lines[i].trim())) {
        buf.push(lines[i].replace(QUOTE, ""));
        i++;
        if (i < lines.length && !QUOTE.test(lines[i]) && lines[i].trim()) break;
      }
      blocks.push({ type: "quote", blocks: parseBlocks(buf) });
      continue;
    }

    const bullet = ULIST.exec(line);
    const number = bullet ? null : OLIST.exec(line);
    if (bullet || number) {
      const ordered = !bullet;
      const startIndent = (bullet || number)[1].length;
      const items = [];
      while (i < lines.length) {
        const m = ordered ? OLIST.exec(lines[i]) : ULIST.exec(lines[i]);
        if (!m || m[1].length !== startIndent) break;   // 同级新项才归本列表
        const contentCol = m[1].length + m[2].length + 1;   // "- " 的宽度
        const buf = [m[3]];
        i++;
        // 续行：缩进到内容列之后（含空行后的缩进行）。嵌套列表因此天然
        // 归进本项——递归解析时会自己长成子列表，不需要单独的嵌套逻辑。
        while (i < lines.length) {
          const next = lines[i];
          if (!next.trim()) {
            const after = lines[i + 1];
            if (after && after.trim() && indentOf(after) >= contentCol) {
              buf.push(""); i++; continue;
            }
            break;
          }
          if (indentOf(next) >= contentCol) {
            buf.push(next.slice(contentCol));
            i++;
            continue;
          }
          break;
        }
        items.push({ blocks: parseBlocks(buf) });
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // 段落：连续非空行合并，行间软换行渲染成 <br>（GitHub 风格，模型输出里
    // 单换行通常是有意的）
    const buf = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    blocks.push({ type: "p", inline: parseInlineAll(buf) });
  }

  return blocks;
}

function isBlockStart(line) {
  return FENCE.test(line) || HEADING.test(line) || HR.test(line) ||
         QUOTE.test(line) || ULIST.test(line) || OLIST.test(line);
}

// ── 行内 ────────────────────────────────────────────────

// 分组表（顺序即优先级）：
//   1,2 行内代码（反引号数量要对称）     3,4 链接 text / href
//   5   ***粗斜***   6  **粗**   7  ___粗斜___   8  __粗__
//   9   ~~删除~~    10  *斜*    11  _斜_
const INLINE_SRC = [
  "(`+)([\\s\\S]*?)\\1",
  "\\[([^\\]]*)\\]\\(\\s*([^)\\s]+)(?:\\s+\"[^\"]*\")?\\s*\\)",
  "\\*\\*\\*([\\s\\S]+?)\\*\\*\\*",
  "\\*\\*([\\s\\S]+?)\\*\\*",
  "___([\\s\\S]+?)___",
  "__([\\s\\S]+?)__",
  "~~([\\s\\S]+?)~~",
  "\\*([^*\\n]+?)\\*",
  "_([^_\\n]+?)_",
].join("|");

/** 多行 → 行内 token 流（段落的软换行在这里变成 br） */
function parseInlineAll(lines) {
  const out = [];
  lines.forEach((line, index) => {
    if (index) out.push({ type: "br" });
    for (const token of parseInline(line)) out.push(token);
  });
  return out;
}

/** 单行 → 行内 token 流。
 *
 *  **每次调用都新建正则实例**，不是性能洁癖，是正确性：
 *  `g` 标志的游标 `lastIndex` 挂在 RegExp 对象上，而本函数会递归
 *  （粗体里套斜体、链接文字里套粗体）。共享一个模块级正则时，内层递归
 *  一执行就把外层的游标踩烂——外层拿着被重置的游标从更早的位置重新匹配，
 *  同一个标记反复命中，循环永不退出。
 *
 *  现场：冒烟脚本跑到"行内：代码/粗/斜/删除"这条时，Node 把 4GB 堆吃满
 *  崩掉（死循环在 push token，不只是空转）。前端里触发就是标签页卡死。
 *  备用修法是"递归前存下 lastIndex、回来复位"，但那要求每个递归点都记得，
 *  漏一处就复发——建实例是一次调用一次，漏不掉。
 */
function parseInline(line) {
  return scanInline(line, new RegExp(INLINE_SRC, "g"));
}

function scanInline(line, re) {
  const out = [];
  let last = 0;
  let m;

  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push({ type: "text", text: line.slice(last, m.index) });
    if (m[2] !== undefined) {
      out.push({ type: "code", text: m[2].trim() });
    } else if (m[3] !== undefined) {
      const href = safeHref(m[4]);
      // 危险的 scheme 直接把链接降级成文字：不渲染 <a> 就没有可点的
      // javascript: ——零 v-html 只挡 HTML 注入，挡不住 href 执行
      if (href) out.push({ type: "link", href, inline: parseInline(m[3]) });
      else out.push({ type: "text", text: line.slice(m.index, re.lastIndex) });
    } else if (m[5] !== undefined) {
      out.push({ type: "strong", inline: [{ type: "em", inline: parseInline(m[5]) }] });
    } else if (m[6] !== undefined) {
      out.push({ type: "strong", inline: parseInline(m[6]) });
    } else if (m[7] !== undefined) {
      out.push({ type: "strong", inline: [{ type: "em", inline: parseInline(m[7]) }] });
    } else if (m[8] !== undefined) {
      out.push({ type: "strong", inline: parseInline(m[8]) });
    } else if (m[9] !== undefined) {
      out.push({ type: "del", inline: parseInline(m[9]) });
    } else if (m[10] !== undefined) {
      out.push({ type: "em", inline: parseInline(m[10]) });
    } else {
      out.push({ type: "em", inline: parseInline(m[11]) });
    }
    if (re.lastIndex === m.index) re.lastIndex++;   // 零长匹配保险丝
    last = re.lastIndex;
  }

  if (last < line.length) out.push({ type: "text", text: line.slice(last) });
  return out;
}

/** 链接白名单：只放行明确安全的 scheme，其余（javascript:、data:…）当普通文字。 */
export function safeHref(url) {
  const u = String(url == null ? "" : url).trim();
  return /^(https?:\/\/|mailto:|\/|#)/i.test(u) ? u : null;
}
