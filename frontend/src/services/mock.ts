/**
 * ★★★ 演示数据（DEMO）—— 不是真实 Agent 执行记录 ★★★
 *
 * Phase 2 只有界面、没有后端。这里的数据**全部是手写的假数据**，
 * 目的是让 UI 能看到真实的数据形状：多会话 / 多任务 / 多工作区分组。
 *
 * 三层防误认：
 *   1. `seq` 从 1000 起（真实事件从 1 起）
 *   2. store 里带 `demo: true`
 *   3. 顶栏常驻 DEMO 徽章
 *
 * ★ 刻意包含的边界场景：
 *   · 同一工作区里的两个会话            → 验证分组聚合
 *   · `D:\Projects\demo-app` 与
 *     `E:\Experiments\demo-app`        → 验证「同名不同盘不合并」
 *   · 空工作区的会话                    → 验证「未设置工作区」独立分组
 *   · 一个会话里两个任务                → 验证执行流的 Task 切换
 */

import type { EventEnvelope, EventType, TaskStatus } from "../types/events";
import type { Message, Session, Task } from "../features/session/types";

let _seq = 1000;

function ev(
  taskId: string,
  type: EventType,
  data: Record<string, unknown>,
  turn = 0,
  ts?: number,
): EventEnvelope {
  _seq += 1;
  return {
    seq: _seq,
    ts: ts ?? 0,
    task_id: taskId,
    turn,
    type,
    data,
  };
}

/** 给一批事件统一打时间戳（相对 base 递增） */
function stamp(list: EventEnvelope[], base: number, gap = 1.2): EventEnvelope[] {
  return list.map((e, i) => ({ ...e, ts: base + i * gap }));
}

// --------------------------------------------------------------------------- //
// 任务 A1：完整流程（分析 + 改文件，含一次失败和一次审批）
// --------------------------------------------------------------------------- //
function taskA1Events(taskId: string, base: number): EventEnvelope[] {
  const raw: EventEnvelope[] = [
    ev(taskId, "task.start", {
      goal: "分析当前项目的结构，并给 README 补一节「快速开始」",
      workspace: "D:\\Loom_agent\\loom",
      model: "qwen3.7-plus",
    }),
    ev(taskId, "thinking", {
      text: "先看目录结构，再读入口文件和 README，最后决定补哪一节。",
      phase: "plan",
      done: true,
    }),
    ev(taskId, "plan", {
      steps: [
        { title: "查看项目根目录", detail: "list_files 拿到顶层结构", status: "done" },
        { title: "读取入口与 README", detail: "read_file 看实际内容", status: "done" },
        { title: "给 README 补「快速开始」", detail: "edit_file 插入一节", status: "done" },
      ],
      note: "先只读观察，再动文件。",
    }, 1),
    ev(taskId, "act", { intent: "先列出目录，确认项目结构", tool: "list_files" }, 1),
    ev(taskId, "tool.call", { call_id: "call_1", tool: "list_files", args: { path: "." } }, 1),
    ev(taskId, "tool.result", {
      call_id: "call_1",
      tool: "list_files",
      ok: true,
      summary: "工作区根目录下有 3 个目录、7 个文件",
      data: { entries: [{ name: "src", type: "dir" }, { name: "README.md", type: "file", size: 1284 }] },
      error: null,
      duration_ms: 7,
    }, 1),
    ev(taskId, "observe", { call_id: "call_1", digest: "有 src/、tests/、docs/ 和 README.md" }, 1),
    ev(taskId, "act", { intent: "读 README，看它现在写到哪一步了", tool: "read_file" }, 1),
    ev(taskId, "tool.call", {
      call_id: "call_2", tool: "read_file", args: { path: "docs/README.md" },
    }, 1),
    // ★ 失败路径（红色）
    ev(taskId, "tool.result", {
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
    }, 2),
    ev(taskId, "act", { intent: "路径写错了，README 在根目录", tool: "read_file" }, 2),
    ev(taskId, "tool.call", { call_id: "call_3", tool: "read_file", args: { path: "README.md" } }, 2),
    ev(taskId, "tool.result", {
      call_id: "call_3",
      tool: "read_file",
      ok: true,
      summary: "读取 README.md（24 行，1.3 KB）",
      data: { content: "# Demo Project\n\n一个用于演示的最小项目。\n" },
      error: null,
      duration_ms: 5,
    }, 2),
    ev(taskId, "reflect", {
      progress: "已经看清结构，README 只有「安装」一节，缺「快速开始」",
      next_action: "在「安装」之后插入一节「快速开始」",
      done: false,
    }, 2),
    // ★ 审批路径（琥珀 → 绿）
    ev(taskId, "approval.request", {
      approval_id: "apr_demo_a1",
      tool: "edit_file",
      args: { path: "README.md" },
      args_hash: "3f9a1c22d4b7e880",
      risk: "write",
      action: "修改文件",
      target: "README.md",
      impact: "将修改 **1 处**：3 行 → 8 行（+5 行）。原内容会先备份到 .loom_backup/。",
      preview: "- ## 安装\n+ ## 快速开始\n+ \n+ ```bash\n+ python -m pip install -e .\n+ python -m demo\n+ ```\n+ \n+ ## 安装",
      turn: 3,
      created_at: base,
    }, 3),
    ev(taskId, "approval.decision", {
      approved: true, approval_id: "apr_demo_a1", status: "approved", reason: "", waited_s: 4.2,
    }, 3),
    ev(taskId, "act", { intent: "已获批准，开始改 README", tool: "edit_file" }, 3),
    ev(taskId, "tool.call", {
      call_id: "call_4",
      tool: "edit_file",
      args: { path: "README.md", old_string: "## 安装", new_string: "## 快速开始\n…\n## 安装" },
    }, 3),
    ev(taskId, "tool.result", {
      call_id: "call_4",
      tool: "edit_file",
      ok: true,
      summary: "修改 README.md（第 7 行，+5 行）；原内容已备份到 .loom_backup/20261020-223000/README.md",
      data: { path: "README.md", action: "edit", replaced: 1, added_lines: 8, removed_lines: 3 },
      error: null,
      duration_ms: 11,
    }, 3),
    ev(taskId, "reflect", {
      progress: "README 已补上「快速开始」，任务目标达成", next_action: "", done: true,
    }, 3),
    ev(taskId, "final_answer", {
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
        "**项目结构**",
        "",
        "| 目录 | 用途 |",
        "| --- | --- |",
        "| `src/` | 源码 |",
        "| `tests/` | 测试 |",
        "| `docs/` | 文档 |",
        "",
        "原文件已自动备份到 `.loom_backup/`。",
      ].join("\n"),
    }, 3),
    ev(taskId, "task.end", {
      status: "done" as TaskStatus,
      answer: "已给 README 补上「快速开始」一节。",
      reason: "agent_done",
    }, 3),
  ];
  return stamp(raw, base);
}

// --------------------------------------------------------------------------- //
// 任务 A2：同一会话里的第二个任务（短，用于验证 Task 切换）
// --------------------------------------------------------------------------- //
function taskA2Events(taskId: string, base: number): EventEnvelope[] {
  const raw: EventEnvelope[] = [
    ev(taskId, "task.start", {
      goal: "再检查一下 .gitignore 里有没有漏掉的构建产物",
      workspace: "D:\\Loom_agent\\loom",
      model: "qwen3.7-plus",
    }),
    ev(taskId, "thinking", { text: "读 .gitignore，重点看前端产物。", phase: "plan", done: true }),
    ev(taskId, "act", { intent: "读取 .gitignore", tool: "read_file" }, 1),
    ev(taskId, "tool.call", { call_id: "c1", tool: "read_file", args: { path: ".gitignore" } }, 1),
    ev(taskId, "tool.result", {
      call_id: "c1",
      tool: "read_file",
      ok: true,
      summary: "读取 .gitignore（41 行，745 B）",
      data: { content: "node_modules/\nfrontend/dist/\n.env\n" },
      error: null,
      duration_ms: 4,
    }, 1),
    ev(taskId, "final_answer", {
      text: "`.gitignore` 已经覆盖了 `node_modules/`、`frontend/dist/`、`.env`，没有发现明显遗漏。",
    }, 1),
    ev(taskId, "task.end", { status: "done" as TaskStatus, answer: "已检查完毕，无遗漏。", reason: "agent_done" }, 1),
  ];
  return stamp(raw, base, 1.5);
}

// --------------------------------------------------------------------------- //
// 其他会话的简短事件
// --------------------------------------------------------------------------- //
function simpleEvents(taskId: string, goal: string, ws: string, base: number): EventEnvelope[] {
  const raw: EventEnvelope[] = [
    ev(taskId, "task.start", { goal, workspace: ws, model: "qwen3.7-plus" }),
    ev(taskId, "thinking", { text: "先看目录再回答。", phase: "plan", done: true }),
    ev(taskId, "act", { intent: "列出目录", tool: "list_files" }, 1),
    ev(taskId, "tool.call", { call_id: "c1", tool: "list_files", args: { path: "." } }, 1),
    ev(taskId, "tool.result", {
      call_id: "c1", tool: "list_files", ok: true,
      summary: "目录下有 5 个文件", data: { entries: [] }, error: null, duration_ms: 6,
    }, 1),
    ev(taskId, "final_answer", { text: `已查看 \`${ws}\` 的结构，共 5 个文件，没有异常。` }, 1),
    ev(taskId, "task.end", { status: "done" as TaskStatus, answer: "已查看。", reason: "agent_done" }, 1),
  ];
  return stamp(raw, base, 1.1);
}

// --------------------------------------------------------------------------- //
// 种子数据
// --------------------------------------------------------------------------- //
export function DEMO_SEED() {
  const now = Date.now() / 1000;
  const H = 3600;

  const sessions: Record<string, Session> = {};
  const tasks: Record<string, Task> = {};
  const messages: Record<string, Message[]> = {};
  const events: Record<string, EventEnvelope[]> = {};
  const approvals: Record<string, Record<string, unknown>> = {};

  function addSession(opts: {
    id: string;
    title: string;
    ws: string;
    createdAgo: number;   // 秒前
    updatedAgo: number;
  }) {
    sessions[opts.id] = {
      session_id: opts.id,
      title: opts.title,
      workspace_path: opts.ws,
      created_at: now - opts.createdAgo,
      updated_at: now - opts.updatedAgo,
    };
    messages[opts.id] = [];
  }

  function addTask(opts: {
    id: string;
    sessionId: string;
    goal: string;
    status: TaskStatus;
    startedAgo: number;
    endedAgo?: number;
    endReason?: string;
    result?: string;
    events: EventEnvelope[];
  }) {
    tasks[opts.id] = {
      task_id: opts.id,
      session_id: opts.sessionId,
      goal: opts.goal,
      status: opts.status,
      started_at: now - opts.startedAgo,
      ended_at: opts.endedAgo !== undefined ? now - opts.endedAgo : undefined,
      end_reason: opts.endReason,
      result: opts.result,
      turn: 3,
    };
    events[opts.id] = opts.events;

    // 用户消息 + 助手消息（从任务派生）
    const list = messages[opts.sessionId] ?? [];
    list.push({
      message_id: `m_${opts.id}_user`,
      session_id: opts.sessionId,
      task_id: opts.id,
      role: "user",
      content: opts.goal,
      created_at: now - opts.startedAgo,
    });
    if (opts.result) {
      list.push({
        message_id: `m_${opts.id}_final`,
        session_id: opts.sessionId,
        task_id: opts.id,
        role: "assistant",
        content: opts.result,
        created_at: now - (opts.endedAgo ?? opts.startedAgo),
      });
    }
    messages[opts.sessionId] = list;
  }

  // ---- 组 1：D:\Loom_agent\loom（两个会话）----
  addSession({ id: "s_loom_1", title: "分析项目结构并补 README", ws: "D:\\Loom_agent\\loom", createdAgo: 5 * H, updatedAgo: 20 * 60 });
  addSession({ id: "s_loom_2", title: "检查 .gitignore 遗漏", ws: "D:\\Loom_agent\\loom", createdAgo: 2 * H, updatedAgo: 40 * 60 });

  const a1 = taskA1Events("task_a1", now - 30 * 60);
  addTask({
    id: "task_a1",
    sessionId: "s_loom_1",
    goal: "分析当前项目的结构，并给 README 补一节「快速开始」",
    status: "done",
    startedAgo: 30 * 60,
    endedAgo: 27 * 60,
    endReason: "agent_done",
    result: String((a1.find((e) => e.type === "final_answer")?.data as { text?: string })?.text ?? ""),
    events: a1,
  });

  addTask({
    id: "task_a2",
    sessionId: "s_loom_1",
    goal: "再检查一下 .gitignore 里有没有漏掉的构建产物",
    status: "done",
    startedAgo: 20 * 60,
    endedAgo: 19 * 60,
    endReason: "agent_done",
    result: "`.gitignore` 已经覆盖了 `node_modules/`、`frontend/dist/`、`.env`，没有发现明显遗漏。",
    events: taskA2Events("task_a2", now - 20 * 60),
  });

  addTask({
    id: "task_b1",
    sessionId: "s_loom_2",
    goal: "检查 .gitignore 里有没有漏掉的构建产物",
    status: "done",
    startedAgo: 40 * 60,
    endedAgo: 39 * 60,
    endReason: "agent_done",
    result: "已检查完毕，`.gitignore` 覆盖完整。",
    events: simpleEvents("task_b1", "检查 .gitignore 里有没有漏掉的构建产物", "D:\\Loom_agent\\loom", now - 40 * 60),
  });

  // ---- 组 2：D:\Projects\demo-app ----
  addSession({ id: "s_demo_d", title: "给 demo 项目加健康检查", ws: "D:\\Projects\\demo-app", createdAgo: 26 * H, updatedAgo: 5 * H });
  addTask({
    id: "task_d1",
    sessionId: "s_demo_d",
    goal: "给 demo 项目加一个 /health 端点",
    status: "done",
    startedAgo: 5 * H + 600,
    endedAgo: 5 * H,
    endReason: "agent_done",
    result: "已加上 `/health` 端点，返回 `{\"status\": \"ok\"}`。",
    events: simpleEvents("task_d1", "给 demo 项目加一个 /health 端点", "D:\\Projects\\demo-app", now - 5 * H),
  });

  // ---- 组 3：E:\Experiments\demo-app ★ 同名不同盘，必须独立成组 ----
  addSession({ id: "s_demo_e", title: "重构 demo 的配置加载", ws: "E:\\Experiments\\demo-app", createdAgo: 50 * H, updatedAgo: 30 * H });
  addTask({
    id: "task_e1",
    sessionId: "s_demo_e",
    goal: "重构配置加载逻辑",
    status: "done",
    startedAgo: 30 * H + 900,
    endedAgo: 30 * H,
    endReason: "agent_done",
    result: "配置加载已抽成 `load_config()`，支持环境变量覆盖。",
    events: simpleEvents("task_e1", "重构配置加载逻辑", "E:\\Experiments\\demo-app", now - 30 * H),
  });

  // ---- 组 4：未设置工作区 ----
  addSession({ id: "s_unset", title: "未绑定目录的会话", ws: "", createdAgo: 72 * H, updatedAgo: 70 * H });
  addTask({
    id: "task_u1",
    sessionId: "s_unset",
    goal: "先随便聊聊，还没选目录",
    status: "aborted",
    startedAgo: 70 * H + 60,
    endedAgo: 70 * H,
    endReason: "no_workspace",
    result: "还没有选择工作目录，无法读取任何文件。",
    events: simpleEvents("task_u1", "先随便聊聊，还没选目录", "", now - 70 * H),
  });

  // 审批条目（从事件里捞出来，保持与事件一致）
  for (const [, list] of Object.entries(events)) {
    for (const e of list) {
      if (e.type === "approval.request") {
        const id = String((e.data as { approval_id?: string }).approval_id ?? "");
        if (id) approvals[id] = { ...(e.data as Record<string, unknown>), resolved: "approved" };
      }
    }
  }

  return {
    sessions,
    tasks,
    messages,
    events,
    approvals: approvals as never,
    activeSessionId: "s_loom_1",
    activeTaskId: "task_a1",
  };
}
