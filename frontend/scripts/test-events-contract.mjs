/**
 * 事件契约一致性测试（后端 EventType ↔ 前端 EVENT_TYPES ↔ EVENT_STYLES ↔ SSE 解析器）
 *
 * 跑法：npm run test:events
 *   （先用 esbuild 把 events.ts / eventStyles.ts / sse.ts 转译到 .tmp/ 再 import）
 *
 * ★ 为什么需要这个测试
 *   历史问题：`eventStream.ts` 曾按 `EVENT_TYPES` 逐个 `addEventListener`。
 *   浏览器只把具名帧派发给同名监听器 —— 清单里少一种，那种事件就**静默丢失**：
 *   不报错、不进时间线、也没有任何日志。`context.compacted` 就是这么丢的。
 *
 *   2026-11-11 起传输层改成自己解析 SSE 帧（`services/sse.ts`），
 *   不再有"白名单"，未知类型也能到达 UI。所以本测试的重点从"清单是否齐全"
 *   扩展成两部分：
 *     · 静态契约：前后端清单 + 样式必须一致（防止长期漂移）
 *     · 传输行为：解析器必须**不认识事件名**也能把帧交出来（这是根治的证据）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { EVENT_TYPES, SESSION_MODES } from "../.tmp/events.mjs";
import { EVENT_STYLES, resolveStyle } from "../.tmp/eventStyles.mjs";
import { parseSseFrame, splitSseFrames } from "../.tmp/sse.mjs";

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
  // 键名可能是 "tool.call" / "user_input.request"（含下划线）或 plan（裸标识符）
  return [...body.matchAll(/^\s{2}(?:"([a-z._]+)"|([a-z][a-z._]*))\s*:/gm)].map((m) => m[1] || m[2]);
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

console.log("\n[3b] permission.mode（步骤 6）");
ok(EVENT_TYPES.includes("permission.mode"),
   "★ permission.mode 在订阅清单里（否则 SSE 帧无人接收）");
const pm = resolveStyle({ type: "permission.mode", data: { mode: "read-only" } });
ok(pm.label === "权限模式", `中文标签正确（${pm.label}）`);
ok(pm.tone === "amber", `色调已定义（${pm.tone}）`);
ok(!!EVENT_STYLES["permission.mode"], "静态样式存在（不依赖运行时兜底）");

console.log("\n[4] 未知事件的处理方式（未来新增事件的兜底行为）");
const unknown = resolveStyle({ type: "future.event", data: {} });
ok(unknown.label === "future.event", "未知事件回落到原始事件名（不崩、不丢）");
ok(unknown.tone === "slate" && unknown.icon === "·", "未知事件用中性样式");
ok(!!EVENT_STYLES[unknown.type] === false, "未知事件不在静态表里（说明确实走了兜底）");

console.log("\n[5] 传输层不再有白名单（这是「未知事件不丢」的根）");
const streamSrc = fs.readFileSync(path.join(frontendRoot, "src", "services", "eventStream.ts"), "utf-8");
ok(!/addEventListener\(\s*type/.test(streamSrc),
   "★ eventStream.ts 不再按清单逐个 addEventListener（白名单已移除）");
ok(streamSrc.includes("getReader"),
   "改用 ReadableStream 逐块读取（fetch + 自己解析）");
ok(/splitSseFrames/.test(streamSrc),
   "解析交给纯函数 splitSseFrames（可单测，见 [6]）");
ok(/onUnknownType/.test(streamSrc),
   "保留了漂移上报通道 onUnknownType（不丢 ≠ 不管）");
ok(/DECLARED_TYPES/.test(streamSrc) && /handlers\.onEvent\(/.test(streamSrc),
   "★ 清单只用于报告漂移，投递路径无条件调用 onEvent");

const sseSrc = fs.readFileSync(path.join(frontendRoot, "src", "services", "sse.ts"), "utf-8");
// 只检查代码，不检查注释 —— 注释里提到 EVENT_TYPES 是在解释"为什么不需要它"
const sseCode = sseSrc
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");
ok(!sseCode.includes("EVENT_TYPES") && !sseCode.includes("import"),
   "★ SSE 解析器零 import、零协议耦合（它不知道有哪些事件类型）");

console.log("\n[6] SSE 解析器行为（真实字节流，不是源码正则）");

// ---- 6.1 基本帧：id / event / data ----
const one = splitSseFrames(
  'id: 7\nevent: tool.call\ndata: {"id":7,"event":{"type":"tool.call"}}\n\n',
);
ok(one.frames.length === 1, "单帧被切出来");
ok(one.frames[0].id === "7" && one.frames[0].event === "tool.call", "id / event 解析正确");
ok(JSON.parse(one.frames[0].data).event.type === "tool.call", "data 是可直接 JSON.parse 的原文");
ok(one.rest === "", "完整帧不留下残余缓冲区");

// ---- 6.2 ★ 未知事件类型：解析器根本不看名字 ----
const future = splitSseFrames(
  'id: 9\nevent: desktop.action\ndata: {"id":9,"event":{"type":"desktop.action"}}\n\n',
);
ok(future.frames.length === 1 && future.frames[0].event === "desktop.action",
   "★ 后端新增的事件类型（前端清单里没有）照样被解析出来");
ok(!EVENT_TYPES.includes("desktop.action"),
   "（确认 desktop.action 确实不在前端清单里 —— 这条就是「以前会丢」的场景）");

// ---- 6.3 心跳注释帧：不是事件，不该进 onEvent ----
const hb = splitSseFrames(": keepalive\n\n");
ok(hb.frames.length === 0, "纯注释帧（keepalive）被丢弃，不产生事件");

// ---- 6.4 多帧连续到达 ----
const multi = splitSseFrames(
  'event: a\ndata: {"n":1}\n\n' + ": keepalive\n\n" + 'event: b\ndata: {"n":2}\n\n',
);
ok(multi.frames.length === 2 && multi.frames[1].event === "b", "多帧 + 中间夹心跳，逐帧切出");

// ---- 6.5 分块：一帧被拆成两次 read() ----
const p1 = 'id: 3\nevent: plan\ndata: {"a":';
const p2 = '1}\n\n';
const step1 = splitSseFrames(p1);
ok(step1.frames.length === 0, "半帧不交付（避免把截断的 JSON 交给上层）");
ok(step1.rest === p1, "半帧原样留在缓冲区");
const step2 = splitSseFrames(step1.rest + p2);
ok(step2.frames.length === 1 && JSON.parse(step2.frames[0].data).a === 1,
   "★ 拼上后半块后完整交付（分块边界与帧边界无关）");

// ---- 6.6 CRLF（某些代理会改行尾）----
const crlf = splitSseFrames('id: 4\r\nevent: error\r\ndata: {"x":1}\r\n\r\n');
ok(crlf.frames.length === 1 && crlf.frames[0].event === "error", "CRLF 行尾也能解析");

// ---- 6.7 跨块的 CRLF：上一块以 \\r 结尾，下一块以 \\n 开头 ----
const half = splitSseFrames('event: x\r');
const joined = splitSseFrames(half.rest + "\ndata: {}\n\n");
ok(joined.frames.length === 1 && joined.frames[0].event === "x",
   "跨块 CRLF 不会串帧（缓冲区整体规范化）");

// ---- 6.8 多行 data ----
const ml = parseSseFrame('event: y\ndata: line1\ndata: line2');
ok(ml.data === "line1\nline2", "多行 data 用换行拼接（按 SSE 规范）");

// ---- 6.9 畸形输入不抛 ----
let threw = false;
try {
  parseSseFrame("::::\nnot a field\n");
  splitSseFrames("data: {\n\n");
} catch {
  threw = true;
}
ok(!threw, "畸形帧不抛异常（坏帧丢弃 > 整条流崩）");

console.log("\n[7] 未知事件的兜底渲染（不丢，还要看得见）");
const fu = resolveStyle({ type: "desktop.action", data: {} });
ok(fu.label === "desktop.action", "标签回落到原始类型名");
ok(fu.unknown === true, "★ 兜底样式带 unknown 标记（卡片会显示「未声明」）");
ok(EVENT_STYLES["desktop.action"] === undefined, "确实不在静态样式表里");
const known = resolveStyle({ type: "plan", data: {} });
ok(known.unknown !== true, "已声明的事件不会被误标成 unknown");

console.log("\n[8] 权限模式枚举也要同步（同类漂移，换个地方长出来）");
// 后端常量在 backend/agent/state.py::VALID_SESSION_MODES
const statePy = fs.readFileSync(path.join(repo, "backend", "agent", "state.py"), "utf-8");
// 后端在 state.py 里先定义常量、再在 VALID_SESSION_MODES 元组里引用它们，
// 所以字面量只出现在 `SESSION_MODE_XXX = "..."` 这几行 —— 从那里抽。
const backendModes = [
  ...statePy.matchAll(/^SESSION_MODE_[A-Z_]+\s*=\s*"([a-z-]+)"/gm),
].map((m) => m[1]);
ok(backendModes.length > 0, `后端 VALID_SESSION_MODES 解析到 ${backendModes.length} 种`, JSON.stringify(backendModes));
const missingMode = backendModes.filter((m) => !SESSION_MODES.includes(m));
const extraMode = [...SESSION_MODES].filter((m) => !backendModes.includes(m));
ok(missingMode.length === 0, "★ 后端支持的权限模式，前端都有", `前端缺少：${missingMode.join(", ")}`);
ok(extraMode.length === 0, "前端没有后端不认的模式", `前端多余：${extraMode.join(", ")}`);
ok(SESSION_MODES.includes("desktop"), "★ desktop（第四种模式）在前端清单里");

console.log("\n[9] ask_user 的自由输入口（选项穷举不完时不能逼用户选最接近的）");
const cardSrc = fs.readFileSync(
  path.join(frontendRoot, "src", "components", "UserInputCard.tsx"), "utf-8");
const apiSrc = fs.readFileSync(
  path.join(frontendRoot, "src", "services", "api.ts"), "utf-8");
const eventCardSrc = fs.readFileSync(
  path.join(frontendRoot, "src", "components", "EventCard.tsx"), "utf-8");
ok(cardSrc.includes("其他（自己填写）"), "★ 卡片里有「其他（自己填写）」入口");
ok(/item\.custom\s*=\s*custom/.test(cardSrc), "★ 自由输入走 custom 字段（不冒充选项）");
ok(/if\s*\(next\s*&&\s*!q\.multi_select\)/.test(cardSrc), "单选时「其他」与普通选项互斥");
ok(/const item: UserInputAnswer = \{ id: q\.id, selected \}/.test(cardSrc),
   "答案结构是 { id, selected[] }（custom 只在有内容时才带上）");
ok(/answers/.test(apiSrc) && /ask_id/.test(apiSrc), "提交走 { ask_id, answers[] }（对齐 DSH）");
ok(/\bpending\b/.test(eventCardSrc) && /<UserInputCard data=\{d\}/.test(eventCardSrc),
   "★ 超时 pending 的 expired 事件也长出可操作卡片（不是只说一句「仍可回答」）");

console.log(`\n${"=".repeat(52)}`);
console.log(`结果：${pass}/${pass + fail} 通过`);
if (fail) {
  console.log(`\n失败 ${fail} 条 —— 见上面 [X]`);
  process.exit(1);
}
console.log("=".repeat(52));
