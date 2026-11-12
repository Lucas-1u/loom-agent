import type { EventEnvelope } from "../types/events";
import { EventCard } from "./EventCard";

/**
 * 事件时间线
 *
 * ★ 顺序完全由后端的 `seq` 决定，前端**不推演**执行流程。
 *   这里做的唯一一件事是按 seq 排序 + 逐条渲染。
 *
 * ★ 关于 `items`（2026-11-11 改名）：
 *   传进来的应该是 `mergeThinkingEvents(events)` 的产物 —— 相邻的思考 delta 已被
 *   合成一段（渲染成一张可折叠的卡）。这**不是推演执行流程**：
 *   分段边界完全由"有没有别的事件插进来"决定，合并段保留原始 seq 区间，
 *   原始事件在 store 里一条不少。改名是为了让这个契约在类型层面可见，
 *   免得以后有人把裸事件列表直接塞进来（那样思考又会碎成一屏）。
 */
export function EventTimeline({
  items,
  onApprovalResolve,
}: {
  items: EventEnvelope[];
  onApprovalResolve?: (id: string, approved: boolean) => void;
}) {
  const ordered = [...items].sort((a, b) => a.seq - b.seq);

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
