/**
 * Session 层派生选择器（纯函数）
 *
 * ★ 分组逻辑只在这里实现一次。
 *   侧栏、Task 上下文、后续的 Session 切换都调它，不各写一套。
 */

import type { Session, WorkspaceGroup, Task, Message } from "./types";
import {
  normalizeWorkspacePath,
  UNSET_WORKSPACE_KEY,
  workspaceKey,
  workspaceLabel,
} from "./workspace";

/**
 * 按工作目录分组。
 *
 * 规则（任务书 §3.3）：
 *   · 组间按「组内最近活动时间」倒序
 *   · 组内按会话活动时间倒序
 *   · 「未设置工作区」永远排最后（它不是真实目录，不该插在真实项目中间）
 */
export function groupSessionsByWorkspace(sessions: Session[]): WorkspaceGroup[] {
  const map = new Map<string, WorkspaceGroup>();

  for (const s of sessions) {
    const key = workspaceKey(s.workspace_path);
    let g = map.get(key);

    if (!g) {
      g = {
        key,
        path: normalizeWorkspacePath(s.workspace_path),
        label: workspaceLabel(s.workspace_path),
        sessions: [],
        latestAt: 0,
      };
      map.set(key, g);
    }

    g.sessions.push(s);
    if (s.updated_at > g.latestAt) g.latestAt = s.updated_at;
  }

  const groups = [...map.values()];

  for (const g of groups) {
    g.sessions.sort((a, b) => b.updated_at - a.updated_at);
  }

  groups.sort((a, b) => {
    const aUnset = a.key === UNSET_WORKSPACE_KEY;
    const bUnset = b.key === UNSET_WORKSPACE_KEY;
    if (aUnset !== bUnset) return aUnset ? 1 : -1;
    return b.latestAt - a.latestAt;
  });

  return groups;
}

/** 某会话的消息，按时间正序（聊天视图从旧到新） */
export function messagesOf(
  messagesBySession: Record<string, Message[]>,
  sessionId: string | null,
): Message[] {
  if (!sessionId) return [];
  const list = messagesBySession[sessionId] ?? [];
  return [...list].sort((a, b) => a.created_at - b.created_at);
}

/** 某会话的任务，按开始时间正序（执行流按时间线看） */
export function tasksOf(tasksBySession: Record<string, Task[]>, sessionId: string | null): Task[] {
  if (!sessionId) return [];
  const list = tasksBySession[sessionId] ?? [];
  return [...list].sort((a, b) => a.started_at - b.started_at);
}

/**
 * 选一个「默认展示的任务」。
 *
 * 优先用调用方指定的；否则取该会话**最后一个**任务 ——
 * 因为用户切到执行流时，最想看的是"刚跑完/正在跑的那个"。
 */
export function pickTask(tasks: Task[], preferredId?: string | null): Task | null {
  if (!tasks.length) return null;
  if (preferredId) {
    const hit = tasks.find((t) => t.task_id === preferredId);
    if (hit) return hit;
  }
  return tasks[tasks.length - 1];
}

/** 会话标题的兜底显示（空标题不该在侧栏留一片空白） */
export function displayTitle(s: Session): string {
  const t = s.title.trim();
  if (t) return t;
  return "未命名会话";
}

/** 相对时间（只用于辅助展示，不参与分组） */
export function relativeTime(ts: number, now = Date.now() / 1000): string {
  const diff = Math.max(0, now - ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/**
 * 消歧：当多个分组显示名相同时，自动加长到「父目录\子目录」。
 *
 * ★ 为什么需要？
 *   `D:\Projects\demo-app` 和 `E:\Experiments\demo-app` 按规则确实是**两个组**
 *   （任务书 §3.2），但如果都显示成 "demo-app"，用户根本分不出哪个是哪个 ——
 *   「没合并」在数据层是对的，在界面上却成了另一种困惑。
 *
 *   加长到两段后：`Projects\demo-app` / `Experiments\demo-app`，一眼能分。
 *   （两段仍重名的情况极罕见，那时只能靠 hover 的完整路径区分。）
 */
export function disambiguateGroupLabels(groups: WorkspaceGroup[]): WorkspaceGroup[] {
  const counts = new Map<string, number>();
  for (const g of groups) {
    counts.set(g.label, (counts.get(g.label) ?? 0) + 1);
  }

  return groups.map((g) => {
    if ((counts.get(g.label) ?? 0) <= 1) return g;
    if (g.key === UNSET_WORKSPACE_KEY) return g;

    const parts = g.path.split("\\").filter(Boolean);
    if (parts.length < 2) return g;

    return { ...g, label: parts.slice(-2).join("\\") };
  });
}
