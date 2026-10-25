/**
 * 会话隔离的 reducer 单元测试
 *
 * 跑法：npm run test:session
 * （先由 esbuild 把 store.tsx 打成 .tmp/store.mjs，再 import 纯函数 reducer）
 *
 * 覆盖的是「切会话不串线」的前端那一半：
 *   · 不同会话的消息各自归属
 *   · 旧任务的迟到事件只写回它自己的会话，不会污染当前会话
 *   · 不能把执行流切到别的会话的任务上
 *
 * 后端那一半（会话归属、任务工作目录、重启恢复）在
 * backend/selfcheck_server.py 的 [G] 段。
 */
import assert from "node:assert/strict";
import { reducer, emptyStore } from "../.tmp/store.mjs";

let pass = 0;
let fail = 0;

function eq(actual, expected, label) {
  try {
    assert.deepEqual(actual, expected);
    pass += 1;
    console.log(`  [ok] ${label}`);
  } catch {
    fail += 1;
    console.log(`  [X]  ${label}\n       期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
  }
}

function ok(cond, label) {
  if (cond) {
    pass += 1;
    console.log(`  [ok] ${label}`);
  } else {
    fail += 1;
    console.log(`  [X]  ${label}`);
  }
}

const now = 1_700_000_000;

function sess(id, ws) {
  return { session_id: id, title: id, workspace_path: ws, created_at: now, updated_at: now };
}
function task(id, sid, goal) {
  return { task_id: id, session_id: sid, goal, status: "running", started_at: now, turn: 0 };
}
function userMsg(id, sid, tid, content) {
  return { message_id: `m_${tid}_user`, session_id: sid, task_id: tid, role: "user", content, created_at: now };
}
function finalEvent(tid, text, seq = 1) {
  return {
    seq,
    ts: now + 1,
    task_id: tid,
    turn: 1,
    type: "final_answer",
    data: { text },
  };
}

console.log("会话隔离（前端 reducer）");

let s = emptyStore();

// ---------------------------------------------------------------- 建两个会话
s = reducer(s, { type: "CREATE_SESSION", session: sess("sA", "D:\\LLMstudy") });
s = reducer(s, { type: "CREATE_SESSION", session: sess("sB", "D:\\Try_agent") });
eq(Object.keys(s.sessions).sort(), ["sA", "sB"], "两个会话独立登记");
eq(s.activeSessionId, "sB", "最后创建的会话被选中");

// ---------------------------------------------------------------- 各自建任务
s = reducer(s, {
  type: "START_TASK",
  task: task("tA", "sA", "ALPHA"),
  userMessage: userMsg("m1", "sA", "tA", "ALPHA"),
});
s = reducer(s, {
  type: "START_TASK",
  task: task("tB", "sB", "BETA"),
  userMessage: userMsg("m2", "sB", "tB", "BETA"),
});
eq(s.messages.sA.map((m) => m.content), ["ALPHA"], "A 只有自己的消息");
eq(s.messages.sB.map((m) => m.content), ["BETA"], "B 只有自己的消息");

// ------------------------------------------------- 切到 B 后，A 的迟到事件回来
s = reducer(s, { type: "SET_ACTIVE_SESSION", sessionId: "sB" });
const beforeB = s.messages.sB.map((m) => m.content);
s = reducer(s, { type: "APPEND_STREAM_EVENT", sseId: 7, event: finalEvent("tA", "A-FINAL") });

eq(s.messages.sB.map((m) => m.content), beforeB, "★ 迟到事件没有写进当前会话 B");
ok(s.messages.sA.some((m) => m.content === "A-FINAL"), "★ 迟到事件回到了它自己的会话 A");
eq(s.events.tA.length, 1, "事件按 task_id 归档");
eq(s.activeSessionId, "sB", "当前会话没有被迟到事件改掉");

// ------------------------------------------------- 不能切到别的会话的任务上
const beforeTask = s.activeTaskId;
s = reducer(s, { type: "SET_ACTIVE_TASK", taskId: "tA" });
eq(s.activeTaskId, beforeTask, "★ 拒绝把执行流切到别的会话的任务");

// ---------------------------------------------------------------- 切回 A
s = reducer(s, { type: "SET_ACTIVE_SESSION", sessionId: "sA" });
// SET_ACTIVE_SESSION 会选该会话最后一个任务
eq(s.activeTaskId, "tA", "切回 A 后执行流指向 A 自己的任务");
eq(s.messages.sA.map((m) => m.content), ["ALPHA", "A-FINAL"], "A 的消息完整且顺序正确");

// ------------------------------------------------- 后端刷新会话列表不能清掉消息
s = reducer(s, {
  type: "HYDRATE_SESSIONS",
  sessions: [sess("sA", "D:\\LLMstudy"), sess("sB", "D:\\Try_agent")],
});
eq(s.messages.sA.length, 2, "HYDRATE 不会清空已有消息");
eq(s.messages.sB.length, 1, "B 的消息也不受影响");
eq(s.activeSessionId, "sA", "刷新后仍停在原会话");

// ------------------------------------------------- 未知 task 的事件直接丢弃
const beforeUnknown = s.events;
s = reducer(s, { type: "APPEND_STREAM_EVENT", sseId: 9, event: finalEvent("t_ghost", "GHOST") });
ok(s.events.t_ghost === undefined, "★ 归属不明的任务事件被丢弃，不会落到当前会话");
eq(s.events, beforeUnknown, "丢弃时不产生新引用");

console.log(`\n结果：${pass}/${pass + fail} 通过`);
if (fail > 0) process.exitCode = 1;
