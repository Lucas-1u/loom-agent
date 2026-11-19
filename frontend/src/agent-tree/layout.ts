import type { AgentTreeNodeData } from "./types";

/**
 * 树 → 坐标的纯函数（不碰 DOM、不碰 React）。
 *
 * ★ 为什么要单独算一遍，而不是用 CSS flex 排：
 *   连线要"从父色条底边中点连到子色条顶边中点"，这要求**事先知道每个节点的
 *   精确坐标**。flex 排完再去量 DOM 也能做，但会多一次 layout 回读、
 *   窗口缩放还要重算。纯函数算完，SVG 和节点用的是同一套数字，缩放不会错位。
 *
 * ★ 布局规则（经典 tidy tree 的简化版）：
 *   1. 叶子按顺序各占一个水平槽位；
 *   2. 父节点的中心 = 第一个子与最后一个子中心的中点；
 *   3. 整棵树按包围盒水平居中。
 *   所以兄弟多的一侧会自然变宽，父节点居中于自己的子树（这也是"扇形连线"
 *   能均匀展开的原因）。层数、分支数都不做假设。
 */

/** 色条尺寸（与视觉规格一致） */
export const NODE_W = 32;
export const NODE_H = 80;

/**
 * 相邻叶子的中心距。
 * ★ 取 130 而不是"刚够放下竖排叶子"的更小值：第二层（及以上）是**横排**标签，
 *   7 个字 × 16px ≈ 112px，一旦中心距小于它，相邻标签就会叠在一起。
 *   竖排的叶子反而不占宽度，所以这个值是被横排层撑出来的。
 */
export const COL_GAP = 130;

/**
 * 相邻层的中心距。
 * 80（条高）+ 29（上方横排标签）+ 78（层间留白，要能放下叶子竖排标签）。
 */
export const ROW_H = 190;

/** 画布留白：左右要能放下横排标签，顶部要放得下根的标签，底部要放得下竖排叶子。 */
export const PAD_X = 96;
export const PAD_TOP = 76;
export const PAD_BOTTOM = 140;

/** 多棵树之间的水平间隔（以槽位为单位，1 槽 = COL_GAP） */
const ROOT_GAP_SLOTS = 1;

export interface PositionedNode {
  node: AgentTreeNodeData;
  /** 第几层，0 = 根 */
  depth: number;
  /** 色条中心 x（渲染时 left = cx - NODE_W/2） */
  cx: number;
  /** 色条顶边 y */
  y: number;
  /** 有没有子节点（决定标签横排还是竖排） */
  isLeaf: boolean;
}

export interface TreeEdge {
  id: string;
  /** 父色条底边中点 */
  x1: number;
  y1: number;
  /** 子色条顶边中点 */
  x2: number;
  y2: number;
}

export interface TreeLayout {
  nodes: PositionedNode[];
  edges: TreeEdge[];
  /** 画布尺寸（px），直接给容器和 SVG 用 */
  width: number;
  height: number;
}

/**
 * 计算整棵（或整片）树的布局。
 *
 * 传数组是为了支持多根：同一会话里可能有几个互不相关的父任务。
 * 单根就传 `[root]`。
 */
export function layoutAgentTree(roots: AgentTreeNodeData[]): TreeLayout {
  const nodes: PositionedNode[] = [];
  const edges: TreeEdge[] = [];
  const cursor = { slot: 0 };
  let maxDepth = 0;

  /** 递归：返回本节点的 (中心 x, 顶边 y, id) 供父节点连线和居中 */
  const walk = (
    node: AgentTreeNodeData,
    depth: number,
  ): { cx: number; y: number; id: string } => {
    const kids = node.children ?? [];
    const anchors: { cx: number; y: number; id: string }[] = [];

    if (kids.length === 0) {
      // 叶子：领一个槽位
      const cx = cursor.slot * COL_GAP;
      cursor.slot += 1;
      const y = PAD_TOP + depth * ROW_H;
      maxDepth = Math.max(maxDepth, depth);
      nodes.push({ node, depth, cx, y, isLeaf: true });
      return { cx, y, id: node.id };
    }

    for (const kid of kids) {
      anchors.push(walk(kid, depth + 1));
    }

    // 父居中于"第一个子 ↔ 最后一个子"的中点
    const cx = (anchors[0].cx + anchors[anchors.length - 1].cx) / 2;
    const y = PAD_TOP + depth * ROW_H;
    maxDepth = Math.max(maxDepth, depth);

    for (const a of anchors) {
      edges.push({
        id: `${node.id}->${a.id}`,
        x1: cx,
        y1: y + NODE_H,
        x2: a.cx,
        y2: a.y,
      });
    }

    nodes.push({ node, depth, cx, y, isLeaf: false });
    return { cx, y, id: node.id };
  };

  for (const root of roots) {
    walk(root, 0);
    cursor.slot += ROOT_GAP_SLOTS;
  }

  if (nodes.length === 0) {
    return { nodes: [], edges: [], width: PAD_X * 2, height: PAD_TOP + PAD_BOTTOM };
  }

  // ---- 水平居中：把包围盒挪到留白之内 ----
  const centers = nodes.map((n) => n.cx);
  const minX = Math.min(...centers) - NODE_W / 2;
  const maxX = Math.max(...centers) + NODE_W / 2;
  const offsetX = PAD_X - minX;

  for (const n of nodes) n.cx += offsetX;
  for (const e of edges) {
    e.x1 += offsetX;
    e.x2 += offsetX;
  }

  return {
    nodes,
    edges,
    width: maxX - minX + PAD_X * 2,
    height: PAD_TOP + maxDepth * ROW_H + NODE_H + PAD_BOTTOM,
  };
}
