/**
 * 派生选择器（纯函数，不依赖 React）
 *
 * ★ 这里是「单一数据源」的落地处。
 *
 *   聊天消息**不单独存**，每次从事件流投影出来：
 *       task.start     → 一条 user 消息
 *       final_answer   → 一条 assistant 消息
 *       error(fatal)   → 一条 system 消息
 *
 *   代价：O(n) 遍历（n 是事件数，通常 < 100）。
 *   收益：聊天记录和执行流永远一致 —— 因为它们是同一份事实的两种读法，
 *        不存在「两份状态需要同步」这种必然会出 bug 的结构。
 *
 * 为什么放在独立文件？
 *   纯函数便于单测，也能在 Phase 3 接真实事件后原样复用。
 */

import type {
  ApprovalDecisionData,
  ApprovalRequestData,
  EventEnvelope,
  TaskStatus,
  ToolResultData,
} from "../../types/events";
import type { ApprovalItem, ChatMessage } from "../../types/chat";

// --------------------------------------------------------------------------- //
// 聊天消息投影
// --------------------------------------------------------------------------- //
export function deriveMessages(events: EventEnvelope[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  const sorted = sortBySeq(events);

  for (const ev of sorted) {
    const d = ev.data as Record<string, unknown>;

    if (ev.type === "task.start") {
      const goal = String(d.goal ?? "");
      if (goal) {
        out.push({
          id: `m_${ev.task_id}_user_${ev.seq}`,
          role: "user",
          text: goal,
          ts: ev.ts,
          sourceSeq: ev.seq,
          taskId: ev.task_id,
        });
      }
      continue;
    }

    if (ev.type === "final_answer") {
      const text = String(d.text ?? "");
      if (text) {
        out.push({
          id: `m_${ev.task_id}_ai_${ev.seq}`,
          role: "assistant",
          text,
          ts: ev.ts,
          sourceSeq: ev.seq,
          taskId: ev.task_id,
        });
      }
      continue;
    }

    // 致命错误也进聊天（用户需要看到"为什么没结果"）
    if (ev.type === "error" && d.fatal === true) {
      out.push({
        id: `m_${ev.task_id}_err_${ev.seq}`,
        role: "system",
        text: String(d.message ?? "发生了不可恢复的错误"),
        ts: ev.ts,
        sourceSeq: ev.seq,
        taskId: ev.task_id,
      });
    }
  }

  return out;
}

// --------------------------------------------------------------------------- //
// 运行状态提示（聊天视图那一行「正在…」）
// --------------------------------------------------------------------------- //
export interface RunHint {
  /** 展示文案 */
  text: string;
  /** 语义色调，与事件配色共用一套 */
  tone: "blue" | "slate" | "cyan" | "amber" | "green" | "red" | "teal" | "violet";
  /** 是否显示转圈 */
  spinning: boolean;
}

/**
 * 根据**最后一条事件**推断当前在干什么。
 *
 * ★ 必须根据真实事件推断，不能用前端计时器假装。
 *   所以这里只看 `events`，没有任何 timer / 随机文案。
 */
export function deriveRunHint(events: EventEnvelope[], status: TaskStatus): RunHint | null {
  if (!events.length) return null;

  if (status === "done") return { text: "任务已完成", tone: "green", spinning: false };
  if (status === "aborted") return { text: "任务已中止", tone: "amber", spinning: false };
  if (status === "failed") return { text: "任务失败", tone: "red", spinning: false };

  const last = sortBySeq(events)[events.length - 1];
  const d = (last.data ?? {}) as Record<string, unknown>;

  switch (last.type) {
    case "task.start":
      return { text: "正在规划任务…", tone: "blue", spinning: true };
    case "plan":
      return { text: "计划已生成，准备动手…", tone: "violet", spinning: true };
    case "thinking":
      return { text: "正在思考…", tone: "slate", spinning: true };
    case "act":
      return { text: `准备调用 ${String(d.tool || "工具")}…`, tone: "cyan", spinning: true };
    case "tool.call":
      return { text: `正在执行 ${String(d.tool || "工具")}…`, tone: "cyan", spinning: true };
    case "tool.result":
      return d.ok
        ? { text: `${String(d.tool || "工具")} 执行完成`, tone: "green", spinning: true }
        : { text: `${String(d.tool || "工具")} 执行失败，正在调整…`, tone: "red", spinning: true };
    case "observe":
      return { text: "正在整理解析结果…", tone: "slate", spinning: true };
    case "reflect":
      return d.done
        ? { text: "正在组织最终答复…", tone: "teal", spinning: true }
        : { text: "正在复盘进展…", tone: "slate", spinning: true };
    case "approval.request":
      return { text: "等待你的审批…", tone: "amber", spinning: false };
    case "approval.decision":
      return d.approved
        ? { text: "已批准，继续执行…", tone: "green", spinning: true }
        : { text: "已拒绝，Agent 正在调整…", tone: "red", spinning: true };
    case "final_answer":
      return { text: "正在收尾…", tone: "teal", spinning: true };
    default:
      return { text: "运行中…", tone: "slate", spinning: true };
  }
}

// --------------------------------------------------------------------------- //
// 审批
// --------------------------------------------------------------------------- //
export function deriveApprovals(events: EventEnvelope[]): ApprovalItem[] {
  const byId = new Map<string, ApprovalItem>();

  for (const ev of sortBySeq(events)) {
    if (ev.type === "approval.request") {
      const d = ev.data as unknown as ApprovalRequestData;
      if (!d?.approval_id) continue;
      byId.set(d.approval_id, { ...d });
      continue;
    }

    if (ev.type === "approval.decision" || ev.type === "approval.expired") {
      const d = ev.data as unknown as ApprovalDecisionData;
      const item = byId.get(d?.approval_id);
      if (item) {
        item.resolved = d.status;
      }
    }
  }

  return [...byId.values()];
}

/** 只返回「还没被决策」的审批 —— 聊天视图用它决定是否挂提示条 */
export function derivePendingApprovals(events: EventEnvelope[]): ApprovalItem[] {
  return deriveApprovals(events).filter((a) => !a.resolved);
}

// --------------------------------------------------------------------------- //
// 杂项
// --------------------------------------------------------------------------- //
export function sortBySeq(events: EventEnvelope[]): EventEnvelope[] {
  return [...events].sort((a, b) => a.seq - b.seq);
}

export function lastEvent(events: EventEnvelope[]): EventEnvelope | null {
  if (!events.length) return null;
  let max = events[0];
  for (const e of events) if (e.seq > max.seq) max = e;
  return max;
}

/** 从 tool.result 里取失败信息，供卡片渲染 */
export function asToolResult(ev: EventEnvelope): ToolResultData {
  return ev.data as unknown as ToolResultData;
}

/** 从 task.end 里取结束信息 */
export function asTaskEnd(ev: EventEnvelope): { status: TaskStatus; answer: string; reason: string } {
  return ev.data as unknown as { status: TaskStatus; answer: string; reason: string };
}
