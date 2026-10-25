import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppHeader } from "./components/AppHeader";
import { ChatView } from "./components/ChatView";
import { ExecutionFlowView } from "./components/ExecutionFlowView";
import { SessionSidebar } from "./components/SessionSidebar";
import { TaskComposer } from "./components/TaskComposer";
import { WorkspaceSelector } from "./components/WorkspaceSelector";
import { BackendBanner } from "./components/BackendBanner";
import {
  useActiveEvents,
  useActiveMessages,
  useActiveSession,
  useActiveTask,
  useActiveTasks,
  useLoom,
} from "./features/session/store";
import { disambiguateGroupLabels, groupSessionsByWorkspace } from "./features/session/selectors";
import { deriveRunHint } from "./features/events/runHint";
import type { Session } from "./features/session/types";
import * as api from "./services/api";
import { DEMO_ALLOWED } from "./services/config";
import { openEventStream, type GapInfo, type StreamEvent } from "./services/eventStream";

/**
 * 应用外壳
 *
 * ★ 启动顺序（真实后端优先）
 *   1. mode="checking" → 打 /api/health
 *   2. 通 → mode="real"：拉会话列表 → 选中第一个 → 拉消息与任务 → 连 SSE
 *   3. 不通 → mode="offline"：显示「后端未启动」+ 重试 + 查看演示数据
 *
 *   **绝不自动进演示模式。** 演示是用户点的，而且和真实数据完全隔离。
 */
export default function App() {
  const { state, dispatch } = useLoom();

  const session = useActiveSession();
  const messages = useActiveMessages();
  const task = useActiveTask();
  const events = useActiveEvents();
  const tasks = useActiveTasks();

  const [banner, setBanner] = useState<string>("");

  // ------------------------------------------------------------------ //
  // 派生
  // ------------------------------------------------------------------ //
  const groups = useMemo(
    () => disambiguateGroupLabels(groupSessionsByWorkspace(Object.values(state.sessions))),
    [state.sessions],
  );

  const runHint = useMemo(
    () => deriveRunHint(events, task?.status ?? "created"),
    [events, task],
  );

  const pendingApprovals = useMemo(
    () =>
      Object.values(state.approvals).filter(
        (a) => a.session_id === state.activeSessionId && !a.resolved,
      ),
    [state.approvals, state.activeSessionId],
  );

  const runningSessionIds = useMemo(() => {
    const set = new Set<string>();
    for (const t of Object.values(state.tasks)) {
      if (t.status === "running" || t.status === "awaiting_approval") set.add(t.session_id);
    }
    return set;
  }, [state.tasks]);

  const busy = task?.status === "running" || task?.status === "awaiting_approval";
  /** 该会话已经有任务 → 工作目录锁定（历史归属不可改） */
  const workspaceLocked = state.mode === "real" && tasks.length > 0;

  // ------------------------------------------------------------------ //
  // ① 启动：健康检查 + 加载会话
  // ------------------------------------------------------------------ //
  const boot = useCallback(async () => {
    dispatch({ type: "SET_MODE", mode: "checking" });
    dispatch({ type: "SET_CONNECTION", connection: "connecting" });
    setBanner("");

    try {
      const h = await api.health();
      dispatch({ type: "SET_MODE", mode: "real" });
      dispatch({ type: "SET_CONNECTION", connection: "connected" });
      if (h.model) dispatch({ type: "SET_MODEL", model: h.model });

      const sessions = await api.listSessions();
      dispatch({ type: "HYDRATE_SESSIONS", sessions });

      if (sessions.length > 0) {
        dispatch({ type: "SET_ACTIVE_SESSION", sessionId: sessions[0].session_id });
      }
    } catch (exc) {
      dispatch({ type: "SET_MODE", mode: "offline" });
      dispatch({ type: "SET_CONNECTION", connection: "error" });
      setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
    }
  }, [dispatch]);

  // 只跑一次（演示模式下完全不碰后端）
  useEffect(() => {
    if (state.mode === "demo") return;
    void boot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ------------------------------------------------------------------ //
  // ② SSE 订阅（只依赖 mode —— 不依赖 lastEventId，否则会疯狂重连）
  // ------------------------------------------------------------------ //
  const cursorRef = useRef(0);
  cursorRef.current = state.lastEventId;

  useEffect(() => {
    if (state.mode !== "real") return;
    // 调试开关：?nostream=1 不建 SSE（用于离线截图 / 排查事件流问题）
    if (new URLSearchParams(window.location.search).get("nostream") === "1") return;

    const close = openEventStream({
      since: cursorRef.current > 0 ? cursorRef.current : null,
      handlers: {
        onEvent: (ev: StreamEvent) => {
          dispatch({ type: "APPEND_STREAM_EVENT", sseId: ev.id, event: ev.event });
        },
        onGap: (info: GapInfo) => {
          dispatch({ type: "MARK_GAP", gap: info });
          if (info.reason === "evicted" || info.reason === "slow_client") {
            setBanner(
              "事件流出现缺口（服务重启或缓冲淘汰）。补不回来的部分会在执行流里明确标注，" +
                "任务结论仍以快照为准。",
            );
          }
        },
        onState: (s) => dispatch({ type: "SET_STREAM_STATE", state: s }),
      },
    });

    return close;
  }, [state.mode, dispatch]);

  // ------------------------------------------------------------------ //
  // ③ 切换会话：拉该会话的消息和任务
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (state.mode !== "real") return;
    const sid = state.activeSessionId;
    if (!sid) return;

    let cancelled = false;
    void (async () => {
      try {
        const [msgs, raw] = await Promise.all([api.sessionMessages(sid), api.listTasks(sid)]);
        if (cancelled) return;
        dispatch({ type: "APPEND_MESSAGES", sessionId: sid, messages: msgs });
        dispatch({ type: "UPSERT_TASKS", tasks: raw.map(api.toTask) });

        // ★ 任务加载完之后才能选任务 —— SET_ACTIVE_SESSION 是在 listTasks 之前
        //   dispatch 的，那时 tasks 还是空的，activeTaskId 会停在 null。
        //   这里补一次：没选中就选该会话最近的一个（后端按 created_at 升序返回）。
        if (raw.length > 0) {
          dispatch({ type: "SET_ACTIVE_TASK", taskId: raw[raw.length - 1].task_id });
        }
      } catch (exc) {
        if (cancelled) return;
        setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.activeSessionId, state.mode, dispatch]);

  // ------------------------------------------------------------------ //
  // ③b 选中任务时补齐历史事件（刷新页面后执行流不能是空的）
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (state.mode !== "real") return;
    const tid = state.activeTaskId;
    if (!tid) return;
    // 已经有事件了就不重复拉（实时流已经在往里塞）
    if ((state.events[tid]?.length ?? 0) > 0) return;

    let cancelled = false;
    void (async () => {
      try {
        const r = await api.taskEvents(tid);
        if (cancelled) return;

        if (!r.available) {
          // ★ 明确告知：事件历史补不回来。不能假装完整。
          setBanner(
            `任务 ${tid} 的事件历史不可用（${r.reason}）。${
              r.note ?? "服务重启或缓冲淘汰后事件不保留，但任务结论仍在。"
            }`,
          );
          return;
        }

        // 按全局 id 升序灌入，走和 SSE 相同的去重入口
        for (const rec of r.events) {
          dispatch({
            type: "APPEND_STREAM_EVENT",
            sseId: rec.id,
            event: rec.event,
          });
        }
      } catch (exc) {
        if (cancelled) return;
        setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.activeTaskId, state.mode, state.events, dispatch]);

  // ------------------------------------------------------------------ //
  // ④ 操作
  // ------------------------------------------------------------------ //
  const handleViewChange = useCallback(
    (view: "chat" | "execution") => dispatch({ type: "SET_VIEW", view }),
    [dispatch],
  );

  const handleNewSession = useCallback(async () => {
    if (state.mode !== "real") {
      const now = Date.now() / 1000;
      dispatch({
        type: "CREATE_SESSION",
        session: {
          session_id: `demo_${Date.now().toString(36)}`,
          title: "演示会话",
          workspace_path: session?.workspace_path ?? "",
          created_at: now,
          updated_at: now,
        },
      });
      return;
    }
    try {
      const s = await api.createSession({
        title: "新建会话",
        workspace: session?.workspace_path ?? "",
      });
      dispatch({ type: "HYDRATE_SESSIONS", sessions: [...Object.values(state.sessions), s] });
      dispatch({ type: "SET_ACTIVE_SESSION", sessionId: s.session_id });
    } catch (exc) {
      setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
    }
  }, [dispatch, session, state.mode, state.sessions]);

  const handleSelectSession = useCallback(
    (s: Session) => dispatch({ type: "SET_ACTIVE_SESSION", sessionId: s.session_id }),
    [dispatch],
  );

  const handleToggleGroup = useCallback(
    (key: string) => dispatch({ type: "TOGGLE_GROUP", key }),
    [dispatch],
  );

  /**
   * 改工作区。
   *
   * ★ 会话的工作目录是**历史事实**：一旦这个会话跑过任务，就不允许再改。
   *   旧实现只改本地字段（后端不知道），于是同一个会话里混进了两个目录的任务 ——
   *   用户看到的就是"LLMstudy 的会话里是 Try_agent 的内容"。
   *   现在：空会话可以改（写回后端）；已有任务就拦下，提示新建会话。
   */
  const handleWorkspaceChange = useCallback(
    (path: string) => {
      if (!session || busy) return;

      if (state.mode !== "real") {
        dispatch({ type: "SET_SESSION_WORKSPACE", sessionId: session.session_id, path });
        return;
      }

      if (tasks.length > 0) {
        setBanner(
          `该会话已绑定工作目录 ${session.workspace_path || "（未设置）"}，不能再改。` +
            "要换目录请点侧栏「新建会话」。",
        );
        return;
      }

      void (async () => {
        try {
          const s = await api.updateSession(session.session_id, { workspace: path });
          dispatch({
            type: "SET_SESSION_WORKSPACE",
            sessionId: s.session_id,
            path: s.workspace_path,
          });
          setBanner("");
        } catch (exc) {
          setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
        }
      })();
    },
    [dispatch, session, busy, state.mode, tasks.length],
  );

  const handleSubmit = useCallback(
    async (goal: string) => {
      if (!session) return;
      setBanner("");

      // ---- 演示模式：不调后端，明确标注 ----
      if (state.mode === "demo") {
        const now = Date.now() / 1000;
        const taskId = `demo_task_${Date.now().toString(36)}`;
        dispatch({
          type: "START_TASK",
          task: {
            task_id: taskId,
            session_id: session.session_id,
            goal,
            status: "done",
            started_at: now,
            ended_at: now,
            end_reason: "demo_mode",
            result: "（演示模式）没有真正执行。真实模式下这里会显示 Agent 的答复。",
          },
          userMessage: {
            message_id: `m_${taskId}_user`,
            session_id: session.session_id,
            task_id: taskId,
            role: "user",
            content: goal,
            created_at: now,
          },
        });
        return;
      }

      if (state.mode !== "real") {
        setBanner("后端未连接，无法提交任务。点上面的「重试」或先启动后端。");
        return;
      }

      // ★ 幂等键：一次提交只生成一个，网络重试必须复用它
      const idemKey = crypto.randomUUID();

      try {
        const snap = await api.createTask(
          { goal, session_id: session.session_id, workspace: session.workspace_path },
          idemKey,
        );

        const now = Date.now() / 1000;
        const t = api.toTask(snap);
        // ★ createTask 成功只代表"任务被受理"，不代表执行成功。
        //   这里只登记任务和用户消息，最终状态由事件/快照决定。
        dispatch({
          type: "START_TASK",
          task: t,
          userMessage: {
            message_id: `m_${t.task_id}_user`,
            session_id: session.session_id,
            task_id: t.task_id,
            role: "user",
            content: goal,
            created_at: now,
          },
        });
      } catch (exc) {
        const msg = exc instanceof api.ApiError ? exc.note : String(exc);
        setBanner(`提交失败：${msg}`);
      }
    },
    [dispatch, session, state.mode],
  );

  const handleStop = useCallback(async () => {
    if (!task) return;
    if (state.mode !== "real") {
      setBanner("演示模式无法取消任务。");
      return;
    }
    try {
      const r = await api.cancelTask(task.task_id);
      // ★ accepted 只代表请求被接受，**不要**在这里把状态改成 aborted
      setBanner(r.note || (r.accepted ? "取消请求已接受，等待任务停下…" : "取消请求未被接受"));
    } catch (exc) {
      setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
    }
  }, [task, state.mode]);

  const handleApprovalResolve = useCallback(
    async (approvalId: string, approved: boolean) => {
      const item = state.approvals[approvalId];
      if (!item || !item.task_id) {
        setBanner("找不到这条审批的归属任务，已忽略。");
        return;
      }
      if (state.mode !== "real") {
        setBanner("演示模式的审批按钮不会真的生效。");
        return;
      }
      dispatch({ type: "SET_APPROVAL_SUBMITTING", approvalId, submitting: true });
      try {
        const r = await api.decideApproval(approvalId, {
          task_id: item.task_id,
          decision: approved ? "approved" : "rejected",
        });
        if (!r.accepted) setBanner(`审批未生效：${r.note}`);
        // 成功时不手动改状态 —— 等后端的 approval.decision 事件回来
      } catch (exc) {
        setBanner(exc instanceof api.ApiError ? exc.note : String(exc));
        dispatch({ type: "SET_APPROVAL_SUBMITTING", approvalId, submitting: false });
      }
    },
    [dispatch, state.approvals, state.mode],
  );

  const handleGoToExecution = useCallback(
    () => dispatch({ type: "SET_VIEW", view: "execution" }),
    [dispatch],
  );

  // ------------------------------------------------------------------ //
  // 渲染
  // ------------------------------------------------------------------ //
  const showBanner = state.mode === "offline" || banner !== "";

  return (
    <div className="flex h-full flex-col overflow-hidden bg-ink-950">
      <AppHeader
        viewMode={state.viewMode}
        onViewChange={handleViewChange}
        model={state.model || (state.mode === "real" ? "—" : "演示")}
        onModelChange={(m) => dispatch({ type: "SET_MODEL", model: m })}
        connection={state.connection}
        demo={state.mode === "demo"}
      />

      {showBanner && (
        <BackendBanner
          mode={state.mode}
          streamState={state.streamState}
          message={state.mode === "offline" ? "后端未启动" : ""}
          detail={banner}
          gaps={state.gaps.length}
          demoAllowed={DEMO_ALLOWED}
          onRetry={() => void boot()}
          onEnterDemo={() => dispatch({ type: "ENTER_DEMO" })}
          onExitDemo={() => {
            dispatch({ type: "EXIT_DEMO" });
            void boot();
          }}
          onDismiss={() => setBanner("")}
        />
      )}

      <div className="flex min-h-0 flex-1">
        <SessionSidebar
          groups={groups}
          activeSessionId={state.activeSessionId}
          collapsedGroups={state.collapsedGroups}
          onToggleGroup={handleToggleGroup}
          onSelectSession={handleSelectSession}
          onNewSession={() => void handleNewSession()}
          runningSessionIds={runningSessionIds}
          busy={false}
        />

        <main className="flex min-w-0 flex-1 flex-col bg-ink-950">
          {state.viewMode === "chat" ? (
            <ChatView
              messages={messages}
              runHint={runHint}
              pendingApprovals={pendingApprovals}
              onGoToExecution={handleGoToExecution}
            />
          ) : (
            <ExecutionFlowView
              events={events}
              tasks={tasks}
              activeTaskId={state.activeTaskId}
              sessionWorkspace={session?.workspace_path ?? ""}
              onSelectTask={(id) => dispatch({ type: "SET_ACTIVE_TASK", taskId: id })}
              onApprovalResolve={(id, ok) => void handleApprovalResolve(id, ok)}
            />
          )}
        </main>
      </div>

      <div className="shrink-0 border-t border-ink-700 bg-ink-900">
        <div className="mx-auto w-full max-w-[1040px] px-4 pt-2.5">
          <WorkspaceSelector
            workspace={session?.workspace_path ?? ""}
            onChange={handleWorkspaceChange}
            disabled={busy || !session || workspaceLocked}
            disabledReason={
              !session
                ? "先选择一个会话"
                : busy
                  ? "任务运行中不能改工作区，否则 Agent 的目录会中途变化"
                  : "该会话已有任务，工作目录已固定；换目录请新建会话"
            }
          />
        </div>

        <TaskComposer
          onSubmit={(goal) => void handleSubmit(goal)}
          onStop={() => void handleStop()}
          running={busy}
          disabled={!session}
          hint={
            state.mode === "demo"
              ? "演示模式 · 不会调用 Agent"
              : state.mode === "real"
                ? undefined
                : "未连接后端"
          }
        />
      </div>
    </div>
  );
}
