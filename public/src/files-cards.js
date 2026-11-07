/* [files] 协议 → 交付卡片的解析与白名单（纯函数，零 import）。

   为什么单独一个文件：这里是**不可信数据**进 UI 的唯一入口——tool_end 的
   文本可以来自仓库里的任意文件（`fs_read` / `bash cat` 都会把它原样带回），
   不是只有 present_files 的产出。所以：
   - 形状校验：必须是数组、每项是对象、path 是非空字符串，条数封顶；
   - URL 白名单：只有 http(s) 才允许当链接（`javascript:` / `data:` 一律
     降级成不可点的卡片）——`<a :href>` 不像 markdown 那样有 safeHref 兜底；
   - 主交付只认第一个：伪造多张 isPrimary 不会画出多个徽标。

   纯函数的好处：冒烟脚本可以用 data: URL 直接 import 它来测（和
   markdown.js 同一套路），不需要浏览器。
*/

const MAX_CARDS = 50;
const HTTP_URL = /^https?:\/\//i;

/** 从工具输出里抽 [files] JSON；没有 / 不是数组 / JSON 坏 → null。 */
export function parseFilesPayload(text) {
  const source = String(text == null ? "" : text);
  const start = source.indexOf("[files]");
  const end = source.indexOf("[/files]");
  if (start < 0 || end <= start) return null;
  let raw;
  try {
    raw = JSON.parse(source.slice(start + "[files]".length, end).trim());
  } catch (e) {
    return null;
  }
  if (!Array.isArray(raw)) return null;

  let primarySeen = false;
  const cards = [];
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const path = typeof item.path === "string" ? item.path : "";
    if (!path) continue;
    const isUrl = item.isUrl === true && HTTP_URL.test(path);
    const primary = item.isPrimary === true && !primarySeen;
    if (primary) primarySeen = true;
    cards.push({
      path,
      isUrl,
      name: typeof item.name === "string" && item.name ? item.name : path,
      icon: typeof item.icon === "string" && item.icon ? item.icon : "📄",
      size: typeof item.size === "string" ? item.size : "",
      exists: item.exists === true,
      isPrimary: primary,
    });
    if (cards.length >= MAX_CARDS) break;
  }
  return cards.length ? cards : null;
}

/** 头行那句"交付 N 个文件：说明"里的说明，没有就空串。 */
export function parseFilesNote(text) {
  const match = String(text == null ? "" : text)
    .match(/交付 (\d+) 个文件[：:](.*)/);
  return match ? match[2].trim() : "";
}
