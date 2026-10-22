"""
一个 Task 的运行时

★ 它做的事，就是 CLI 里 `run_one()` 做的事：
      造 EventBus / ApprovalManager / AgentConfig / AgentCore
      → 订阅事件
      → await core.run(...)
  **Agent Loop、Policy、Approval Manager 一行都没改。**

★ 三个状态来源要分清
────────────────────────────────────────────────────────────────
    core.state        Agent 的权威状态（TaskState）
    self.status       服务层视角的粗粒度状态（created/running/done/failed...）
    事件流             给前端看的执行过程

  落盘时以 `core.state.to_dict()` 为准（它是唯一真相），服务层只补
  session_id 之类 core 不知道的字段。

★ 为什么 started_at 单独记
────────────────────────────────────────────────────────────────
  排队中的任务 status=created、started_at=NULL。
  只有真的被调度起来（拿到并发槽位）才 mark_started() —— 
  否则"排了 3 分钟"会被误读成"任务跑了 3 分钟"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..agent.approval import ApprovalManager
from ..agent.core import AgentConfig, AgentCore
from ..events import Event, EventBus
from ..models.adapter import ModelAdapter
from ..tools.registry import ToolRegistry
from .hub import EventHub
from .store import TaskStore

log = logging.getLogger("loom.server.runtime")


class TaskRuntime:
    """单个任务的生命周期。一个实例只管一个 task_id。"""

    def __init__(
        self,
        *,
        task_id: str,
        session_id: str,
        goal: str,
        workspace: str,
        model: str,
        adapter: ModelAdapter,
        registry: ToolRegistry,
        config: AgentConfig,
        hub: EventHub,
        store: TaskStore,
    ) -> None:
        self.task_id = task_id
        self.session_id = session_id
        self.goal = goal
        self.workspace = workspace
        self.model = model

        self._hub = hub
        self._store = store
        self._config = config

        # ---- 每个任务独立的一套 ----
        self.bus = EventBus()
        self.bus.bind_task(task_id)
        self.approvals = ApprovalManager(timeout_s=config.approval_timeout_s)
        self.core = AgentCore(adapter, registry, config=config, bus=self.bus, approvals=self.approvals)

        # ---- 服务层状态 ----
        self.status: str = "created"
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.end_reason: str = ""
        self.result: str = ""

        # 完成信号（app.py 用它判断能不能回收），也用于测试等待
        self.finished = asyncio.Event()

        self._run_task: asyncio.Task[Any] | None = None
        self._cancel_requested = False

    # ===================================================================== #
    # 生命周期
    # ===================================================================== #
    async def start(self) -> None:
        """
        真正开始执行。

        ★ 这里才填 started_at —— 排队期间是 NULL。
        """
        if self.status != "created":
            return

        self.status = "running"
        self.started_at = time.time()
        await self._store.mark_started(self.task_id)

        # ★ 必须在 core.run 之前订阅：否则 task.start 事件就漏了
        self.bus.subscribe(self._on_event)

        self._run_task = asyncio.create_task(self._run(), name=f"loom-task-{self.task_id}")

    async def _run(self) -> None:
        try:
            state = await self.core.run(self.goal, workspace=self.workspace, model_label=self.model)

            self.status = state.status.value
            self.end_reason = state.end_reason
            self.result = state.final_answer or ""
            self.ended_at = time.time()

        except asyncio.CancelledError:
            # 任务被外部取消（很少走到这里，core.cancel 是协作式的）
            self.status = "aborted"
            self.end_reason = "cancelled"
            self.ended_at = time.time()
            raise

        except Exception as exc:  # noqa: BLE001 —— 服务层不能因为一个任务崩掉
            log.exception("任务 %s 执行异常", self.task_id)
            self.status = "failed"
            self.end_reason = "internal_error"
            self.ended_at = time.time()
            await self._hub.publish(
                self.task_id,
                self.session_id,
                Event.make(
                    "error",
                    {"kind": "internal_error", "message": f"{type(exc).__name__}: {exc}", "fatal": True},
                    task_id=self.task_id,
                ),
            )

        finally:
            try:
                await self._persist_final()
            except Exception:  # noqa: BLE001
                log.exception("任务 %s 落盘失败", self.task_id)
            self.finished.set()

    # ===================================================================== #
    # 事件桥：EventBus → hub
    # ===================================================================== #
    async def _on_event(self, ev: Event) -> None:
        """
        EventBus 的每条事件都经过这里，转给 hub 广播。

        ★ 这个函数里**没有任何真正的 await 让出点**（hub.publish 内部只有
          deque.append 和 queue.put_nowait），所以多个投递协程不会交错，
          事件的先后顺序与 emit 顺序一致。
        """
        # ---- 顺带维护服务层状态 ----
        if ev.type == "approval.request":
            self.status = "awaiting_approval"
            # 审批等待时立刻落盘：这是"任务停在哪儿"的关键信息
            await self._persist_state("awaiting_approval")

        elif ev.type == "approval.decision":
            self.status = "running"
            await self._persist_state("running")

        elif ev.type == "final_answer":
            text = (ev.data or {}).get("text", "")
            if text:
                self.result = str(text)

        await self._hub.publish(self.task_id, self.session_id, ev)

    # ===================================================================== #
    # 控制
    # ===================================================================== #
    def cancel(self) -> bool:
        """
        请求取消。

        返回 True 表示「请求已被接受」—— **不代表任务已经停了**。
        真正的终止要看 `status` 变成 aborted。这一点必须在 HTTP 层说清楚。
        """
        if self.status in ("done", "aborted", "failed"):
            return False

        self._cancel_requested = True
        self.core.cancel()          # 设置 cancel_event + 取消挂起的审批
        return True

    async def resolve_approval(self, approval_id: str, decision: str, reason: str = "") -> bool:
        """
        把用户的决策交回 ApprovalManager。

        ★ 直接调现有 `resolve()` —— 它自带一次性消费（pop）、超时处理、
          args_hash 校验（在执行前的 recheck 里）。服务层不重新实现任何一条。
        """
        accepted = self.approvals.resolve(approval_id, decision, reason)
        if not accepted:
            return False

        # 状态由 _on_event 里收到 approval.decision 后改回 running，
        # 但那条事件是 core 发出的，可能稍晚。这里先乐观改一下，
        # 避免 200ms 内前端来查快照仍看到 awaiting_approval。
        if self.status == "awaiting_approval":
            self.status = "running"
        return True

    # ===================================================================== #
    # 快照
    # ===================================================================== #
    def snapshot(self) -> dict[str, Any]:
        """
        当前任务快照。

        以 `core.state.to_dict()` 为基础（它是 Agent 的权威状态），
        再补上 core 不知道的服务层字段（session_id / created_at 等）。
        """
        state = self.core.state
        base = state.to_dict() if state else {}

        # ★ 事件可用性：**看 hub 里是不是真的还有这个任务的事件**
        #   （之前这里是 `... or True` 的恒真占位，语义是错的）
        buffered = self._hub.buffered_for_task(self.task_id)

        return {
            **base,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "workspace": self.workspace,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "result": self.result,
            # ---- 事件可用性（内存视角）----
            # available=false 时前端要明确提示"事件历史不可用"，不能假装完整
            "events_available": bool(buffered),
            "events_buffered": len(buffered),
        }

    # ===================================================================== #
    # 落盘
    # ===================================================================== #
    async def _persist_state(self, status: str) -> None:
        """中间状态（审批等待/恢复）落盘。"""
        await self._store.save_task(
            self.task_id,
            status=status,
            state=self.snapshot(),
            turn=self.core.state.turn if self.core.state else None,
        )

    async def _persist_final(self) -> None:
        """终态落盘：摘要字段 + 完整快照，同一个 UPDATE。"""
        await self._store.save_task(
            self.task_id,
            status=self.status,
            state=self.snapshot(),
            end_reason=self.end_reason,
            result=self.result,
            ended_at=self.ended_at or time.time(),
            turn=self.core.state.turn if self.core.state else None,
        )
