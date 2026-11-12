/**
 * SSE 帧解析（纯函数：不碰 DOM、不碰网络、不认识任何业务事件名）
 *
 * ★ 为什么需要它
────────────────────────────────────────────────────────────────
 * 原来是 `EventSource` + 按 `EVENT_TYPES` 逐个 `addEventListener`。
 * `EventSource` 只把帧派发给**同名监听器**，所以前端清单少列一种 →
 * 那种事件被浏览器静默丢弃（不报错、不进时间线、没有日志）。
 * `context.compacted` 当年就是这么丢的；今天每加一种后端事件都要赌前端记得同步。
 *
 * 改成自己解析之后，传输层**只认 SSE 语法，不认识业务类型**：
 * `event: 任何名字` 都会被原样交出去。未知类型不再是"丢失"，
 * 而是"照常到达、由 UI 决定怎么显示（兜底卡片）"。
 *
 * ★ 规范要点（只实现我们真的会收到的部分）
────────────────────────────────────────────────────────────────
 *   帧分隔     空行（`\n\n`；`\r\n\r\n` 也接受，某些代理会改行尾）
 *   `:` 开头   注释，通常是心跳（后端每 SSE_KEEPALIVE_S 发一次 `: keepalive`）
 *   `field: v` value 前的**一个**空格要去掉（`data: x` → `x`，不是 ` x`）
 *   `data`     多行时用 `\n` 拼起来
 *   `event`    缺省为空（规范里等价于 `message`）
 *   `retry`    忽略 —— 重连节奏由客户端自己管（见 eventStream.ts 的退避）
 *
 * 不解析 `data` 里的 JSON：那是业务层的事。解析器越"笨"越不容易崩。
 */

export interface SseFrame {
  /** `id:` 字段（全局事件 id）。没有则为空串 —— 心跳与 stream.gap 都没有 id */
  id: string;
  /** `event:` 字段。空串表示未声明（规范等价于 "message"） */
  event: string;
  /** `data:` 字段，多行已用 `\n` 连接。空串的帧会被丢弃，不会出现在结果里 */
  data: string;
}

/** 帧分隔符（规范化之后） */
const FRAME_SEP = "\n\n";

/**
 * 缓冲区上限。
 *
 * 正常情况下每帧几百字节，这个值几乎不可能触发。设它是因为：
 * 如果服务端返回了 `text/event-stream` 却不是 SSE（比如被网关塞了个 HTML 错误页），
 * 缓冲区会一直涨到内存爆掉。宁可丢掉畸形数据，也不能把标签页撑死。
 */
const MAX_BUFFER = 4 * 1024 * 1024;

/**
 * 解析单个帧的原始文本（不含分隔用的空行）。
 *
 * 返回 null 表示这不是一个可投递的帧（纯注释 / 心跳 / 没有 data）。
 * 任何畸形输入都不会抛异常 —— 坏帧丢弃比整条流崩掉好。
 */
export function parseSseFrame(raw: string): SseFrame | null {
  let id = "";
  let event = "";
  const dataLines: string[] = [];

  for (const line of raw.split("\n")) {
    if (line === "") continue; // 规范里空行只出现在帧边界，这里防御性跳过
    if (line.startsWith(":")) continue; // 注释 / keepalive

    const idx = line.indexOf(":");
    const field = idx < 0 ? line : line.slice(0, idx);
    let value = idx < 0 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1); // 只去掉一个前导空格

    if (field === "data") dataLines.push(value);
    else if (field === "event") event = value;
    else if (field === "id") id = value;
    // retry / 未知字段：忽略
  }

  if (dataLines.length === 0) return null;
  return { id, event, data: dataLines.join("\n") };
}

/**
 * 从累积缓冲区里切出所有**已完整**的帧。
 *
 * ★ 必须保留尾巴：网络分块与帧边界无关，一帧可能被拆成两次 `read()`。
 *   所以函数签名是 `(buffer) -> {frames, rest}`，调用方把 `rest` 留着和下一块拼。
 *
 * ★ 行尾规范化每次都作用在**整个缓冲区**上，所以"上一块以 `\r` 结尾、下一块以 `\n` 开头"
 *   这种跨块 CRLF 也能被正确识别（上一轮留下的单个 `\r` 下一轮会变成 `\r\n`）。
 */
export function splitSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames: SseFrame[] = [];

  let start = 0;
  for (;;) {
    const idx = normalized.indexOf(FRAME_SEP, start);
    if (idx < 0) break;
    const frame = parseSseFrame(normalized.slice(start, idx));
    if (frame) frames.push(frame);
    start = idx + FRAME_SEP.length;
  }

  let rest = normalized.slice(start);
  if (rest.length > MAX_BUFFER) rest = ""; // 畸形流：丢弃而不是无限增长
  return { frames, rest };
}
