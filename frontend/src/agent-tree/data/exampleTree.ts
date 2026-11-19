import type { AgentTreeNodeData } from "../types";

/**
 * 示例数据 —— **可整体替换**。
 *
 * ★ 为什么放在组件外部、并且导出成数组：
 *   页面组件只负责"把这棵树画出来"，不知道也不关心里面是谁。
 *   将来换成真实数据（后端父子任务）时，只要保持 AgentTreeNodeData 的形状，
 *   改这一个文件即可，渲染层一行都不用动。
 *
 * ★ 为什么是数组而不是单个根：
 *   真实场景里可能有多个并列的根（同一会话里跑了几个互不相关的父任务）。
 *   用数组开头，比"以后再加一层森林支持"要便宜。
 */
export const AGENT_WORK_TREE: AgentTreeNodeData[] = [
  {
    id: "root",
    label: "主 Agent",
    status: "done",
    detail:
      "统筹完成用户目标：分析项目结构并给出改造方案，汇总各子 Agent 结果。",
    children: [
      {
        id: "code-scout",
        label: "代码勘察 Agent",
        status: "done",
        detail: "扫描 backend 目录，梳理模块职责与依赖关系，产出结构清单。",
        children: [
          {
            id: "module-map",
            label: "模块梳理",
            status: "done",
            detail: "归类 core / tools / server 等模块，标注各自职责。",
            children: [],
          },
          {
            id: "dep-check",
            label: "依赖分析",
            status: "done",
            detail: "检查模块 import 方向，确认无反向依赖。",
            children: [],
          },
        ],
      },
      {
        id: "fe-scout",
        label: "前端分析 Agent",
        status: "running",
        detail: "正在检查执行流视图与任务树组件的渲染逻辑。",
        children: [],
      },
      {
        id: "sandbox-scout",
        label: "沙箱执行 Agent",
        status: "failed",
        detail: "尝试启动 Docker 沙箱执行验证，环境不可用，任务中止。",
        children: [],
      },
    ],
  },
];
