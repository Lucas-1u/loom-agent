import { memo } from "react";
import { NODE_H, NODE_W, type PositionedNode } from "./layout";
import { STATUS_COLOR, type AgentTreeNodeData } from "./types";

/**
 * 单个树节点 = 一根彩色竖条 + 一个标签。
 *
 * ★ 为什么节点要绝对定位、由 layout 算好 left/top：
 *   连线是 SVG，按同一套坐标画的。两者共用坐标系，窗口怎么缩放都不会"线跑偏"。
 *
 * ★ 标签的两种摆法（严格按视觉规格）：
 *   前两层 → 横排、色条正上方（空间充裕，横排更好读）
 *   更深的层 → 竖排（vertical-rl）、色条正下方
 *     竖排是为了在底部一排挤满叶子时，标签不互相压住 —— 横排的 7 字标签
 *     要 112px 宽，底部槽位没有这么宽。
 *   注意这里按 **depth** 判断而不是"是不是叶子"：第二层的叶子（示例里的
 *   「前端分析 Agent」）仍然横排，和规格一致。
 *
 * ★ memo：树是静态数据，hover 只改页面上的一个 tooltip 状态。
 *   不 memo 的话每次 hover 都要把几十个节点全部重渲染一遍。
 */
interface Props {
  item: PositionedNode;
  onHover: (node: AgentTreeNodeData, el: HTMLElement) => void;
  onLeave: () => void;
}

function AgentTreeNodeViewInner({ item, onHover, onLeave }: Props) {
  const { node, depth } = item;
  const labelOnTop = depth <= 1;

  return (
    <div
      className={`at-node ${labelOnTop ? "at-node--label-top" : "at-node--label-bottom"}`}
      style={{ left: item.cx - NODE_W / 2, top: item.y, width: NODE_W, height: NODE_H }}
      onMouseEnter={(e) => onHover(node, e.currentTarget)}
      onMouseLeave={onLeave}
      // 键盘可达：既是无障碍，也方便逐节点检查 tooltip
      tabIndex={0}
      onFocus={(e) => onHover(node, e.currentTarget)}
      onBlur={onLeave}
      aria-label={`${node.label}（${node.status}）`}
    >
      <span className="at-node__label">{node.label}</span>
      <span
        className="at-node__bar"
        data-status={node.status}
        style={{ background: STATUS_COLOR[node.status] }}
      />
    </div>
  );
}

export const AgentTreeNodeView = memo(AgentTreeNodeViewInner);
