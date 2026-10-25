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

        # ---- 落盘故障（可诊断性）----
        # 落盘失败时不能只留一行日志：把故障记在这里，`/api/tasks/{id}` 能查到
        self.persist_failures: int = 0
        self.persist_error: dict[str, Any] | None = None

        # ---- 落盘顺序 ----
        # 广播前置之后，两次落盘是并发的：审批请求那次如果很慢（DB 抖动），
        # 可能晚于审批决策那次才写完，把库里的状态**写回退**。
        # 这把锁保证同一任务的状态转换按发生顺序落盘。
        self._persist_lock = asyncio.Lock()

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
            except Exception as exc:  # noqa: BLE001
                self._record_persist_failure(
                    "final",
                    exc,
                    context=(
                        f"status={self.status}, "
                        f"turn={self.core.state.turn if self.core.state else None}；"
                        f"结论只在内存里，进程重启后不可恢复"
                    ),
                )
            self.finished.set()

    # ===================================================================== #
    # 事件桥：EventBus → hub
    # ===================================================================== #
    async def _on_event(self, ev: Event) -> None:
        """
        EventBus 的每条事件都经过这里，转给 hub 广播。

        ★ 顺序：先改内存状态 → **立刻广播** → 最后落盘。
          之前是「先落盘再广播」，有两个后果：
            ① 落盘是个真正的 await，会把广播推到后面，SSE 的到达顺序
               被数据库耗时打乱（seq 是对的，但到达乱序）；
            ② 落盘一旦抛异常，广播根本不会执行 —— 事件直接消失，
               任务内 seq 会留下一个永远补不回来的空洞。
          广播放在落盘之前，落盘失败就只影响"重启后能不能恢复"，
          不影响实时事件。

        ★ 广播本身没有 await 让出点（hub.publish 内部只有 deque.append 和
          queue.put_nowait），所以多个投递协程不会交错，
          事件顺序 == emit 顺序。

        ★ 为什么要把 `ev.task_id` 改掉？
          `AgentCore.run()` 内部会创建自己的 `TaskState`（自带一个新 task_id），
          并调用 `bus.bind_task(state.task_id)` —— 于是事件里的 task_id 是
          **Core 生成的**，和调度器分配的 task_id 不是同一个。
          对前端来说这是致命的：按 event.task_id 去 store 里查任务会找不到。

          所以这里统一改写成运行时（= 调度器/数据库）的 task_id。
          不改 Core —— 它自己的 id 在内部仍然自洽。
        """
        if ev.task_id != self.task_id:
            ev.task_id = self.task_id

        # ---- ① 服务层状态（同步，先改好，快照立刻看得到）----
        persist_status = ""
        if ev.type == "approval.request":
            self.status = "awaiting_approval"
            persist_status = "awaiting_approval"
        elif ev.type == "approval.decision":
            self.status = "running"
            persist_status = "running"
        elif ev.type == "final_answer":
            text = (ev.data or {}).get("text", "")
            if text:
                self.result = str(text)

        # ---- ② 广播：不能被落盘失败拖累 ----
        await self._hub.publish(self.task_id, self.session_id, ev)

        # ---- ③ 落盘：审批等待/恢复是"任务停在哪儿"的关键信息 ----
        if persist_status:
            try:
                await self._persist_state(persist_status)
            except Exception as exc:  # noqa: BLE001
                self._record_persist_failure(
                    f"state:{persist_status}", exc, event_type=ev.type
                )

    def _record_persist_failure(
        self,
        stage: str,
        exc: BaseException,
        *,
        event_type: str = "",
        context: str = "",
    ) -> None:
        """
        记一次落盘失败：计数 + 结构化字段 + 带堆栈的 ERROR 日志。

        ★ 不吞异常的正确姿势：
          调用方**已经**把该做的事（广播）做完了，这里的失败不影响实时事件，
          只影响"重启后能不能恢复"。但必须留下足以定位的信息：
          哪个任务、哪一步、哪类事件、什么错 —— 而不是一行 `xxx 落盘失败`。

          同时写进 `persist_error`，`GET /api/tasks/{id}` 能直接查到；
          进程还在时这是最方便的排查入口。
        """
        self.persist_failures += 1
        self.persist_error = {
            "stage": stage,
            "event_type": event_type,
            "error": f"{type(exc).__name__}: {exc}",
            "at": time.time(),
            "count": self.persist_failures,
        }
        log.error(
            "任务 %s 落盘失败（stage=%s, event=%s, 第 %d 次%s）：%s: %s",
            self.task_id,
            stage,
            event_type or "-",
            self.persist_failures,
            f", {context}" if context else "",
            type(exc).__name__,
            exc,
            exc_info=exc,
        )

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
            # ---- 落盘故障（正常情况是 0 / None）----
            # 有值时说明「库里的快照可能是旧的」，排查入口就在这里
            "persist_failures": self.persist_failures,
            "persist_error": self.persist_error,
        }

    # ===================================================================== #
    # 落盘
    # ===================================================================== #
    def _state_for_persist(self) -> dict[str, Any]:
        """
        落库用的快照。

        ★ 去掉 `persist_*` 两个字段：它们是**本次进程内的诊断信息**，
          不属于 Agent 状态。留在 state_json 里会出现
          「这次成功了，但库里永远带着上次的报错」，反而误导。
          诊断信息只走 `snapshot()`（HTTP 快照）和日志。
        """
        snap = self.snapshot()
        snap.pop("persist_failures", None)
        snap.pop("persist_error", None)
        return snap

    async def _persist_state(self, status: str) -> None:
        """
        中间状态（审批等待/恢复）落盘。

        ★ 两层保护，防止"慢的旧写入"覆盖"快的新写入"：
          ① `_persist_lock`：同一任务的落盘串行执行，先发生的先写；
          ② 状态校验：真正写之前再看一眼 `self.status` —— 如果已经被更晚的
             事件推进了（比如 approval.request 的落盘还在排队，approval.decision
             已经把它推到 running），就**跳过**这次旧写入。

        只加锁不校验是不够的：`_persist_final` 写完之后，队列里可能还压着
        一条 `awaiting_approval`，它会把终态盖回去。
        """
        async with self._persist_lock:
            if self.status != status:
                return
            await self._store.save_task(
                self.task_id,
                status=status,
                state=self._state_for_persist(),
                turn=self.core.state.turn if self.core.state else None,
            )

    async def _persist_final(self) -> None:
        """终态落盘：摘要字段 + 完整快照，同一个 UPDATE。"""
        async with self._persist_lock:
            await self._store.save_task(
                self.task_id,
                status=self.status,
                state=self._state_for_persist(),
                end_reason=self.end_reason,
                result=self.result,
                ended_at=self.ended_at or time.time(),
                turn=self.core.state.turn if self.core.state else None,
            )
