import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 自动滚动 hook（聊天视图和执行流共用）
 *
 * ★ 只做一件有难度的事：**区分「用户停在底部」和「用户正在往上翻」**。
 *
 *   如果无脑 `scrollTop = scrollHeight`，用户想回看历史时会被不断拽回底部 ——
 *   这是聊天界面最招人烦的 bug 之一。
 *
 * 用法：
 *   const { ref, onScroll, showJump, scrollToBottom, newCount } = useAutoScroll<HTMLDivElement>(events.length);
 */
const THRESHOLD = 40;      // 距离底部多少 px 内算"在底部"

export function useAutoScroll<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const lastDep = useRef<unknown>(dep);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const nowBottom = distance <= THRESHOLD;
    setAtBottom(nowBottom);
    if (nowBottom) setNewCount(0);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const el = ref.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    setAtBottom(true);
    setNewCount(0);
  }, []);

  useEffect(() => {
    const el = ref.current;
    const grew = Number(dep) - Number(lastDep.current);
    lastDep.current = dep;

    if (!el) return;

    if (atBottom) {
      // 等 DOM 真正长高再滚，否则滚到的是旧高度
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      });
    } else if (grew > 0) {
      setNewCount((n) => n + grew);
    }
  }, [dep, atBottom]);

  return { ref, onScroll, atBottom, showJump: !atBottom, scrollToBottom, newCount };
}
