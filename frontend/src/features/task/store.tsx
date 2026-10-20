/**
 * 任务状态中心（唯一数据源）
 *
 * ★ 设计要点
 * ────────────────────────────────────────────────────────────
 * 1. **只有一份状态**。聊天视图和执行流视图都读它，谁也不维护自己的副本。
 *    所以「切换视图不丢数据 / 不中断任务」是**结构性保证**，不是靠同步逻辑。
 *
 * 2. **事件按 task_id 分组存**，事件本身按 seq 去重。
 *    Phase 3 的 SSE 重连会重复推送，靠 seq 去重即可幂等。
 *
 * 3. **聊天消息不存**，由 `selectors.deriveMessages()` 从事件投影出来。
 *    这里只存"事实"（事件），视图形状由选择器算。
 *
 * 4. Phase 2 是演示模式：初始 state 灌入 mock 事件，并带 `demo: true`。
 *    Phase 3 只需把 `APPEND_EVENT` 接到 SSE 上，其余不用改。
 */

import {
  createContext,
  useContext,
  useMemo,
  useReducer,
  type Dispatch,
  type ReactNode,
} from "react";
import type { EventEnvelope, TaskStatus } from "../../types/events";
import type { ApprovalItem, ConnectionState, TaskSummary, ViewMode } from "../../types/chat";
import { DEMO_MODE } from "../../services/config";
import { DEMO_EVENTS, DEMO_TASK } from "../../services/mock";
import { DEMO_TASK_ID, DEMO_WORKSPACE, DEMO_MODEL } from "../../services/demoConstants";

// --------------------------------------------------------------------------- //
// State
// --------------------------------------------------------------------------- //
export interface TaskStateStore {
  /** 当前展示模式 —— 只影响渲染，不影响任何执行状态 */
  viewMode: ViewMode;

  /** 是否处于演示模式（数据来自 mock.ts） */
  demo: boolean;

  /** 与后端的连接状态（Phase 3 接 SSE 后才会变成 connected） */
  connection: ConnectionState;

  workspace: string;
  model: string;

  /** task_id → 任务摘要 */
  tasks: Record<string, TaskSummary>;

  /** task_id → 该任务的全部事件（按 seq 自然有序，但我用选择器排序保证） */
  events: Record<string, EventEnvelope[]>;

  /** approval_id → 审批条目（前端侧状态，比后端多 submitting / resolved） */
  approvals: Record<string, ApprovalItem>;

  /** 当前聚焦的任务 */
  activeTaskId: string | null;
}

function makeInitialState(): TaskStateStore {
  // 支持 ?view=execution 直接打开执行流（便于截图 / 分享 / 调试）
  const initialView: ViewMode =
    new URLSearchParams(window.location.search).get("view") === "execution"
      ? "execution"
      : "chat";

  if (!DEMO_MODE) {
    return {
      viewMode: initialView,
      demo: false,
      connection: "idle",
      workspace: "",
      model: DEMO_MODEL,
      tasks: {},
      events: {},
      approvals: {},
      activeTaskId: null,
    };
  }

  // 演示模式：把 mock 事件灌进来，界面立刻有内容
  const events: Record<string, EventEnvelope[]> = { [DEMO_TASK_ID]: DEMO_EVENTS };
  const approvals: Record<string, ApprovalItem> = {};
  for (const ev of DEMO_EVENTS) {
    if (ev.type === "approval.request") {
      const d = ev.data as { approval_id: string };
      approvals[d.approval_id] = { ...(ev.data as unknown as ApprovalItem), resolved: "approved" };
    }
  }

  return {
    viewMode: initialView,
    demo: true,
    connection: "idle",       // 演示模式没有真连接，顶部会显示 DEMO
    workspace: DEMO_WORKSPACE,
    model: DEMO_MODEL,
    tasks: { [DEMO_TASK_ID]: DEMO_TASK },
    events,
    approvals,
    activeTaskId: DEMO_TASK_ID,
  };
}

// --------------------------------------------------------------------------- //
// Actions
// --------------------------------------------------------------------------- //
export type TaskAction =
  | { type: "SET_VIEW"; view: ViewMode }
  | { type: "SET_CONNECTION"; connection: ConnectionState }
  | { type: "SET_WORKSPACE"; workspace: string }
  | { type: "SET_MODEL"; model: string }
  | { type: "UPSERT_TASK"; task: TaskSummary }
  | { type: "SET_ACTIVE_TASK"; taskId: string | null }
  /** 追加单条事件（Phase 3 的 SSE 入口）—— 按 seq 幂等去重 */
  | { type: "APPEND_EVENT"; event: EventEnvelope }
  /** 批量灌入（拉历史 / 初始化）—— 同样去重 */
  | { type: "APPEND_EVENTS"; taskId: string; events: EventEnvelope[] }
  | { type: "SET_APPROVAL_SUBMITTING"; approvalId: string; submitting: boolean }
  | { type: "RESOLVE_APPROVAL"; approvalId: string; status: "approved" | "rejected" | "expired" }
  | { type: "RESET_TO_DEMO" };

// --------------------------------------------------------------------------- //
// Reducer
// --------------------------------------------------------------------------- //
/** 按 seq 合并两组事件并去重（幂等） */
function mergeEvents(existing: EventEnvelope[], incoming: EventEnvelope[]): EventEnvelope[] {
  const seen = new Set(existing.map((e) => e.seq));
  const added = incoming.filter((e) => !seen.has(e.seq));
  if (!added.length) return existing;                  // 没有新事件 → 返回原引用，避免无谓重渲染
  return [...existing, ...added].sort((a, b) => a.seq - b.seq);
}

/** 从事件流里补齐任务摘要（status / endReason） */
function taskFromEvents(taskId: string, events: EventEnvelope[], prev?: TaskSummary): TaskSummary {
  const first = events[0];
  const last = events[events.length - 1];
  let status: TaskStatus = prev?.status ?? "running";
  let endReason = prev?.endReason ?? "";
  let goal = prev?.goal ?? "";

  for (const ev of events) {
    if (ev.type === "task.start") {
      const d = ev.data as { goal?: string };
      goal = d.goal ?? goal;
    } else if (ev.type === "approval.request") {
      status = "awaiting_approval";
    } else if (ev.type === "approval.decision") {
      status = "running";
    } else if (ev.type === "task.end") {
      const d = ev.data as { status?: TaskStatus; reason?: string };
      status = d.status ?? status;
      endReason = d.reason ?? endReason;
    }
  }

  return {
    id: taskId,
    goal,
    status,
    startedAt: first?.ts ?? prev?.startedAt ?? Date.now() / 1000,
    endedAt: status === "done" || status === "aborted" || status === "failed" ? last?.ts : undefined,
    endReason,
    turn: last?.turn ?? prev?.turn ?? 0,
  };
}

function reducer(state: TaskStateStore, action: TaskAction): TaskStateStore {
  switch (action.type) {
    case "SET_VIEW":
      // ★ 只改渲染模式。不碰 events / tasks / approvals。
      return { ...state, viewMode: action.view };

    case "SET_CONNECTION":
      return { ...state, connection: action.connection };

    case "SET_WORKSPACE":
      return { ...state, workspace: action.workspace };

    case "SET_MODEL":
      return { ...state, model: action.model };

    case "UPSERT_TASK":
      return { ...state, tasks: { ...state.tasks, [action.task.id]: action.task } };

    case "SET_ACTIVE_TASK":
      return { ...state, activeTaskId: action.taskId };

    case "APPEND_EVENT": {
      const ev = action.event;
      const tid = ev.task_id || state.activeTaskId || "unknown";
      const prev = state.events[tid] ?? [];
      const merged = mergeEvents(prev, [ev]);
      if (merged === prev) return state;               // 重复事件，直接忽略

      const next: TaskStateStore = {
        ...state,
        events: { ...state.events, [tid]: merged },
        activeTaskId: state.activeTaskId ?? tid,
        tasks: { ...state.tasks, [tid]: taskFromEvents(tid, merged, state.tasks[tid]) },
      };

      // 审批事件顺带更新前端审批表
      if (ev.type === "approval.request") {
        const d = ev.data as { approval_id?: string };
        if (d.approval_id) {
          next.approvals = {
            ...next.approvals,
            [d.approval_id]: { ...(ev.data as unknown as ApprovalItem), submitting: false },
          };
        }
      } else if (ev.type === "approval.decision" || ev.type === "approval.expired") {
        const d = ev.data as { approval_id?: string; status?: string };
        if (d.approval_id && next.approvals[d.approval_id]) {
          next.approvals = {
            ...next.approvals,
            [d.approval_id]: {
              ...next.approvals[d.approval_id],
              submitting: false,
              resolved: (d.status as "approved" | "rejected" | "expired") ?? undefined,
            },
          };
        }
      }

      return next;
    }

    case "APPEND_EVENTS": {
      const prev = state.events[action.taskId] ?? [];
      const merged = mergeEvents(prev, action.events);
      if (merged === prev) return state;
      return {
        ...state,
        events: { ...state.events, [action.taskId]: merged },
        tasks: { ...state.tasks, [action.taskId]: taskFromEvents(action.taskId, merged, state.tasks[action.taskId]) },
      };
    }

    case "SET_APPROVAL_SUBMITTING": {
      const item = state.approvals[action.approvalId];
      if (!item) return state;
      return {
        ...state,
        approvals: {
          ...state.approvals,
          [action.approvalId]: { ...item, submitting: action.submitting },
        },
      };
    }

    case "RESOLVE_APPROVAL": {
      const item = state.approvals[action.approvalId];
      if (!item) return state;
      return {
        ...state,
        approvals: {
          ...state.approvals,
          [action.approvalId]: { ...item, submitting: false, resolved: action.status },
        },
      };
    }

    case "RESET_TO_DEMO":
      return makeInitialState();

    default:
      return state;
  }
}

// --------------------------------------------------------------------------- //
// Context
// --------------------------------------------------------------------------- //
interface TaskContextValue {
  state: TaskStateStore;
  dispatch: Dispatch<TaskAction>;
}

const TaskContext = createContext<TaskContextValue | null>(null);

export function TaskProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, makeInitialState);
  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <TaskContext.Provider value={value}>{children}</TaskContext.Provider>;
}

export function useTaskStore(): TaskContextValue {
  const ctx = useContext(TaskContext);
  if (!ctx) throw new Error("useTaskStore 必须在 <TaskProvider> 内部使用");
  return ctx;
}

/** 取当前任务的事件（空数组兜底，避免各处写 `?? []`） */
export function useActiveEvents(): EventEnvelope[] {
  const { state } = useTaskStore();
  const tid = state.activeTaskId;
  return useMemo(() => (tid ? state.events[tid] ?? [] : []), [state.events, tid]);
}
