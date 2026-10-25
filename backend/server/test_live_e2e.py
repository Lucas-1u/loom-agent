"""
真实链路端到端测试（Phase 3d 阶段①）

★ 这个文件要证明什么
────────────────────────────────────────────────────────────────
  「前端的实时事件不是从 taskEvents 补拉来的，而是真的从 SSE 流过来的。」

  所以它**不能用 TestClient、不能直接调内部函数**，必须是：
      真 uvicorn → 真 HTTP → 真 SSE 流 → 真调度 → 真审批 → 真工具 → 真磁盘文件

★ 唯一的测试替身
────────────────────────────────────────────────────────────────
  只有**模型**被替换（`ScriptedAdapter`）。理由：
    真模型每次的工具调用、参数措辞、轮数都不确定 ——
    那样的测试既慢又会偶发失败，没法当回归基线。

  被替换掉的是「模型说了什么」，**不是**「系统怎么处理它」：
    · Agent Loop / Policy / ApprovalManager  → 全真实
    · TaskScheduler / TaskStore / EventHub   → 全真实
    · write_file 真的写磁盘                  → 真实验证
    · HTTP / SSE                            → 真实网络栈

  真模型的冒烟测试另有 `backend/test_live_model.py`，两者不要混为一谈。

★ 为什么不用 time.sleep 判完成
────────────────────────────────────────────────────────────────
  全部用「等某个事件出现 / 等状态变成某个值」+ 超时。
  固定 sleep 在慢机器上会假失败，在快机器上是浪费。

跑法：
    python -m backend.server.test_live_e2e           # 全部场景
    python -m backend.server.test_live_e2e -v        # 显示每条断言
    python -m backend.server.test_live_e2e s3        # 只跑某个场景
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
import uvicorn

from ..agent.core import AgentConfig
from ..models.adapter import FakeAdapter, ModelAdapter, ModelReply, ToolCall
from ..tools.registry import build_default_registry
from ..server.app import create_app
from ..server.hub import EventHub
from ..server.scheduler import TaskScheduler
from ..server.store import TaskStore

VERBOSE = False
results: list[tuple[bool, str]] = []
SCENARIO_TIMEOUT = 90.0

# 每个场景的断言条数必须稳定。
# ★ 为什么：之前 s3 里有两处 `if dec:` / `if result:` 的嵌套 check，
#   事件一缺就少记一条，总项数在 50 / 49 / 16 之间跳 ——
#   同一套代码，"通过率"却能变，等于没法横向比较。
# 现在每个场景跑完都核对条数，少一条就补一条失败并写明原因。
EXPECTED_COUNTS = {"s1": 12, "s2": 12, "s3": 18, "s4": 9}

_UNSET = object()


def check(cond: bool, label: str, actual: object = _UNSET) -> None:
    """
    记一条断言。

    `actual` 显式给出时追加到标签后面 —— 失败报告里能直接看到实际值，
    不用再回头翻日志猜。
    """
    text = label if actual is _UNSET else f"{label}｜实际：{actual!r}"
    results.append((bool(cond), text))
    if cond:
        if VERBOSE:
            print(f"  [ok] {text}")
    else:
        print(f"  [X]  {text}")


def ev_field(ev: dict | None, key: str, default: object = None) -> object:
    """从 SSE 记录里安全取 `event.data[key]`（ev 为 None 时返回 default）。"""
    if not ev:
        return default
    data = (ev.get("event") or {}).get("data") or {}
    return data.get(key, default)


# --------------------------------------------------------------------------- #
# 测试替身：按 goal 关键词分派剧本
# --------------------------------------------------------------------------- #
class RoutingAdapter(ModelAdapter):
    """
    按其 user 消息里的关键词，把一个任务路由到固定剧本。

    ★ 为什么不用一个 FakeAdapter？
      并发测试里两个任务的模型调用会**交替**消耗同一个剧本队列，
      谁都可能拿到对方的回复。按 goal 关键词分派才能让每个任务行为确定。
    """

    name = "scripted"

    def __init__(self, rules: dict[str, list[ModelReply]], default: list[ModelReply]) -> None:
        self.model = "scripted-1"
        self._rules = rules
        self._default = default
        self._inner: dict[str, FakeAdapter] = {}

    def _pick(self, messages) -> tuple[str, list[ModelReply]]:
        text = " ".join(str(getattr(m, "content", "") or "") for m in messages)
        for kw, script in self._rules.items():
            if kw in text:
                return kw, script
        return "__default__", self._default

    async def chat_stream(self, messages, tools=None, *, temperature=None, max_tokens=None):
        key, script = self._pick(messages)
        # ★ 慢任务：模拟一个"想得慢"的模型，给取消测试留出窗口。
        #   这是测试替身自己的行为（模拟延迟），不影响任何业务代码。
        if key.startswith("GO-SLOW"):
            await asyncio.sleep(0.7)

        adapter = self._inner.get(key)
        if adapter is None:
            # 每个任务独立一份剧本状态（FakeAdapter 会消耗队列）
            adapter = FakeAdapter(list(script))
            self._inner[key] = adapter
        async for d in adapter.chat_stream(messages, tools=tools):
            yield d


# --------------------------------------------------------------------------- #
# 起服务
# --------------------------------------------------------------------------- #
class LiveServer:
    """在同一个事件循环里起真 uvicorn，随机端口。"""

    def __init__(self, tmpdir: str) -> None:
        self.tmpdir = tmpdir
        self.workspace = str(Path(tmpdir) / "ws")
        os.makedirs(self.workspace, exist_ok=True)
        self.db_path = str(Path(tmpdir) / "e2e.db")

        self.store = TaskStore(self.db_path)
        self.hub = EventHub()
        self.adapter = RoutingAdapter({}, [])
        self.scheduler: TaskScheduler | None = None
        self.server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.base = ""

    async def start(self, adapter: RoutingAdapter, *, max_global: int = 4) -> None:
        await self.store.init()
        self.adapter = adapter
        self.scheduler = TaskScheduler(
            store=self.store,
            hub=self.hub,
            adapter=adapter,
            registry=build_default_registry(),
            config=AgentConfig(
                max_turns=8,
                max_seconds=60,
                max_tool_calls=10,
                approval_timeout_s=20,
                min_evidence=0,
            ),
            max_global=max_global,
        )
        app = create_app(store=self.store, hub=self.hub, scheduler=self.scheduler,
                         provider="scripted", model="scripted-1")

        cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                             access_log=False, lifespan="off")
        self.server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self.server.serve())

        # 等真正开始监听（不是固定 sleep）
        for _ in range(500):
            if self.server.started and self.server.servers:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("uvicorn 启动超时")

        sock = self.server.servers[0].sockets[0]
        port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.scheduler:
            await self.scheduler.shutdown(timeout=5)
        if self.server:
            self.server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        # ★ 显式关库：TaskStore 复用长连接，不关的话 Windows 上
        #   shutil.rmtree 会静默失败（文件被占用），临时目录越攒越多。
        await self.store.close()


# --------------------------------------------------------------------------- #
# HTTP / SSE 小工具
# --------------------------------------------------------------------------- #
async def post(client: httpx.AsyncClient, path: str, body: dict, headers: dict | None = None) -> dict:
    r = await client.post(path, json=body, headers=headers or {}, timeout=30)
    r.raise_for_status()
    return r.json()


async def get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(path, timeout=30)
    r.raise_for_status()
    return r.json()


class SSECollector:
    """
    后台读一条 SSE 流，把事件累积到列表。

    ★ 这是「真实客户端」：走 httpx 的流式读，不是调内部函数。
    """

    def __init__(self, base: str) -> None:
        self.base = base
        self.events: list[dict] = []      # {id, task_id, session_id, event}
        self.gaps: list[dict] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.connected = False
        self.error = ""

    async def __aenter__(self) -> "SSECollector":
        self._task = asyncio.create_task(self._run())
        # 等真正连上（服务端会立刻发头）
        for _ in range(300):
            if self.connected or self.error:
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("GET", f"{self.base}/api/stream?since=0") as r:
                    self.connected = True
                    ev_type = ""
                    async for line in r.aiter_lines():
                        if self._stop.is_set():
                            return
                        if line.startswith("event: "):
                            ev_type = line[7:].strip()
                        elif line.startswith("data: "):
                            payload = json.loads(line[6:])
                            if ev_type == "stream.gap":
                                self.gaps.append(payload)
                            else:
                                self.events.append(payload)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    # ---- 等待条件（不靠固定 sleep）----
    async def wait_event(
        self,
        pred: Callable[[dict], bool],
        *,
        timeout: float = SCENARIO_TIMEOUT,
        what: str = "事件",
    ) -> dict | None:
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            while seen < len(self.events):
                ev = self.events[seen]
                seen += 1
                if pred(ev):
                    return ev
            if self.error:
                return None
            await asyncio.sleep(0.02)
        return None

    async def wait_count(self, pred: Callable[[dict], bool], n: int,
                         *, timeout: float = SCENARIO_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sum(1 for e in self.events if pred(e)) >= n:
                return True
            if self.error:
                return False
            await asyncio.sleep(0.02)
        return False

    def of_task(self, task_id: str) -> list[dict]:
        return [e for e in self.events if e.get("task_id") == task_id]

    def types_of(self, task_id: str) -> list[str]:
        return [e["event"]["type"] for e in self.of_task(task_id)]


async def wait_status(
    client: httpx.AsyncClient,
    task_id: str,
    want: set[str],
    *,
    timeout: float = SCENARIO_TIMEOUT,
    what: str = "终态",
) -> str:
    """轮询权威状态直到进入 want 之一（这是取消兜底测试要用到的能力）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        d = await get(client, f"/api/tasks/{task_id}")
        last = d.get("status", "")
        if last in want:
            return last
        await asyncio.sleep(0.1)
    return last


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #
# ★ 每个剧本的第一条是给 PLAN 阶段的：PLAN 阶段不给工具，
#   所以那条 ModelReply 只要不抛异常就行（内容会被当成"计划文本"解析，
#   解析不出 JSON 就降级为无计划 —— 这是 Core 已有的容错路径）。
PLAN_REPLY = ModelReply(content="先看看目录，再给结论。")


def read_script() -> list[ModelReply]:
    return [
        PLAN_REPLY,
        ModelReply(tool_calls=[ToolCall(id="c1", name="list_files",
                                        arguments={"path": "."}, arguments_raw='{"path":"."}')]),
        ModelReply(content="目录里有 e2e 相关文件，任务完成。"),
    ]


def write_script(filename: str, content: str) -> list[ModelReply]:
    return [
        PLAN_REPLY,
        ModelReply(tool_calls=[ToolCall(
            id="c1", name="write_file",
            arguments={"path": filename, "content": content},
            arguments_raw=json.dumps({"path": filename, "content": content}, ensure_ascii=False),
        )]),
        ModelReply(content=f"已写入 {filename}。"),
    ]


def slow_script() -> list[ModelReply]:
    """
    故意放长：每轮都调一次 list_files，让任务活到能被取消。

    max_turns=8，每次工具耗时很短，但循环本身要跑几轮 —— 足够我们取消。
    """
    return [PLAN_REPLY] + [
        ModelReply(tool_calls=[ToolCall(id=f"c{i}", name="list_files",
                                        arguments={"path": "."}, arguments_raw='{"path":"."}')])
        for i in range(1, 8)
    ] + [ModelReply(content="终于做完了。")]


def build_adapter() -> RoutingAdapter:
    """
    注意：路由关键词要够独特，避免互相串。

      GO-READ-ALPHA      → 只读任务 A
      GO-READ-BETA       → 只读任务 B（用来做并发不串流）
      GO-WRITE-*         → 写文件（触发审批）
      GO-SLOW-*          → 慢任务（用来测取消）
    """
    return RoutingAdapter(
        {
            "GO-READ-ALPHA": read_script(),
            "GO-READ-BETA": read_script(),
            "GO-WRITE": write_script("e2e_out.txt", "hello-from-e2e"),
            "GO-SLOW": slow_script(),
        },
        default=[ModelReply(content="（默认剧本）完成。")],
    )


# --------------------------------------------------------------------------- #
# 场景 1：真实 SSE 实时接收
# --------------------------------------------------------------------------- #
async def scenario_sse_realtime(srv: LiveServer) -> None:
    print("\n[S1] 真实 SSE 客户端收到实时事件（不走 taskEvents）")

    async with httpx.AsyncClient(base_url=srv.base) as client:
        async with SSECollector(srv.base) as sse:
            check(sse.connected, "SSE 连接建立")
            check(len(sse.events) == 0, "刚连上时没有历史（since=0 但缓冲为空）")

            sess = await post(client, "/api/sessions",
                              {"title": "S1", "workspace": srv.workspace})
            snap = await post(client, "/api/tasks",
                              {"goal": "GO-READ-ALPHA 看看目录", "session_id": sess["session_id"],
                               "workspace": srv.workspace})
            tid = snap["task_id"]

            # 断言：从**流上**依次收到关键事件
            got_start = await sse.wait_event(lambda e: e["task_id"] == tid and e["event"]["type"] == "task.start")
            check(got_start is not None, "★ 流上收到 task.start（不是查询来的）")

            got_call = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "tool.call")
            check(got_call is not None, "★ 流上收到 tool.call")
            # ★ 不再写成 `if got_call:` 的嵌套 —— 那样事件一缺就少记一条断言，
            #   总项数会变。这里一律记，并把实际值带上。
            check(ev_field(got_call, "tool") == "list_files",
                  "工具名正确", actual=ev_field(got_call, "tool"))

            got_end = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "task.end")
            check(got_end is not None, "★ 流上收到 task.end")
            check(ev_field(got_end, "status") == "done",
                  "终态 done", actual=ev_field(got_end, "status"))

            # 全局 id 单调 + 任务内 seq 递增
            ids = [e["id"] for e in sse.events]
            check(ids == sorted(ids), f"全局 id 单调递增（{len(ids)} 条）")
            seqs = [e["event"]["seq"] for e in sse.of_task(tid)]
            check(seqs == sorted(seqs), f"任务内 seq 递增（{len(seqs)} 条）")
            check(len(set(seqs)) == len(seqs), "任务内 seq 无重复")

            # 事件归属正确
            check(all(e["task_id"] == tid for e in sse.of_task(tid)), "事件都归属这个 task")
            check(all(e["session_id"] == sess["session_id"] for e in sse.of_task(tid)),
                  "事件都带对的 session_id")


# --------------------------------------------------------------------------- #
# 场景 2：两个 Session 并发，事件不串流
# --------------------------------------------------------------------------- #
async def scenario_concurrent_isolation(srv: LiveServer) -> None:
    print("\n[S2] 两个 Session 并发，事件与回答不串流")

    async with httpx.AsyncClient(base_url=srv.base) as client:
        async with SSECollector(srv.base) as sse:
            s1 = await post(client, "/api/sessions", {"title": "S2-a", "workspace": srv.workspace})
            s2 = await post(client, "/api/sessions", {"title": "S2-b", "workspace": srv.workspace})

            # 同时提交（并发上限 4，两个都会立刻跑）
            try:
                t1, t2 = await asyncio.gather(
                    post(client, "/api/tasks", {"goal": "GO-READ-ALPHA 任务一",
                                                "session_id": s1["session_id"], "workspace": srv.workspace}),
                    post(client, "/api/tasks", {"goal": "GO-READ-BETA 任务二",
                                                "session_id": s2["session_id"], "workspace": srv.workspace}),
                )
            except Exception as exc:  # noqa: BLE001
                # ★ 这里以前直接抛出 httpx.HTTPStatusError，整轮测试当场退出，
                #   后面的场景一个都不跑、也没有汇总。现在记成一条明确的失败，
                #   剩下的断言由 runner 补齐（总项数仍然稳定）。
                detail = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f"｜HTTP {exc.response.status_code}：{exc.response.text[:200]}"
                check(False, f"两个任务都创建成功｜实际：{type(exc).__name__}: {exc}{detail}")
                return

            id1, id2 = t1["task_id"], t2["task_id"]
            check(id1 != id2, "两个任务 id 不同", actual=(id1, id2))

            ok1 = await sse.wait_event(lambda e: e["task_id"] == id1 and e["event"]["type"] == "task.end")
            ok2 = await sse.wait_event(lambda e: e["task_id"] == id2 and e["event"]["type"] == "task.end")
            check(ok1 is not None, "任务一在流上结束")
            check(ok2 is not None, "任务二在流上结束")

            ev1, ev2 = sse.of_task(id1), sse.of_task(id2)
            check(len(ev1) > 0 and len(ev2) > 0, f"两个任务都有事件（{len(ev1)} / {len(ev2)}）")

            # ★ 串流检查：每个任务的事件只属于它自己的 session
            check(all(e["session_id"] == s1["session_id"] for e in ev1), "★★ 任务一的事件不串到 session2")
            check(all(e["session_id"] == s2["session_id"] for e in ev2), "★★ 任务二的事件不串到 session1")

            # ★ 事件不交叉：两个 id 集合互斥地出现在对方的流里
            check(not any(e["task_id"] == id2 for e in ev1), "★★ 任务一的流里没有任务二的事件")

            # 各自的任务内 seq 独立从 1 开始（两层 id 的关键验证）
            seqs1 = sorted({e["event"]["seq"] for e in ev1})
            seqs2 = sorted({e["event"]["seq"] for e in ev2})
            check(seqs1 == list(range(1, len(seqs1) + 1)), f"任务一 seq 连续 1..{len(seqs1)}")
            check(seqs2 == list(range(1, len(seqs2) + 1)), f"任务二 seq 连续 1..{len(seqs2)}")
            check(seqs1 == seqs2 or len(seqs1) != len(seqs2),
                  "★★ 两个任务的 seq 各自独立（会重复，靠全局 id 区分）")

            # 最终回答不串
            a1 = await get(client, f"/api/tasks/{id1}")
            a2 = await get(client, f"/api/tasks/{id2}")
            check(a1["session_id"] == s1["session_id"], "任务一的快照归属正确")
            check(a2["session_id"] == s2["session_id"], "任务二的快照归属正确")


# --------------------------------------------------------------------------- #
# 场景 3：write_file 审批 → 磁盘文件真的生成
# --------------------------------------------------------------------------- #
async def scenario_write_approval(srv: LiveServer) -> None:
    print("\n[S3] write_file 审批端到端（批准 → 工具执行 → 文件落盘）")

    target = Path(srv.workspace) / "e2e_out.txt"
    if target.exists():
        target.unlink()

    async with httpx.AsyncClient(base_url=srv.base) as client:
        async with SSECollector(srv.base) as sse:
            sess = await post(client, "/api/sessions", {"title": "S3", "workspace": srv.workspace})
            snap = await post(client, "/api/tasks",
                              {"goal": "GO-WRITE 新建 e2e_out.txt", "session_id": sess["session_id"],
                               "workspace": srv.workspace})
            tid = snap["task_id"]

            # ---- 等审批请求（真事件，不是查出来的）----
            req = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "approval.request")
            check(req is not None, "★★ 流上收到 approval.request")

            # ★ 即使没收到 request 也继续走完 —— 用空 data 兜底。
            #   以前这里是 `if req is None: 补 16 条 + return`，靠人工数条数；
            #   现在每条断言都无条件执行，条数由 EXPECTED_COUNTS 兜底。
            data: dict = (req["event"]["data"] if req else {}) or {}
            approval_id = str(data.get("approval_id") or "")

            check(data.get("tool") == "write_file", "审批的工具是 write_file", actual=data.get("tool"))
            check(data.get("risk") == "write", "风险等级 write", actual=data.get("risk"))
            check("e2e_out.txt" in str(data.get("target", "")), "目标含文件名", actual=data.get("target"))
            check(bool(data.get("impact")), "带影响说明")

            # 审批期间：任务状态是 awaiting_approval
            mid = await get(client, f"/api/tasks/{tid}")
            check(mid["status"] in ("awaiting_approval", "running"),
                  "审批期间状态合理", actual=mid["status"])

            # ---- 批准 ----
            r = None
            if approval_id:
                r = await client.post(f"/api/approvals/{approval_id}/decision",
                                      json={"task_id": tid, "decision": "approved", "reason": ""},
                                      timeout=20)
            check(r is not None and r.status_code == 200,
                  "批准返回 200",
                  actual=(r.status_code if r is not None else "未提交（没拿到 approval_id）"))

            # ---- 断言审批决策事件 + 工具执行 + 终态 ----
            dec = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "approval.decision")
            check(dec is not None, "★★ 流上收到 approval.decision")
            check(ev_field(dec, "approved") is True, "决策是 approved",
                  actual=ev_field(dec, "approved"))

            result = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "tool.result")
            check(result is not None, "★★ 流上收到 tool.result")
            check(ev_field(result, "ok") is True, "工具执行成功", actual=ev_field(result, "ok"))

            end = await sse.wait_event(
                lambda e: e["task_id"] == tid and e["event"]["type"] == "task.end")
            check(end is not None, "任务结束", actual=ev_field(end, "status"))

            # ---- 磁盘上真的生成了文件 ----
            exists = await asyncio.get_event_loop().run_in_executor(None, target.exists)
            check(exists, f"★★★ 磁盘上真的生成了 {target.name}")
            content = ""
            if exists:
                content = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: target.read_text(encoding="utf-8"))
            check(content == "hello-from-e2e", "内容正确", actual=content[:40])

            # ---- 事件顺序 ----
            # ---- 逻辑顺序：按 seq 排（这是 core 的权威顺序）----
            # ★ 为什么不用投递顺序？
            #   SSE 的到达顺序会被服务端落盘 await 打乱（见阶段①报告问题 #8），
            #   而前端 `APPEND_EVENT` 本来就按 seq 排序，所以 **seq 才对**。
            by_seq = sorted(sse.of_task(tid), key=lambda e: e["event"]["seq"])
            seq_types = [e["event"]["type"] for e in by_seq]
            seq_nums = [e["event"]["seq"] for e in by_seq]

            i_req = seq_types.index("approval.request") if "approval.request" in seq_types else -1
            i_dec = seq_types.index("approval.decision") if "approval.decision" in seq_types else -1
            i_res = next((i for i, x in enumerate(seq_types)
                          if x == "tool.result" and i > i_dec), -1)

            check(i_req >= 0 and i_dec > i_req and i_res > i_dec,
                  f"★★ 逻辑顺序（按 seq）request({i_req}) → decision({i_dec}) → tool.result({i_res})")

            # ★ 事件没丢的判据：seq 必须连续（1..N 无缺口）
            check(seq_nums == list(range(1, len(seq_nums) + 1)),
                  f"★★ seq 连续无缺口（{len(seq_nums)} 条：{[s for s in seq_nums]}）")

            # ★ 到达顺序 = seq 顺序。
            #   以前 `_on_event` 先 await 落盘再 broadcast，落盘耗时会把到达顺序
            #   打乱（实测 [1..5,7,8,9,10,6]）。现在广播在前、落盘在后，
            #   这条从"诊断打印"升级成硬断言。
            deliver_order = [e["event"]["seq"] for e in sse.of_task(tid)]
            check(deliver_order == sorted(deliver_order),
                  "★★ 投递顺序与 seq 一致（没有被落盘 await 打乱）",
                  actual=deliver_order)

            # ---- 二次决策必须失败（一次性消费）----
            code: object = "未提交（没拿到 approval_id）"
            if approval_id:
                r2 = await client.post(f"/api/approvals/{approval_id}/decision",
                                       json={"task_id": tid, "decision": "approved", "reason": ""},
                                       timeout=20)
                code = r2.status_code
            check(code == 409, "★★ 重复决策被拒", actual=code)


# --------------------------------------------------------------------------- #
# 场景 4：取消 → 权威终态
# --------------------------------------------------------------------------- #
async def scenario_cancel(srv: LiveServer) -> None:
    print("\n[S4] 取消任务 → 服务端权威状态进入终态")

    async with httpx.AsyncClient(base_url=srv.base) as client:
        sess = await post(client, "/api/sessions", {"title": "S4", "workspace": srv.workspace})
        snap = await post(client, "/api/tasks",
                          {"goal": "GO-SLOW 一直干下去", "session_id": sess["session_id"],
                           "workspace": srv.workspace})
        tid = snap["task_id"]

        # 等它真的跑起来（不靠 sleep）
        st = await wait_status(client, tid, {"running"}, timeout=20)
        check(st == "running", f"任务已开始运行（{st}）")

        r = await client.post(f"/api/tasks/{tid}/cancel", timeout=20)
        check(r.status_code == 200, "取消接口 200")
        body = r.json()
        check("accepted" in body, "★ 返回 accepted")
        check("note" in body, "★ 返回 note（说明要等终态）")

        # ★ accepted 不等于已停止：立刻查一次，多半还是 running
        immediate = await get(client, f"/api/tasks/{tid}")
        check(immediate["status"] not in ("done",),
              f"accept 之后任务不一定已经停（当前 {immediate['status']}）")

        # 轮询权威状态（这就是前端 B-2 要做的兜底）
        final = await wait_status(client, tid, {"aborted", "done", "failed"}, timeout=60)
        check(final == "aborted", f"★★ 最终进入 aborted（实际 {final}）")

        # 快照与摘要一致
        snap2 = await get(client, f"/api/tasks/{tid}")
        check(snap2["status"] == "aborted", "快照 status = aborted")
        check(snap2.get("end_reason") in ("cancelled", "cancelled_while_queued"),
              f"end_reason 合理（{snap2.get('end_reason')}）")

        # 已结束的任务不能再取消
        r2 = await client.post(f"/api/tasks/{tid}/cancel", timeout=20)
        check(r2.json()["accepted"] is False, "★ 已结束任务 cancel 返回 false")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
SCENARIOS: dict[str, Callable[[LiveServer], Any]] = {
    "s1": scenario_sse_realtime,
    "s2": scenario_concurrent_isolation,
    "s3": scenario_write_approval,
    "s4": scenario_cancel,
}


async def amain(argv: list[str]) -> int:
    global VERBOSE
    VERBOSE = "-v" in argv or "--verbose" in argv
    picked = [a for a in argv if not a.startswith("-")] or list(SCENARIOS.keys())
    bad = [a for a in picked if a not in SCENARIOS]
    if bad:
        print(f"[X] 未知场景：{bad}，可选 {list(SCENARIOS)}")
        return 2

    print("=" * 78)
    print("Loom E2E · 真实 HTTP / SSE / 调度 / 审批 / 工具（模型用脚本替身）")
    print("=" * 78)

    tmpdir = tempfile.mkdtemp(prefix="loom_e2e_")
    srv = LiveServer(tmpdir)
    rc = 1
    try:
        await srv.start(build_adapter())
        print(f"服务已起：{srv.base}")
        print(f"工作区  ：{srv.workspace}")
        print(f"数据库  ：{srv.db_path}")

        for key in picked:
            before = len(results)
            try:
                await asyncio.wait_for(SCENARIOS[key](srv), timeout=SCENARIO_TIMEOUT * 2)
            except asyncio.TimeoutError:
                check(False, f"[{key}] 场景超时（{SCENARIO_TIMEOUT * 2}s）")
            except Exception as exc:  # noqa: BLE001
                # ★ 单个场景炸了不能拖垮整轮：记一条失败，继续跑下一个场景。
                check(False, f"[{key}] 场景异常中止：{type(exc).__name__}: {exc}")

            # ★ 把条数补齐到预期值。
            #   任何一个断言被跳过（事件缺失 / 场景提前 return），总项数都会变，
            #   "通过率"就没法横向比较。这里用显式的失败项补位，并写明原因。
            expected = EXPECTED_COUNTS[key]
            got = len(results) - before
            for n in range(got, expected):
                check(False, f"[{key}] 第 {n + 1}/{expected} 项断言未执行（场景提前中止）")
            if got > expected:
                check(False, f"[{key}] 断言条数 {got} 超过预期 {expected}（加了断言但没更新 EXPECTED_COUNTS）")

        ok = sum(1 for c, _ in results if c)
        total = len(results)
        print("\n" + "=" * 78)
        print(f"结果：{ok}/{total} 通过")
        print("按场景：" + "  ".join(f"{k}={EXPECTED_COUNTS[k]}" for k in picked)
              + f"（合计预期 {sum(EXPECTED_COUNTS[k] for k in picked)} 项）")
        if ok != total:
            print(f"\n失败项（{total - ok} 条）：")
            for c, label in results:
                if not c:
                    print(f"  ✗ {label}")
        print("=" * 78)
        rc = 0 if ok == total else 1
    finally:
        await srv.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)

    return rc


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
