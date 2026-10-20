import { useCallback, useMemo } from "react";
import { AppHeader } from "./components/AppHeader";
import { ChatView } from "./components/ChatView";
import { ExecutionFlowView } from "./components/ExecutionFlowView";
import { SessionSidebar } from "./components/SessionSidebar";
import { TaskComposer } from "./components/TaskComposer";
import { WorkspaceSelector } from "./components/WorkspaceSelector";
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
import type { Session, Task, Message } from "./features/session/types";
import { DEMO_MODE } from "./services/config";
import type { EventEnvelope } from "./types/events";

/**
 * 应用外壳
 *
 * ★ 只做三件事：布局 / 从 store 派生视图数据 / 把用户操作翻译成 dispatch。
 *   具体渲染全在子组件里 —— App 不写任何事件渲染逻辑。
 */
export default function App() {
  const { state, dispatch } = useLoom();

  // ---- 当前上下文 ----
  const session = useActiveSession();
  const messages = useActiveMessages();
  const task = useActiveTask();
  const events = useActiveEvents();
  const tasks = useActiveTasks();

  // ---- 派生 ----
  const groups = useMemo(
    () => disambiguateGroupLabels(groupSessionsByWorkspace(Object.values(state.sessions))),
    [state.sessions],
  );

  const runHint = useMemo(
    () => deriveRunHint(events, task?.status ?? "created"),
    [events, task],
  );

  /**
   * ★ 待审批只取**当前会话**的。
   *   审批条目在 store 里带 `session_id` / `task_id`（reducer 从 event.task_id 反查补上），
   *   所以这里过滤可靠 —— 不会出现"切到另一个会话，却看到上个会话的审批"。
   */
  const pendingApprovals = useMemo(
    () =>
      Object.values(state.approvals).filter(
        (a) => a.session_id === state.activeSessionId && !a.resolved,
      ),
    [state.approvals, state.activeSessionId],
  );

  /** 侧栏小圆点：哪些会话有任务在跑 */
  const runningSessionIds = useMemo(() => {
    const set = new Set<string>();
    for (const t of Object.values(state.tasks)) {
      if (t.status === "running" || t.status === "awaiting_approval") set.add(t.session_id);
    }
    return set;
  }, [state.tasks]);

  const busy = task?.status === "running" || task?.status === "awaiting_approval";

  // ---- 操作 ----
  const handleViewChange = useCallback(
    (view: "chat" | "execution") => dispatch({ type: "SET_VIEW", view }),
    [dispatch],
  );

  /** 新建会话：继承最近的工作目录，但保存为**自己的一份**配置 */
  const handleNewSession = useCallback(() => {
    const now = Date.now() / 1000;
    const s: Session = {
      session_id: `s_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
      title: "新建会话",
      workspace_path: session?.workspace_path ?? "",
      created_at: now,
      updated_at: now,
    };
    dispatch({ type: "CREATE_SESSION", session: s });
  }, [dispatch, session]);

  const handleSelectSession = useCallback(
    (s: Session) => dispatch({ type: "SET_ACTIVE_SESSION", sessionId: s.session_id }),
    [dispatch],
  );

  const handleToggleGroup = useCallback(
    (key: string) => dispatch({ type: "TOGGLE_GROUP", key }),
    [dispatch],
  );

  /** 改工作区：只动当前会话 */
  const handleWorkspaceChange = useCallback(
    (path: string) => {
      if (!session || busy) return;
      dispatch({ type: "SET_SESSION_WORKSPACE", sessionId: session.session_id, path });
    },
    [dispatch, session, busy],
  );

  /**
   * 提交任务。
   *
   * Phase 2（演示模式）：创建 Task 与 user Message，插两条说明事件后**立即正常结束** ——
   *   既不伪造一整套假执行过程冒充真实运行，也不让任务永远停在 "running"。
   * Phase 3：把 demo 那一段换成 `await api.createTask(...)` + SSE，其余逻辑不变。
   */
  const handleSubmit = useCallback(
    (goal: string) => {
      if (!session) return;

      const now = Date.now() / 1000;
      const taskId = `task_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

      const newTask: Task = {
        task_id: taskId,
        session_id: session.session_id, // ★ 明确归属，不靠隐式"当前会话"
        goal,
        status: DEMO_MODE ? "done" : "running",
        started_at: now,
        ended_at: DEMO_MODE ? now : undefined,
        end_reason: DEMO_MODE ? "demo_mode" : undefined,
        result: DEMO_MODE ? "（演示模式）Phase 3 接入后端后，这里会显示真实结果。" : undefined,
      };

      const userMsg: Message = {
        message_id: `m_${taskId}_user`,
        session_id: session.session_id,
        task_id: taskId,
        role: "user",
        content: goal,
        created_at: now,
      };

      dispatch({ type: "START_TASK", task: newTask, userMessage: userMsg });

      // 首条消息 → 用 goal 生成简短标题
      if (!session.title.trim() || session.title === "新建会话") {
        dispatch({
          type: "SET_SESSION_TITLE",
          sessionId: session.session_id,
          title: goal.length > 26 ? `${goal.slice(0, 26)}…` : goal,
        });
      }

      if (!DEMO_MODE) return;

      // ---- 演示：两条事件说明"没有真的跑"，并正常结束 ----
      let seq = 1000 + Math.floor(Math.random() * 100000);
      const mk = (type: EventEnvelope["type"], data: Record<string, unknown>): EventEnvelope => {
        seq += 1;
        return { seq, ts: now + (seq % 100) * 0.1, task_id: taskId, turn: 0, type, data };
      };

      dispatch({
        type: "APPEND_EVENT",
        event: mk("task.start", {
          goal,
          workspace: session.workspace_path,
          model: state.model,
        }),
      });
      dispatch({
        type: "APPEND_EVENT",
        event: mk("thinking", {
          text:
            "（演示模式）这条任务只展示界面反应，并没有真的调用 Agent。\n" +
            "Phase 3 接入 HTTP + SSE 后，这里会出现真实的 PLAN / TOOL_CALL / 审批 / 最终回答。",
          phase: "plan",
          done: true,
        }),
      });
      dispatch({
        type: "APPEND_EVENT",
        event: mk("task.end", {
          status: "done",
          answer: "（演示模式）未真正执行。",
          reason: "demo_mode",
        }),
      });
    },
    [dispatch, session, state.model],
  );

  /** 停止任务：Phase 3 接 POST /api/tasks/{id}/cancel（必须带正确的 task_id） */
  const handleStop = useCallback(() => {
    if (!task) return;
    window.alert(
      `停止任务将在 Phase 3 接入。\n\n目标任务：${task.task_id}\n` +
        `所属会话：${task.session_id}\n\n` +
        `接入后会调用 POST /api/tasks/${task.task_id}/cancel —— 只取消这一个任务，` +
        `不影响同会话的其他任务，也不影响其他会话。`,
    );
  }, [task]);

  /** 审批决策：Phase 5 接后端 */
  const handleApprovalResolve = useCallback((approvalId: string, approved: boolean) => {
    window.alert(
      `Phase 5 才会把审批结果发给后端。\n\n本次演示：${approved ? "批准" : "拒绝"} ${approvalId}`,
    );
  }, []);

  const handleGoToExecution = useCallback(
    () => dispatch({ type: "SET_VIEW", view: "execution" }),
    [dispatch],
  );

  return (
    <div className="flex h-full flex-col overflow-hidden bg-ink-950">
      <AppHeader
        viewMode={state.viewMode}
        onViewChange={handleViewChange}
        model={state.model}
        onModelChange={(m) => dispatch({ type: "SET_MODEL", model: m })}
        connection={state.connection}
        demo={state.demo}
        taskRunning={busy}
      />

      <div className="flex min-h-0 flex-1">
        <SessionSidebar
          groups={groups}
          activeSessionId={state.activeSessionId}
          collapsedGroups={state.collapsedGroups}
          onToggleGroup={handleToggleGroup}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
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
              onSelectTask={(id) => dispatch({ type: "SET_ACTIVE_TASK", taskId: id })}
              onApprovalResolve={handleApprovalResolve}
            />
          )}
        </main>
      </div>

      {/* ---- 底部：工作区选择器 + 输入框 ---- */}
      <div className="shrink-0 border-t border-ink-700 bg-ink-900">
        <div className="mx-auto w-full max-w-[1040px] px-4 pt-2.5">
          <WorkspaceSelector
            workspace={session?.workspace_path ?? ""}
            onChange={handleWorkspaceChange}
            disabled={busy || !session}
            disabledReason={
              !session ? "先选择一个会话" : "任务运行中不能改工作区，否则 Agent 的目录会中途变化"
            }
          />
        </div>

        <TaskComposer
          onSubmit={handleSubmit}
          onStop={handleStop}
          running={busy}
          disabled={!session}
          hint={state.demo ? "演示模式 · 不会真的调用 Agent" : undefined}
        />
      </div>
    </div>
  );
}
