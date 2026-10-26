/**
 * SSE 客户端
 *
 * ★ 重连策略：**只有一套**，手写指数退避，原生 EventSource 的自动重连被显式关掉
────────────────────────────────────────────────────────────────
  浏览器原生 `EventSource` 自带重连，但：
    · 间隔不可控（默认 3s，可能被服务端 retry: 改）
    · 次数无限（后端挂了会一直撞）
    · 你自己再叠一层重连 → **两套机制打架**，表现为重复连接、重复事件、状态乱跳

  所以这里的做法是：`onerror` 时 `close()` 掉原生连接，由我们自己的定时器
  按 1s → 2s → 4s → 8s → 16s（上限 30s）重试，最多 5 次。
  每次重连都显式带 `?since=lastId`，不依赖浏览器是否带 Last-Event-ID。

★ 为什么用 `addEventListener` 逐个注册 16 种事件类型？
────────────────────────────────────────────────────────────────
  后端每帧都写 `event: <type>`（比如 `event: tool.call`）。
  原生 `EventSource.onmessage` **只收没有 event 字段的消息**，
  对具名事件一律不触发 —— 这是最容易踩的坑：连上了但一个事件都收不到。

★ ID 与去重
────────────────────────────────────────────────────────────────
  `msg.id` 是**全局 SSE id**（断线补发用）；
  `msg.event.seq` 是**任务内序号**（检测单个任务的事件缺口用）。
  两者不能混。去重逻辑在 store 层，这里只做透传。
 */

import { ENDPOINTS, url } from "./config";
import { EVENT_TYPES } from "../types/events";
import type { EventEnvelope } from "../types/events";

// --------------------------------------------------------------------------- //
// 类型
// --------------------------------------------------------------------------- //
export interface StreamEvent {
  /** 全局 SSE id（单调递增，跨任务唯一） */
  id: number;
  task_id: string;
  session_id: string;
  /** 任务内事件（含 seq） */
  event: EventEnvelope;
}

export interface GapInfo {
  reason: string;
  requested_since?: number;
  oldest_available?: number;
  latest?: number;
  dropped?: number;
  evicted?: number;
  note?: string;
}

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamHandlers {
  onEvent: (ev: StreamEvent) => void;
  onGap: (info: GapInfo) => void;
  onState: (state: StreamState, attempt?: number) => void;
}

export interface OpenOptions {
  /** 从哪个全局 id 之后开始补发；不传则只收新事件 */
  since?: number | null;
  handlers: StreamHandlers;
  /** 最大重试次数（默认 5）；超过后 onState("closed") 并不再尝试 */
  maxRetries?: number;
}

// --------------------------------------------------------------------------- //
// 退避参数
// --------------------------------------------------------------------------- //
const BASE_DELAY_MS = 1_000;
const MAX_DELAY_MS = 30_000;
const DEFAULT_MAX_RETRIES = 5;

function backoff(attempt: number): number {
  const d = BASE_DELAY_MS * 2 ** (attempt - 1);
  return Math.min(d, MAX_DELAY_MS);
}

// --------------------------------------------------------------------------- //
// 主函数
// --------------------------------------------------------------------------- //
/**
 * 打开事件流。返回一个 `close()`，供组件卸载时调用。
 *
 * ★ 调用方负责在 `close()` 之后不要再使用 handlers（避免向已卸载组件 setState）。
 */
export function openEventStream(opts: OpenOptions): () => void {
  const { handlers, maxRetries = DEFAULT_MAX_RETRIES } = opts;

  let since = opts.since ?? null;
  let attempt = 0;
  let closed = false;          // 调用方主动关闭
  let es: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  function connect(): void {
    if (closed) return;

    const path = since !== null ? `${ENDPOINTS.stream}?since=${since}` : ENDPOINTS.stream;
    handlers.onState(attempt === 0 ? "connecting" : "reconnecting", attempt);

    let source: EventSource;
    try {
      source = new EventSource(url(path));
    } catch {
      scheduleRetry();
      return;
    }
    es = source;

    source.onopen = () => {
      attempt = 0;                     // 连上了就重置退避
      handlers.onState("open");
    };

    // ---- 19 种业务事件 + 1 种控制事件，逐个注册 ----
    // ★ EventSource 只把帧派发给**同名监听器**：EVENT_TYPES 少列一种，
    //   该事件的帧就永远不会进 onEvent（浏览器不报错、也不触发 onmessage）。
    //   所以 EVENT_TYPES 必须与 backend/events.py 一一对应，
    //   由 scripts/test-events-contract.mjs 兜底（跑 npm run test:events）。
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        const msg = raw as MessageEvent<string>;
        let payload: { id?: number; task_id?: string; session_id?: string; event?: EventEnvelope };
        try {
          payload = JSON.parse(msg.data);
        } catch {
          return;                      // 坏帧直接丢，不崩整条流
        }
        if (!payload.event) return;

        // 推进游标：下次重连从这条之后开始补
        const gid = typeof payload.id === "number" ? payload.id : Number(msg.lastEventId || 0);
        if (gid > (since ?? 0)) since = gid;

        handlers.onEvent({
          id: gid,
          task_id: payload.task_id ?? "",
          session_id: payload.session_id ?? "",
          event: payload.event,
        });
      });
    }

    // ---- 缺口通知（这类帧故意不带 id，所以不影响 since）----
    source.addEventListener("stream.gap", (raw) => {
      const msg = raw as MessageEvent<string>;
      try {
        handlers.onGap(JSON.parse(msg.data) as GapInfo);
      } catch {
        handlers.onGap({ reason: "unknown" });
      }
    });

    // ---- 错误：关掉原生重连，交给我们自己 ----
    source.onerror = () => {
      source.close();
      es = null;
      if (closed) return;
      scheduleRetry();
    };
  }

  function scheduleRetry(): void {
    if (closed) return;
    attempt += 1;

    if (attempt > maxRetries) {
      handlers.onState("closed", attempt);
      return;
    }

    handlers.onState("reconnecting", attempt);
    retryTimer = setTimeout(connect, backoff(attempt));
  }

  connect();

  // ---- 返回关闭函数 ----
  return function close(): void {
    closed = true;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (es !== null) {
      es.close();
      es = null;
    }
    handlers.onState("closed");
  };
}
