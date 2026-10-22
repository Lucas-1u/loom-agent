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
import { DEMO_ALLOWED } from "../../services/config";
import { DEMO_SEED } from "../../services/mock";
import type { GapInfo, StreamState } from "../../services/eventStream";

/**
 * 应用运行模式
 *
 * ★ 真实后端优先，**绝不自动 fallback 到 mock**：
 *     checking  → 正在打 /api/health
 *     real      → 连上了，一切数据来自后端
 *     offline   → 连不上，显示「后端未启动」+ 重试 + 「查看演示数据」
 *     demo      → 用户**手动**点进来的，数据来自 mock，与真实数据完全隔离
 *
 *   为什么隔离？
 *     如果把演示数据写进同一个 store，用户会看到"假会话混在真会话里"，
 *     而且一旦切回真实模式，那些假数据会试图落到后端。
 */
export type AppMode = "checking" | "real" | "offline" | "demo";

/** 已终结的任务状态。到达之后不该被增量事件改回运行态。 */
const TERMINAL_STATUSES = new Set(["done", "aborted", "failed"]);

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
  /** 运行模式：真实后端优先，演示需手动进入 */
  mode: AppMode;
  /** SSE 连接状态（真实模式下才有意义） */
  streamState: StreamState;
  /** 与后端的粗粒度连接状态（health 的结果） */
  connection: ConnectionState;
  model: string;

  /** 全局 SSE id 游标：去重基准 + 重连补发的 since */
  lastEventId: number;
  /** 收到的缺口通知（stream.gap），UI 据此提示"事件不完整" */
  gaps: GapInfo[];

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
  | { type: "SET_MODE"; mode: AppMode }
  | { type: "SET_STREAM_STATE"; state: StreamState }
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
  /** ★ 带全局 id 的事件写入（SSE 用）。id 已见过则整体丢弃。 */
  | { type: "APPEND_STREAM_EVENT"; sseId: number; event: EventEnvelope }
  | { type: "APPEND_MESSAGES"; sessionId: string; messages: Message[] }
  /** 用后端列表**合并**任务（不删本地新增的，终态不被降级） */
  | { type: "UPSERT_TASKS"; tasks: Task[] }
  /**
   * 改任务状态。
   * `authoritative=true` 表示这是 `getTask()` 拉回来的权威状态 ——
   * 允许它纠正本地误判；否则（增量事件）不得把终态改回运行态。
   */
  | { type: "SET_TASK_STATUS"; taskId: string; status: TaskStatus; authoritative?: boolean }
  /** 用后端 Session 列表覆盖本地（真实来源是后端） */
  | { type: "HYDRATE_SESSIONS"; sessions: Session[] }
  | { type: "MARK_GAP"; gap: GapInfo }
  | { type: "CLEAR_GAPS" }
  | { type: "SET_APPROVAL_SUBMITTING"; approvalId: string; submitting: boolean }
  | { type: "RESOLVE_APPROVAL"; approvalId: string; status: "approved" | "rejected" | "expired" }
  | { type: "ENTER_DEMO" }
  | { type: "EXIT_DEMO" };

// --------------------------------------------------------------------------- //
// 初始状态
// --------------------------------------------------------------------------- //
function emptyStore(): LoomStore {
  return {
    viewMode: "chat",
    mode: "checking",
    streamState: "closed",
    connection: "idle",
    model: "",
    lastEventId: 0,
    gaps: [],
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

  // ★ 默认永远是"检查中"的空状态 —— 要不要进演示模式由用户点，不由代码决定。
  //   `?demo=1` 留给截图/离线预览用（仍算"手动"）。
  if (q.get("demo") === "1" && DEMO_ALLOWED) {
    return { ...emptyStore(), ...DEMO_SEED(), viewMode: view, mode: "demo", collapsedGroups: {} };
  }

  return { ...emptyStore(), viewMode: view };
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

    case "SET_MODE":
      return { ...state, mode: action.mode };

    case "SET_STREAM_STATE":
      return { ...state, streamState: action.state };

    case "MARK_GAP":
      // 缺口要留着让用户看见，不能悄悄吞掉
      return { ...state, gaps: [...state.gaps, action.gap].slice(-20) };

    case "CLEAR_GAPS":
      return { ...state, gaps: [] };

    case "ENTER_DEMO": {
      // ★ 演示模式与真实数据隔离：进去时**整个换掉**，不是叠加
      const seeded = DEMO_SEED();
      return { ...emptyStore(), ...seeded, viewMode: state.viewMode, mode: "demo" };
    }

    case "EXIT_DEMO":
      // 退出时同样整个清空 —— 演示数据绝不带进真实模式
      return { ...emptyStore(), viewMode: state.viewMode, mode: "checking" };

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

    case "APPEND_STREAM_EVENT": {
      /**
       * SSE 专用入口：先用**全局 id** 挡掉重复投递。
       *
       * 为什么不能只靠 (task_id, seq)？
       *   两者都要用，但职责不同：
       *     全局 id  → 断线重连补发时可能整段重来，用它快速挡掉
       *     (task, seq) → 单任务内部的事件缺口检测（seq 是连续编号）
       *
       * 这里先做全局 id 判断，再交给 APPEND_EVENT 做 (task, seq) 去重。
       */
      if (action.sseId > 0 && action.sseId <= state.lastEventId) {
        return state;                       // 重复投递
      }
      const next = reducer(state, { type: "APPEND_EVENT", event: action.event });
      if (next === state) {
        // 事件被 (task, seq) 判重了，但游标仍要前进，否则下次重连还会补发它
        return action.sseId > state.lastEventId
          ? { ...state, lastEventId: action.sseId }
          : state;
      }
      return action.sseId > next.lastEventId
        ? { ...next, lastEventId: action.sseId }
        : next;
    }

    case "UPSERT_TASKS": {
      /**
       * 合并后端任务列表。
       *
       * ★ 不能直接覆盖：本地可能有刚提交、后端列表还没收录的任务；
       *   也不能让列表里的旧状态把本地终态降级（列表可能略滞后）。
       */
      const tasks = { ...state.tasks };
      for (const incoming of action.tasks) {
        const prev = tasks[incoming.task_id];
        if (!prev) {
          tasks[incoming.task_id] = incoming;
          continue;
        }
        const prevTerminal = TERMINAL_STATUSES.has(prev.status);
        const nextTerminal = TERMINAL_STATUSES.has(incoming.status);
        // 本地已终态、列表还不是 → 保留本地（列表滞后）
        if (prevTerminal && !nextTerminal) continue;
        tasks[incoming.task_id] = { ...prev, ...incoming };
      }
      return { ...state, tasks };
    }

    case "SET_TASK_STATUS": {
      const prev = state.tasks[action.taskId];
      if (!prev) return state;

      // ★ 终态保护：增量事件不得把已终态改回运行态；
      //   但 authoritative（getTask）允许纠正 —— 例如本地误判成 done，
      //   而服务端说是 failed，应该听服务端的。
      if (!action.authoritative) {
        if (TERMINAL_STATUSES.has(prev.status) && !TERMINAL_STATUSES.has(action.status)) {
          return state;
        }
      }
      if (prev.status === action.status) return state;
      return {
        ...state,
        tasks: { ...state.tasks, [action.taskId]: { ...prev, status: action.status } },
      };
    }

    case "HYDRATE_SESSIONS": {
      /**
       * 后端是 Session 的权威来源。这里**覆盖**本地会话表，
       * 但保留本地选中的 activeSessionId（如果它还在）。
       */
      const sessions: Record<string, Session> = {};
      const messages = { ...state.messages };
      for (const s of action.sessions) {
        sessions[s.session_id] = s;
        if (!messages[s.session_id]) messages[s.session_id] = [];
      }
      const stillThere =
        state.activeSessionId !== null && sessions[state.activeSessionId] !== undefined;
      return {
        ...state,
        sessions,
        messages,
        activeSessionId: stillThere ? state.activeSessionId : null,
        activeTaskId: stillThere ? state.activeTaskId : null,
      };
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
  // ★ 必须无条件调用 useMemo：早退 `if (!id) return []` 会让 Hook 数量随状态变化，
  //   React 会报 "change in the order of Hooks"。空值判断放在回调内部。
  return useMemo(() => {
    if (!id) return [];
    return [...(state.messages[id] ?? [])].sort((a, b) => a.created_at - b.created_at);
  }, [state.messages, id]);
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
  return useMemo(() => {
    if (!id) return [];
    return [...(state.events[id] ?? [])].sort((a, b) => a.seq - b.seq);
  }, [state.events, id]);
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
