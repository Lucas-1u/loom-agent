import { useLayoutEffect, useRef, useState } from "react";
import { STATUS_TEXT, type AgentTreeNodeData } from "./types";

/**
 * 悬停说明卡片。
 *
 * ★ 为什么是 fixed 定位 + 每次重新量 anchor：
 *   tooltip 挂在节点**内部**会被容器的 overflow 裁掉；挂在页面里用 absolute
 *   又得算滚动偏移。fixed 直接对齐视口，滚动/缩放都不用重算坐标系。
 *
 * ★ 为什么位置要在 useLayoutEffect 里算、而不是拿到 rect 就算：
 *   卡片高度取决于 detail 长短（宽度固定 260，行数不定）。不知道真实高度就
 *   无法判断"下方放不放得下"，只能瞎猜一个值 —— 短说明会飘太远，长说明会
 *   顶出屏幕。先渲染、量到真实宽高、再定位，画的这一帧之前就定好，不会闪。
 *
 * ★ 为什么要"候选位置 + 重叠面积最小"，而不是简单左右翻：
 *   只判视口边界，卡片会盖住隔壁节点的色条；而只做"不重叠"的布尔判断，
 *   在这种密度的树里（3 列叶子只隔 130px，卡片宽 260）四个方向全都会被占满，
 *   最后还是退回右侧压住邻居。所以改成算面积：右 → 左 → 下 → 上 四个候选，
 *   选**与其它节点重叠面积最小**的那个，蹭 1px 和压半个色条不是一回事。
 *   叶子节点额外优先往上放（它的标签在色条下方），往下时则跳过标签高度。
 */
interface Props {
  node: AgentTreeNodeData | null;
  /** 触发悬停的元素；用它的矩形当锚点 */
  anchor: HTMLElement | null;
}

const GAP = 14;   // 卡片与节点的间距
const PAD = 10;   // 与视口边缘的最小距离

export function AgentTreeTooltip({ node, anchor }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!node || !anchor || !ref.current) {
      setPos(null);
      return;
    }
    const card = ref.current;
    const a = anchor.getBoundingClientRect();
    const w = card.offsetWidth;
    const h = card.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const cx = a.left + a.width / 2;
    const cy = a.top + a.height / 2;

    /**
     * 邻居的矩形。★ 为什么直接查 DOM 而不从 layout 传进来：
     *   layout 给的是画布坐标系，这里要的是**视口**坐标（受滚动、缩放影响）。
     *   页面上只有这一棵树，查一次 .at-node 是最短的路径，也不会不同步。
     */
    type Rect = { left: number; top: number; right: number; bottom: number };

    /**
     * 一个节点的"占位矩形" = 色条 ∪ 标签。
     * ★ 为什么要把标签算进去：只挡色条时看起来还行，但标签是**文字**，
     *   被卡片压住比压住色条刺眼得多（实测第一版就压住了「前端分析 Agent」
     *   和「沙箱执行 Agent」两行字）。竖排标签尤其高，不并进来会严重低估占位。
     */
    const rectOf = (el: HTMLElement): Rect => {
      const b = el.getBoundingClientRect();
      const lab = el.querySelector<HTMLElement>(".at-node__label");
      if (!lab) return { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
      const l = lab.getBoundingClientRect();
      return {
        left: Math.min(b.left, l.left),
        top: Math.min(b.top, l.top),
        right: Math.max(b.right, l.right),
        bottom: Math.max(b.bottom, l.bottom),
      };
    };

    const allEls = Array.from(document.querySelectorAll<HTMLElement>(".at-node"));
    const allRects = allEls.map(rectOf);
    const others = allEls.filter((el) => el !== anchor).map(rectOf);
    // 整棵树的最低点：近处全被占满时，往树下方空白处放
    const treeBottom = Math.max(...allRects.map((r) => r.bottom), a.bottom);

    // 自己的标签底部（竖排会伸到色条下方）：往下放时从这里开始，别糊住自己
    const anchorLabel = anchor.querySelector<HTMLElement>(".at-node__label");
    const ownBottom = anchorLabel
      ? Math.max(a.bottom, anchorLabel.getBoundingClientRect().bottom)
      : a.bottom;

    const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, Math.max(lo, hi)));
    /** 卡片放在 (left, top) 时，与其它节点（色条 ∪ 标签）的重叠面积 px² */
    const overlapArea = (left: number, top: number) =>
      others.reduce((sum, r) => {
        const ox = Math.max(0, Math.min(left + w, r.right) - Math.max(left, r.left));
        const oy = Math.max(0, Math.min(top + h, r.bottom) - Math.max(top, r.top));
        return sum + ox * oy;
      }, 0);

    /** 叶子优先往上（它自己的标签在下方）；非叶子往上会压自己的色条，所以先下后上 */
    const isLeaf = node.children.length === 0;
    const vertical = isLeaf
      ? [
          { left: cx - w / 2, top: a.top - h - GAP },
          { left: cx - w / 2, top: ownBottom + GAP },
        ]
      : [
          { left: cx - w / 2, top: ownBottom + GAP },
          { left: cx - w / 2, top: a.top - h - GAP },
        ];

    // 候选：右 → 左 → 上/下 → 树下方空白
    const candidates = [
      { left: a.right + GAP, top: cy - h / 2 },
      { left: a.left - w - GAP, top: cy - h / 2 },
      ...vertical,
      { left: cx - w / 2, top: treeBottom + GAP },
    ];

    /**
     * ★ 为什么按"重叠面积最小"选，而不是"第一个不重叠的"：
     *   这棵树很密（3 列叶子只隔 130px，卡片宽 260），四个方向往往**全都会**
     *   蹭到某个节点。布尔判断在这种情况下只能退回默认位（右侧），结果就是
     *   稳稳压住隔壁色条。改成比面积：蹭 1px 和压住半个色条是两回事，
     *   选面积最小的那个，视觉上基本等于"没挡住"。
     */
    let chosen: { left: number; top: number } | null = null;
    let bestArea = Number.POSITIVE_INFINITY;
    for (const c of candidates) {
      // 先夹到视口内，再用夹过的位置算重叠 —— 否则"被裁掉的边缘"会漏算
      const left = clamp(c.left, PAD, vw - w - PAD);
      const top = clamp(c.top, PAD, vh - h - PAD);
      const area = overlapArea(left, top);
      if (area < bestArea) {
        bestArea = area;
        chosen = { left, top };
      }
      if (area === 0) break;      // 完全不挡，没必要再试
    }
    if (!chosen) {
      chosen = {
        left: clamp(candidates[0].left, PAD, vw - w - PAD),
        top: clamp(candidates[0].top, PAD, vh - h - PAD),
      };
    }

    setPos(chosen);
  }, [node, anchor]);

  if (!node) return null;

  return (
    <div
      ref={ref}
      className="at-tip"
      role="tooltip"
      style={{
        left: pos?.left ?? -9999,
        top: pos?.top ?? -9999,
        // 位置没算好之前先藏住，避免卡片从左上角闪一下
        visibility: pos ? "visible" : "hidden",
      }}
    >
      <div className="at-tip__head">
        <span className="at-tip__label">{node.label}</span>
        <span className="at-tip__status" data-status={node.status}>
          {STATUS_TEXT[node.status]}
        </span>
      </div>
      <p className="at-tip__detail">{node.detail}</p>
    </div>
  );
}
