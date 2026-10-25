/**
 * ★ 后端地址的唯一来源
 *
 * 任务书要求：「不要把后端地址散落在各个组件里，统一放到 API 配置层，
 * 方便后续切换开发环境和桌面环境。」
 *
 * 所以：**任何组件都不许直接写 URL**，全部从这里取。
 * `services/api.ts` / `services/eventStream.ts` 也只从这里读 `API_BASE`。
 *
 * 三种运行环境的差异：
 *   · 浏览器开发（vite dev）      → http://127.0.0.1:8765
 *   · Tauri 桌面（Phase 6）        → 同样是 127.0.0.1，但端口可能由 sidecar 动态分配
 *                                    → 到时候通过 `VITE_API_BASE` 或 Tauri 的
 *                                      invoke 把端口注入进来，**只改这一个文件**
 *   · 远程调试                     → 设 VITE_API_BASE 环境变量覆盖
 */

const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_PORT = 8765;

/**
 * 优先级：
 *   1. VITE_API_BASE（构建/运行时注入）
 *   2. 默认 http://127.0.0.1:8765  ← 与 backend/config.py 的 ServerConfig 一致
 */
export const API_BASE: string = (
  (import.meta.env.VITE_API_BASE as string | undefined) ?? `http://${DEFAULT_HOST}:${DEFAULT_PORT}`
).replace(/\/+$/, "");

/**
 * 端点路径集中定义 —— **与 `backend/server/app.py` 的实际路由一一对应**。
 *
 * ★ 曾经有个 `workspace: "/api/workspace"` 是 Phase 1 的规划遗留，
 *   后端从来没实现过。已删除。改这个文件前请先 grep 后端 @app 路由。
 */
export const ENDPOINTS = {
  health: "/api/health",

  // ---- Session ----
  sessions: "/api/sessions",
  session: (id: string) => `/api/sessions/${id}`,
  sessionMessages: (id: string) => `/api/sessions/${id}/messages`,

  // ---- Task ----
  tasks: "/api/tasks",
  task: (id: string) => `/api/tasks/${id}`,
  taskEvents: (id: string) => `/api/tasks/${id}/events`,
  cancel: (id: string) => `/api/tasks/${id}/cancel`,

  // ---- 审批 ----
  approvalDecision: (id: string) => `/api/approvals/${id}/decision`,

  // ---- SSE ----
  stream: "/api/stream",
} as const;

/** 拼成完整 URL 的小工具（避免各处手写字符串拼接） */
export function url(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * 是否允许**手动**进入演示模式。
 *
 * ★ 真实后端优先：应用启动先打 `/api/health`，
 *   连不上才显示「后端未启动」+ 重试 + 「查看演示数据」按钮。
 *   **绝不自动 fallback 到 mock** —— 那会让界面看起来能用，
 *   实际上什么都没跑，是最容易误导人的一种"友好"。
 *
 * 需要彻底禁用演示入口时设 `VITE_DEMO_DISABLED=true`。
 */
export const DEMO_ALLOWED: boolean = import.meta.env.VITE_DEMO_DISABLED !== "true";
