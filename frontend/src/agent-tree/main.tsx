import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./agentTree.css";
import { AgentWorkTreePage } from "./AgentWorkTreePage";

/**
 * 「Agent 工作树」页面的独立入口。
 *
 * ★ 为什么不复用 src/main.tsx / App.tsx：
 *   那一套挂着 LoomProvider（会话/任务/SSE 全在里头）。这一页只是"把一棵
 *   静态数据画出来"，拉整个应用状态进来既没必要，也会让"换数据就能重画"
 *   这件事变得不干净。独立入口 = 独立依赖树。
 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AgentWorkTreePage />
  </StrictMode>,
);
