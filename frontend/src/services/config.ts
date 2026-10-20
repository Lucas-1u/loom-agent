/**
 * ★ 后端地址的唯一来源
 *
 * 任务书要求：「不要把后端地址散落在各个组件里，统一放到 API 配置层，
 * 方便后续切换开发环境和桌面环境。」
 *
 * 所以：**任何组件都不许直接写 URL**，全部从这里取。
 * Phase 3 的 `services/api.ts` / `eventStream.ts` 也只从这里读 `API_BASE`。
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
 * 端点路径集中定义。
 *
 * 与任务书 §9 的参考接口一一对应，但要等 Phase 3 真正实现后
 * 才把这里的"预期路径"变成"真实路径"（见 README 的 Phase 3 说明）。
 */
export const ENDPOINTS = {
  health: "/api/health",
  workspace: "/api/workspace",
  tasks: "/api/tasks",
  task: (id: string) => `/api/tasks/${id}`,
  taskEvents: (id: string) => `/api/tasks/${id}/events`,
  stream: "/api/stream",
  approvalDecision: (id: string) => `/api/approvals/${id}/decision`,
  cancel: (id: string) => `/api/tasks/${id}/cancel`,
} as const;

/** 拼成完整 URL 的小工具（避免各处手写字符串拼接） */
export function url(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * 当前是否处于**演示模式**。
 *
 * Phase 2 没有后端，UI 用 mock 数据渲染。
 * Phase 3 接上后端后，把这里改成 `false`（或由 env 控制），
 * 界面上的 DEMO 徽章会自动消失。
 */
export const DEMO_MODE: boolean = import.meta.env.VITE_DEMO_MODE !== "false";
