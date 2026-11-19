import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentTreeNodeView } from "./AgentTreeNodeView";
import { AgentTreeTooltip } from "./AgentTreeTooltip";
import { AGENT_WORK_TREE } from "./data/exampleTree";
import { layoutAgentTree } from "./layout";
import type { AgentTreeNodeData } from "./types";

/**
 * 「Agent 工作树」独立页面。
 *
 * ★ 数据从哪来：只从这里传进来的 `roots`（默认读 data/exampleTree.ts）。
 *   组件内部**不认识**任何具体节点：不写死标签、不写死层数、不写死说明文字。
 *   验收第 4 条（换一组数据要能重新渲染）靠的就是这一点。
 *
 * ★ 为什么要 useMemo：布局是纯计算，但每次 hover 都会触发本组件重渲染。
 *   不缓存的话，鼠标在树上移动时每秒要重算几十次坐标。
 *
 * ★ 悬停为什么要延迟 120ms：鼠标从根划到叶子会经过多个节点，
 *   不延迟的话卡片会连续闪三次。延迟只在"进入"时生效，"离开"立刻收起。
 */
interface Props {
  /** 树的数据源。默认用示例数据，真实接入时换这里即可。 */
  roots?: AgentTreeNodeData[];
}

/** 进入节点后多久弹卡片（毫秒）。够短不觉得迟钝，够长能滤掉划过。 */
const HOVER_DELAY_MS = 120;

export function AgentWorkTreePage({ roots = AGENT_WORK_TREE }: Props) {
  const layout = useMemo(() => layoutAgentTree(roots), [roots]);

  const [hover, setHover] = useState<{ node: AgentTreeNodeData; el: HTMLElement } | null>(null);
  const timer = useRef<number | null>(null);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const handleHover = useCallback(
    (node: AgentTreeNodeData, el: HTMLElement) => {
      clearTimer();
      timer.current = window.setTimeout(() => setHover({ node, el }), HOVER_DELAY_MS);
    },
    [clearTimer],
  );

  const handleLeave = useCallback(() => {
    clearTimer();
    setHover(null);
  }, [clearTimer]);

  // 卸载时清掉挂起的定时器，避免对已卸载组件 setState
  useEffect(() => clearTimer, [clearTimer]);

  // 滚动 / 缩放会让锚点位置失效：直接收起，比让卡片"追着节点跑"更稳
  useEffect(() => {
    const hide = () => setHover(null);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, []);

  return (
    <div className="at-page">
      <header className="at-header">
        <h1 className="at-title">Agent 工作树</h1>
        <p className="at-sub">
          {layout.nodes.length} 个节点 · 悬停查看每个 Agent 做了什么
        </p>
      </header>

      <div className="at-scroll">
        <div
          className="at-canvas"
          style={{ width: layout.width, height: layout.height }}
        >
          {/* 连线画在节点下层：SVG 只负责线段，坐标全部来自 layout */}
          <svg
            className="at-edges"
            width={layout.width}
            height={layout.height}
            aria-hidden="true"
          >
            {layout.edges.map((e) => (
              <line key={e.id} x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} />
            ))}
          </svg>

          {layout.nodes.map((item) => (
            <AgentTreeNodeView
              key={item.node.id}
              item={item}
              onHover={handleHover}
              onLeave={handleLeave}
            />
          ))}
        </div>
      </div>

      <AgentTreeTooltip node={hover?.node ?? null} anchor={hover?.el ?? null} />
    </div>
  );
}
