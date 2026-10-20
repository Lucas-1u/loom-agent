import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { TaskProvider } from "./features/task/store";

/**
 * 入口
 *
 * ★ TaskProvider 在最外层：整个应用只有**一份**任务状态。
 *   两个视图都从它读 —— 这是「切换视图不丢数据」的结构性保证。
 */
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TaskProvider>
      <App />
    </TaskProvider>
  </StrictMode>,
);
