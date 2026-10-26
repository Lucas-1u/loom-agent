/**
 * 事件 → 视觉样式 的唯一映射表
 *
 * ★ 铁律：**同一个事件类型，在整个应用里永远是同一套颜色 + 图标 + 标签。**
 *   所以所有配色判断都收在这一个文件里。组件只管查表，
 *   不许写 `if (type === "tool.result" && !ok) className = "text-red-500"` 这种。
 *
 * 为什么用「色调（Tone）」而不是直接写颜色类？
 *   因为语义比色值稳定。将来把 green 调成别的绿，只改 index.css 一处，
 *   所有组件自动跟着变 —— 而组件里只写 `tone: "green"`。
 *
 * ★ 注意 Tailwind 的一个硬约束：**它靠扫描源码里的完整类名字符串来生成 CSS**，
 *   不支持运行时拼接（`bg-${tone}/10` 会失效）。所以下面的类名必须写成字面量。
 */

import type { EventType } from "../types/events";
import type { EventEnvelope } from "../types/events";

/** 语义色调 —— 只有这 8 种，避免颜色失控 */
export type Tone =
  | "blue"    // 用户输入 / 任务起点
  | "violet"  // 计划、任务分解
  | "slate"   // 普通执行状态
  | "cyan"    // 工具调用
  | "green"   // 成功、已完成
  | "red"     // 失败、被拒绝
  | "amber"   // 需要人注意（等待审批）
  | "teal";   // 最终答复

export interface EventVisualStyle {
  /** 中文标签（事件本身不带人话，这里补上） */
  label: string;
  tone: Tone;
  /** 图标只用几何符号，不依赖 emoji 字体渲染 */
  icon: string;
  /** 重要节点：时间线上用更大的实心圆点 */
  emphasis?: boolean;
}

/** 静态映射：一种类型一套样式 */
export const EVENT_STYLES: Record<EventType, EventVisualStyle> = {
  "task.start":        { label: "任务开始", tone: "blue",   icon: "▶", emphasis: true },
  "task.end":          { label: "任务结束", tone: "green",  icon: "■", emphasis: true },
  plan:                { label: "计划",     tone: "violet", icon: "◆", emphasis: true },
  thinking:            { label: "思考",     tone: "slate",  icon: "…" },
  act:                 { label: "决定行动", tone: "slate",  icon: "→" },
  observe:             { label: "观察",     tone: "slate",  icon: "◎" },
  "tool.call":         { label: "工具调用", tone: "cyan",   icon: "⚙" },
  "tool.result":       { label: "工具结果", tone: "green",  icon: "✓" },
  "approval.request":  { label: "等待审批", tone: "amber",  icon: "!", emphasis: true },
  "approval.decision": { label: "审批结果", tone: "green",  icon: "✓" },
  "approval.expired":  { label: "审批超时", tone: "red",    icon: "◷" },
  reflect:             { label: "复盘",     tone: "slate",  icon: "↻" },
  final_answer:        { label: "最终回答", tone: "teal",   icon: "★", emphasis: true },
  error:               { label: "错误",     tone: "red",    icon: "✕", emphasis: true },
  "budget.exceeded":   { label: "预算耗尽", tone: "red",    icon: "⊘", emphasis: true },
  "context.compacted": { label: "上下文压缩", tone: "violet", icon: "⤓" },
  "state.snapshot":    { label: "状态快照", tone: "slate",  icon: "▤" },
};

/**
 * 动态样式：同一类型、不同载荷 → 不同视觉。
 *
 * 只有三种情况需要动态：
 *   tool.result     成功 / 失败
 *   approval.decision  批准 / 拒绝
 *   task.end        完成 / 中止 / 失败
 *
 * 其余一律走静态表。
 */
export function resolveStyle(
  ev: Pick<EventEnvelope, "type" | "data">,
): EventVisualStyle {
  const base = EVENT_STYLES[ev.type] ?? {
    label: ev.type,
    tone: "slate" as Tone,
    icon: "·",
  };
  const d = (ev.data ?? {}) as Record<string, unknown>;

  if (ev.type === "tool.result") {
    return d.ok
      ? base
      : { ...base, label: "工具失败", tone: "red", icon: "✕" };
  }

  if (ev.type === "approval.decision") {
    return d.approved
      ? { ...base, label: "已批准", tone: "green", icon: "✓" }
      : { ...base, label: "已拒绝", tone: "red", icon: "✕" };
  }

  if (ev.type === "task.end") {
    const st = String(d.status ?? "");
    if (st === "done") return { ...base, label: "任务完成", tone: "green", icon: "■" };
    if (st === "aborted") return { ...base, label: "任务中止", tone: "amber", icon: "■" };
    return { ...base, label: "任务失败", tone: "red", icon: "■" };
  }

  return base;
}

/**
 * 色调 → Tailwind 类名（字面量，供 Tailwind 扫描）
 *
 * 每一项都是完整类名，不拼接。
 */
export const TONE: Record<
  Tone,
  { text: string; dot: string; ring: string; soft: string; border: string }
> = {
  blue: {
    text: "text-tone-blue",
    dot: "bg-tone-blue",
    ring: "ring-tone-blue/40",
    soft: "bg-tone-blue/10",
    border: "border-tone-blue/30",
  },
  violet: {
    text: "text-tone-violet",
    dot: "bg-tone-violet",
    ring: "ring-tone-violet/40",
    soft: "bg-tone-violet/10",
    border: "border-tone-violet/30",
  },
  slate: {
    text: "text-tone-slate",
    dot: "bg-tone-slate",
    ring: "ring-tone-slate/40",
    soft: "bg-tone-slate/8",
    border: "border-tone-slate/25",
  },
  cyan: {
    text: "text-tone-cyan",
    dot: "bg-tone-cyan",
    ring: "ring-tone-cyan/40",
    soft: "bg-tone-cyan/10",
    border: "border-tone-cyan/30",
  },
  green: {
    text: "text-tone-green",
    dot: "bg-tone-green",
    ring: "ring-tone-green/40",
    soft: "bg-tone-green/10",
    border: "border-tone-green/30",
  },
  red: {
    text: "text-tone-red",
    dot: "bg-tone-red",
    ring: "ring-tone-red/40",
    soft: "bg-tone-red/10",
    border: "border-tone-red/35",
  },
  amber: {
    text: "text-tone-amber",
    dot: "bg-tone-amber",
    ring: "ring-tone-amber/40",
    soft: "bg-tone-amber/10",
    border: "border-tone-amber/35",
  },
  teal: {
    text: "text-tone-teal",
    dot: "bg-tone-teal",
    ring: "ring-tone-teal/40",
    soft: "bg-tone-teal/10",
    border: "border-tone-teal/30",
  },
};
