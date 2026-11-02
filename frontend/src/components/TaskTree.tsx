import { useState } from "react";
import type { TaskTreeNode } from "../features/session/taskTree";
import { relativeTime } from "../features/session/selectors";
import { sameWorkspace } from "../features/session/workspace";

/**
 * 任务树组件（执行流视图顶部）
 *
 * ★ 设计决策：
 *   - 不新建侧栏，而是升级 ExecutionFlowView 顶部现有的扁平 Task 选择器
 *     为树形。有子任务时显示树（可折叠），无子任务时退化为扁平列表（行为不变）。
 *     这样布局不变、改动最小，且与现有"事件来自选中任务"的语义兼容。
 *
 * ★ 交互：
 *   - 点击节点 → onSelect(task_id)
 *   - 父节点前的 ▶ 箭头 → 折叠/展开子任务
 *   - 折叠状态本地维护（组件内 state），不进 store
 *
 * ★ 显示：
 *   - 状态圆点 + 目标（截断）+ 轮次/子任务数
 *   - 当前选中任务高亮
 *   - 历史任务跑在别的目录 → 标注警告
 */

export interface TaskTreeProps {
  tree: TaskTreeNode[];
  activeTaskId: string | null;
  sessionWorkspace?: string;
  onSelect: (taskId: string) => void;
}

export function TaskTree({ tree, activeTaskId, sessionWorkspace, onSelect }: TaskTreeProps) {
  // 折叠的父节点集合（task_id）
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggle = (id: string) => {
    const next = new Set(collapsed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setCollapsed(next);
  };

  return (
    <div className="flex flex-col gap-0.5">
      {tree.map((node) => (
        <TreeRow
          key={node.task.task_id}
          node={node}
          level={0}
          collapsed={collapsed}
          onToggle={toggle}
          activeTaskId={activeTaskId}
          sessionWorkspace={sessionWorkspace}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

/** 树的一行（递归） */
function TreeRow({
  node,
  level,
  collapsed,
  onToggle,
  activeTaskId,
  sessionWorkspace,
  onSelect,
}: {
  node: TaskTreeNode;
  level: number;
  collapsed: Set<string>;
  onToggle: (id: string) => void;
  activeTaskId: string | null;
  sessionWorkspace?: string;
  onSelect: (taskId: string) => void;
}) {
  const task = node.task;
  const hasChildren = node.children.length > 0;
  const isCollapsed = collapsed.has(task.task_id);
  const active = task.task_id === activeTaskId;
  const mismatch = Boolean(
    task.workspace && sessionWorkspace && !sameWorkspace(task.workspace, sessionWorkspace),
  );

  return (
    <div>
      {/* 当前行 */}
      <button
        type="button"
        onClick={() => onSelect(task.task_id)}
        className={[
          "flex w-full items-start gap-1.5 rounded px-2 py-1.5 text-left transition-colors",
          active
            ? "bg-tone-blue/12 text-slate-100"
            : "text-slate-400 hover:bg-ink-850 hover:text-slate-200",
        ].join(" ")}
      >
        {/* 缩进 */}
        <span style={{ width: `${level * 12}px` }} className="shrink-0" />

        {/* 折叠箭头（有子任务时显示） */}
        {hasChildren ? (
          <span
            role="button"
            tabIndex={-1}
            onClick={(e) => {
              e.stopPropagation();
              onToggle(task.task_id);
            }}
            className={[
              "mt-[3px] w-2.5 shrink-0 text-[9px] text-slate-600 transition-transform",
              isCollapsed ? "" : "rotate-90",
            ].join(" ")}
          >
            ▶
          </span>
        ) : (
          <span className="mt-[3px] w-2.5 shrink-0 text-[9px] text-slate-700">·</span>
        )}

        {/* 状态圆点 */}
        <span className={`mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full ${dotClass(task.status)}`} />

        {/* 内容 */}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12px] leading-[1.4]">{task.goal}</span>
          <span className="block text-[10px] text-slate-600">
            {hasChildren && (
              <span className="text-slate-500">子任务 {node.children.length} · </span>
            )}
            {task.turn != null && task.turn > 0 && <span>t{task.turn} · </span>}
            {relativeTime(task.started_at)}
            {mismatch && (
              <span className="ml-1 text-tone-amber">
                · ⚠ 在 {task.workspace}
              </span>
            )}
          </span>
        </span>
      </button>

      {/* 子任务（折叠时不显示） */}
      {hasChildren && !isCollapsed && (
        <div>
          {node.children.map((child) => (
            <TreeRow
              key={child.task.task_id}
              node={child}
              level={level + 1}
              collapsed={collapsed}
              onToggle={onToggle}
              activeTaskId={activeTaskId}
              sessionWorkspace={sessionWorkspace}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** 状态 → 圆点颜色类（与 ExecutionFlowView.StatusDot 一致） */
function dotClass(status: string): string {
  const map: Record<string, string> = {
    running: "loom-blink bg-tone-cyan",
    awaiting_approval: "bg-tone-amber",
    done: "bg-tone-green",
    aborted: "bg-tone-amber",
    failed: "bg-tone-red",
    created: "bg-slate-600",
  };
  return map[status] ?? "bg-slate-600";
}
