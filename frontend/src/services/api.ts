/**
 * 后端 HTTP 客户端
 *
 * ★ 三条纪律
────────────────────────────────────────────────────────────────
  ① **组件里不许拼 URL**。所有请求走这里，URL 从 `config.ts` 取。
  ② **错误必须能被区分**。网络断 / 超时 / HTTP 4xx / 响应格式不对，
     是四类完全不同的问题，UI 该给四种提示。
  ③ **后端字段 ≠ 前端字段**。后端叫 `workspace`，前端叫 `workspace_path`
     （从 Phase 2 就定了的名字）。映射只在这一层做，不污染组件。

★ 后端错误有两种形状（这是实测出来的）
────────────────────────────────────────────────────────────────
    404 / 422  →  {"detail": "会话不存在：s_xxx"}          ← 字符串
    409        →  {"detail": {"accepted": false, "reason": "..."}}  ← 对象

  一律 `detail.slice()` 会拿到 "[object Object]"。这里统一提取出可读文案。
 */

import { ENDPOINTS, url } from "./config";
import type { EventEnvelope, TaskStatus } from "../types/events";
import type { Message, Session, Task } from "../features/session/types";

// --------------------------------------------------------------------------- //
// 错误
// --------------------------------------------------------------------------- //
export type ApiErrorKind = "network" | "timeout" | "http" | "invalid";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  /** 后端原始 detail（可能是对象） */
  readonly detail: unknown;

  constructor(kind: ApiErrorKind, status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }

  /** 给人看的一句话，UI 可以直接显示 */
  get note(): string {
    switch (this.kind) {
      case "network":
        return "连不上后端服务，请确认它已经启动（python -m backend.server）";
      case "timeout":
        return "后端响应超时，它可能正忙或已经卡住";
      case "invalid":
        return "后端返回的格式不符合预期，可能是版本不匹配";
      case "http":
        return this.message;
    }
  }
}

// --------------------------------------------------------------------------- //
// 底层 fetch
// --------------------------------------------------------------------------- //
const DEFAULT_TIMEOUT_MS = 15_000;
const CREATE_TASK_TIMEOUT_MS = 60_000;

interface RequestOpts {
  method?: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
  timeoutMs?: number;
}

async function request<T>(path: string, opts: RequestOpts = {}): Promise<T> {
  const { method = "GET", body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = opts;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(url(path), {
      method,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (exc) {
    clearTimeout(timer);
    if (exc instanceof DOMException && exc.name === "AbortError") {
      throw new ApiError("timeout", 0, `请求超时（${timeoutMs}ms）：${path}`);
    }
    throw new ApiError("network", 0, `网络请求失败：${path}`, exc);
  }
  clearTimeout(timer);

  // ---- 解析响应 ----
  let payload: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      if (!res.ok) {
        throw new ApiError("http", res.status, `HTTP ${res.status}：${text.slice(0, 120)}`);
      }
      throw new ApiError("invalid", res.status, `响应不是合法 JSON：${text.slice(0, 120)}`);
    }
  }

  if (!res.ok) {
    throw new ApiError("http", res.status, extractDetail(payload, res.status), payload);
  }

  return payload as T;
}

/**
 * 从 FastAPI 的错误体里抠出可读文案。
 *
 * 覆盖三种形状：
 *   {"detail": "字符串"}
 *   {"detail": {"note": "...", "reason": "..."}}
 *   {"detail": [{msg: "...", loc: [...]}]}   ← 422 校验错误
 */
function extractDetail(payload: unknown, status: number): string {
  const d = (payload as { detail?: unknown } | null)?.detail;

  if (typeof d === "string") return d;

  if (d && typeof d === "object" && !Array.isArray(d)) {
    const o = d as Record<string, unknown>;
    if (typeof o.note === "string" && o.note) return o.note;
    if (typeof o.reason === "string" && o.reason) return o.reason;
    return JSON.stringify(o).slice(0, 160);
  }

  if (Array.isArray(d)) {
    const first = d[0] as { msg?: string; loc?: unknown[] } | undefined;
    if (first?.msg) return `${first.msg}（${JSON.stringify(first.loc ?? [])}）`;
  }

  return `HTTP ${status}`;
}

// --------------------------------------------------------------------------- //
// 健康检查
// --------------------------------------------------------------------------- //
export interface HealthInfo {
  ok: boolean;
  version: string;
  phase: string;
  provider: string;
  model: string;
  scheduler: {
    max_global: number;
    running: number;
    queued: number;
    known_runtimes: number;
  };
  hub: {
    latest_id: number;
    oldest_id: number;
    buffered: number;
    bytes: number;
    evicted: number;
    dropped: number;
    subscribers: number;
  };
}

export function health(): Promise<HealthInfo> {
  // 健康检查要快 —— 3 秒连不上就当后端没起
  return request<HealthInfo>(ENDPOINTS.health, { timeoutMs: 3_000 });
}

// --------------------------------------------------------------------------- //
// Session
// --------------------------------------------------------------------------- //
interface RawSession {
  session_id: string;
  title: string;
  workspace: string;      // ★ 后端叫 workspace
  created_at: number;
  updated_at: number;
}

function toSession(r: RawSession): Session {
  return {
    session_id: r.session_id,
    title: r.title,
    workspace_path: r.workspace,   // ★ 映射成前端的字段名
    created_at: r.created_at,
    updated_at: r.updated_at,
  };
}

export async function listSessions(): Promise<Session[]> {
  const r = await request<{ sessions: RawSession[] }>(ENDPOINTS.sessions);
  return (r.sessions ?? []).map(toSession);
}

export async function createSession(b: {
  title?: string;
  workspace?: string;
}): Promise<Session> {
  const r = await request<RawSession>(ENDPOINTS.sessions, {
    method: "POST",
    body: { title: b.title ?? "", workspace: b.workspace ?? "" },
  });
  return toSession(r);
}

export async function sessionMessages(sessionId: string): Promise<Message[]> {
  const r = await request<{ session_id: string; messages: Message[] }>(
    ENDPOINTS.sessionMessages(sessionId),
  );
  return r.messages ?? [];
}

// --------------------------------------------------------------------------- //
// Task
// --------------------------------------------------------------------------- //
/** 后端任务摘要（列表用，没有 state） */
export interface RawTask {
  task_id: string;
  session_id: string;
  goal: string;
  status: TaskStatus;
  model: string;
  workspace: string;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  end_reason: string;
  result: string;
  turn: number;
  queued: boolean;
  events_available: boolean;
}

function toTask(r: RawTask): Task {
  return {
    task_id: r.task_id,
    session_id: r.session_id,
    goal: r.goal,
    status: r.status,
    // 排队中 started_at 为 null —— 用 created_at 兜底，避免 UI 显示 NaN
    started_at: r.started_at ?? r.created_at,
    ended_at: r.ended_at ?? undefined,
    end_reason: r.end_reason || undefined,
    result: r.result || undefined,
    turn: r.turn,
  };
}

export async function listTasks(sessionId?: string): Promise<RawTask[]> {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const r = await request<{ tasks: RawTask[] }>(ENDPOINTS.tasks + q);
  return r.tasks ?? [];
}

/** 任务快照（权威状态，含 core 的完整 state） */
export interface TaskSnapshot extends RawTask {
  state?: Record<string, unknown> | null;
  pending_approval?: {
    approval_id: string;
    tool: string;
    risk: string;
    action: string;
    target: string;
    impact: string;
    preview: string;
    args?: Record<string, unknown>;
    args_hash?: string;
  } | null;
  events_buffered?: number;
  idempotent_replay?: boolean;
  queue_position?: number;
}

export async function getTask(taskId: string): Promise<TaskSnapshot> {
  return request<TaskSnapshot>(ENDPOINTS.task(taskId));
}

/**
 * 创建任务。
 *
 * ★ `idemKey` 必传：网络重试时必须复用**同一个 key 和同样的 body**，
 *   否则后端会当成两次新任务。新的任务才生成新 key。
 */
export async function createTask(
  b: { goal: string; session_id: string; workspace: string },
  idemKey: string,
): Promise<TaskSnapshot> {
  return request<TaskSnapshot>(ENDPOINTS.tasks, {
    method: "POST",
    body: b,
    headers: { "Idempotency-Key": idemKey },
    timeoutMs: CREATE_TASK_TIMEOUT_MS,
  });
}

export interface TaskEventsResult {
  task_id: string;
  available: boolean;
  reason: string;
  note?: string;
  events: Array<{
    id: number;              // 全局 SSE id
    task_id: string;
    session_id: string;
    size: number;
    event: EventEnvelope;    // 里面带任务内 seq
  }>;
}

export function taskEvents(taskId: string, since?: number): Promise<TaskEventsResult> {
  const q = since !== undefined ? `?since=${since}` : "";
  return request<TaskEventsResult>(ENDPOINTS.taskEvents(taskId) + q);
}

export interface CancelResult {
  accepted: boolean;
  state: string;
  note: string;
}

export function cancelTask(taskId: string): Promise<CancelResult> {
  return request<CancelResult>(ENDPOINTS.cancel(taskId), { method: "POST" });
}

// --------------------------------------------------------------------------- //
// 审批
// --------------------------------------------------------------------------- //
export interface ApprovalDecisionResult {
  accepted: boolean;
  reason: string;
  note: string;
}

export async function decideApproval(
  approvalId: string,
  b: { task_id: string; decision: "approved" | "rejected"; reason?: string },
): Promise<ApprovalDecisionResult> {
  try {
    return await request<ApprovalDecisionResult>(
      ENDPOINTS.approvalDecision(approvalId),
      { method: "POST", body: { reason: "", ...b } },
    );
  } catch (exc) {
    // 409 的 detail 是对象，已经由 extractDetail 转成 note；
    // 这里把「已被处理」这类语义也包装成结果，方便 UI 直接显示
    if (exc instanceof ApiError && exc.status === 409) {
      const d = exc.detail as { detail?: ApprovalDecisionResult } | null;
      const inner = d?.detail;
      if (inner && typeof inner === "object") {
        return { accepted: false, reason: inner.reason ?? "conflict", note: inner.note ?? exc.note };
      }
      return { accepted: false, reason: "conflict", note: exc.note };
    }
    throw exc;
  }
}

// --------------------------------------------------------------------------- //
// 便利：把 RawTask 转成前端的 Task
// --------------------------------------------------------------------------- //
export { toTask };
