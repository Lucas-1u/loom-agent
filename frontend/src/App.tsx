import { useCallback, useMemo } from "react";
import { AppHeader } from "./components/AppHeader";
import { ChatView } from "./components/ChatView";
import { ExecutionFlowView } from "./components/ExecutionFlowView";
import { TaskComposer } from "./components/TaskComposer";
import { WorkspacePanel } from "./components/WorkspacePanel";
import { useActiveEvents, useTaskStore } from "./features/task/store";
import {
  deriveMessages,
  derivePendingApprovals,
  deriveRunHint,
} from "./features/task/selectors";
import { DEMO_MODE } from "./services/config";
import type { EventEnvelope } from "./types/events";

/**
 * 应用外壳
 *
 * ★ 这里只做三件事：
 *   1. 布局（header / 左栏 / 主区 / 底部输入）
 *   2. 从 store 派生视图数据（消息、运行状态、待审批）
 *   3. 把用户操作翻译成 dispatch
 *
 *   具体渲染全在子组件里 —— App 不写任何事件渲染逻辑。
 */
export default function App() {
  const { state, dispatch } = useTaskStore();
  const events = useActiveEvents();

  // ---- 派生数据（不存第二份）----
  const messages = useMemo(() => deriveMessages(events), [events]);
  const pendingApprovals = useMemo(() => derivePendingApprovals(events), [events]);

  const activeTask = state.activeTaskId ? state.tasks[state.activeTaskId] : undefined;
  const taskStatus = activeTask?.status ?? "created";
  const running = taskStatus === "running" || taskStatus === "awaiting_approval";

  const runHint = useMemo(() => deriveRunHint(events, taskStatus), [events, taskStatus]);

  // ---- 操作 ----
  const handleViewChange = useCallback(
    (view: "chat" | "execution") => dispatch({ type: "SET_VIEW", view }),
    [dispatch],
  );

  /**
   * 提交任务。
   *
   * Phase 2（演示模式）：只往界面插「任务开始 + 一条说明」，明确告诉用户
   *   真实执行要等 Phase 3 —— **不伪造一整套假执行过程冒充真实运行**。
   * Phase 3：换成 `await api.createTask(goal)`，其余逻辑不变。
   */
  const handleSubmit = useCallback(
    (goal: string) => {
      if (!DEMO_MODE) return;

      const maxSeq = events.reduce((m, e) => Math.max(m, e.seq), 0);
      const now = Date.now() / 1000;
      const taskId = `task_demo_${Date.now().toString(36)}`;

      const start: EventEnvelope = {
        seq: maxSeq + 1,
        ts: now,
        task_id: taskId,
        turn: 0,
        type: "task.start",
        data: { goal, workspace: state.workspace, model: state.model },
      };

      const note: EventEnvelope = {
        seq: maxSeq + 2,
        ts: now + 0.2,
        task_id: taskId,
        turn: 0,
        type: "thinking",
        data: {
          text:
            "（演示模式）这条任务只是展示界面反应，并没有真的调用 Agent。\n" +
            "Phase 3 接入 HTTP + SSE 之后，这里会出现真实的 PLAN / TOOL_CALL / 审批 / 最终回答。",
          phase: "plan",
          done: true,
        },
      };

      dispatch({ type: "SET_ACTIVE_TASK", taskId });
      dispatch({ type: "APPEND_EVENT", event: start });
      dispatch({ type: "APPEND_EVENT", event: note });
    },
    [dispatch, events, state.workspace, state.model],
  );

  /** 停止任务：Phase 3 接 POST /api/tasks/{id}/cancel */
  const handleStop = useCallback(() => {
    window.alert("停止任务会在 Phase 3 接入（POST /api/tasks/{id}/cancel）。");
  }, []);

  /** 审批决策：Phase 5 接后端；现在只说明，不伪造"已批准" */
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
        taskRunning={running}
      />

      <div className="flex min-h-0 flex-1">
        <WorkspacePanel
          workspace={state.workspace}
          onWorkspaceChange={(p) => dispatch({ type: "SET_WORKSPACE", workspace: p })}
          taskRunning={running}
        />

        {/* ★ 主区：两种视图共用同一份 events，只是渲染方式不同。
              切换视图 = 换一个组件，事件数组原地不动。 */}
        <main className="min-w-0 flex-1 bg-ink-950">
          {state.viewMode === "chat" ? (
            <ChatView
              messages={messages}
              runHint={runHint}
              pendingApprovals={pendingApprovals}
              onGoToExecution={handleGoToExecution}
            />
          ) : (
            <ExecutionFlowView events={events} onApprovalResolve={handleApprovalResolve} />
          )}
        </main>
      </div>

      <TaskComposer
        onSubmit={handleSubmit}
        onStop={handleStop}
        running={running}
        disabled={!state.workspace}
        hint={state.demo ? "演示模式 · 不会真的调用 Agent" : undefined}
      />
    </div>
  );
}
