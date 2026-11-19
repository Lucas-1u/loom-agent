/**
 * 父子 Agent 工作树的数据契约。
 *
 * ★ 为什么单独抽一个类型文件：
 *   这棵树将来要接真实的父子 Agent 数据（后端 subtask / spawn_parallel 的结果），
 *   渲染层只认这几个字段。字段定义在这里，数据侧和视图侧各改各的、互不影响。
 *
 * ★ 为什么 children 是必填（而不是可选）：
 *   让"叶子"和"忘了填"在类型层面就区分开。数据侧要表达叶子就写 `[]`，
 *   渲染层不必到处写 `node.children?.length ?? 0`。
 */

export type AgentNodeStatus = "done" | "running" | "failed" | "created";

export interface AgentTreeNodeData {
  /** 唯一标识；也是 React key 与连线 id 的来源 */
  id: string;
  /** 短标签，显示在色条旁（建议 4–8 字） */
  label: string;
  /** 状态：决定色条颜色 */
  status: AgentNodeStatus;
  /** 详细说明（悬停时显示）：这个 Agent 做了什么、结果如何 */
  detail: string;
  /** 子节点；没有就传空数组 */
  children: AgentTreeNodeData[];

  /**
   * 预留扩展位。真实数据接进来时常会带 token 数、耗时、模型名、工具调用数…
   * 这些不进当前渲染，但不阻止数据侧先带着走，避免为了加一个字段就改契约。
   */
  meta?: Record<string, unknown>;
}

/**
 * 状态 → 颜色。渲染层唯一的色彩来源，改配色只改这里。
 *
 * ★ 为什么不是规格里那四个深色值（#2E8B2E / #2E86C1 / #C0392B / #95A5A6）：
 *   那是给浅色底准备的。这一页现在跑在 ink-950（#0a0c11）上，
 *   深绿/深红在深底上对比度极低，色条会"糊"进背景。
 *   这里改用主应用 index.css 的语义色（tone-green / blue / red / slate），
 *   **语义完全不变**：绿=完成、蓝=进行、红=失败、灰=未开始，只调了明度。
 */
export const STATUS_COLOR: Record<AgentNodeStatus, string> = {
  done: "#4ade80",     // tone-green
  running: "#5b9dff",  // tone-blue
  failed: "#f87272",   // tone-red
  created: "#8b9bb4",  // tone-slate
};

/** 状态 → 中文名（给悬停卡片用，免得在组件里硬编码中文）。 */
export const STATUS_TEXT: Record<AgentNodeStatus, string> = {
  done: "已完成",
  running: "进行中",
  failed: "已失败",
  created: "已创建",
};
