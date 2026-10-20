import type { EventEnvelope } from "../types/events";
import { EventCard } from "./EventCard";

/**
 * 事件时间线
 *
 * ★ 顺序完全由后端的 `seq` 决定，前端**不推演**执行流程。
 *   这里做的唯一一件事是按 seq 排序 + 逐条渲染。
 */
export function EventTimeline({
  events,
  onApprovalResolve,
}: {
  events: EventEnvelope[];
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const ordered = [...events].sort((a, b) => a.seq - b.seq);

  return (
    <div className="relative">
      {ordered.map((ev, i) => (
        <EventCard
          key={`${ev.task_id}-${ev.seq}`}
          ev={ev}
          isLast={i === ordered.length - 1}
          onApprovalResolve={onApprovalResolve}
        />
      ))}
    </div>
  );
}
