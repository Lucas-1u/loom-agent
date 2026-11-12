/**
 * SSE 客户端
 *
 * ★ 为什么不用 `EventSource`（2026-11-11 改）
────────────────────────────────────────────────────────────────
 * `EventSource` 只把具名帧派发给**同名监听器**：
 *     后端发 `event: rag.result`  →  只有 `addEventListener("rag.result")` 能收到
 * 于是"前端得有一份和事件名完全一致的清单"变成了**投递前提**。
 * 清单少列一种 → 那种事件在浏览器里被静默丢弃：不报错、不进时间线、没有日志。
 * `context.compacted` 就是这么丢过一次；每加一种后端事件都要赌前端记得同步，
 * 这是协议层最脆弱的一环（`npm run test:events` 只能在构建期兜住，拦不住"已经上线的旧前端"）。
 *
 * 现在换成 `fetch` + `ReadableStream` + 自己解析（见 `services/sse.ts`）：
 * 传输层只认 SSE 语法，不认识任何业务事件名 —— **未知类型照常投递**，
 * 由 UI 用兜底卡片显示。`EVENT_TYPES` 从此只用来做两件事：
 *   ① TypeScript 的类型来源；② **发现漂移**（收到清单外类型时报告，见 onUnknownType）。
 * 它不再是投递的门票。
 *
 * ★ 重连策略：**只有一套**
────────────────────────────────────────────────────────────────
 * 手写指数退避 1s → 2s → 4s → 8s → 16s（上限 30s），最多 5 次。
 * 每次重连都显式带 `?since=lastId`（不依赖 `Last-Event-ID` 头），
 * 服务端据此补发缺口；补不回来时服务端会先发 `stream.gap` 明确告知。
 *
 * ★ ID 与去重
────────────────────────────────────────────────────────────────
 * 帧里的 `payload.id` 是**全局 SSE id**（断线补发用）；
 * `payload.event.seq` 是**任务内序号**（检测单个任务的事件缺口用）。两者不能混。
 * 去重逻辑在 store 层，这里只做透传。
 */

import { ENDPOINTS, url } from "./config";
import { EVENT_TYPES } from "../types/events";
import type { EventEnvelope } from "../types/events";
import { splitSseFrames } from "./sse";

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
  /**
   * 收到了 `EVENT_TYPES` 里没有声明的事件类型（协议漂移）。
   *
   * ★ 意义：事件**没有丢**（已经通过 onEvent 交给上层了），
   *   这里只是告诉 UI「你的清单落后于后端」。每种类型只报一次。
   *   实现方应当把它变成用户可见的提示 —— 否则"不丢但没人知道"等于慢性漂移。
   */
  onUnknownType?: (type: string) => void;
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

/**
 * 本地声明过的事件类型。
 *
 * ★ 只用于**判断要不要报告漂移**，绝不参与"这条帧要不要处理"。
 *   这条边界是本次改造的全部意义：把「能不能收到」和「认不认识」彻底解耦。
 */
const DECLARED_TYPES = new Set<string>(EVENT_TYPES);

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
  let closed = false; // 调用方主动关闭
  let abort: AbortController | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  /** 已报告过的未知类型（每种只报一次，避免刷屏） */
  const reported = new Set<string>();

  function scheduleRetry(): void {
    if (closed) return;
    attempt += 1;

    if (attempt > maxRetries) {
      handlers.onState("closed", attempt);
      return;
    }

    handlers.onState("reconnecting", attempt);
    retryTimer = setTimeout(() => void connect(), backoff(attempt));
  }

  /** 处理一帧：路由到 onGap / onEvent，顺带发现协议漂移。 */
  function handleFrame(frame: { id: string; event: string; data: string }): void {
    let payload: {
      id?: number;
      task_id?: string;
      session_id?: string;
      event?: EventEnvelope;
    };
    try {
      payload = JSON.parse(frame.data);
    } catch {
      return; // 坏帧直接丢，不崩整条流
    }
    if (!payload || typeof payload !== "object") return;

    // ---- 控制帧：缺口通知（故意不带 id，所以不影响 since）----
    if (frame.event === "stream.gap") {
      // 两种历史格式并存：裸 data 与 {type,data} 包一层，这里都认
      const raw = payload as unknown as Record<string, unknown>;
      const info =
        raw.data && typeof raw.data === "object"
          ? (raw.data as GapInfo)
          : (raw as unknown as GapInfo);
      handlers.onGap(info && typeof info === "object" ? info : { reason: "unknown" });
      return;
    }

    const ev = payload.event;
    if (!ev || typeof ev !== "object") return;

    // ---- 推进游标：下次重连从这条之后开始补 ----
    const gid = typeof payload.id === "number" ? payload.id : Number(frame.id || 0);
    if (gid > (since ?? 0)) since = gid;

    // ---- 协议漂移：只报告，不拦截 ----
    //   ★ 顺序很重要：先判断再投递，而且无论认不认识都要投递。
    const type = String((ev as { type?: unknown }).type ?? "");
    if (type && !DECLARED_TYPES.has(type) && !reported.has(type)) {
      reported.add(type);
      handlers.onUnknownType?.(type);
    }

    handlers.onEvent({
      id: gid,
      task_id: payload.task_id ?? "",
      session_id: payload.session_id ?? "",
      event: ev,
    });
  }

  async function connect(): Promise<void> {
    if (closed) return;

    const path = since !== null ? `${ENDPOINTS.stream}?since=${since}` : ENDPOINTS.stream;
    handlers.onState(attempt === 0 ? "connecting" : "reconnecting", attempt);

    const ctl = new AbortController();
    abort = ctl;

    let resp: Response;
    try {
      resp = await fetch(url(path), {
        method: "GET",
        headers: { Accept: "text/event-stream" },
        cache: "no-store",
        signal: ctl.signal,
      });
    } catch {
      if (!closed) scheduleRetry();
      return;
    }

    if (!resp.ok || !resp.body) {
      if (!closed) scheduleRetry();
      return;
    }

    // ---- 连上了：重置退避 ----
    attempt = 0;
    handlers.onState("open");

    const reader = resp.body.getReader();
    // ★ stream: true —— 一个中文字符可能被切在两块之间，不能按块独立解码
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { frames, rest } = splitSseFrames(buffer);
        buffer = rest;
        for (const f of frames) handleFrame(f);
      }
    } catch {
      // 读流中断（服务重启 / 网络断 / 我们主动 abort）—— 统一走重连判断
    } finally {
      try {
        await reader.cancel();
      } catch {
        /* 已经断了就算了 */
      }
    }

    if (!closed) scheduleRetry();
  }

  void connect();

  // ---- 返回关闭函数 ----
  return function close(): void {
    closed = true;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (abort !== null) {
      abort.abort();
      abort = null;
    }
    handlers.onState("closed");
  };
}
