/**
 * 演示模式用的固定常量。
 *
 * 单独一个文件是为了避免 `mock.ts` 和 `features/task/store.ts` 互相 import
 * 造成的循环依赖 —— 两边都从这里取。
 */

export const DEMO_TASK_ID = "task_demo0001";

/** 演示用工作区路径（Windows 风格，和真实使用一致） */
export const DEMO_WORKSPACE = "D:\\Projects\\demo-app";

export const DEMO_MODEL = "qwen3.7-plus";
