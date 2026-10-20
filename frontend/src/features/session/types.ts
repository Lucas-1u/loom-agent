/**
 * Session / Task / Message 数据模型
 *
 * ★ 四层语义，职责必须分清（任务书 §4）：
 *
 *     Session  一个持续对话的容器（有自己的工作目录）
 *       └── Task   一次具体的 Agent 执行（有自己的 task_id 和事件流）
 *             └── Message  可读的对话内容（用户说的 / Agent 最终答的）
 *             └── AgentEvent  执行过程（tool.call 之类，不是聊天消息）
 *
 *   为什么不能用一个全局消息数组？
 *     因为「执行过程」和「对话内容」的粒度、消费者、生命周期都不同：
 *     · Message 给聊天视图看，一条 user + 一条 assistant 就够
 *     · AgentEvent 给执行流看，一个任务能产生几十条
 *     混在一起，要么聊天里刷屏，要么执行流里丢细节。
 *
 *   为什么 Task 必须挂在 Session 下？
 *     因为工作区是 Session 的属性，而 Task 必须在那个工作区里跑。
 *     把 Task 做成全局列表，"这个任务属于哪个工作区"就得靠反查，很容易错。
 */

import type { TaskStatus } from "../../types/events";

// --------------------------------------------------------------------------- //
// Session
// --------------------------------------------------------------------------- //
export interface Session {
  session_id: string;
  title: string;
  /** 该会话自己的工作目录（原始写法，未规范化） */
  workspace_path: string;
  created_at: number;
  /** 最近活动时间 —— 用于排序，**不再用于分组** */
  updated_at: number;
}

// --------------------------------------------------------------------------- //
// Task
// --------------------------------------------------------------------------- //
export interface Task {
  task_id: string;
  /** ★ 必须明确归属，不能靠"当前会话"这种隐式状态推断 */
  session_id: string;
  goal: string;
  status: TaskStatus;
  started_at: number;
  ended_at?: number;
  end_reason?: string;
  /** 最终答复（也同步写一条 assistant Message，这里留一份便于查任务） */
  result?: string;
  /** 该任务的轮次数（来自最后一条事件） */
  turn?: number;
}

// --------------------------------------------------------------------------- //
// Message
// --------------------------------------------------------------------------- //
export type MessageRole = "user" | "assistant" | "system";

export interface Message {
  message_id: string;
  session_id: string;
  /** 关联到产生它的 Task（用户消息在任务开始前就存在，此时可以没有） */
  task_id?: string;
  role: MessageRole;
  content: string;
  created_at: number;
}

// --------------------------------------------------------------------------- //
// 侧栏分组（派生结构，不存储）
// --------------------------------------------------------------------------- //
export interface WorkspaceGroup {
  /** 规范化 + 小写后的完整路径；未设置时是 "__unset__" */
  key: string;
  /** 规范化后的原始路径（用于显示与 tooltip） */
  path: string;
  /** 显示名（最后一段） */
  label: string;
  sessions: Session[];
  /** 组内最近活动时间（用于组间排序） */
  latestAt: number;
}
