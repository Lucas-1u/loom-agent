/**
 * ★★★ 演示数据（DEMO）—— 不是真实 Agent 执行记录 ★★★
 *
 * Phase 2 只有界面、没有后端。这里的事件**全部是手写的假数据**，
 * 目的只有一个：让执行流视图能看到「真实事件长什么样」以及配色是否合理。
 *
 * 三层防误认：
 *   1. `seq` 从 1000 起（真实事件从 1 起）—— 一眼能看出不是真跑出来的
 *   2. store 里带 `demo: true`
 *   3. 界面上常驻一个 DEMO 徽章（见 AppHeader）
 *
 * Phase 3 接上后端后，这个文件只在 `VITE_DEMO_MODE=false` 时不被使用；
 * 保留它是为了「没有 Python 也能看界面」这个场景（比如纯前端调样式）。
 */

import type { EventEnvelope, EventType, TaskStatus } from "../types/events";
import type { ChatMessage, TaskSummary } from "../types/chat";
import { DEMO_TASK_ID, DEMO_WORKSPACE } from "./demoConstants";

// --------------------------------------------------------------------------- //
// 构造辅助
// --------------------------------------------------------------------------- //
let _seq = 1000;
let _ts = Date.now() - 42_000;          // 假装 42 秒前开始
const _taskId = DEMO_TASK_ID;

function ev<T = Record<string, unknown>>(
  type: EventType,
  data: T,
  turn = 0,
  gapMs = 900,
): EventEnvelope<T> {
  _seq += 1;
  _ts += gapMs;
  return { seq: _seq, ts: _ts / 1000, task_id: _taskId, turn, type, data };
}

export const DEMO_GOAL =
  "分析当前项目的结构，并给 README 补一节「快速开始」";

// --------------------------------------------------------------------------- //
// 事件流：模拟一次真实的「分析 → 改文件」任务
//
// 刻意包含三条会用到"非默认颜色"的路径，用来验证配色映射：
//   · 一次 tool.result 失败（红色）
//   · 一次 approval.request → decision（琥珀 → 绿色）
//   · 最后 task.end done（绿色）
// --------------------------------------------------------------------------- //
export const DEMO_EVENTS: EventEnvelope[] = [
  ev("task.start", {
    goal: DEMO_GOAL,
    workspace: DEMO_WORKSPACE,
    model: "qwen3.7-plus",
  }, 0, 0),

  ev("thinking", {
    text: "先看目录结构，再读入口文件和 README，最后决定补哪一节。",
    phase: "plan",
    done: true,
  }, 0, 1200),

  ev("plan", {
    steps: [
      { title: "查看项目根目录", detail: "list_files 拿到顶层结构", status: "done" },
      { title: "读取入口与 README", detail: "read_file 看实际内容", status: "done" },
      { title: "给 README 补「快速开始」", detail: "edit_file 插入一节", status: "doing" },
    ],
    note: "先只读观察，再动文件。",
  }, 1, 900),

  ev("act", { intent: "先列出目录，确认项目结构", tool: "list_files" }, 1, 700),
  ev("tool.call", {
    call_id: "call_1",
    tool: "list_files",
    args: { path: "." },
  }, 1, 200),
  ev("tool.result", {
    call_id: "call_1",
    tool: "list_files",
    ok: true,
    summary: "工作区根目录下有 3 个目录、7 个文件",
    data: {
      entries: [
        { name: "src", type: "dir" },
        { name: "tests", type: "dir" },
        { name: "docs", type: "dir" },
        { name: "README.md", type: "file", size: 1284 },
        { name: "pyproject.toml", type: "file", size: 512 },
      ],
    },
    error: null,
    duration_ms: 7,
  }, 1, 500),

  ev("observe", { call_id: "call_1", digest: "有 src/、tests/、docs/ 和 README.md" }, 1, 300),
  ev("act", { intent: "读 README，看它现在写到哪一步了", tool: "read_file" }, 1, 800),
  ev("tool.call", {
    call_id: "call_2",
    tool: "read_file",
    args: { path: "docs/README.md" },   // ← 故意给错路径，制造一次真实的失败
  }, 1, 200),

  // ★ 失败路径：用来验证 tool.result 的红色样式
  ev("tool.result", {
    call_id: "call_2",
    tool: "read_file",
    ok: false,
    summary: "文件不存在：docs/README.md",
    error: {
      kind: "not_found",
      message: "文件不存在：docs/README.md",
      hint: "可以先用 list_files 确认实际路径。",
      retryable: true,
      security: false,
    },
    duration_ms: 3,
  }, 2, 400),

  ev("act", { intent: "路径写错了，README 在根目录", tool: "read_file" }, 2, 900),
  ev("tool.call", {
    call_id: "call_3",
    tool: "read_file",
    args: { path: "README.md" },
  }, 2, 200),
  ev("tool.result", {
    call_id: "call_3",
    tool: "read_file",
    ok: true,
    summary: "读取 README.md（24 行，1.3 KB）",
    data: {
      content: "# Demo Project\n\n一个用于演示的最小项目。\n\n## 安装\n\n```bash\npip install -e .\n```\n",
    },
    error: null,
    duration_ms: 5,
  }, 2, 500),

  ev("reflect", {
    progress: "已经看清结构，README 只有「安装」一节，缺「快速开始」",
    next_action: "在「安装」之后插入一节「快速开始」",
    done: false,
  }, 2, 700),

  // ★ 审批路径：琥珀 → 绿色
  ev("approval.request", {
    approval_id: "apr_demo0001",
    tool: "edit_file",
    args: { path: "README.md", old_string: "## 安装", new_string: "## 快速开始\n..." },
    args_hash: "3f9a1c22d4b7e880",
    risk: "write",
    action: "修改文件",
    target: "README.md",
    impact: "将修改 **1 处**：3 行 → 8 行（+5 行）。原内容会先备份到 .loom_backup/，可手工还原。",
    preview:
      "- ## 安装\n+ ## 快速开始\n+ \n+ ```bash\n+ python -m pip install -e .\n+ python -m demo\n+ ```\n+ \n+ ## 安装",
    turn: 3,
    created_at: Date.now() / 1000,
  }, 3, 600),

  ev("approval.decision", {
    approved: true,
    approval_id: "apr_demo0001",
    status: "approved",
    reason: "",
    waited_s: 4.2,
  }, 3, 4200),

  ev("act", { intent: "已获批准，开始改 README", tool: "edit_file" }, 3, 500),
  ev("tool.call", {
    call_id: "call_4",
    tool: "edit_file",
    args: {
      path: "README.md",
      old_string: "## 安装",
      new_string: "## 快速开始\n\n```bash\npython -m pip install -e .\npython -m demo\n```\n\n## 安装",
    },
  }, 3, 200),
  ev("tool.result", {
    call_id: "call_4",
    tool: "edit_file",
    ok: true,
    summary: "修改 README.md（第 7 行，+5 行）；原内容已备份到 .loom_backup/20261020-223000/README.md",
    data: { path: "README.md", action: "edit", replaced: 1, added_lines: 8, removed_lines: 3 },
    error: null,
    duration_ms: 11,
  }, 3, 600),

  ev("reflect", {
    progress: "README 已补上「快速开始」，任务目标达成",
    next_action: "",
    done: true,
  }, 3, 800),

  ev("final_answer", {
    text: [
      "已给 `README.md` 补上「快速开始」一节，插在「安装」之前。",
      "",
      "**改动内容**",
      "",
      "```bash",
      "python -m pip install -e .",
      "python -m demo",
      "```",
      "",
      "**项目结构**（顺带说明）",
      "",
      "| 目录 | 用途 |",
      "| --- | --- |",
      "| `src/` | 源码 |",
      "| `tests/` | 测试 |",
      "| `docs/` | 文档 |",
      "",
      "原文件已自动备份到 `.loom_backup/`，不满意可以手工还原。",
    ].join("\n"),
  }, 3, 1400),

  ev("task.end", {
    status: "done" as TaskStatus,
    answer: "已给 README 补上「快速开始」一节。",
    reason: "agent_done",
  }, 3, 300),
];

// --------------------------------------------------------------------------- //
// 派生数据（和真实链路走同一套投影逻辑，见 features/task/selectors.ts）
// --------------------------------------------------------------------------- //
export const DEMO_TASK: TaskSummary = {
  id: _taskId,
  goal: DEMO_GOAL,
  status: "done",
  startedAt: DEMO_EVENTS[0].ts,
  endedAt: DEMO_EVENTS[DEMO_EVENTS.length - 1].ts,
  endReason: "agent_done",
  turn: 3,
};

/** 仅用于「刷新页面后聊天视图不空」的兜底（真实逻辑见 selectors） */
export const DEMO_MESSAGES: ChatMessage[] = [
  {
    id: `m_${DEMO_TASK_ID}_user`,
    role: "user",
    text: DEMO_GOAL,
    ts: DEMO_EVENTS[0].ts,
    sourceSeq: DEMO_EVENTS[0].seq,
    taskId: DEMO_TASK_ID,
  },
  {
    id: `m_${DEMO_TASK_ID}_assistant`,
    role: "assistant",
    text: String(
      (DEMO_EVENTS.find((e) => e.type === "final_answer")?.data as { text?: string })?.text ?? "",
    ),
    ts: DEMO_EVENTS[DEMO_EVENTS.length - 2].ts,
    sourceSeq: DEMO_EVENTS[DEMO_EVENTS.length - 2].seq,
    taskId: DEMO_TASK_ID,
  },
];
