"""
子任务派生（M5）：父 Agent 通过 `spawn_subtask` 派生一个**子 Agent 任务**。

★ 它复用了什么（不重建）
--------------------
· 子任务就是一个普通的 AgentCore 任务：同样的 `Policy` / `ApprovalManager` /
  预算 / 验证闸门 / 事件 / 历史落盘，只是换了 task_id 与更小的预算。
· 子任务**没有自己的注册表**：与父任务共用同一个 ToolRegistry
  （所以 MCP 工具、RAG 的 `search_docs`、内置工具在子任务里都在，且同样要过 Policy）。
· 证据不另造：直接读子任务 `TaskState.audit` 与核心的 `_verification_view()`。

★ 首版的硬边界
------------
· **串行**：一次只跑一个子任务（父任务在 `await` 里等），不做并行调度。
· **最大派生层级**（默认 2）：子任务再派生孙任务会被拒绝，不会无限递归。
· **每个父任务的子任务数量上限**（默认 3）。
· **子任务预算更小**（轮次/秒/工具调用都单独收窄），但**计入同一套预算机制**，
  不是"派生一次就白拿一份新预算"。
· **取消联动**：父任务的 cancel_event 被 set → 立刻 `core.cancel()` 子任务并等其收尾；
  父任务不会被子任务拖住（单次派生还有总超时兜底）。
· **结果不盲信**：子任务返回 `done` 也不等于"已验证"——
  返回的 `evidence`（等级/理由/命令/改动）才是父任务的判据。

★ 为什么 spawner 是"注入"的
------------------------
工具不能自己去 import store / hub（那样就不可测、也拿不到当前任务上下文）。
和 RAG 的 `knowledge`、MCP 的 `mcp` 一样：由服务端装配好，经 `ToolContext.spawner`
传给工具 —— 工具只看到"给我一个目标，还我一个结构化结果"。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("loom.subtask")


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:10]}"


@dataclass
class SubtaskResult:
    """一次派生结果（给父 Agent 看的结构化信封）。"""

    task_id: str = ""
    status: str = ""            # done / aborted / failed / rejected
    end_reason: str = ""
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    engine: str = ""
    error: str = ""
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """子任务是否**真的完成了目标**（而不是"调用本身没报错"）。"""
        return self.status == "done"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "end_reason": self.end_reason,
            "summary": self.summary,
            "evidence": self.evidence,
            "tools": self.tools,
            "commands": self.commands,
            "changes": self.changes,
            "turn": self.turn,
            "engine": self.engine,
            "error": self.error,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }


class SubtaskSpawner:
    """派生并**同步等待**一个子任务（首版串行）。"""

    def __init__(
        self,
        *,
        store: Any,
        adapter: Any,
        registry: Any,
        config: Any,
        hub: Any = None,
        session_id: str = "",
        depth: int = 0,
        max_depth: int = 2,
        max_children: int = 3,
        child_max_turns: int = 8,
        child_max_seconds: float = 180.0,
        child_max_tool_calls: int = 12,
        approval_timeout_s: float = 60.0,
        timeout_s: float = 240.0,
        child_use_plan_phase: bool = False,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.registry = registry
        self.config = config
        self.hub = hub
        self.session_id = session_id
        self.depth = depth
        self.max_depth = max_depth
        self.max_children = max_children
        self.child_max_turns = child_max_turns
        self.child_max_seconds = child_max_seconds
        self.child_max_tool_calls = child_max_tool_calls
        self.approval_timeout_s = approval_timeout_s
        self.timeout_s = timeout_s
        self.child_use_plan_phase = child_use_plan_phase

    # ---- 内部：为下一层准备（层级 +1，其余继承）------------------------
    def _child_spawner(self, session_id: str) -> "SubtaskSpawner":
        return SubtaskSpawner(
            store=self.store, adapter=self.adapter, registry=self.registry,
            config=self.config, hub=self.hub, session_id=session_id,
            depth=self.depth + 1, max_depth=self.max_depth,
            max_children=self.max_children,
            child_max_turns=self.child_max_turns,
            child_max_seconds=self.child_max_seconds,
            child_max_tool_calls=self.child_max_tool_calls,
            child_use_plan_phase=self.child_use_plan_phase,
            approval_timeout_s=self.approval_timeout_s,
            timeout_s=self.timeout_s,
        )

    def _reject(self, reason: str) -> SubtaskResult:
        return SubtaskResult(status="rejected", end_reason="spawn_rejected", error=reason)

    # ---- 主入口 --------------------------------------------------------
    async def spawn(
        self,
        *,
        goal: str,
        parent_task_id: str,
        workspace: str,
        allowed_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
        cancel_event: Any = None,
    ) -> SubtaskResult:
        from ..events import EventBus
        from ..agent.approval import ApprovalManager
        from ..agent.core import AgentCore, AgentConfig
        from ..server.runtime import StoreHistorySink

        goal = (goal or "").strip()
        if not goal:
            return self._reject("子任务目标为空")

        # ---- ① 层级与数量上限（越权派生在这里就被挡住，不给执行机会）----
        if self.depth >= self.max_depth:
            return self._reject(
                f"已达到最大派生层级（{self.max_depth}）：不允许再派生子任务"
            )
        try:
            children = await self.store.count_children(parent_task_id)
        except Exception as exc:  # noqa: BLE001
            return self._reject(f"子任务数量检查失败：{type(exc).__name__}: {exc}")
        if children >= self.max_children:
            return self._reject(
                f"子任务数量已达上限（{self.max_children} 个）：不允许再派生"
            )

        parent = await self.store.get_task(parent_task_id)
        if not parent:
            return self._reject(f"父任务不存在：{parent_task_id}")

        session_id = parent.get("session_id") or self.session_id
        child_id = new_task_id()

        # ---- ② 落库（parent_task_id 在这里写死，不是事后补）-------------
        await self.store.create_task({
            "task_id": child_id,
            "session_id": session_id,
            "parent_task_id": parent_task_id,
            "goal": goal,
            "status": "running",
            "model": parent.get("model", ""),
            "workspace": workspace,
        })
        await self.store.mark_started(child_id)

        # ---- ③ 子任务自己的事件总线（并转发到 hub，前端可按 task_id 订阅）----
        bus = EventBus()
        bus.bind_task(child_id)
        if self.hub is not None:
            def _forward(ev):  # noqa: ANN001
                try:
                    asyncio.create_task(self.hub.publish(child_id, session_id, ev))
                except Exception:  # noqa: BLE001
                    pass
            bus.subscribe(_forward)

        # ---- ④ 构造子 Agent（预算更小、引擎继承父任务）----------------
        child_cfg = AgentConfig(
            max_turns=min(self.child_max_turns, getattr(self.config, "max_turns", 20)),
            max_seconds=min(self.child_max_seconds, getattr(self.config, "max_seconds", 300.0)),
            max_tool_calls=min(self.child_max_tool_calls, getattr(self.config, "max_tool_calls", 40)),
            min_evidence=getattr(self.config, "min_evidence", 1),
            # ★ 子任务默认不再自己规划/反思：父任务已经做过拆解，
            #   子任务应该"直奔目标 + 拿证据"，省下的轮次留给工具与验证。
            use_plan_phase=self.child_use_plan_phase,
            use_reflect_phase=False,
            approval_timeout_s=self.approval_timeout_s,
            stream=getattr(self.config, "stream", True),
            compact_max_chars=getattr(self.config, "compact_max_chars", 60_000),
            compact_keep_turns=getattr(self.config, "compact_keep_turns", 3),
            budget_extend_turns=getattr(self.config, "budget_extend_turns", 10),
            budget_extend_enabled=getattr(self.config, "budget_extend_enabled", True),
            # ★ 引擎与父任务一致（M4.3 起默认 graph；显式 legacy 时也跟着 legacy）
            orchestrator=getattr(self.config, "orchestrator", "graph"),
            rag_auto=getattr(self.config, "rag_auto", True),
        )
        core = AgentCore(
            self.adapter, self.registry,
            config=child_cfg,
            bus=bus,
            approvals=ApprovalManager(timeout_s=self.approval_timeout_s),
            history_sink=StoreHistorySink(self.store),
            spawner=self._child_spawner(session_id),
        )

        # ---- ⑤ 串行执行 + 超时 / 取消联动 -----------------------------
        started = time.time()
        result = SubtaskResult(task_id=child_id, engine=child_cfg.orchestrator)
        runner = asyncio.create_task(
            core.run(goal, workspace=workspace, task_id=child_id,
                     allowed_paths=allowed_paths, forbidden_paths=forbidden_paths)
        )
        cancel_waiter = None
        if cancel_event is not None:
            cancel_waiter = asyncio.create_task(_wait_event(cancel_event))

        waiters = [runner] + ([cancel_waiter] if cancel_waiter else [])
        try:
            done, _pending = await asyncio.wait(
                waiters, timeout=self.timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        except Exception as exc:  # noqa: BLE001
            core.cancel()
            result.error = f"{type(exc).__name__}: {exc}"
            done, _pending = set(), set()

        if runner in done:
            state = runner.result()
        elif cancel_waiter is not None and cancel_waiter in done:
            # 父任务被取消 → 立刻停止子任务，并等它把当前一步走完（有界）
            result.cancelled = True
            core.cancel()
            state = await _settle(runner, timeout=5.0)
        else:
            # 超时：不等了 —— 父任务不能被子任务无限拖住
            result.timed_out = True
            core.cancel()
            state = await _settle(runner, timeout=5.0)
            if state is not None and not state.end_reason:
                result.error = f"子任务超时（>{self.timeout_s:.0f}s）"
        if cancel_waiter is not None and not cancel_waiter.done():
            cancel_waiter.cancel()

        # ---- ⑥ 落终态 + 组装结果 --------------------------------------
        if state is None:
            state = core.state
        if state is not None:
            result.status = state.status.value
            result.end_reason = state.end_reason or ""
            # ★ 超时/取消后，AgentCore 可能还没走到 finish()（它只在下一轮检查取消）。
            #   这时 state.status 仍是 running —— 不能把"还在跑"当成终态返回给父任务，
            #   统一收敛成 aborted + 明确原因，避免父任务误读。
            if result.status in ("running", "created", ""):
                if result.timed_out:
                    result.status, result.end_reason = "aborted", "timeout"
                elif result.cancelled:
                    result.status, result.end_reason = "aborted", "cancelled"
                else:
                    result.status, result.end_reason = "aborted", result.end_reason or "unknown"
            if not result.end_reason:
                result.end_reason = "timeout" if result.timed_out else ("cancelled" if result.cancelled else result.end_reason)
            result.summary = (state.final_answer or "")[:2000]
            result.turn = state.turn
            result.engine = getattr(state, "engine", "") or child_cfg.orchestrator
            result.tools = [
                {"tool": e.tool, "ok": e.ok, "summary": e.summary} for e in state.tool_events
            ]
            result.commands = [
                {"command": c.command, "exit_code": c.exit_code, "ok": c.ok}
                for c in state.command_history
            ]
            result.changes = [dict(c) if isinstance(c, dict) else {"detail": str(c)}
                              for c in state.change_records]
            result.evidence = _evidence_of(core, state)
            try:
                await self.store.save_task(
                    child_id,
                    status=result.status,
                    state=state.to_dict(),
                    end_reason=result.end_reason,
                    result=result.summary,
                    ended_at=time.time(),
                    turn=state.turn,
                )
            except Exception as exc:  # noqa: BLE001 —— 落盘失败不能吞掉子任务结果
                log.warning("子任务 %s 落盘失败：%s", child_id, exc)
        else:
            result.status = "failed"
            result.end_reason = "no_state"

        log.info("子任务 %s（父 %s）：%s/%s，%d 轮，%.1fs", child_id, parent_task_id,
                 result.status, result.end_reason, result.turn, time.time() - started)
        return result


async def _wait_event(ev: Any) -> None:
    """等一个 threading/asyncio Event（兼容两种实现）。"""
    is_set = getattr(ev, "is_set", None)
    wait = getattr(ev, "wait", None)
    if wait is None:
        return
    while True:
        if is_set and is_set():
            return
        try:
            if asyncio.iscoroutinefunction(wait):
                await wait()
            else:
                import asyncio as _a
                await _a.to_thread(wait, 0.2)
        except Exception:  # noqa: BLE001
            return
        await asyncio.sleep(0.05)


async def _settle(task: asyncio.Task, *, timeout: float) -> Any:
    """取消后等任务收尾（有界，避免父任务卡死）。"""
    if task.done():
        return task.result() if not task.cancelled() else None
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except Exception:  # noqa: BLE001
        task.cancel()
        return None


def _evidence_of(core: Any, state: Any) -> dict[str, Any]:
    """
    取子任务的证据等级（复用既有 `_verification_view`，不另造一套分级）。
    """
    try:
        ev, risks = core._verification_view(state)
        out = dict(ev or {})
        if risks:
            out["risks"] = [
                {"path": r.get("path"), "level": r.get("level")} for r in risks[:5]
            ]
        return out
    except Exception:  # noqa: BLE001
        audit = getattr(state, "audit", None) or {}
        return dict(audit.get("evidence") or {})
