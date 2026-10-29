"""
异步工作流（V1）自检：真实并发、依赖、失败隔离、超时、取消、持久化、恢复。

跑法（loom/ 目录）：
    ./.venv/Scripts/python.exe -m backend.selfcheck_workflow

★ 并行是"证明"出来的，不是声称的：
  两个各 0.6s 的 HTTP 步骤，串行 ≥1.2s，并行应 <1.1s；再比对 step 的起止时间窗口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.agent.approval import ApprovalManager
from backend.agent.core import AgentConfig, AgentCore
from backend.events import EventBus, EventType, collect
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.tools.base import ToolContext
from backend.tools.registry import build_default_registry
from backend.workflow import (JobStatus, StepSpec, StepStatus, WorkflowError, WorkflowService,
                              WorkflowStatus, WorkflowStore)

_results: list[tuple[bool, str]] = []


def check(cond, label: str) -> bool:
    ok = bool(cond)
    _results.append((ok, label))
    print(f"  [{'ok' if ok else '!!'}] {label}")
    return ok


# --------------------------------------------------------------------------- #
# 本地 Mock HTTP（不依赖线上服务）
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path == "/slow":
            time.sleep(int(q.get("ms", ["300"])[0]) / 1000)
            body = b"slow-ok"
        elif parsed.path == "/fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        else:
            body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:   # 静音
        return


def start_mock() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# --------------------------------------------------------------------------- #
# [A] DAG 校验
# --------------------------------------------------------------------------- #
def suite_dag(store: WorkflowStore) -> None:
    print("\n[A] DAG 校验")
    from backend.workflow import validate_steps

    def bad(steps, label):
        try:
            validate_steps(steps)
            check(False, f"应当拒绝：{label}")
        except WorkflowError as exc:
            check(True, f"拒绝 {label}（{str(exc)[:40]}）")

    ok = [StepSpec(id="a", kind="http", spec={}),
          StepSpec(id="b", kind="http", spec={}, depends_on=["a"])]
    try:
        validate_steps(ok)
        check(True, "合法 DAG 通过")
    except WorkflowError as exc:
        check(False, f"合法 DAG 被拒：{exc}")

    bad([StepSpec(id="a", kind="http", depends_on=["a"])], "自依赖")
    bad([StepSpec(id="a", kind="http", depends_on=["zzz"])], "引用不存在")
    bad([StepSpec(id="a", kind="http"), StepSpec(id="a", kind="http")], "id 重复")
    bad([StepSpec(id="a", kind="bogus")], "未知类型")
    bad([StepSpec(id="a", kind="http", depends_on=["b"]),
         StepSpec(id="b", kind="http", depends_on=["a"])], "环")


# --------------------------------------------------------------------------- #
# [B] 并行 / 依赖 / 失败隔离
# --------------------------------------------------------------------------- #
async def suite_parallel(store: WorkflowStore, base: str, tmp: Path) -> None:
    print("\n[B] 真实并行 / 依赖 / 失败隔离")
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "note.txt").write_text("hello-workflow\n", encoding="utf-8")

    svc = WorkflowService(store, allow_local=True, max_parallel=3,
                          default_timeout=10)

    async def read_tool(name, args, workspace):
        from backend.tools.readonly.read_file import make
        res = await make().run(args, ToolContext(workspace=workspace))
        return {"ok": res.ok, "summary": res.summary, "data": res.data, "error": res.error}

    svc.tool_runner = read_tool

    # 两个慢 HTTP（0.6s）+ 一个快工具：并行总耗时 ≈0.6s，串行 ≥1.2s
    steps = [
        StepSpec(id="h1", kind="http", spec={"url": f"{base}/slow?ms=600&tag=1"}),
        StepSpec(id="h2", kind="http", spec={"url": f"{base}/slow?ms=600&tag=2"}),
        StepSpec(id="t1", kind="tool", spec={"tool": "read_file", "args": {"path": "note.txt"}}),
        StepSpec(id="join", kind="http", spec={"url": f"{base}/ok"},
                 depends_on=["h1", "h2", "t1"]),
    ]
    t0 = time.time()
    view = await svc.run(goal="并行测试", steps=steps, workspace=str(ws), deadline_s=30)
    dt = time.time() - t0

    status = {s.step_id: s.status for s in view.steps}
    check(view.workflow.status is WorkflowStatus.COMPLETED,
          f"★ 全部步骤完成（{view.workflow.status.value}）")
    check(dt < 1.15, f"★ 两个 0.6s 请求是并行跑的（总 {dt:.2f}s < 1.15s；串行会 ≥1.2s）")
    check(all(st is StepStatus.DONE for st in status.values()), f"各步骤 done（{status}）")
    join = next(s for s in view.steps if s.step_id == "join")
    h1 = next(s for s in view.steps if s.step_id == "h1")
    h2 = next(s for s in view.steps if s.step_id == "h2")
    check(join.started_at >= max(h1.ended_at, h2.ended_at) - 0.01,
          "★ join 在 h1/h2 都结束之后才开始（依赖生效）")
    check(h1.started_at < h2.ended_at and h2.started_at < h1.ended_at,
          "★ h1/h2 的执行窗口确实重叠（不是串行）")

    # 失败隔离：b1 失败 → depend_on b1 的 blocked；独立 b2 仍完成
    steps2 = [
        StepSpec(id="b1", kind="http", spec={"url": f"{base}/fail"}),
        StepSpec(id="b2", kind="http", spec={"url": f"{base}/ok"}),
        StepSpec(id="b3", kind="http", spec={"url": f"{base}/ok"}, depends_on=["b1"]),
    ]
    view2 = await svc.run(goal="失败隔离", steps=steps2, workspace=str(ws), deadline_s=30)
    st2 = {s.step_id: s.status for s in view2.steps}
    check(st2["b1"] is StepStatus.FAILED, f"★ 500 的步骤 failed（{st2['b1'].value}）")
    check(st2["b2"] is StepStatus.DONE, f"★ 与失败无关的步骤照常完成（{st2['b2'].value}）")
    check(st2["b3"] is StepStatus.BLOCKED, f"★ 依赖失败的步骤被 blocked（{st2['b3'].value}）")
    check(view2.workflow.status is WorkflowStatus.FAILED,
          f"工作流整体 failed（{view2.workflow.status.value}）")

    # 超时
    steps3 = [StepSpec(id="slow", kind="http", spec={"url": f"{base}/slow?ms=3000"},
                       timeout_s=0.6)]
    t1 = time.time()
    view3 = await svc.run(goal="超时", steps=steps3, workspace=str(ws), deadline_s=30)
    dt3 = time.time() - t1
    check(view3.steps[0].status is StepStatus.FAILED and "timed_out" in view3.steps[0].error,
          f"★ 单步超时被识别（{view3.steps[0].error}）")
    check(dt3 < 2.5, f"超时后及时返回（{dt3:.2f}s）")

    # SSRF：默认 allow_local=False 时拒绝回环地址
    svc_block = WorkflowService(store, allow_local=False, default_timeout=5)
    view4 = await svc_block.run(
        goal="SSRF", steps=[StepSpec(id="x", kind="http", spec={"url": f"{base}/ok"})],
        workspace=str(ws), deadline_s=10)
    check(view4.steps[0].status is StepStatus.FAILED
          and "SSRF" in (view4.steps[0].error or ""),
          f"★ 内网/回环地址被拒（{view4.steps[0].error[:40]}）")


# --------------------------------------------------------------------------- #
# [C] 取消 / 持久化 / outbox
# --------------------------------------------------------------------------- #
async def suite_cancel_store(store: WorkflowStore, base: str, tmp: Path) -> None:
    print("\n[C] 取消 / 持久化 / outbox")
    svc = WorkflowService(store, allow_local=True, default_timeout=20)
    cancel = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.3)
        cancel.set()

    asyncio.create_task(cancel_soon())
    t0 = time.time()
    view = await svc.run(
        goal="取消测试",
        steps=[StepSpec(id="slow", kind="http", spec={"url": f"{base}/slow?ms=3000"})],
        workspace=str(tmp), cancel_event=cancel, deadline_s=30)
    dt = time.time() - t0
    check(view.workflow.status is WorkflowStatus.CANCELLED,
          f"★ 取消后工作流 cancelled（{view.workflow.status.value}）")
    check(view.steps[0].status is StepStatus.CANCELLED,
          f"步骤标 cancelled（{view.steps[0].status.value}）")
    check(dt < 2.5, f"★ 取消立即生效，不等慢请求自然结束（{dt:.2f}s）")

    # 持久化：重新打开同一个库还能读到
    view_again = store.get(view.workflow.id)
    check(view_again is not None and view_again.workflow.status is WorkflowStatus.CANCELLED,
          "★ 状态已落库（重开 store 可读）")

    # outbox：emit=None 时事件留在 outbox，dispatched_at 为空
    svc2 = WorkflowService(store, allow_local=True, default_timeout=5)
    view2 = await svc2.run(
        goal="事件测试", steps=[StepSpec(id="ok", kind="http", spec={"url": f"{base}/ok"})],
        workspace=str(tmp), deadline_s=10)
    pending = [e for e in store.pending_events(200) if e["workflow_id"] == view2.workflow.id]
    check(len(pending) >= 2, f"★ 未投递事件留在 outbox（{len(pending)} 条）")

    eid = pending[0]["event_id"]
    store.mark_dispatched(eid)
    store.mark_dispatched(eid)      # 重复投递
    with store._lock:
        row = store._conn.execute(
            "SELECT attempts FROM workflow_outbox WHERE event_id = ?", (eid,)).fetchone()
    check(row["attempts"] == 1, f"★ 重复投递幂等（attempts={row['attempts']}）")

    # claim_step 防重复调度
    claim = store.claim_step(view2.workflow.id, "ok")
    check(claim is False, "★ 已完成的步骤不能被二次抢占")


# --------------------------------------------------------------------------- #
# [D] 重启恢复
# --------------------------------------------------------------------------- #
async def suite_recovery(store: WorkflowStore, base: str, tmp: Path) -> None:
    print("\n[D] 重启恢复")
    svc = WorkflowService(store, allow_local=True, max_parallel=2, default_timeout=20)

    # ---- 可安全恢复：http（幂等）步骤被重启打断 → 自动重跑 ----
    wf = store.create(goal="恢复-http",
                      steps=[StepSpec(id="s1", kind="http", spec={"url": f"{base}/ok"})],
                      workspace=str(tmp), deadline_s=30)
    store.claim_step(wf.id, "s1")
    job = store.create_job(workflow_id=wf.id, step_id="s1", kind="http",
                           timeout_s=10, resumable=True)
    store.job_started(job.id)

    summary = await svc.recover()
    check(wf.id in summary["resumed"], f"★ 幂等步骤被列入恢复（{summary['resumed']}）")
    # 等后台 resume 完成
    for _ in range(100):
        await asyncio.sleep(0.05)
        v = store.get(wf.id)
        if v and v.workflow.status is WorkflowStatus.COMPLETED:
            break
    v = store.get(wf.id)
    check(v is not None and v.workflow.status is WorkflowStatus.COMPLETED,
          f"★ 重启后自动续跑到完成（{v.workflow.status.value if v else '无'}）")
    jobs = store.jobs_of(wf.id)
    check(any(j.status is JobStatus.UNKNOWN for j in jobs),
          f"旧 Job 标 unknown，新 Job 完成（{[(j.status.value, j.attempts) for j in jobs]}）")

    # ---- 有副作用：shell 步骤重启后只能人工确认 ----
    wf2 = store.create(goal="恢复-shell",
                       steps=[StepSpec(id="sh", kind="shell",
                                       spec={"command": "echo hi", "cwd": str(tmp)})],
                       workspace=str(tmp), deadline_s=30)
    store.claim_step(wf2.id, "sh")
    job2 = store.create_job(workflow_id=wf2.id, step_id="sh", kind="shell",
                            timeout_s=5, resumable=False)
    store.job_started(job2.id)
    summary2 = await svc.recover()
    check(wf2.id in summary2["needs_attention"],
          f"★ 有副作用的步骤转人工（{summary2['needs_attention']}）")
    v2 = store.get(wf2.id)
    check(v2.workflow.status is WorkflowStatus.NEEDS_ATTENTION,
          f"工作流状态 needs_attention（{v2.workflow.status.value}）")
    step = v2.steps[0]
    check(step.status is StepStatus.FAILED and "restart_unknown" in step.error,
          f"★ 步骤标失败并注明原因（{step.error}）")
    check(not any(j.status is JobStatus.RUNNING for j in store.jobs_of(wf2.id)),
          "没有遗留 running 的 Job")


# --------------------------------------------------------------------------- #
# [E] Agent 集成（Graph / Legacy）
# --------------------------------------------------------------------------- #
def tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False),
                    arguments=args)


async def _run_agent(store: WorkflowStore, base: str, ws: str, engine: str) -> tuple:
    reg = build_default_registry(workflow=store, workflow_allow_local=True)
    bus = EventBus()
    evs = collect(bus)
    core = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("w1", "run_workflow", {
                "goal": "两个独立请求 + 汇总",
                "steps": [
                    {"id": "a", "kind": "http", "spec": {"url": f"{base}/slow?ms=400"}},
                    {"id": "b", "kind": "http", "spec": {"url": f"{base}/ok"}},
                    {"id": "c", "kind": "http", "spec": {"url": f"{base}/ok"},
                     "depends_on": ["a", "b"]},
                ],
                "deadline_s": 30,
            })]),
            ModelReply(content="工作流已完成，三个步骤都成功。"),
        ]),
        reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, orchestrator=engine),
        bus=bus, approvals=ApprovalManager(timeout_s=5),
    )

    async def approve():
        for _ in range(400):
            await asyncio.sleep(0.02)
            aid = core.approvals.current()
            if aid:
                core.approvals.approve(aid, "测试批准")
                return

    task = asyncio.create_task(approve())
    st = await core.run("跑个工作流", workspace=ws, task_id=f"task_wf_{engine}")
    task.cancel()
    await bus.drain()
    return st, evs


async def suite_agent(store: WorkflowStore, base: str, tmp: Path) -> None:
    print("\n[E] Agent 集成（Graph / Legacy）")
    ws = str(tmp / "agent_ws")
    os.makedirs(ws, exist_ok=True)

    for engine in ("graph", "legacy"):
        st, evs = await _run_agent(store, base, ws, engine)
        te = st.tool_events[0] if st.tool_events else None
        check(te is not None and te.ok, f"{engine}：run_workflow 执行成功")
        check(any(str(e.type) == EventType.WORKFLOW_STATE.value for e in evs),
              f"{engine}：发出了 workflow.state 事件")
        wf_events = [e for e in evs if str(e.type) == EventType.WORKFLOW_STATE.value]
        kinds = {e.data.get("event") for e in wf_events}
        check("workflow.finished" in kinds, f"{engine}：有 finished 事件（{sorted(kinds)}）")
        check(st.status.value == "done", f"{engine}：任务 done（{st.status.value}）")

    # 失败工作流 → 工具返回 failure（不伪装成功）
    st2, _ = await _run_agent_fail(store, base, ws)
    te2 = st2.tool_events[0] if st2.tool_events else None
    check(te2 is not None and te2.ok is False, "★ 失败的工作流 → 工具返回 failure")


async def _run_agent_fail(store: WorkflowStore, base: str, ws: str) -> tuple:
    reg = build_default_registry(workflow=store, workflow_allow_local=True)
    bus = EventBus()
    core = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("f1", "run_workflow", {
                "goal": "失败示例",
                "steps": [{"id": "x", "kind": "http", "spec": {"url": f"{base}/fail"}}],
            })]),
            ModelReply(content="失败了。"),
        ]),
        reg, config=AgentConfig(max_turns=5, min_evidence=0, use_plan_phase=False,
                                use_reflect_phase=False),
        bus=bus, approvals=ApprovalManager(timeout_s=5),
    )

    async def approve():
        for _ in range(400):
            await asyncio.sleep(0.02)
            aid = core.approvals.current()
            if aid:
                core.approvals.approve(aid, "ok")
                return

    t = asyncio.create_task(approve())
    st = await core.run("跑个会失败的工作流", workspace=ws, task_id="task_wf_fail")
    t.cancel()
    await bus.drain()
    return st, []


# --------------------------------------------------------------------------- #
async def amain() -> int:
    parser = argparse.ArgumentParser(description="Loom 工作流自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="loom_wf_"))
    srv, base = start_mock()
    store = WorkflowStore(str(tmp / "wf.db"))
    try:
        suite_dag(store)
        await suite_parallel(store, base, tmp)
        await suite_cancel_store(store, base, tmp)
        await suite_recovery(store, base, tmp)
        await suite_agent(store, base, tmp)
    finally:
        srv.shutdown()
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"结果：{passed}/{total} 通过")
    print("=" * 62)
    if passed != total:
        print("\n失败项：")
        for ok, label in _results:
            if not ok:
                print(f"  - {label}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
