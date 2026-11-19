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

/** 24 种事件类型（顺序与后端 EventType 一致）
 *
 * ★ 这份清单**不是投递门票**（2026-11-11 起）。
 *   历史上 `services/eventStream.ts` 会按它逐个 `addEventListener`，
 *   于是清单少列一种 → 该事件的 SSE 帧在浏览器里没有监听器 → **静默丢失**。
 *   现在传输层改成自己解析 SSE 帧（`services/sse.ts`），**不认识事件名也能收到**，
 *   未知类型会走兜底卡片显示，并通过 `onUnknownType` 冒到界面顶部。
 *
 *   那这份清单还留着干什么？两件事，都不能少：
 *     ① TypeScript 的类型来源（`EventType` / 各 `*Data` 的判别依据）
 *     ② **漂移探测**：收到清单外的类型时报告「前端落后于后端」
 *     ③ 构建期比对（`npm run test:events` 拿它和 backend/events.py 逐项对齐）
 *
 *   换句话说：漏写这里不会让你"看不见事件"，只会让那个事件显示成原始类型名 +
 *   JSON 兜底卡片，同时顶部亮一条"未声明事件"提示。**看得见的不完美 > 看不见的完美。**
 */
export const EVENT_TYPES = [
  "task.start",
  "task.end",
  "plan",
  "thinking",
  "llm.usage",
  "act",
  "observe",
  "tool.call",
  "tool.result",
  "approval.request",
  "approval.decision",
  "approval.expired",
  "user_input.request",
  "user_input.response",
  "user_input.expired",
  "permission.mode",
  "reflect",
  "final_answer",
  "error",
  "budget.exceeded",
  "context.compacted",
  "rag.retrieve",
  "rag.result",
  "workflow.state",
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

/** 权限模式（步骤 6/7）
 *
 * ★ `enforced` 是后端如实报告的状态，不是承诺：
 *   read-only 只有在写/执行工具都没注册时才是 true。
 *   步骤 7 起服务端会按任务切换注册表，read-only 才真正生效。
 *
 * ★ 这份清单为什么是**运行时数组**而不是纯 union：
 *   纯 union 在运行时什么都不存在，漏了一种也没人发现 —— 事实上就漏过：
 *   2026-11-11 补 desktop（第四种模式）时发现前端还停在三种，
 *   界面选不到它、`permission.mode` 事件也没有对应文案。
 *   写成数组之后 `npm run test:events` 能拿它和后端
 *   `backend/agent/state.py::VALID_SESSION_MODES` 逐项比对。
 */
export const SESSION_MODES = [
  "read-only",
  "workspace-write",
  "full-access",
  "desktop",
] as const;

export type SessionMode = (typeof SESSION_MODES)[number];

export interface PermissionModeData {
  mode: SessionMode;
  /** api = 调用方显式传入；default = 没传，走默认值 */
  source: "api" | "default";
  /** 该模式下注册表里的工具总数 */
  tools: number;
  /** 这个模式在当前进程里是否真的生效 */
  enforced: boolean;
}

/** ask_user 的一个选项（对齐 DSH：label 必填，description 可选） */
export interface UserInputOption {
  label: string;
  description?: string;
}

/** ask_user 的一个问题 */
export interface UserInputQuestion {
  /** 稳定 id，答案里原样回显 */
  id: string;
  question: string;
  /** 可选短标题（如"确认"/"选模式"） */
  header?: string;
  options?: UserInputOption[];
  /** 是否允许多选（默认单选） */
  multi_select?: boolean;
}

/** 用户对一个问题的回答
 *
 * ★ `selected` 为空且没有 `custom` = 用户**明确跳过**了这个问题。
 *   `custom` 是「其他（自己填写）」的内容 —— 本地增强，
 *   对齐 DSH 答案结构里的同名字段。
 */
export interface UserInputAnswer {
  id: string;
  selected: string[];
  custom?: string;
}

/** `user_input.request` 载荷 */
export interface UserInputRequestData {
  ask_id: string;
  task_id: string;
  /** 第一个问题的文本（兼容旧渲染；新代码读 questions） */
  question: string;
  questions?: UserInputQuestion[];
  timeout_s?: number | null;
}

/** `user_input.response` 载荷 */
export interface UserInputResponseData {
  ask_id: string;
  task_id: string;
  answers: UserInputAnswer[];
  /** true = 前台等待超时之后才答的（Agent 会在下一步看到） */
  late: boolean;
  /** 渲染好的纯文本（兼容旧渲染） */
  answer?: string;
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
