/**
 * 思考片段合并的纯函数单元测试
 *
 * 跑法：npm run test:thinking
 * （先由 esbuild 把 mergeThinking.ts 打成 .tmp/mergeThinking.mjs，再 import 纯函数测试）
 *
 * 覆盖：mergeThinkingEvents
 *   · 相邻同轮次同阶段 → 粘成一段（文本拼接 / parts / seq 区间）
 *   · 任何别的事件插进来 → 断成两段（工具调用是主要的分段边界）
 *   · 空文本 done 标记 → 收尾当前段，且**不产生空卡片**
 *   · done 之后再来的思考另起一段（不会被二次粘合）
 *   · 跨 turn / 跨 phase 不合并
 *   · 幂等（合并过的列表再合一次不变）、不可变（不改入参）
 *
 * 为什么这些细节值得单测：合并规则一错，用户看到的就是"思考被切碎"或
 * "两次不同推理被粘成一段"——后者更糟，因为它看起来像模型连贯思考过。
 */
import assert from "node:assert/strict";
import { mergeThinkingEvents } from "../.tmp/mergeThinking.mjs";

let pass = 0;
let fail = 0;

function eq(actual, expected, label) {
  try {
    assert.deepEqual(actual, expected);
    pass += 1;
    console.log(`  [ok] ${label}`);
  } catch {
    fail += 1;
    console.log(`  [X]  ${label}\n       期望 ${JSON.stringify(expected)}\n       实际 ${JSON.stringify(actual)}`);
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

/** 造一条 thinking delta */
function think(seq, text, opts = {}) {
  return {
    seq,
    ts: 1_700_000_000 + seq,
    task_id: "t1",
    turn: opts.turn ?? 1,
    type: "thinking",
    data: { text, phase: opts.phase ?? "act", done: opts.done ?? false },
  };
}

/** 造一条别的事件（工具调用 / 计划 / 观察…） */
function other(seq, type, data = {}) {
  return { seq, ts: 1_700_000_000 + seq, task_id: "t1", turn: 1, type, data };
}

/** 取出合并后各段的文本，便于断言 */
const texts = (items) => items.map((e) => (e.type === "thinking" ? e.data.text : `<${e.type}>`));

console.log("\n[1] 基本合并");
{
  eq(mergeThinkingEvents([]), [], "空列表 → 空列表");

  const one = [think(1, "先看目录结构")];
  const r1 = mergeThinkingEvents(one);
  eq(r1.length, 1, "单条 → 一张卡");
  eq(r1[0].data.text, "先看目录结构", "单条文本不变");
  eq([r1[0].data.parts, r1[0].data.seq_from, r1[0].data.seq_to], [1, 1, 1], "单条 parts/区间都是自己");

  const three = [think(1, "先看"), think(2, "目录结构"), think(3, "，再决定改哪里")];
  const r3 = mergeThinkingEvents(three);
  eq(r3.length, 1, "三条相邻 → 一张卡");
  eq(r3[0].data.text, "先看目录结构，再决定改哪里", "文本按顺序拼接（不加分隔符）");
  eq(r3[0].data.parts, 3, "parts 记录吸收了几条");
  eq([r3[0].seq, r3[0].data.seq_from, r3[0].data.seq_to], [1, 1, 3], "seq 取首条（React key 稳定），区间覆盖到末条");
  ok(r3[0].data.ui_merged === true, "打上 ui_merged 标记（幂等的基础）");
}

console.log("\n[2] 分段：工具调用是边界");
{
  const r = mergeThinkingEvents([
    think(1, "先跑一下测试"),
    think(2, "看看现在什么状态"),
    other(3, "tool.call", { call_id: "c1", tool: "run_command", args: {} }),
    other(4, "tool.result", { call_id: "c1", tool: "run_command", ok: true }),
    think(5, "三个用例挂了"),
    think(6, "先看第一个的报错"),
  ]);
  eq(r.length, 4, "思考2 + 工具2 + 思考2 → 4 张卡（原来 6 张）");
  eq(texts(r), ["先跑一下测试看看现在什么状态", "<tool.call>", "<tool.result>", "三个用例挂了先看第一个的报错"], "两段思考被工具调用正确切开");

  const seg1 = r[0];
  const seg2 = r[3];
  eq([seg1.turn, seg1.data.parts, seg1.data.seq_from, seg1.data.seq_to], [1, 2, 1, 2], "第一段只吃 #1–#2");
  eq([seg2.turn, seg2.data.parts, seg2.data.seq_from, seg2.data.seq_to], [1, 2, 5, 6], "第二段从 #5 重新开始");

  const rPlan = mergeThinkingEvents([
    think(1, "a"),
    other(2, "plan", { steps: [] }),
    think(3, "b"),
  ]);
  eq(texts(rPlan), ["a", "<plan>", "b"], "plan 也切断（任何非 thinking 事件都切断）");
}

console.log("\n[3] done 标记：收尾 + 不产生空卡片");
{
  const r = mergeThinkingEvents([think(1, "想好了"), think(2, "", { done: true })]);
  eq(r.length, 1, "正文 + 空 done → 仍然只有一张卡（空标记不单独成卡）");
  eq([r[0].data.done, r[0].data.parts, r[0].data.seq_to], [true, 1, 2], "done 落到段上，seq 区间覆盖标记；parts 不把标记算作一段");

  eq(mergeThinkingEvents([think(1, "", { done: true })]), [], "孤立的空 done → 一张卡都不产生");
  eq(mergeThinkingEvents([think(1, "", { done: false })]), [], "空正文且非 done → 丢弃");

  const rTwo = mergeThinkingEvents([
    think(1, "第一段"),
    think(2, "", { done: true }),
    think(3, "第二段"),
    think(4, "", { done: true }),
  ]);
  eq(rTwo.length, 2, "done 收尾后，同一 turn 的下一条另起一段");
  eq(texts(rTwo), ["第一段", "第二段"], "两段文本没有互相污染");
  eq([rTwo[0].data.seq_from, rTwo[0].data.seq_to, rTwo[1].data.seq_from], [1, 2, 3], "区间不重叠");

  const rDoneText = mergeThinkingEvents([think(1, "甲"), think(2, "乙", { done: true }), think(3, "丙")]);
  eq(texts(rDoneText), ["甲乙", "丙"], "带正文的 done 也收尾");
}

console.log("\n[4] 不合并的两种情况");
{
  const rTurn = mergeThinkingEvents([think(1, "第一轮", { turn: 1 }), think(2, "第二轮", { turn: 2 })]);
  eq(rTurn.length, 2, "跨 turn 不合并（中间没有别的事件也不合并）");

  const rPhase = mergeThinkingEvents([
    think(1, "计划阶段", { phase: "plan" }),
    think(2, "执行阶段", { phase: "act" }),
  ]);
  eq(rPhase.length, 2, "跨 phase 不合并（两种阶段的语义不同）");
}

console.log("\n[5] 透明性与幂等");
{
  const toolEv = other(2, "tool.call", { call_id: "c1", tool: "read_file", args: { path: "a.py" } });
  const input = [think(1, "看文件"), toolEv];
  const snapshot = JSON.parse(JSON.stringify(input));
  const out = mergeThinkingEvents(input);

  eq(input, snapshot, "入参没有被修改（合并是派生，不是改写事实）");
  ok(out[1] === toolEv, "非 thinking 事件**引用透传**，不复制不重排");
  eq(out.map((e) => e.seq), [1, 2], "顺序与 seq 保持不变");

  const twice = mergeThinkingEvents(out);
  eq(twice, out, "幂等：已合并的列表再合一次完全不变（防两段被二次粘合）");

  const empty = mergeThinkingEvents([think(1, ""), think(2, "")]);
  eq(empty, [], "全是空片段 → 没有卡片（不会留下空壳）");
}

console.log("\n[6] 文本细节");
{
  const r = mergeThinkingEvents([
    think(1, "第一行\n"),
    think(2, "  第二行有缩进\n"),
    think(3, "中文与 English 混排"),
  ]);
  eq(r.length, 1, "含换行/中文时仍合并");
  eq(r[0].data.text, "第一行\n  第二行有缩进\n中文与 English 混排", "换行与缩进原样保留（pre 渲染靠它）");
  ok(r[0].data.text.length > 0, "长度可用（折叠时的字数提示靠它）");
}

console.log(`\n结果：${pass} 通过 / ${fail} 失败（共 ${pass + fail} 项）`);
if (fail > 0) process.exit(1);
