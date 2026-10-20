import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EventEnvelope } from "../types/events";
import type { Task } from "../features/session/types";
import { relativeTime } from "../features/session/selectors";
import { EventTimeline } from "./EventTimeline";

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
}: {
  events: EventEnvelope[];
  tasks: Task[];
  activeTaskId: string | null;
  onSelectTask: (taskId: string) => void;
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [newSinceLeave, setNewSinceLeave] = useState(0);
  const lastCount = useRef(events.length);
  const [expandedPicker, setExpandedPicker] = useState(false);

  const activeTask = useMemo(
    () => tasks.find((t) => t.task_id === activeTaskId) ?? null,
    [tasks, activeTaskId],
  );

  const initialScroll = useRef(
    new URLSearchParams(window.location.search).get("scroll") === "top" ? "top" : "bottom",
  );

  // 首帧 / 切换任务时定位到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = initialScroll.current === "top" ? 0 : el.scrollHeight;
    lastCount.current = events.length;
    setAtBottom(true);
    setNewSinceLeave(0);
  }, [activeTaskId, events.length]);

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
    const grew = events.length - lastCount.current;
    lastCount.current = events.length;

    if (!el || grew <= 0) return;

    if (atBottom) {
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      });
    } else {
      setNewSinceLeave((n) => n + grew);
    }
  }, [events.length, atBottom]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setAtBottom(true);
    setNewSinceLeave(0);
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* ---- Task 选择器：只有多于一个任务时才出现 ---- */}
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
            <div className="absolute left-4 right-4 top-full z-20 mx-auto mt-1 max-w-[860px] overflow-hidden rounded-lg border border-ink-600 bg-ink-850 shadow-xl">
              {tasks.map((t, i) => {
                const active = t.task_id === activeTaskId;
                return (
                  <button
                    key={t.task_id}
                    type="button"
                    onClick={() => {
                      onSelectTask(t.task_id);
                      setExpandedPicker(false);
                    }}
                    className={[
                      "flex w-full items-start gap-2 px-3 py-2 text-left transition-colors",
                      active ? "bg-tone-blue/10" : "hover:bg-ink-800",
                    ].join(" ")}
                  >
                    <span className="mt-[2px] shrink-0 font-mono text-[10px] text-slate-600">
                      #{i + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span
                        className={`block truncate text-[12px] ${
                          active ? "text-slate-100" : "text-slate-300"
                        }`}
                      >
                        {t.goal}
                      </span>
                      <span className="block text-[10px] text-slate-600">
                        <StatusDot status={t.status} /> {t.status} · {relativeTime(t.started_at)}
                      </span>
                    </span>
                  </button>
                );
              })}
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
              <EventTimeline events={events} onApprovalResolve={onApprovalResolve} />

              <div className="flex items-center gap-2 pl-8 pt-1">
                <span className="h-1.5 w-1.5 rounded-full bg-ink-700" />
                <span className="text-[10px] text-slate-600">
                  共 {events.length} 条事件 · 全部来自后端事件流
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

function StatusDot({ status }: { status: string }) {
  const map: Record<string, string> = {
    running: "bg-tone-cyan",
    awaiting_approval: "bg-tone-amber",
    done: "bg-tone-green",
    aborted: "bg-tone-amber",
    failed: "bg-tone-red",
    created: "bg-slate-600",
  };
  return (
    <span
      className={`inline-block h-1.5 w-1.5 rounded-full align-middle ${map[status] ?? "bg-slate-600"}`}
    />
  );
}
