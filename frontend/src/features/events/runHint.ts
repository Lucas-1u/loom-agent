/**
 * 运行状态提示（聊天视图那一行「正在…」）
 *
 * ★ 铁律：**必须根据真实事件推断，不能用前端计时器假装。**
 *   所以这里只读 `events` 的最后一条，没有任何 timer / 随机文案。
 *
 * 这也是「聊天视图和执行流共享同一份事实」的体现 —— 两边的状态不会打架。
 */

import type { EventEnvelope, TaskStatus } from "../../types/events";

export interface RunHint {
  text: string;
  tone: "blue" | "slate" | "cyan" | "amber" | "green" | "red" | "teal" | "violet";
  /** 是否显示转圈（等待审批时不该转，因为不是「在跑」） */
  spinning: boolean;
}

export function deriveRunHint(
  events: EventEnvelope[],
  status: TaskStatus | "created",
): RunHint | null {
  if (status === "done") return { text: "任务已完成", tone: "green", spinning: false };
  if (status === "aborted") return { text: "任务已中止", tone: "amber", spinning: false };
  if (status === "failed") return { text: "任务失败", tone: "red", spinning: false };

  if (!events.length) return null;

  const last = events[events.length - 1];
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
      return { text: "正在整理结果…", tone: "slate", spinning: true };
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
