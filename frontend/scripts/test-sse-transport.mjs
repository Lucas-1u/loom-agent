/**
 * SSE 传输层行为测试（真起 HTTP 服务器，不是 mock）
 *
 * 跑法：npm run test:sse
 *
 * ★ 为什么需要它
 *   2026-11-11 把前端传输层从 `EventSource` 换成了 `fetch + ReadableStream + 自己解析`。
 *   这是**最容易"编译能过、单测能过、上线收不到事件"**的一类改动：
 *     · 帧切分错 → 半个 JSON 交给上层 → 全部事件静默丢弃
 *     · 心跳注释帧没跳过 → 解析出空帧
 *     · 忘了解码 UTF-8 分块 → 中文变乱码
 *   单测（test:events [6]）只覆盖纯函数，覆盖不到"真的连上去"。
 *
 * ★ 两段
 *   ① 假服务器：验证「未声明的类型也照常投递」+ 游标推进 + gap 控制帧 + keepalive
 *   ② 真后端：验证我们的解析器与服务端 `sse_frame()` 的真实输出格式对得上（回归保险）
 */
import http from "node:http";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", ".."); // loom/

let pass = 0;
let fail = 0;
function ok(cond, label, detail = "") {
  if (cond) {
    pass += 1;
    console.log(`  [ok] ${label}`);
  } else {
    fail += 1;
    console.log(`  [X]  ${label}${detail ? `\n       ${detail}` : ""}`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 端口被占用时给出人话，而不是一个裸 EADDRINUSE 栈 */
function listen(server, port) {
  return new Promise((resolve, reject) => {
    server.once("error", (err) => {
      if (err.code === "EADDRINUSE") {
        console.log(
          `\n!! 端口 ${port} 已被占用 —— 本测试要独占它（要起假服务器 / 起真后端）。\n` +
            "   请先停掉正在跑的 python -m backend.server 或前端 dev server，再重跑。",
        );
        process.exit(1);
      }
      reject(err);
    });
    server.listen(port, "127.0.0.1", resolve);
  });
}

// --------------------------------------------------------------------------- //
// 与会话端 sse_frame() 完全同构的拼帧函数
// --------------------------------------------------------------------------- //
function frame(type, payload, eid) {
  const head = eid === undefined ? "" : `id: ${eid}\n`;
  return `${head}event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function envelope(type, data, seq = 1, turn = 1) {
  return { seq, ts: Date.now() / 1000, task_id: "task_probe", turn, type, data };
}

// --------------------------------------------------------------------------- //
// ① 假服务器
// --------------------------------------------------------------------------- //
async function phase1() {
  console.log("\n[1] 假服务器：未声明的事件类型也照常投递");

  const server = http.createServer((req, res) => {
    if (!req.url.startsWith("/api/stream")) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });

    // 心跳（注释帧）：不该被当成事件
    res.write(": keepalive\n\n");

    // 已声明类型
    res.write(
      frame(
        "task.start",
        { id: 1, task_id: "task_probe", session_id: "s_1", event: envelope("task.start", { goal: "探针" }) },
        1,
      ),
    );

    // ★ 未声明类型：前端 EVENT_TYPES 里没有它，旧实现会静默丢弃
    res.write(
      frame(
        "desktop.action",
        {
          id: 2,
          task_id: "task_probe",
          session_id: "s_1",
          event: envelope("desktop.action", { action: "click", x: 10, y: 20 }, 2),
        },
        2,
      ),
    );

    // 缺口控制帧（没有 id，不能污染游标）
    res.write(frame("stream.gap", { type: "stream.gap", data: { reason: "slow_client", dropped: 3 } }));

    // 中文载荷，且故意在 200ms 后断开（验证断流后的状态回调）
    setTimeout(() => {
      res.write(
        frame(
          "tool.result",
          {
            id: 3,
            task_id: "task_probe",
            session_id: "s_1",
            event: envelope("tool.result", { tool: "read_file", ok: true, summary: "中文摘要测试" }, 3),
          },
          3,
        ),
      );
      res.end();
    }, 200);
  });

  await listen(server, 8765);

  const { openEventStream } = await import("../.tmp/eventStream.mjs");

  const events = [];
  const gaps = [];
  const unknown = [];
  const states = [];

  const close = openEventStream({
    since: null,
    maxRetries: 0,
    handlers: {
      onEvent: (ev) => events.push(ev),
      onGap: (g) => gaps.push(g),
      onUnknownType: (t) => unknown.push(t),
      onState: (s) => states.push(s),
    },
  });

  await sleep(1200);
  close();
  await new Promise((r) => server.close(r));
  await sleep(50);

  const types = events.map((e) => e.event.type);
  ok(states.includes("open"), "连接建立（onState=open）");
  ok(types.includes("task.start"), "已声明事件到达");
  ok(types.includes("desktop.action"), "★ 未声明事件照样到达（旧实现会在这里丢掉）");
  ok(types.includes("tool.result"), "中文载荷事件到达（UTF-8 解码正确）");
  ok(
    events.find((e) => e.event.type === "tool.result")?.event.data.summary === "中文摘要测试",
    "中文内容未被截断/乱码",
  );
  ok(gaps.length === 1 && gaps[0].reason === "slow_client", "stream.gap 走 onGap，不混进事件流");
  ok(unknown.length === 1 && unknown[0] === "desktop.action", "★ 漂移被上报一次（每种只报一次）");
  ok(
    events.map((e) => e.id).join(",") === "1,2,3",
    "游标按 id 推进（不受 gap 帧影响）",
    `实际：${events.map((e) => e.id).join(",")}`,
  );
  ok(events.every((e) => e.task_id === "task_probe"), "task_id 透传正确");
}

// --------------------------------------------------------------------------- //
// ①b 断线重连与补发游标
// --------------------------------------------------------------------------- //
async function phase1b() {
  console.log("\n[1b] 断线重连：带 ?since= 补发，且不重复投递");

  const seenSince = [];
  let conn = 0;

  const server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    if (!u.pathname.startsWith("/api/stream")) {
      res.writeHead(404).end();
      return;
    }
    conn += 1;
    seenSince.push(u.searchParams.get("since"));

    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });

    if (conn === 1) {
      // 第一次连接：只发一条就断（模拟服务重启）
      res.write(
        frame(
          "task.start",
          { id: 1, task_id: "t1", session_id: "s1", event: envelope("task.start", { goal: "x" }) },
          1,
        ),
      );
      setTimeout(() => res.destroy(), 50);
      return;
    }

    // 重连：应该带着 since=1；补发第二条后保持连接
    res.write(
      frame(
        "tool.call",
        {
          id: 2,
          task_id: "t1",
          session_id: "s1",
          event: envelope("tool.call", { call_id: "c1", tool: "read_file", args: {} }, 2),
        },
        2,
      ),
    );
  });

  await listen(server, 8765);

  const { openEventStream } = await import("../.tmp/eventStream.mjs");

  const events = [];
  const states = [];
  const close = openEventStream({
    since: null,
    maxRetries: 3,
    handlers: {
      onEvent: (ev) => events.push(ev),
      onGap: () => {},
      onState: (s) => states.push(s),
    },
  });

  // 第一次退避 1s，留足时间
  await sleep(2200);
  close();
  await new Promise((r) => server.close(r));

  ok(conn >= 2, "断线后确实重连了", `连接次数=${conn}`);
  ok(seenSince[0] === null, "首次连接不带 since（新连接只收新事件）");
  ok(seenSince[1] === "1", "★ 重连带上了 ?since=1（补发游标正确推进）", `实际：${seenSince[1]}`);
  ok(states.includes("reconnecting"), "状态如实报告 reconnecting（不是假装在线）");
  ok(events.length === 2, "两帧各投递一次，没有重复", `实际 ${events.length} 条`);
  ok(
    events.length === 2 && events[0].id === 1 && events[1].id === 2,
    "去重基准正确：id 1 → 2 顺序投递",
  );
}

// --------------------------------------------------------------------------- //
// ② 真后端：帧格式兼容性
// --------------------------------------------------------------------------- //
async function phase2() {
  console.log("\n[2] 真后端：解析器认得服务端 sse_frame() 的真实输出");

  const db = path.join(repo, "sandbox", "_sse_probe.sqlite3");
  const py = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python";
  const child = spawn(py, ["-m", "backend.server", "--db", db], {
    cwd: repo,
    stdio: "ignore",
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });

  // 等健康检查通过
  let up = false;
  for (let i = 0; i < 40; i += 1) {
    await sleep(500);
    try {
      const r = await fetch("http://127.0.0.1:8765/api/health");
      if (r.ok) {
        up = true;
        break;
      }
    } catch {
      /* 还没起来 */
    }
  }
  ok(up, "后端已启动");

  try {
    const { openEventStream } = await import("../.tmp/eventStream.mjs");

    const events = [];
    const states = [];
    const close = openEventStream({
      since: null,
      maxRetries: 0,
      handlers: {
        onEvent: (ev) => events.push(ev),
        onGap: () => {},
        onState: (s) => states.push(s),
      },
    });

    await sleep(600);
    ok(states.includes("open"), "连上真实 /api/stream");

    // 建会话 + 建任务，逼出一条 task.start
    const sess = await (
      await fetch("http://127.0.0.1:8765/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "SSE 探针", workspace: repo }),
      })
    ).json();

    await fetch("http://127.0.0.1:8765/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal: "只回一句话：收到。不要调用任何工具。",
        session_id: sess.session_id,
        workspace: repo,
        mode: "read-only",
      }),
    });

    // 最多等 45s（模型可能慢/失败，但 task.start 一定先到）
    for (let i = 0; i < 90 && !events.some((e) => e.event.type === "task.start"); i += 1) {
      await sleep(500);
    }
    await sleep(300);
    close();

    const types = events.map((e) => e.event.type);
    ok(types.includes("task.start"), "★ 真实服务端发出的 task.start 被正确解析");
    ok(events.every((e) => typeof e.event.seq === "number"), "envelope 字段完整（seq 可读）");
    ok(
      events.every((e) => e.id > 0),
      "全局 id 已从帧里取到（断线补发游标可用）",
      `ids=${events.map((e) => e.id).join(",")}`,
    );
    console.log(`       收到事件：${types.join(" → ") || "（无）"}`);
  } finally {
    child.kill();
    await sleep(300);
    try {
      const fs = await import("node:fs");
      for (const suffix of ["", "-shm", "-wal"]) fs.rmSync(db + suffix, { force: true });
    } catch {
      /* 清理失败不影响结论 */
    }
  }
}

// --------------------------------------------------------------------------- //
await phase1();
await phase1b();
await phase2();

console.log(`\n${"=".repeat(52)}`);
console.log(`结果：${pass}/${pass + fail} 通过`);
if (fail) {
  console.log(`\n失败 ${fail} 条 —— 见上面 [X]`);
  process.exit(1);
}
console.log("=".repeat(52));
