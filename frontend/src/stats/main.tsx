import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./stats.css";
import { StatsPage } from "./StatsPage";

/**
 * 「会话统计」页面的独立入口。
 *
 * ★ 和 agent-tree 一样是独立 HTML 入口：统计是"偶尔看一眼"的页面，
 *   不应该把主应用的 LoomProvider（会话/事件/SSE）全拉起来。
 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <StatsPage />
  </StrictMode>,
);
