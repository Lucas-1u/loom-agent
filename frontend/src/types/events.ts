/**
 * Loom 事件契约 —— 前端镜像
 *
 * ★ 唯一真相在后端 `backend/events.py`。改协议必须同时改这两份：
 *     backend/events.py
 *     frontend/src/types/events.ts   ← 你在这里
 *   单边改不会报错，只会让 GUI 静默丢字段（这是最难受的一种 bug）。
 *
 * 从后端继承的两条约束：
 *   · 事件只描述「发生了什么」，不含面向人类的措辞
 *     → 显示文案由 `lib/eventStyles.ts` 决定
 *   · `seq` 单调递增 → 用它排序、去重、断线补发（Last-Event-ID）
 */

/** 19 种事件类型（顺序与后端 EventType 一致）
 *
 * ★ 这份清单不只是类型：`services/eventStream.ts` 会**按它逐个 addEventListener**。
 *   少列一种 → 该事件的 SSE 帧在浏览器里没有监听器 → **静默丢失**（不会报错）。
 *   所以 frontend/scripts/test-events-contract.mjs 会拿它和 backend/events.py 逐项比对，
 *   两边不一致时测试直接失败 —— 这是防止再次漂移的唯一机制。
 */
export const EVENT_TYPES = [
  "task.start",
  "task.end",
  "plan",
  "thinking",
  "act",
  "observe",
  "tool.call",
  "tool.result",
  "approval.request",
  "approval.decision",
  "approval.expired",
  "reflect",
  "final_answer",
  "error",
  "budget.exceeded",
  "context.compacted",
  "rag.retrieve",
  "rag.result",
  "state.snapshot",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** 任务生命周期（对应后端 TaskStatus） */
export type TaskStatus =
  | "created"
  | "running"
  | "awaiting_approval"
  | "done"
  | "aborted"
  | "failed";

/** 风险等级（对应后端 RiskLevel） */
export type RiskLevel = "read" | "write" | "delete" | "exec";

/** 事件信封（对应后端 Event.to_dict()） */
export interface EventEnvelope<T = Record<string, unknown>> {
  seq: number;
  ts: number;
  task_id: string;
  turn: number;
  type: EventType;
  data: T;
}

// --------------------------------------------------------------------------- //
// 各事件载荷（字段严格照搬后端）
// --------------------------------------------------------------------------- //

export interface TaskStartData {
  goal: string;
  workspace: string;
  model: string;
}

export interface TaskEndData {
  status: TaskStatus;
  answer: string;
  reason: string;
}

export interface PlanStep {
  title: string;
  detail?: string;
  status?: "pending" | "doing" | "done" | "skipped";
}

export interface PlanData {
  steps: PlanStep[];
  note: string;
}

export interface ThinkingData {
  text: string;
  phase: string;
  done: boolean;
}

export interface ActData {
  intent: string;
  tool: string;
}

export interface ToolCallData {
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
}

export interface ToolError {
  kind: string;
  message: string;
  hint?: string;
  retryable?: boolean;
  security?: boolean;
}

export interface ToolResultData {
  call_id: string;
  tool: string;
  ok: boolean;
  summary: string;
  data?: unknown;
  error?: ToolError | null;
  duration_ms: number;
}

export interface ObserveData {
  call_id: string;
  digest: string;
}

export interface ReflectData {
  progress: string;
  next_action: string;
  done: boolean;
}

export interface FinalAnswerData {
  text: string;
}

export interface ErrorData {
  kind: string;
  message: string;
  fatal: boolean;
}

export interface BudgetExceededData {
  kind: string;
  limit: number | string;
  used: number | string;
}

/** 审批请求（对应 PendingApproval.to_dict()） */
export interface ApprovalRequestData {
  approval_id: string;
  tool: string;
  args: Record<string, unknown>;
  args_hash: string;
  risk: RiskLevel;
  action: string;
  target: string;
  impact: string;
  preview: string;
  turn: number;
  created_at: number;
}

/** 审批决策（对应 Decision.to_dict()） */
export interface ApprovalDecisionData {
  approved: boolean;
  approval_id: string;
  status: "approved" | "rejected" | "expired";
  reason: string;
  waited_s: number;
}

/**
 * 把信封的 data 窄化成具体类型。
 *
 * TS 的判别联合对「跨模块的运行时数据」帮助有限 —— 后端发来的是 JSON，
 * 我们只能信任它符合契约。这个函数只做**编译期**的收窄，不校验运行时。
 */
export function dataAs<T>(ev: EventEnvelope): T {
  return ev.data as unknown as T;
}
