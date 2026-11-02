/**
 * 任务树构建的纯函数单元测试
 *
 * 跑法：npm run test:tasktree
 * （先由 esbuild 把 taskTree.ts 打成 .tmp/taskTree.mjs，再 import 纯函数测试）
 *
 * 覆盖：buildTaskTree / flattenTree / collectTaskIds / countTreeNodes
 *   · 空列表 / 单根 / 根+子 / 多层嵌套
 *   · 孤儿任务（父不存在）提升为根，不丢子树
 *   · 同父 / 同根按 started_at 升序
 *   · parent_task_id null 与 undefined 都算根
 *   · 三个遍历辅助函数的正确性
 *
 * 后端那一半（parent_task_id 落库、count_children）在
 * backend/selfcheck_subtask.py 的 [A] 段。
 */
import assert from "node:assert/strict";
import {
  buildTaskTree,
  flattenTree,
  collectTaskIds,
  countTreeNodes,
} from "../.tmp/taskTree.mjs";

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

/** 造一个任务（默认根任务，parent 为 null） */
function task(id, opts = {}) {
  return {
    task_id: id,
    session_id: opts.sid ?? "s1",
    goal: opts.goal ?? `goal-${id}`,
    status: opts.status ?? "done",
    started_at: opts.started_at ?? now,
    turn: opts.turn ?? 1,
    workspace: opts.workspace ?? "D:/proj",
    parent_task_id: opts.parent ?? null,
  };
}

// ===========================================================================
// [A] 空列表 / 单根
// ===========================================================================
console.log("\n[A] 空列表与单根");
{
  const tree = buildTaskTree([]);
  eq(tree, [], "A1 空列表 → 空树");

  const single = buildTaskTree([task("t1")]);
  eq(single.length, 1, "A2 单根 → 一个顶层节点");
  eq(single[0].children.length, 0, "A3 单根无子任务");
  eq(single[0].depth, 0, "A4 单根 depth=0");
  eq(single[0].task.task_id, "t1", "A5 单根 task_id 正确");
}

// ===========================================================================
// [B] 根 + 子（两层）
// ===========================================================================
console.log("\n[B] 根 + 子任务（两层）");
{
  const root = task("root");
  const child = task("c1", { parent: "root" });
  const tree = buildTaskTree([root, child]);

  eq(tree.length, 1, "B1 一个顶层节点");
  eq(tree[0].task.task_id, "root", "B2 顶层是 root");
  eq(tree[0].children.length, 1, "B3 root 有一个子任务");
  eq(tree[0].children[0].task.task_id, "c1", "B4 子任务是 c1");
  eq(tree[0].children[0].depth, 1, "B5 子任务 depth=1");
}

// ===========================================================================
// [C] 根 + 多个子
// ===========================================================================
console.log("\n[C] 根 + 多个子任务");
{
  const root = task("root");
  const c1 = task("c1", { parent: "root" });
  const c2 = task("c2", { parent: "root" });
  const c3 = task("c3", { parent: "root" });
  const tree = buildTaskTree([root, c1, c2, c3]);

  eq(tree[0].children.length, 3, "C1 root 有三个子任务");
  eq(
    tree[0].children.map((n) => n.task.task_id),
    ["c1", "c2", "c3"],
    "C2 子任务全部挂到 root",
  );
  eq(countTreeNodes(tree), 4, "C3 总节点数 4（root + 3 子）");
}

// ===========================================================================
// [D] 多层嵌套（孙任务）
// ===========================================================================
console.log("\n[D] 多层嵌套（根 → 子 → 孙）");
{
  const root = task("root");
  const child = task("child", { parent: "root" });
  const grandchild = task("gc", { parent: "child" });
  const tree = buildTaskTree([root, child, grandchild]);

  eq(tree[0].children.length, 1, "D1 root 有一个子");
  eq(tree[0].children[0].task.task_id, "child", "D2 子是 child");
  eq(tree[0].children[0].children.length, 1, "D3 child 有一个孙");
  eq(tree[0].children[0].children[0].task.task_id, "gc", "D4 孙是 gc");
  eq(tree[0].children[0].children[0].depth, 2, "D5 孙 depth=2");
  eq(countTreeNodes(tree), 3, "D6 总节点数 3");
}

// ===========================================================================
// [E] 孤儿任务（父不存在）提升为根
// ===========================================================================
console.log("\n[E] 孤儿任务提升为根");
{
  // child 的父 parent 不在列表里 → 提升为根，不丢
  const orphan = task("orphan", { parent: "nonexistent" });
  const root = task("root");
  const tree = buildTaskTree([root, orphan]);

  eq(tree.length, 2, "E1 两个顶层（root + 孤儿）");
  const ids = tree.map((n) => n.task.task_id);
  ok(ids.includes("root") && ids.includes("orphan"), "E2 root 与 orphan 都在顶层");
  ok(countTreeNodes(tree) === 2, "E3 总节点数 2（孤儿未被吞掉）");
}

// ===========================================================================
// [F] 排序：同根 / 同父按 started_at 升序
// ===========================================================================
console.log("\n[F] 排序（按 started_at 升序）");
{
  const root = task("root", { started_at: now + 50 });
  // 乱序传入
  const c2 = task("c2", { parent: "root", started_at: now + 30 });
  const c1 = task("c1", { parent: "root", started_at: now + 10 });
  const c3 = task("c3", { parent: "root", started_at: now + 20 });

  const tree = buildTaskTree([root, c2, c1, c3]);

  eq(
    tree[0].children.map((n) => n.task.task_id),
    ["c1", "c3", "c2"],
    "F1 子任务按 started_at 升序",
  );

  // 多个根也按 started_at 升序
  const r1 = task("r1", { started_at: now + 20 });
  const r0 = task("r0", { started_at: now + 5 });
  const tree2 = buildTaskTree([r1, r0]);
  eq(
    tree2.map((n) => n.task.task_id),
    ["r0", "r1"],
    "F2 顶层根也按 started_at 升序",
  );
}

// ===========================================================================
// [G] parent_task_id null 与 undefined 都算根
// ===========================================================================
console.log("\n[G] parent_task_id null / undefined 都算根");
{
  const tNull = task("tn", { parent: null });
  const tUndef = task("tu"); // 默认 parent: null，这里显式去掉
  tUndef.parent_task_id = undefined;
  const tree = buildTaskTree([tNull, tUndef]);

  eq(tree.length, 2, "G1 null 与 undefined 都算根（两个顶层）");
}

// ===========================================================================
// [H] 遍历辅助函数
// ===========================================================================
console.log("\n[H] flattenTree / collectTaskIds / countTreeNodes");
{
  const root = task("root");
  const c1 = task("c1", { parent: "root" });
  const c2 = task("c2", { parent: "root" });
  const gc = task("gc", { parent: "c1" });
  const tree = buildTaskTree([root, c1, c2, gc]);

  // flatten：深度优先 → root, c1, gc, c2
  eq(
    flattenTree(tree).map((t) => t.task_id),
    ["root", "c1", "gc", "c2"],
    "H1 flattenTree 深度优先顺序",
  );

  // collectTaskIds
  const ids = collectTaskIds(tree);
  ok(ids.size === 4, "H2 collectTaskIds 收集 4 个 id");
  ok(ids.has("root") && ids.has("c1") && ids.has("gc") && ids.has("c2"), "H3 ids 包含全部节点");

  // countTreeNodes
  eq(countTreeNodes(tree), 4, "H4 countTreeNodes = 4");
  eq(countTreeNodes([]), 0, "H5 空树 count=0");
}

// ===========================================================================
// [I] 不同会话的任务不串线（buildTaskTree 只处理传入的列表）
// ===========================================================================
console.log("\n[I] 只处理传入列表（不跨会话）");
{
  // 模拟：传进来的任务都是同一会话的，但 parent 指向另一会话的任务
  // 由于 byId 只索引传入列表，跨会话的 parent 不在索引里 → 提升为根
  const root = task("root", { sid: "s1" });
  const crossChild = task("cross", { sid: "s1", parent: "other-session-task" });
  const tree = buildTaskTree([root, crossChild]);

  eq(tree.length, 2, "I1 跨会话的父不在索引 → 提升为根");
  eq(countTreeNodes(tree), 2, "I2 两个都可见");
}

// ===========================================================================
console.log("\n" + "=".repeat(50));
console.log(`结果：${pass}/${pass + fail} 通过`);
if (fail > 0) {
  console.log("有失败项");
  process.exit(1);
}
