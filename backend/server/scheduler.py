"""
任务调度

★ 三条规则
────────────────────────────────────────────────────────────────
  ① 全局最多 `max_global`（默认 2）个任务在跑
  ② 同一 Session 同时最多 1 个在跑
  ③ 超出的排队（status 仍为 created），有槽位空出来就自动拉起

★ 竞态：多个任务同时结束 / 取消 / 失败
────────────────────────────────────────────────────────────────
  每个 runtime 都有自己的 `finished` Event。由一个 `_watch()` 协程盯着它，
  在 **finally** 里释放槽位再 `pump()` ——
  这样无论正常结束、抛异常、还是被取消，槽位都会被释放（不会泄漏）。

  `pump()` 里"挑下一个"和"占用槽位"在同一把锁内完成，
  避免两个并发 pump 抢到同一个任务。

★ 为什么 started_at 只在 launch 时填
────────────────────────────────────────────────────────────────
  排队期间 status=created、started_at=NULL。这是用户明确要求的语义：
  「排了 3 分钟」和「跑了 3 分钟」必须能区分开。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..agent.core import AgentConfig
from ..models.adapter import ModelAdapter
from ..tools.registry import ToolRegistry
from .hub import EventHub
from .runtime import TaskRuntime
from .store import TaskStore

log = logging.getLogger("loom.server.scheduler")

DEFAULT_MAX_GLOBAL = 2

# 这些状态代表"这个任务已经不会再前进了"
TERMINAL = {"done", "aborted", "failed"}


class SchedulerError(RuntimeError):
    pass


class TaskScheduler:
    def __init__(
        self,
        *,
        store: TaskStore,
        hub: EventHub,
        adapter: ModelAdapter,
        registry: ToolRegistry,
        config: AgentConfig,
        max_global: int = DEFAULT_MAX_GLOBAL,
    ) -> None:
        self._store = store
        self._hub = hub
        self._adapter = adapter          # 无状态，可被多个任务共用
        self._registry = registry
        self._config = config
        self._max_global = max_global

        self._runtimes: dict[str, TaskRuntime] = {}
        self._session_of: dict[str, str] = {}
        self._running: set[str] = set()
        self._queue: list[str] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    # ===================================================================== #
    # 提交
    # ===================================================================== #
    async def submit(
        self,
        *,
        session_id: str,
        goal: str,
        workspace: str,
        model: str = "",
        idem_key: str = "",
    ) -> dict[str, Any]:
        """
        创建任务并尝试调度。

        ★ 幂等：同一个 `idem_key` 再来一次 → 返回**原来那个任务**，不新建。
          这是后端行为，不依赖前端禁用按钮。
        """
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        now = time.time()

        tid, created = await self._store.create_task(
            {
                "task_id": task_id,
                "session_id": session_id,
                "goal": goal,
                "status": "created",
                "model": model,
                "workspace": workspace,
                "created_at": now,
                "started_at": None,          # 排队中，等 launch 才填
            },
            idem_key=idem_key,
        )

        if not created:
            # 幂等命中
            log.info("幂等键 %s 命中，返回既有任务 %s", idem_key, tid)
            snap = await self.describe(tid)
            if snap is not None:
                snap["idempotent_replay"] = True
                return snap
            # 极端情况：幂等表指向的任务不存在（不该发生）→ 当成新任务处理
            raise SchedulerError(f"幂等键指向的任务不存在：{tid}")

        async with self._lock:
            self._session_of[tid] = session_id
            self._queue.append(tid)

        await self.pump()
        return await self.describe(tid) or {}

    # ===================================================================== #
    # 调度
    # ===================================================================== #
    async def pump(self) -> int:
        """
        尽量多地启动排队任务，直到没有槽位或没有可启动的。

        返回本次启动了几个。
        """
        started = 0
        while True:
            async with self._lock:
                tid = self._pick_next_locked()
                if tid is None:
                    break
                self._running.add(tid)

            await self._launch(tid)
            started += 1

        return started

    def _pick_next_locked(self) -> str | None:
        """
        挑下一个能跑的任务（调用方必须持锁）。

        「全局上限」和「同会话唯一」都在这里判断，保证不会两个 pump 抢同一个。
        """
        if len(self._running) >= self._max_global:
            return None

        busy_sessions = {self._session_of.get(t) for t in self._running}

        for i, tid in enumerate(self._queue):
            if self._session_of.get(tid) in busy_sessions:
                continue          # 这个会话已经有任务在跑了
            self._queue.pop(i)
            return tid

        return None

    async def _launch(self, task_id: str) -> None:
        """真正启动一个任务（锁外执行）。"""
        session_id = self._session_of.get(task_id, "")
        row = await self._store.get_task(task_id)
        if row is None:
            log.error("任务 %s 在库里不存在，跳过", task_id)
            async with self._lock:
                self._running.discard(task_id)
            return

        rt = TaskRuntime(
            task_id=task_id,
            session_id=session_id,
            goal=row["goal"],
            workspace=row["workspace"],
            model=row["model"],
            adapter=self._adapter,
            registry=self._registry,
            config=self._config,
            hub=self._hub,
            store=self._store,
        )
        self._runtimes[task_id] = rt

        await rt.start()
        log.info("任务 %s 已启动（session=%s）", task_id, session_id)

        # 盯住它，结束后释放槽位并继续调度
        asyncio.create_task(self._watch(rt), name=f"loom-watch-{task_id}")

    async def _watch(self, rt: TaskRuntime) -> None:
        """
        等任务结束 → 释放槽位 → 拉起下一个。

        ★ 用 finally 保证**任何**退出路径都释放槽位：
          Agent 正常结束、内部异常、被取消 —— 都不会把槽位漏占着。
        """
        try:
            await rt.finished.wait()
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                self._running.discard(rt.task_id)
            try:
                await self.pump()
            except Exception:  # noqa: BLE001 —— 调度失败不能影响已完成的任务
                log.exception("调度后续任务失败")

    # ===================================================================== #
    # 取消
    # ===================================================================== #
    async def cancel(self, task_id: str) -> dict[str, Any]:
        """
        请求取消。

        返回 `{"accepted": bool, "state": "...", "note": "..."}`：

            accepted=True  → 请求已被接受，**任务还没停**，要等 status 变 aborted
            accepted=False → 任务不存在 / 已在终态

        ★ 排队中的任务可以直接出队并标 aborted（它一步都没跑）。
        """
        rt = self._runtimes.get(task_id)

        if rt is not None:
            if rt.status in TERMINAL:
                return {
                    "accepted": False,
                    "state": rt.status,
                    "note": "任务已结束，无需取消",
                }
            ok = rt.cancel()
            return {
                "accepted": ok,
                "state": rt.status,
                "note": "取消请求已接受，将在下一次循环边界生效；最终以 status=aborted 为准"
                if ok
                else "取消请求未被接受",
            }

        # ---- 不在内存：可能还在排队，或已经结束 ----
        async with self._lock:
            if task_id in self._queue:
                self._queue.remove(task_id)
                queued = True
            else:
                queued = False

        if queued:
            now = time.time()
            await self._store.save_task(
                task_id,
                status="aborted",
                state={"task_id": task_id, "status": "aborted", "end_reason": "cancelled_while_queued"},
                end_reason="cancelled_while_queued",
                result="任务在排队阶段被取消，从未开始执行。",
                ended_at=now,
            )
            return {
                "accepted": True,
                "state": "aborted",
                "note": "任务在排队中，已直接取消（未执行任何步骤）",
            }

        row = await self._store.get_task(task_id)
        if row is None:
            return {"accepted": False, "state": "", "note": "任务不存在"}
        return {
            "accepted": False,
            "state": row["status"],
            "note": "任务已结束，无法取消",
        }

    # ===================================================================== #
    # 审批
    # ===================================================================== #
    async def resolve_approval(
        self,
        task_id: str,
        approval_id: str,
        decision: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """
        提交审批决策。

        ★ 三层校验（缺一不可）：
           ① 任务必须在内存里（已结束/重启的任务没有挂起的审批）
           ② 任务不能已在终态
           ③ approval_id 必须**真的属于这个任务** —— 防止把 A 任务的决策
              投到 B 任务的审批上（两个任务可能同时在等）
        """
        rt = self._runtimes.get(task_id)
        if rt is None:
            return {"accepted": False, "reason": "task_not_running", "note": "任务不在运行中（可能已结束或服务重启过）"}

        if rt.status in TERMINAL:
            return {"accepted": False, "reason": "task_finished", "note": f"任务已结束（{rt.status}），无法审批"}

        pending = rt.snapshot().get("pending_approval") or {}
        if pending.get("approval_id") != approval_id:
            return {
                "accepted": False,
                "reason": "approval_mismatch",
                "note": "这个 approval_id 不属于该任务，或已被处理",
                "current": pending.get("approval_id", ""),
            }

        ok = await rt.resolve_approval(approval_id, decision, reason)
        if not ok:
            return {"accepted": False, "reason": "already_resolved", "note": "该审批已被处理或已超时"}

        return {"accepted": True, "reason": "", "note": "决策已提交"}

    # ===================================================================== #
    # 查询
    # ===================================================================== #
    async def describe(self, task_id: str) -> dict[str, Any] | None:
        """
        任务快照。优先用内存里的 runtime（最实时），否则从库里读。

        两种情况都要回答「事件还在不在」。
        """
        rt = self._runtimes.get(task_id)

        if rt is not None:
            snap = rt.snapshot()
            async with self._lock:
                in_queue = task_id in self._queue
                pos = self._queue.index(task_id) + 1 if in_queue else 0
            snap["queued"] = in_queue
            snap["queue_position"] = pos
            return snap

        row = await self._store.get_task(task_id)
        if row is None:
            return None

        # ★ 排队中的任务**没有 runtime**，走的是这个分支 ——
        #   所以队列状态必须在这里查，不能硬编码成 False。
        async with self._lock:
            in_queue = task_id in self._queue
            pos = self._queue.index(task_id) + 1 if in_queue else 0

        base = row.get("state") or {}
        snap = {
            **base,
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "goal": row["goal"],
            "workspace": row["workspace"],
            "model": row["model"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "end_reason": row["end_reason"],
            "result": row["result"],
            "queued": in_queue,
            "queue_position": pos,
            # ★ 不在内存 = 已结束或服务重启过。此时事件只可能还在 hub 缓冲里，
            #   没了就是没了 —— 明确告诉前端，不能假装完整。
            "events_available": self._hub.has_events_for(task_id),
            "events_buffered": len(self._hub.buffered_for_task(task_id)),
        }
        return snap

    async def list_tasks(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """任务摘要列表（不含 state_json）。"""
        rows = await self._store.list_tasks(session_id)
        out = []
        for r in rows:
            rt = self._runtimes.get(r["task_id"])
            item = dict(r)
            item["queued"] = r["task_id"] in self._queue
            item["events_available"] = self._hub.has_events_for(r["task_id"])
            if rt is not None:
                # 内存里的状态更新（可能刚变，还没来得及落盘）
                item["status"] = rt.status
            out.append(item)
        return out

    def runtime_of(self, task_id: str) -> TaskRuntime | None:
        return self._runtimes.get(task_id)

    def stats(self) -> dict[str, Any]:
        async def _noop() -> None:
            return None

        return {
            "max_global": self._max_global,
            "running": len(self._running),
            "queued": len(self._queue),
            "known_runtimes": len(self._runtimes),
        }

    # ===================================================================== #
    # 退出
    # ===================================================================== #
    async def shutdown(self, timeout: float = 5.0) -> None:
        """尽力取消所有正在跑的任务（进程退出前调用）。"""
        self._stopping = True
        targets = [rt for rt in self._runtimes.values() if rt.status not in TERMINAL]
        for rt in targets:
            rt.cancel()

        if not targets:
            return

        try:
            await asyncio.wait_for(
                asyncio.gather(*(rt.finished.wait() for rt in targets), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("关闭时有 %d 个任务未在 %.1fs 内结束", len(targets), timeout)
