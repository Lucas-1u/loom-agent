import {
  createContext,
  useContext,
  useMemo,
  useReducer,
  type Dispatch,
  type ReactNode,
} from "react";
import type { EventEnvelope, TaskStatus } from "../../types/events";
import type { ApprovalItem, ConnectionState, ViewMode } from "../../types/chat";
import type { Message, Session, Task } from "./types";
import { workspaceKey } from "./workspace";
import { DEMO_MODE } from "../../services/config";
import { DEMO_SEED } from "../../services/mock";

/**
 * 统一状态中心
 *
 * ★ 结构上分四层，对应 Session / Task / Message / Event：
 *
 *     sessions: Record<session_id, Session>
 *     tasks:    Record<task_id, Task>            ← 每个 Task 带 session_id
 *     messages: Record<session_id, Message[]>    ← 按会话归属
 *     events:   Record<task_id, EventEnvelope[]> ← 按任务归属
 *
 *   为什么不放在一个数组里？因为两个索引维度不同：
 *     · 聊天视图按 session 查消息
 *     · 执行流按 task 查事件
 *   用 Record 做索引，查询是 O(1)，也不用每次 filter 全表。
 *
 * ★ 铁律：**task_id 与 session_id 绝不混用**。
 *   Task 一定带 `session_id` 字段；Session 从不直接持有事件。
 */

// --------------------------------------------------------------------------- //
// State
// --------------------------------------------------------------------------- //
export interface LoomStore {
  // ---- UI ----
  viewMode: ViewMode;
  demo: boolean;
  connection: ConnectionState;
  model: string;

  // ---- 数据 ----
  sessions: Record<string, Session>;
  tasks: Record<string, Task>;
  messages: Record<string, Message[]>;
  events: Record<string, EventEnvelope[]>;
  approvals: Record<string, ApprovalItem>;

  // ---- 选择 ----
  /** 当前会话（聊天视图展示它） */
  activeSessionId: string | null;
  /** 执行流当前聚焦的任务（必须属于 activeSessionId） */
  activeTaskId: string | null;

  // ---- 侧栏 UI 状态 ----
  /** 只记录「被折叠的」分组 —— 默认展开，不用记一大堆 true */
  collapsedGroups: Record<string, true>;
}

// --------------------------------------------------------------------------- //
// Actions
// --------------------------------------------------------------------------- //
export type LoomAction =
  | { type: "SET_VIEW"; view: ViewMode }
  | { type: "SET_CONNECTION"; connection: ConnectionState }
  | { type: "SET_MODEL"; model: string }
  | { type: "CREATE_SESSION"; session: Session }
  | { type: "SET_ACTIVE_SESSION"; sessionId: string }
  | { type: "SET_ACTIVE_TASK"; taskId: string | null }
  | { type: "TOGGLE_GROUP"; key: string }
  | { type: "SET_SESSION_WORKSPACE"; sessionId: string; path: string }
  | { type: "SET_SESSION_TITLE"; sessionId: string; title: string }
  /** 开始一次任务：登记 Task + 落一条 user Message */
  | { type: "START_TASK"; task: Task; userMessage: Message }
  | { type: "APPEND_EVENT"; event: EventEnvelope }
  | { type: "APPEND_MESSAGES"; sessionId: string; messages: Message[] }
  | { type: "SET_APPROVAL_SUBMITTING"; approvalId: string; submitting: boolean }
  | { type: "RESOLVE_APPROVAL"; approvalId: string; status: "approved" | "rejected" | "expired" }
  | { type: "RESET_TO_DEMO" };

// --------------------------------------------------------------------------- //
// 初始状态
// --------------------------------------------------------------------------- //
function emptyStore(): LoomStore {
  return {
    viewMode: "chat",
    demo: false,
    connection: "idle",
    model: "qwen3.7-plus",
    sessions: {},
    tasks: {},
    messages: {},
    events: {},
    approvals: {},
    activeSessionId: null,
    activeTaskId: null,
    collapsedGroups: {},
  };
}

function makeInitialState(): LoomStore {
  const q = new URLSearchParams(window.location.search);
  const view: ViewMode = q.get("view") === "execution" ? "execution" : "chat";

  if (!DEMO_MODE) return { ...emptyStore(), viewMode: view };

  const seeded = DEMO_SEED();
  return {
    ...emptyStore(),   // 先铺默认值（connection / model / 空的各类 Record）
    ...seeded,         // 再用种子数据覆盖
    viewMode: view,
    demo: true,
    // 演示模式下侧栏全部展开，方便一眼看到分组效果
    collapsedGroups: {},
  };
}

// --------------------------------------------------------------------------- //
// 内部工具
// --------------------------------------------------------------------------- //
/** 按 seq 合并去重（幂等：SSE 重连重复推送不会重复渲染） */
function mergeEvents(existing: EventEnvelope[], incoming: EventEnvelope[]): EventEnvelope[] {
  const seen = new Set(existing.map((e) => e.seq));
  const added = incoming.filter((e) => !seen.has(e.seq));
  if (!added.length) return existing; // 引用不变 → 避免无谓重渲染
  return [...existing, ...added].sort((a, b) => a.seq - b.seq);
}

/** 从事件流推断任务状态（不依赖前端计时器） */
function taskFromEvents(task: Task, events: EventEnvelope[]): Task {
  let status: TaskStatus = task.status;
  let endReason = task.end_reason ?? "";
  let result = task.result ?? "";
  let endedAt = task.ended_at;
  let turn = task.turn ?? 0;

  for (const ev of events) {
    if (ev.turn > turn) turn = ev.turn;

    if (ev.type === "approval.request") {
      status = "awaiting_approval";
    } else if (ev.type === "approval.decision") {
      status = "running";
    } else if (ev.type === "final_answer") {
      const d = ev.data as { text?: string };
      if (d.text) result = d.text;
    } else if (ev.type === "task.end") {
      const d = ev.data as { status?: TaskStatus; reason?: string; answer?: string };
      status = d.status ?? status;
      endReason = d.reason ?? endReason;
      if (d.answer) result = d.answer;
      endedAt = ev.ts;
    }
  }

  return { ...task, status, end_reason: endReason, result, ended_at: endedAt, turn };
}

/** 从事件派生一条 assistant 消息（final_answer 落一条就够，不逐条转） */
function assistantMessageFrom(ev: EventEnvelope, sessionId: string): Message | null {
  if (ev.type !== "final_answer") return null;
  const text = String((ev.data as { text?: string }).text ?? "").trim();
  if (!text) return null;
  return {
    message_id: `m_${ev.task_id}_final`,
    session_id: sessionId,
    task_id: ev.task_id,
    role: "assistant",
    content: text,
    created_at: ev.ts,
  };
}

/** 会话活动时间推进（用于侧栏排序与分组排序） */
function touchSession(sessions: Record<string, Session>, sessionId: string, ts: number) {
  const s = sessions[sessionId];
  if (!s || ts <= s.updated_at) return sessions;
  return { ...sessions, [sessionId]: { ...s, updated_at: ts } };
}

// --------------------------------------------------------------------------- //
// Reducer
// --------------------------------------------------------------------------- //
function reducer(state: LoomStore, action: LoomAction): LoomStore {
  switch (action.type) {
    case "SET_VIEW":
      // ★ 只改渲染模式。不碰 sessions / tasks / messages / events。
      return { ...state, viewMode: action.view };

    case "SET_CONNECTION":
      return { ...state, connection: action.connection };

    case "SET_MODEL":
      return { ...state, model: action.model };

    case "CREATE_SESSION": {
      const s = action.session;
      const key = workspaceKey(s.workspace_path);
      return {
        ...state,
        sessions: { ...state.sessions, [s.session_id]: s },
        messages: { ...state.messages, [s.session_id]: [] },
        activeSessionId: s.session_id,
        activeTaskId: null,
        // 新会话所在的组要自动展开（任务书 §3.3）
        collapsedGroups: { ...state.collapsedGroups, [key]: undefined as never },
      };
    }

    case "SET_ACTIVE_SESSION": {
      const target = state.sessions[action.sessionId];
      if (!target) return state;

      // 切会话时，执行流默认看该会话的最后一个任务
      const ownTasks = Object.values(state.tasks)
        .filter((t) => t.session_id === action.sessionId)
        .sort((a, b) => a.started_at - b.started_at);
      const lastTaskId = ownTasks.length ? ownTasks[ownTasks.length - 1].task_id : null;

      return {
        ...state,
        activeSessionId: action.sessionId,
        activeTaskId: lastTaskId,
      };
    }

    case "SET_ACTIVE_TASK": {
      // 只允许切到当前会话自己的任务
      if (action.taskId) {
        const t = state.tasks[action.taskId];
        if (!t || t.session_id !== state.activeSessionId) return state;
      }
      return { ...state, activeTaskId: action.taskId };
    }

    case "TOGGLE_GROUP": {
      const next = { ...state.collapsedGroups };
      if (next[action.key]) delete next[action.key];
      else next[action.key] = true;
      return { ...state, collapsedGroups: next };
    }

    case "SET_SESSION_WORKSPACE": {
      const s = state.sessions[action.sessionId];
      if (!s) return state;
      return {
        ...state,
        sessions: {
          ...state.sessions,
          [action.sessionId]: {
            ...s,
            workspace_path: action.path,
            updated_at: Date.now() / 1000,
          },
        },
      };
    }

    case "SET_SESSION_TITLE": {
      const s = state.sessions[action.sessionId];
      if (!s) return state;
      return {
        ...state,
        sessions: { ...state.sessions, [action.sessionId]: { ...s, title: action.title } },
      };
    }

    case "START_TASK": {
      const { task, userMessage } = action;
      const prevMsgs = state.messages[task.session_id] ?? [];
      return {
        ...state,
        tasks: { ...state.tasks, [task.task_id]: task },
        messages: {
          ...state.messages,
          [task.session_id]: [...prevMsgs, userMessage],
        },
        events: { ...state.events, [task.task_id]: [] },
        sessions: touchSession(state.sessions, task.session_id, userMessage.created_at),
        activeSessionId: task.session_id,
        activeTaskId: task.task_id,
      };
    }

    case "APPEND_EVENT": {
      const ev = action.event;
      const task = state.tasks[ev.task_id];

      // 事件必须能归属到一个已知 Task —— 否则丢弃（可能是上一轮遗留的流）
      if (!task) return state;

      const prev = state.events[ev.task_id] ?? [];
      const merged = mergeEvents(prev, [ev]);
      if (merged === prev) return state; // 重复事件

      const next: LoomStore = {
        ...state,
        events: { ...state.events, [ev.task_id]: merged },
        tasks: { ...state.tasks, [ev.task_id]: taskFromEvents(task, merged) },
        sessions: touchSession(state.sessions, task.session_id, ev.ts),
      };

      // final_answer → 落一条 assistant 消息
      const msg = assistantMessageFrom(ev, task.session_id);
      if (msg) {
        const list = next.messages[task.session_id] ?? [];
        if (!list.some((m) => m.message_id === msg.message_id)) {
          next.messages = { ...next.messages, [task.session_id]: [...list, msg] };
        }
      }

      // 审批事件 → 维护审批表（★ 必须带 session/task 关联，否则切会话会串台）
      if (ev.type === "approval.request") {
        const d = ev.data as Record<string, unknown>;
        const id = String(d.approval_id ?? "");
        if (id) {
          next.approvals = {
            ...next.approvals,
            [id]: {
              ...(d as unknown as ApprovalItem),
              session_id: task.session_id,
              task_id: ev.task_id,
              submitting: false,
            },
          };
        }
      } else if (ev.type === "approval.decision" || ev.type === "approval.expired") {
        const d = ev.data as Record<string, unknown>;
        const id = String(d.approval_id ?? "");
        if (id && next.approvals[id]) {
          next.approvals = {
            ...next.approvals,
            [id]: {
              ...next.approvals[id],
              submitting: false,
              resolved: (d.status as "approved" | "rejected" | "expired") ?? undefined,
            },
          };
        }
      }

      return next;
    }

    case "APPEND_MESSAGES": {
      const prev = state.messages[action.sessionId] ?? [];
      const seen = new Set(prev.map((m) => m.message_id));
      const added = action.messages.filter((m) => !seen.has(m.message_id));
      if (!added.length) return state;
      return {
        ...state,
        messages: {
          ...state.messages,
          [action.sessionId]: [...prev, ...added].sort((a, b) => a.created_at - b.created_at),
        },
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
interface LoomContextValue {
  state: LoomStore;
  dispatch: Dispatch<LoomAction>;
}

const LoomContext = createContext<LoomContextValue | null>(null);

export function LoomProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, makeInitialState);
  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <LoomContext.Provider value={value}>{children}</LoomContext.Provider>;
}

export function useLoom(): LoomContextValue {
  const ctx = useContext(LoomContext);
  if (!ctx) throw new Error("useLoom 必须在 <LoomProvider> 内部使用");
  return ctx;
}

/** 当前会话对象 */
export function useActiveSession(): Session | null {
  const { state } = useLoom();
  return state.activeSessionId ? state.sessions[state.activeSessionId] ?? null : null;
}

/** 当前会话的消息（正序） */
export function useActiveMessages(): Message[] {
  const { state } = useLoom();
  const id = state.activeSessionId;
  if (!id) return [];
  return useMemo(
    () => [...(state.messages[id] ?? [])].sort((a, b) => a.created_at - b.created_at),
    [state.messages, id],
  );
}

/** 当前执行流聚焦的任务 */
export function useActiveTask(): Task | null {
  const { state } = useLoom();
  return state.activeTaskId ? state.tasks[state.activeTaskId] ?? null : null;
}

/** 当前任务的事件（正序） */
export function useActiveEvents(): EventEnvelope[] {
  const { state } = useLoom();
  const id = state.activeTaskId;
  return useMemo(
    () => (id ? [...(state.events[id] ?? [])].sort((a, b) => a.seq - b.seq) : []),
    [state.events, id],
  );
}

/** 当前会话的任务列表（正序） */
export function useActiveTasks(): Task[] {
  const { state } = useLoom();
  const sid = state.activeSessionId;
  return useMemo(() => {
    if (!sid) return [];
    return Object.values(state.tasks)
      .filter((t) => t.session_id === sid)
      .sort((a, b) => a.started_at - b.started_at);
  }, [state.tasks, sid]);
}
