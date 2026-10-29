"""
工作流调度器（V1）：DAG + 并发 + 完成推进 + 重启恢复。

★ 它是怎么"并行"的
--------------
每个可执行的 Step 起一个 asyncio task，同时最多 `max_parallel` 个在跑；
`asyncio.wait(FIRST_COMPLETED)` 驱动主循环——某个 Step 一结束就把控制权拿回来，
重新计算 Ready 集合。所以"等待"和"推进别的步骤"是同一件事的两面。

★ 它怎么保证不重复执行
------------------
1. `claim_step()` 原子抢占（DB 条件更新，rowcount=0 即没抢到）；
2. outbox 事件带 `event_id`，`mark_dispatched` 只对 `dispatched_at IS NULL` 生效；
3. Step 一旦进入 done/failed/blocked，就不再回到 ready（除非重启恢复显式 reset）。

★ 它怎么恢复
----------
`recover()` 扫描未终结的 Workflow：
  · running 的 Job：`resumable=True` → 标 unknown + 步骤退回 pending（随后 `resume()` 重跑）
                   `resumable=False` → 标 unknown + 步骤 FAILED(reason=restart_unknown)，
                                       整个 Workflow 转 needs_attention（等人看，绝不自动重跑）
  · 其余未完成步骤：交给 `resume()` 继续调度

明确不做的：跨进程的 Job 句柄接管（做不到），所以 shell 步骤重启后只能人工确认。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .executors import ExecResult, get_executor
from .models import (OK_STEP, TERMINAL_STEP, Job, JobStatus, Step, StepSpec, StepStatus,
                     WorkflowStatus, WorkflowView)
from .store import WorkflowStore

log = logging.getLogger("loom.workflow")

MAX_STEPS = 16


class WorkflowError(ValueError):
    """步骤定义非法（环、引用不存在、类型未知）。"""


def validate_steps(steps: list[StepSpec]) -> None:
    if not steps:
        raise WorkflowError("steps 不能为空")
    if len(steps) > MAX_STEPS:
        raise WorkflowError(f"步骤太多（{len(steps)} > {MAX_STEPS}）")
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise WorkflowError("步骤 id 必须唯一")
    idset = set(ids)
    for s in steps:
        if s.id in s.depends_on:
            raise WorkflowError(f"步骤 {s.id} 依赖了自己")
        for dep in s.depends_on:
            if dep not in idset:
                raise WorkflowError(f"步骤 {s.id} 依赖了不存在的 {dep}")
        if (s.kind or "").lower() not in ("http", "tool", "shell"):
            raise WorkflowError(f"步骤 {s.id} 的类型未知：{s.kind}")
    # 环检测：Kahn
    indeg = {s.id: len(s.depends_on) for s in steps}
    children: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for dep in s.depends_on:
            children[dep].append(s.id)
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in children[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(steps):
        raise WorkflowError("步骤依赖存在环（无法拓扑排序）")


class WorkflowService:
    def __init__(
        self,
        store: WorkflowStore,
        *,
        tool_runner: Callable[[str, dict[str, Any]], Any] | None = None,
        allow_local: bool = False,
        max_parallel: int = 3,
        default_timeout: float = 60.0,
        max_deadline: float = 600.0,
    ) -> None:
        self.store = store
        self.tool_runner = tool_runner
        self.allow_local = allow_local
        self.max_parallel = max(1, max_parallel)
        self.default_timeout = default_timeout
        self.max_deadline = max_deadline

    # ===================================================================== #
    # 对外：执行一个工作流（阻塞到全部结束，由工具 await）
    # ===================================================================== #
    async def run(self, *, goal: str, steps: list[StepSpec], task_id: str = "",
                  workspace: str = "", cancel_event: Any = None,
                  deadline_s: float = 0.0,
                  emit: Callable[[dict[str, Any]], Any] | None = None) -> WorkflowView:
        validate_steps(steps)
        deadline_s = min(max(deadline_s or 0.0, 0.0), self.max_deadline)
        wf = self.store.create(goal=goal, steps=steps, task_id=task_id,
                               workspace=workspace, deadline_s=deadline_s)
        view = self.store.get(wf.id)
        assert view is not None
        await self._emit(view, "workflow.started", emit)
        await self._drive(wf.id, workspace=workspace, cancel_event=cancel_event, emit=emit)
        final = self.store.get(wf.id)
        return final or view

    # ===================================================================== #
    # 对外：重启恢复
    # ===================================================================== #
    async def recover(self, *, emit: Callable[[dict[str, Any]], Any] | None = None,
                      resume_safe: bool = True) -> dict[str, Any]:
        """
        启动时扫一遍未终结的工作流。返回摘要（便于日志/健康检查）。

        ★ `resume_safe=True` 时，只会重跑"幂等步骤"；有副作用的步骤一律转人工。
        """
        summary = {"scanned": 0, "resumed": [], "needs_attention": [], "jobs_marked": 0}
        for wid in self.store.list_unfinished():
            summary["scanned"] += 1
            view = self.store.get(wid)
            if view is None:
                continue
            if view.workflow.cancel_requested:
                self.store.set_workflow_status(wid, WorkflowStatus.CANCELLED, error="cancelled")
                continue

            unsafe = False
            touched_steps: set[str] = set()
            for job in self.store.running_jobs(wid):
                summary["jobs_marked"] += 1
                self.store.job_finished(
                    job.id, JobStatus.UNKNOWN,
                    error="进程重启，原执行状态无法确认",
                )
                if job.resumable:
                    touched_steps.add(job.step_id)
                else:
                    unsafe = True
                    self.store.finish_step(wid, job.step_id, StepStatus.FAILED,
                                           error="restart_unknown: 有副作用的步骤无法确认是否已执行")
            for step_id in touched_steps:
                self.store.reset_step(wid, step_id)

            if unsafe or not resume_safe:
                self.store.set_workflow_status(wid, WorkflowStatus.NEEDS_ATTENTION,
                                               error="restart_unknown")
                summary["needs_attention"].append(wid)
                await self._emit(self.store.get(wid), "workflow.needs_attention", emit)
                continue

            self.store.set_workflow_status(wid, WorkflowStatus.PENDING, error="")
            summary["resumed"].append(wid)
            # 后台继续跑（不阻塞启动）
            asyncio.create_task(self.resume(wid, emit=emit))

        return summary

    async def resume(self, workflow_id: str, *,
                     cancel_event: Any = None,
                     emit: Callable[[dict[str, Any]], Any] | None = None) -> WorkflowView | None:
        view = self.store.get(workflow_id)
        if view is None:
            return None
        workspace = view.workflow.workspace   # 恢复时沿用创建时的工作区
        await self._drive(workflow_id, workspace=workspace, cancel_event=cancel_event,
                          emit=emit)
        return self.store.get(workflow_id)

    # ===================================================================== #
    # 主循环
    # ===================================================================== #
    async def _drive(self, workflow_id: str, *, workspace: str, cancel_event: Any,
                     emit: Callable[[dict[str, Any]], Any] | None) -> None:
        if self.store.get(workflow_id) is None:
            return
        self.store.set_workflow_status(workflow_id, WorkflowStatus.RUNNING)
        tasks: dict[asyncio.Task, str] = {}

        while True:
            view = self.store.get(workflow_id)
            if view is None:
                return

            # ---- 取消 / 超时 ------------------------------------------------
            if self.store.cancelled(workflow_id) or _is_set(cancel_event):
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for step in view.steps:
                    if step.status not in TERMINAL_STEP:
                        self.store.finish_step(workflow_id, step.step_id,
                                               StepStatus.CANCELLED, error="cancelled")
                self.store.set_workflow_status(workflow_id, WorkflowStatus.CANCELLED,
                                               error="cancelled")
                await self._emit(self.store.get(workflow_id), "workflow.cancelled", emit)
                return
            if view.workflow.deadline_at and time.time() > view.workflow.deadline_at:
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for step in view.steps:
                    if step.status not in TERMINAL_STEP:
                        self.store.finish_step(workflow_id, step.step_id, StepStatus.FAILED,
                                               error="deadline_exceeded")
                self.store.set_workflow_status(workflow_id, WorkflowStatus.FAILED,
                                               error="deadline_exceeded")
                await self._emit(self.store.get(workflow_id), "workflow.failed", emit)
                return

            # ---- 结算：上游失败 → 下游 BLOCKED ------------------------------
            await self._propagate_blocks(view, emit)

            view = self.store.get(workflow_id)
            if view is None:
                return
            running = [s for s in view.steps if s.status is StepStatus.RUNNING]

            # ---- 启动 Ready Steps ------------------------------------------
            ready = [s for s in view.steps if s.status in (StepStatus.PENDING, StepStatus.READY)
                     and all(_dep_ok(view, d) for d in s.depends_on)]
            for step in ready:
                if len(tasks) >= self.max_parallel:
                    break
                if not self.store.claim_step(workflow_id, step.step_id):
                    continue      # 别人抢到了（重复调度防护）
                await self._emit(self.store.get(workflow_id), "workflow.step", emit)
                task = asyncio.create_task(
                    self._run_step(workflow_id, step, workspace=workspace,
                                   cancel_event=cancel_event, emit=emit)
                )
                tasks[task] = step.step_id

            if not tasks and not ready:
                # 没有在跑的、也没有可跑的：要么全完，要么卡住
                view = self.store.get(workflow_id)
                if view is None:
                    return
                pending = [s for s in view.steps if s.status not in TERMINAL_STEP]
                if pending:
                    # 有遗留（例如依赖被阻断但没传播到）→ 保守标 blocked
                    for s in pending:
                        self.store.finish_step(workflow_id, s.step_id, StepStatus.BLOCKED,
                                               error="上游未完成")
                    view = self.store.get(workflow_id)
                status = self._final_status(view)
                self.store.set_workflow_status(workflow_id, status)
                await self._emit(self.store.get(workflow_id),
                                 "workflow.finished" if status is WorkflowStatus.COMPLETED
                                 else "workflow.failed", emit)
                return

            if not tasks:
                continue

            # ★ 带 0.25s 超时：取消/截止检查不会等到慢请求自然结束才被看见
            done, _pending = await asyncio.wait(set(tasks), timeout=0.25,
                                                return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                tasks.pop(t, None)
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:      # 调度器自身异常：不许静默吞
                    log.warning("工作流 %s 步骤异常：%s", workflow_id, exc)

    async def _run_step(self, workflow_id: str, step: Step, *, workspace: str,
                        cancel_event: Any,
                        emit: Callable[[dict[str, Any]], Any] | None) -> None:
        """一个 Step 的执行（含重试）。异常一律转成 Step 状态，不往上抛。"""
        spec = dict(step.spec or {})
        timeout_s = float(step.timeout_s or 0) or self.default_timeout
        executor = None
        try:
            executor = get_executor(step.kind, allow_local=self.allow_local,
                                    tool_runner=self.tool_runner, workspace=workspace)
        except ValueError as exc:
            self.store.finish_step(workflow_id, step.step_id, StepStatus.FAILED, error=str(exc))
            await self._emit(self.store.get(workflow_id), "workflow.step", emit)
            return

        max_attempts = 2 if getattr(executor, "resumable", False) else 1
        last: ExecResult | None = None
        for attempt in range(1, max_attempts + 1):
            job = self.store.create_job(workflow_id=workflow_id, step_id=step.step_id,
                                        kind=step.kind, timeout_s=timeout_s,
                                        resumable=getattr(executor, "resumable", False),
                                        max_attempts=max_attempts)
            self.store.job_started(job.id)
            try:
                last = await asyncio.wait_for(
                    executor.run(spec, timeout_s=timeout_s, cancel_event=cancel_event),
                    timeout=timeout_s + 5,
                )
            except asyncio.TimeoutError:
                last = ExecResult(timed_out=True, error=f"超过 {timeout_s:.0f}s")
            except asyncio.CancelledError:
                self.store.job_finished(job.id, JobStatus.CANCELLED, error="cancelled")
                self.store.finish_step(workflow_id, step.step_id, StepStatus.CANCELLED,
                                       error="cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                last = ExecResult(error=f"{type(exc).__name__}: {exc}", retryable=False)

            if last.cancelled:
                self.store.job_finished(job.id, JobStatus.CANCELLED, result=value_safe(last))
                self.store.finish_step(workflow_id, step.step_id, StepStatus.CANCELLED,
                                       error="cancelled")
                return
            if last.timed_out:
                self.store.job_finished(job.id, JobStatus.TIMED_OUT, result=value_safe(last),
                                        error=last.error)
                self.store.finish_step(workflow_id, step.step_id, StepStatus.FAILED,
                                       error="timed_out")
                await self._emit(self.store.get(workflow_id), "workflow.step", emit)
                return
            if last.ok:
                self.store.job_finished(job.id, JobStatus.DONE, result=value_safe(last))
                self.store.finish_step(workflow_id, step.step_id, StepStatus.DONE,
                                       result=value_safe(last))
                await self._emit(self.store.get(workflow_id), "workflow.step", emit)
                return

            self.store.job_finished(job.id, JobStatus.FAILED, result=value_safe(last),
                                    error=last.error)
            if last.retryable and attempt < max_attempts:
                await asyncio.sleep(0.2 * attempt)     # 有界退避
                continue
            self.store.finish_step(workflow_id, step.step_id, StepStatus.FAILED,
                                   error=last.error or "failed")
            await self._emit(self.store.get(workflow_id), "workflow.step", emit)
            return

    # ===================================================================== #
    # 辅助
    # ===================================================================== #
    async def _propagate_blocks(self, view: WorkflowView,
                                emit: Callable[[dict[str, Any]], Any] | None) -> None:
        changed = False
        for step in view.steps:
            if step.status not in (StepStatus.PENDING, StepStatus.READY):
                continue
            for dep in step.depends_on:
                d = _by_id(view, dep)
                if d and d.status in (StepStatus.FAILED, StepStatus.BLOCKED,
                                      StepStatus.CANCELLED):
                    self.store.finish_step(view.workflow.id, step.step_id, StepStatus.BLOCKED,
                                           error=f"上游 {dep} {d.status.value}")
                    changed = True
                    break
        if changed:
            await self._emit(self.store.get(view.workflow.id), "workflow.step", emit)

    @staticmethod
    def _final_status(view: WorkflowView | None) -> WorkflowStatus:
        if view is None:
            return WorkflowStatus.FAILED
        statuses = [s.status for s in view.steps]
        if statuses and all(s is StepStatus.DONE for s in statuses):
            return WorkflowStatus.COMPLETED
        if any(s is StepStatus.BLOCKED for s in statuses):
            return WorkflowStatus.FAILED
        if all(s in TERMINAL_STEP for s in statuses):
            return WorkflowStatus.FAILED
        return WorkflowStatus.FAILED

    async def _emit(self, view: WorkflowView | None, kind: str,
                    emit: Callable[[dict[str, Any]], Any] | None) -> None:
        """
        先落 outbox 再投递：进程如果在两步之间挂掉，重启还能补发。
        投递失败不抛（事件可重投），但状态推进不受影响。
        """
        if view is None:
            return
        payload = {"event": kind, **view.to_dict()}
        event_id = self.store.enqueue_event(view.workflow.id, kind, payload)
        if emit is None:
            return
        try:
            await _maybe_await(emit(payload))
            self.store.mark_dispatched(event_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("工作流事件投递失败（已入 outbox，可重投）：%s", exc)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _by_id(view: WorkflowView, step_id: str) -> Step | None:
    for s in view.steps:
        if s.step_id == step_id:
            return s
    return None


def _dep_ok(view: WorkflowView, dep: str) -> bool:
    s = _by_id(view, dep)
    return bool(s and s.status is StepStatus.DONE)


def _is_set(ev: Any) -> bool:
    return bool(ev is not None and getattr(ev, "is_set", lambda: False)())


def value_safe(res: ExecResult) -> dict[str, Any]:
    return dict(res.value or {})


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value
