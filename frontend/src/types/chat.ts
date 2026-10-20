import type { ApprovalRequestData, EventType, TaskStatus } from "./events";

/** 两种展示模式（★ 是「怎么显示」，不是「跑哪个 Agent」） */
export type ViewMode = "chat" | "execution";

export type Role = "user" | "assistant" | "system";

/**
 * 聊天消息。
 *
 * ★ 关键设计：**它不是独立数据源**，而是从事件流「投影」出来的。
 *   task.start  → 一条 user 消息
 *   final_answer → 一条 assistant 消息
 *   error(fatal) → 一条 system 消息
 *
 *   这样做的好处：切换视图、刷新、重连时，聊天记录和执行流永远一致 ——
 *   因为它们是同一份事实的两种读法，而不是两份需要同步的状态。
 */
export interface ChatMessage {
  id: string;
  role: Role;
  text: string;
  ts: number;
  /** 由哪条事件派生（便于点消息跳转到对应事件） */
  sourceSeq: number;
  taskId: string;
  /** 该条消息是否还在生成（Phase 3 流式用） */
  streaming?: boolean;
}

/** 前端侧的审批条目（比后端多几个 UI 状态字段） */
export interface ApprovalItem extends ApprovalRequestData {
  /**
   * ★ 归属信息 —— 后端事件契约里没有这两个字段，
   *   但前端在 reducer 里从 `event.task_id` 反查出 session 后补上。
   *   作用：切换会话时，**绝不能**把别的 Session 的待审批显示出来，
   *        也绝不能把按钮接到错误的 task 上。
   */
  session_id?: string;
  task_id?: string;

  /** 已提交决策、等待后端确认 —— 用于禁用按钮防重复点击 */
  submitting?: boolean;
  /** 本地已知的最终状态 */
  resolved?: "approved" | "rejected" | "expired";
}

/** SSE / HTTP 连接状态 */
export type ConnectionState = "idle" | "connecting" | "connected" | "error";

/** 任务摘要（顶栏、侧栏展示用） */
export interface TaskSummary {
  id: string;
  goal: string;
  status: TaskStatus;
  startedAt: number;
  endedAt?: number;
  endReason?: string;
  turn: number;
}

/**
 * 事件 → 视觉分组的静态信息（不属于契约，属于展示层）。
 * 放在这里是为了让 EventCard 能拿到「这条事件该显示成什么」，而不用自己 switch。
 */
export interface EventMeta {
  type: EventType;
  seq: number;
}
