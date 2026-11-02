/**
 * 任务树构建 —— 把扁平 Task 列表变成树形结构
 *
 * ★ 为什么单独抽出来？
 *   因为建树是纯逻辑，不碰 DOM / 不碰网络，可以脱离 React 直接测试。
 *   前端历史上因为"树构建"和"渲染"耦合在一起，改缩进就会弄坏数据结构 ——
 *   把纯逻辑抽出来，渲染只负责"拿到树画出来"，改 UI 不动数据。
 *
 * ★ 建树规则：
 *   1. 根任务（parent_task_id 为 null/undefined）作为顶层
 *   2. 子任务挂在父任务下（同一 session_id 内按 parent_task_id 关联）
 *   3. 孤儿任务（parent_task_id 指向不存在的任务）→ 提升为根，
 *      避免整棵子树从界面上消失（数据不完整时仍可见）
 *   4. 按 started_at 升序，保证同一父下子任务按时间排列
 *
 * ★ 性能：单次 O(n)，用 Map 建索引，不用嵌套 find。
 */

import type { Task } from "./types";

export interface TaskTreeNode {
  task: Task;
  children: TaskTreeNode[];
  /** 深度：根=0，子=1，孙=2（用于缩进与连线） */
  depth: number;
}

/**
 * 把扁平任务列表构建成树。
 *
 * @param tasks 该会话的全部任务（扁平）
 * @returns 顶层任务节点列表（每个含 children）
 */
export function buildTaskTree(tasks: Task[]): TaskTreeNode[] {
  // ---- 索引：task_id → Task（只取当前这批的）----
  const byId = new Map<string, Task>();
  for (const t of tasks) byId.set(t.task_id, t);

  // ---- 第一遍：建立 parent → children 映射 ----
  const childrenByParent = new Map<string, Task[]>();
  const roots: Task[] = [];

  for (const t of tasks) {
    const pid = t.parent_task_id;
    if (pid && byId.has(pid) && pid !== t.task_id) {
      // 有有效父节点 → 挂到父下
      const arr = childrenByParent.get(pid) ?? [];
      arr.push(t);
      childrenByParent.set(pid, arr);
    } else {
      // 根任务，或父节点不存在（孤儿）→ 顶层
      roots.push(t);
    }
  }

  // ---- 第二遍：递归构建节点（带 depth）----
  const build = (task: Task, depth: number): TaskTreeNode => {
    const rawChildren = childrenByParent.get(task.task_id) ?? [];
    // 同一父下按开始时间升序
    const sorted = [...rawChildren].sort((a, b) => a.started_at - b.started_at);
    return {
      task,
      depth,
      children: sorted.map((c) => build(c, depth + 1)),
    };
  };

  // 顶层也按时间排序
  const sortedRoots = [...roots].sort((a, b) => a.started_at - b.started_at);
  return sortedRoots.map((r) => build(r, 0));
}

/**
 * 扁平化树（用于需要按树顺序遍历的场景）。
 *
 * @param nodes 顶层节点
 * @returns 深度优先顺序的 Task 列表
 */
export function flattenTree(nodes: TaskTreeNode[]): Task[] {
  const out: Task[] = [];
  const walk = (n: TaskTreeNode[]) => {
    for (const node of n) {
      out.push(node.task);
      if (node.children.length) walk(node.children);
    }
  };
  walk(nodes);
  return out;
}

/**
 * 收集树中所有节点的 task_id（用于判断"某任务是否在某棵树里"）。
 */
export function collectTaskIds(nodes: TaskTreeNode[]): Set<string> {
  const set = new Set<string>();
  const walk = (n: TaskTreeNode[]) => {
    for (const node of n) {
      set.add(node.task.task_id);
      if (node.children.length) walk(node.children);
    }
  };
  walk(nodes);
  return set;
}

/**
 * 统计树的总任务数（含子任务）。
 */
export function countTreeNodes(nodes: TaskTreeNode[]): number {
  let n = 0;
  const walk = (arr: TaskTreeNode[]) => {
    for (const node of arr) {
      n++;
      if (node.children.length) walk(node.children);
    }
  };
  walk(nodes);
  return n;
}
