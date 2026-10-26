/**
 * 事件契约一致性测试（后端 EventType ↔ 前端 EVENT_TYPES ↔ EVENT_STYLES）
 *
 * 跑法：npm run test:events
 *   （先 esbuild 把 src/lib/eventStyles.ts 转译到 .tmp/ 再 import）
 *
 * ★ 为什么需要这个测试
 *   frontend/src/services/eventStream.ts 是**按 EVENT_TYPES 逐个 addEventListener** 的。
 *   浏览器只把 SSE 帧派发给同名监听器 —— 清单里少一种，那种事件就**静默丢失**：
 *   不报错、不进时间线、也没有任何日志。`context.compacted` 就是这么丢的。
 *   同理，EVENT_STYLES 少一条时组件会走兜底，界面显示英文事件名。
 *
 *   这两类问题都是"编译能过、运行不报错、只是看不到"，只能靠跨文件比对兜住。
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { EVENT_TYPES } from "../.tmp/events.mjs";
import { EVENT_STYLES, resolveStyle } from "../.tmp/eventStyles.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");      // loom/（仓库根：backend/ 与 frontend/ 的父目录）
const frontendRoot = path.join(repo, "frontend"); // loom/frontend

let pass = 0;
let fail = 0;

function ok(cond, label, detail = "") {
  if (cond) {
    pass += 1;
    console.log(`  [ok] ${label}`);
  } else {
    fail += 1;
    console.log(`  [X]  ${label}${detail ? `\n       ${detail}` : ""}`);
  }
}

// --------------------------------------------------------------------------- //
// 1. 从后端源码抽出事件名（不 import Python，只做源码级比对）
// --------------------------------------------------------------------------- //
function backendEventTypes() {
  const src = fs.readFileSync(path.join(repo, "backend", "events.py"), "utf-8");
  const start = src.indexOf("class EventType(");
  if (start < 0) throw new Error("backend/events.py 里找不到 class EventType");
  const end = src.indexOf("\nclass ", start + 1);
  const body = src.slice(start, end < 0 ? undefined : end);
  const found = [...body.matchAll(/^\s{4}[A-Z_]+\s*=\s*"([a-z][a-z._]*[a-z])"/gm)].map((m) => m[1]);
  return found;
}

// --------------------------------------------------------------------------- //
// 2. 从 eventStyles.ts 源码抽出样式键（用来发现"有类型但没样式"）
// --------------------------------------------------------------------------- //
function styleKeys() {
  const src = fs.readFileSync(path.join(frontendRoot, "src", "lib", "eventStyles.ts"), "utf-8");
  const start = src.indexOf("export const EVENT_STYLES");
  const end = src.indexOf("\n};", start);
  const body = src.slice(start, end);
  // 键名可能是 "tool.call" 或 plan（裸标识符）
  return [...body.matchAll(/^\s{2}(?:"([a-z.]+)"|([a-z][a-z._]*))\s*:/gm)].map((m) => m[1] || m[2]);
}

const backend = backendEventTypes();
const frontend = [...EVENT_TYPES];
const styles = styleKeys();

console.log("\n[1] 后端与前端的事件清单一致");
ok(backend.length > 0, `后端 EventType 解析到 ${backend.length} 种`, JSON.stringify(backend));
const missing = backend.filter((t) => !frontend.includes(t));
const extra = frontend.filter((t) => !backend.includes(t));
ok(missing.length === 0, "★ 后端有的事件，前端都订阅了", `前端缺少：${missing.join(", ")}`);
ok(extra.length === 0, "前端没有多余事件（不会订阅到不存在的帧）", `前端多余：${extra.join(", ")}`);
ok(frontend.length === backend.length,
   `数量一致（后端 ${backend.length} / 前端 ${frontend.length}）`);

console.log("\n[2] 每种事件都有静态样式（不依赖运行时兜底）");
for (const t of frontend) {
  const s = EVENT_STYLES[t];
  ok(!!s && !!s.label && !!s.tone && !!s.icon, `样式存在：${t}`);
}
const styleMissing = frontend.filter((t) => !styles.includes(t));
ok(styleMissing.length === 0, "★ 源码里的 EVENT_STYLES 覆盖全部类型", `缺少：${styleMissing.join(", ")}`);

console.log("\n[3] context.compacted（本轮修复目标）");
const cc = resolveStyle({ type: "context.compacted", data: { triggered: true } });
ok(cc.label === "上下文压缩", `中文标签正确（${cc.label}）`);
ok(cc.tone === "violet", `色调已定义（${cc.tone}）`);
ok(cc.label !== "context.compacted", "★ 不是兜底的原始事件名");

console.log("\n[4] 未知事件的处理方式（未来新增事件的兜底行为）");
const unknown = resolveStyle({ type: "future.event", data: {} });
ok(unknown.label === "future.event", "未知事件回落到原始事件名（不崩、不丢）");
ok(unknown.tone === "slate" && unknown.icon === "·", "未知事件用中性样式");
ok(!!EVENT_STYLES[unknown.type] === false, "未知事件不在静态表里（说明确实走了兜底）");

console.log("\n[5] 实时订阅确实由 EVENT_TYPES 驱动（而不是另写一份硬编码清单）");
const streamSrc = fs.readFileSync(path.join(frontendRoot, "src", "services", "eventStream.ts"), "utf-8");
ok(/for \(const type of EVENT_TYPES\)/.test(streamSrc),
   "★ eventStream.ts 遍历 EVENT_TYPES 注册监听器");
ok(streamSrc.includes("source.addEventListener(type"),
   "addEventListener 的 type 来自上面那份清单");
ok(EVENT_TYPES.includes("context.compacted"),
   "★ context.compacted 在订阅清单里（否则 SSE 帧无人接收）");

console.log(`\n${"=".repeat(52)}`);
console.log(`结果：${pass}/${pass + fail} 通过`);
if (fail) {
  console.log(`\n失败 ${fail} 条 —— 见上面 [X]`);
  process.exit(1);
}
console.log("=".repeat(52));
