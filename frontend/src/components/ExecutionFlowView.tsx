import { useCallback, useEffect, useRef, useState } from "react";
import type { EventEnvelope } from "../types/events";
import { EventTimeline } from "./EventTimeline";

/**
 * 执行流视图（瀑布式时间线）
 *
 * ★ 自动滚动的三条规则（任务书 §5.5）：
 *   1. 用户停在底部 → 新事件到达自动滚下去
 *   2. 用户往上翻 → 绝不强制拉回底部
 *   3. 提供一个「回到最新」按钮，并显示"错过了几条"
 *
 *   实现关键：用 `atBottom` 记录用户当前位置，而不是"每次有新事件就滚"。
 *   判断阈值给 40px —— 太严格会导致轻微滚动就被判定为"离开了底部"。
 */
const BOTTOM_THRESHOLD = 40;

export function ExecutionFlowView({
  events,
  onApprovalResolve,
}: {
  events: EventEnvelope[];
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [newSinceLeave, setNewSinceLeave] = useState(0);
  const lastCount = useRef(events.length);

  // 首帧定位：默认贴底（用户切到执行流时应该看到最新进展）。
  // ?scroll=top 可以改成从头看 —— 便于截图/分享某一段。
  const initialScroll = useRef(
    new URLSearchParams(window.location.search).get("scroll") === "top" ? "top" : "bottom",
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (initialScroll.current === "bottom") {
      el.scrollTop = el.scrollHeight;
    } else {
      el.scrollTop = 0;
    }
  }, []);

  // ---- 监听滚动位置 ----
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nowBottom = distance <= BOTTOM_THRESHOLD;
    setAtBottom(nowBottom);
    if (nowBottom) setNewSinceLeave(0);
  }, []);

  // ---- 事件变化：在底部就跟随，不在就累计提醒 ----
  useEffect(() => {
    const el = scrollRef.current;
    const grew = events.length - lastCount.current;
    lastCount.current = events.length;

    if (!el || grew <= 0) return;

    if (atBottom) {
      // 用 requestAnimationFrame 等 DOM 真正长高后再滚，否则会滚到旧高度
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

  // ---- 空状态 ----
  if (!events.length) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <div className="max-w-[420px] text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl border border-ink-700 bg-ink-850">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <circle cx="4.5" cy="4" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
              <circle cx="4.5" cy="10" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
              <circle cx="4.5" cy="16" r="1.6" stroke="#4b5a6e" strokeWidth="1.2" />
              <path
                d="M8.5 4h7M8.5 10h5M8.5 16h6"
                stroke="#374151"
                strokeWidth="1.2"
                strokeLinecap="round"
              />
            </svg>
          </div>
          <p className="text-[13px] text-slate-400">还没有执行记录</p>
          <p className="mt-1 text-[11.5px] leading-relaxed text-slate-600">
            提交一个任务后，Agent 的每一步都会按真实顺序出现在这里 ——
            规划、工具调用、审批、最终回答。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto overscroll-contain px-4 py-4"
      >
        <div className="mx-auto max-w-[860px]">
          <EventTimeline events={events} onApprovalResolve={onApprovalResolve} />

          {/* 时间线收尾 */}
          <div className="flex items-center gap-2 pl-8 pt-1">
            <span className="h-1.5 w-1.5 rounded-full bg-ink-700" />
            <span className="text-[10px] text-slate-600">
              共 {events.length} 条事件 · 全部来自后端事件流
            </span>
          </div>
        </div>
      </div>

      {/* 回到最新 */}
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
  );
}
