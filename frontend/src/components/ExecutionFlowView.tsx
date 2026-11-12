import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EventEnvelope } from "../types/events";
import type { Task } from "../features/session/types";
import { buildTaskTree } from "../features/session/taskTree";
import { mergeThinkingEvents } from "../features/events/mergeThinking";
import { sameWorkspace } from "../features/session/workspace";
import { EventTimeline } from "./EventTimeline";
import { TaskTree } from "./TaskTree";

/**
 * 执行流视图（瀑布式时间线）
 *
 * ★ 自动滚动的三条规则：
 *   1. 用户停在底部 → 新事件到达自动滚下去
 *   2. 用户往上翻 → 绝不强制拉回底部
 *   3. 提供「回到最新」按钮，并显示"错过了几条"
 *
 * ★ 多任务：当一个会话有多个 Task 时，顶部出现 Task 选择器。
 *   事件**只来自选中的那个 Task**，不会串台（任务书 §4.6.3）。
 */
const BOTTOM_THRESHOLD = 40;

export function ExecutionFlowView({
  events,
  tasks,
  activeTaskId,
  onSelectTask,
  onApprovalResolve,
  sessionWorkspace,
}: {
  events: EventEnvelope[];
  tasks: Task[];
  activeTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onApprovalResolve?: (id: string, approved: boolean) => void;
  /** 当前会话的工作目录，用来标出「历史任务跑在别的目录」 */
  sessionWorkspace?: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [newSinceLeave, setNewSinceLeave] = useState(0);
  const [expandedPicker, setExpandedPicker] = useState(false);

  /**
   * 渲染用的时间线：**相邻的思考 delta 先合并成一段**（见 mergeThinkingEvents）。
   *
   * ★ 在这里算而不是在 EventTimeline 里算：下面的自动滚动、"+N 条"计数
   *   都要用"卡片条数"而不是"原始事件条数" —— 否则一次思考会跳 5 次，
   *   用户看到的是 "+37" 这种没法解读的数字。
   * ★ 原始事件一条不少，仍在 `events` 里；这里只是派生出**另一种计数口径**。
   */
  const timeline = useMemo(() => mergeThinkingEvents(events), [events]);
  /** 原始事件被折掉多少条（只用于底部那行说明，如实告知合并了什么） */
  const folded = events.length - timeline.length;
  const thinkSegments = useMemo(
    () => timeline.filter((e) => e.type === "thinking").length,
    [timeline],
  );
  /** 上一次渲染的**卡片**条数（不是事件条数）—— 用于算"错过了几条" */
  const lastCount = useRef(timeline.length);
  /** 已经为哪个任务做过"定位到底部"（见下面的定位 effect） */
  const positionedFor = useRef<string | null>(null);

  const activeTask = useMemo(
    () => tasks.find((t) => t.task_id === activeTaskId) ?? null,
    [tasks, activeTaskId],
  );

  /**
   * 历史任务是否跑在别的目录。
   *
   * ★ 旧版本允许在会话里切换工作目录（只改本地），于是同一个会话里
   *   可能混着两个项目的任务。数据不能悄悄改写，但要**明确标出来**，
   *   否则用户看到的就是"LLMstudy 的会话里是 Try_agent 的内容"。
   */
  const mismatch = useCallback(
    (t: Task) => Boolean(t.workspace && sessionWorkspace && !sameWorkspace(t.workspace, sessionWorkspace)),
    [sessionWorkspace],
  );

  /**
   * 任务树：把扁平任务列表按 parent_task_id 构建成树。
   *
   * ★ 有子任务时，下拉里显示树（可折叠）；无子任务时退化为扁平列表（行为不变）。
   *   buildTaskTree 是纯函数，每次 tasks 变化时重算。
   */
  const taskTree = useMemo(() => buildTaskTree(tasks), [tasks]);

  const initialScroll = useRef(
    new URLSearchParams(window.location.search).get("scroll") === "top" ? "top" : "bottom",
  );

  /**
   * 首帧 / 切换任务时定位到底部。
   *
   * ★ 这个 effect 以前把 `events.length` 写进依赖，于是**每来一条事件都会执行一次**：
   *   强制拉回底部（用户往上翻根本翻不住）+ 每次 `setAtBottom(true)`
   *   → 下面那个"回到最新"按钮永远不出现。注释写的是"首帧/切任务"，代码却在每帧跑。
   *   现在用 `positionedFor` 这个 ref 做闸门：同一个任务只定位一次；
   *   依赖列表仍然完整（lint 不用关规则），行为才是注释说的那个意思。
   */
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const key = activeTaskId ?? "";
    if (positionedFor.current === key) return; // 这个任务已经定位过，不要抢用户的滚动
    positionedFor.current = key;

    el.scrollTop = initialScroll.current === "top" ? 0 : el.scrollHeight;
    lastCount.current = timeline.length;
    setAtBottom(true);
    setNewSinceLeave(0);
  }, [activeTaskId, timeline.length]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nowBottom = distance <= BOTTOM_THRESHOLD;
    setAtBottom(nowBottom);
    if (nowBottom) setNewSinceLeave(0);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    const grew = timeline.length - lastCount.current;
    lastCount.current = timeline.length;

    if (!el || grew <= 0) return;

    if (atBottom) {
      /* ★ 跟随新事件用**瞬时**滚动，不用 smooth。
       *   smooth 动画期间 scroll 事件里的 distance 还很大，会把 atBottom 翻成 false
       *   → 下一条事件就不跟了，用户卡在半路看到"回到最新 +N"。
       *   日志型界面本来就是"贴着底走"，瞬时更稳也更跟得上。
       *   （按钮上的"回到最新"仍然用 smooth —— 那是用户主动的一次位移。） */
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    } else {
      setNewSinceLeave((n) => n + grew);
    }
  }, [timeline.length, atBottom]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
    setNewSinceLeave(0);
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* ---- Task 选择器：多任务时显示，支持父子树 ---- */}
      {tasks.length > 1 && (
        <div className="relative shrink-0 border-b border-ink-700 bg-ink-900/60 px-4 py-1.5">
          <button
            type="button"
            onClick={() => setExpandedPicker((v) => !v)}
            className="flex w-full max-w-[860px] mx-auto items-center gap-2 text-left"
          >
            <span className="shrink-0 text-[10px] uppercase tracking-[0.1em] text-slate-600">
              任务
            </span>
            <span className="shrink-0 rounded bg-ink-800 px-1.5 py-px font-mono text-[10px] text-slate-500">
              {Math.max(1, tasks.findIndex((t) => t.task_id === activeTaskId) + 1)}/{tasks.length}
            </span>
            <span className="min-w-0 flex-1 truncate text-[12px] text-slate-300">
              {activeTask?.goal ?? "（未选择任务）"}
              {activeTask && mismatch(activeTask) && (
                <span className="ml-2 text-[10px] font-normal text-tone-amber">
                  ⚠ 该任务实际在 {activeTask.workspace} 执行，与会话目录不同
                </span>
              )}
            </span>
            <span className="shrink-0 text-[10px] text-slate-600">
              {expandedPicker ? "收起" : "切换"}
            </span>
            <span
              className={`shrink-0 text-[9px] text-slate-600 transition-transform ${
                expandedPicker ? "rotate-90" : ""
              }`}
            >
              ▶
            </span>
          </button>

          {expandedPicker && (
            <div className="absolute left-4 right-4 top-full z-20 mx-auto mt-1 max-w-[860px] overflow-hidden rounded-lg border border-ink-600 bg-ink-850 shadow-xl p-1.5">
              <TaskTree
                tree={taskTree}
                activeTaskId={activeTaskId}
                sessionWorkspace={sessionWorkspace}
                onSelect={(id) => {
                  onSelectTask(id);
                  setExpandedPicker(false);
                }}
              />
            </div>
          )}
        </div>
      )}

      {/* ---- 时间线 ---- */}
      {events.length === 0 ? (
        <EmptyState hasTask={Boolean(activeTask)} />
      ) : (
        <div className="relative min-h-0 flex-1">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="h-full overflow-y-auto overscroll-contain px-4 py-4"
          >
            <div className="mx-auto max-w-[860px]">
              <EventTimeline items={timeline} onApprovalResolve={onApprovalResolve} />

              <div className="flex items-center gap-2 pl-8 pt-1">
                <span className="h-1.5 w-1.5 rounded-full bg-ink-700" />
                <span className="text-[10px] text-slate-600">
                  共 {events.length} 条事件 · 全部来自后端事件流
                  {/* ★ 合并了就要说出来，不能让"少了几十条"变成没人解释的谜 */}
                  {folded > 0 && (
                    <span className="text-slate-700">
                      （思考合并为 {thinkSegments} 段，省下 {folded} 张卡片）
                    </span>
                  )}
                </span>
              </div>
            </div>
          </div>

          {!atBottom && (
            <button
              type="button"
              onClick={scrollToBottom}
              className="loom-enter absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-ink-600 bg-ink-800/95 px-3 py-1.5 text-[11.5px] text-slate-300 shadow-lg backdrop-blur transition-colors hover:border-tone-cyan/40 hover:text-slate-100"
            >
              <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path
                  d="M6 2v7M3 6.5 6 9.5l3-3"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              回到最新
              {newSinceLeave > 0 && (
                <span className="rounded-full bg-tone-cyan/20 px-1.5 text-[10px] text-tone-cyan">
                  +{newSinceLeave}
                </span>
              )}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function EmptyState({ hasTask }: { hasTask: boolean }) {
  return (
    <div className="flex flex-1 items-center justify-center px-6">
      <div className="max-w-[420px] text-center">
        <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl border border-ink-700 bg-ink-850">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <circle cx="4.5" cy="4" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
            <circle cx="4.5" cy="10" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
            <circle cx="4.5" cy="16" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
            <path d="M8.5 4h7M8.5 10h5M8.5 16h6" stroke="#374151" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </div>
        <p className="text-[13px] text-slate-400">
          {hasTask ? "这个任务还没有事件" : "还没有执行记录"}
        </p>
        <p className="mt-1 text-[11.5px] leading-relaxed text-slate-600">
          {hasTask
            ? "任务刚开始，事件马上就到。"
            : "提交一个任务后，Agent 的每一步都会按真实顺序出现在这里。"}
        </p>
      </div>
    </div>
  );
}
