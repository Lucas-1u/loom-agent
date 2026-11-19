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
import copy
import json
import logging
import os
import time
import uuid
from typing import Any

from ..agent.checkpoint import LocalDiskCheckpointStore, plan_resume
from ..agent.core import AgentConfig
from ..models.adapter import ModelAdapter
from ..tools.registry import ToolRegistry
from .hub import EventHub
from .runtime import FORCE_CANCEL_GRACE_S, TaskRuntime
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
        knowledge: object | None = None,
        spawner: object | None = None,
        readonly_registry: ToolRegistry | None = None,
        no_desktop_registry: ToolRegistry | None = None,
    ) -> None:
        self._store = store
        self._hub = hub
        self._adapter = adapter          # 无状态，可被多个任务共用
        self._registry = registry
        # 步骤 7：read-only 会话模式用的工具子集（只含 risk=read）。
        # 不传 = 服务端不支持按模式切换（老行为，read-only 只声明不强制）。
        self._readonly_registry = readonly_registry
        # ★ desktop 模式接入：剔除桌面工具（computer 组 + desktop_* MCP 工具）的注册表，
        #   workspace-write / full-access 使用。不传 = 不支持剔除（老行为）。
        self._no_desktop_registry = no_desktop_registry
        self._config = config
        self._max_global = max_global
        # M3：项目知识库（同一进程内被所有任务共享；按 workspace 隔离数据）
        self._knowledge = knowledge
        # M5：子任务派生器
        self._spawner = spawner

        self._runtimes: dict[str, TaskRuntime] = {}
        self._session_of: dict[str, str] = {}
        self._scope_of: dict[str, tuple[list[str], list[str]]] = {}
        # 步骤 6：权限模式跟着任务走（排队任务可能几秒后才启动）
        self._mode_of: dict[str, str] = {}
        # 对比表 #21：联网搜索开关跟着任务走（每个任务可独立决定是否允许搜索）
        self._web_search_of: dict[str, bool] = {}
        # Checkpoint 恢复：待续跑的现场（launch 时取出并清空）
        self._resume_of: dict[str, Any] = {}
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
        allowed_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
        mode: str = "",
        web_search: bool = False,
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
            # ★ 范围声明跟着任务走：排队任务可能几秒后才启动，
            #   不能依赖"提交时的局部变量"还在。
            self._scope_of[tid] = (list(allowed_paths or []), list(forbidden_paths or []))
            # 步骤 6：模式先落表，等 _launch 时再校验（非法值在 core.run 里抛）
            self._mode_of[tid] = str(mode or "")
            # 对比表 #21：联网搜索开关先落表（_registry_for 在 launch 时读）
            self._web_search_of[tid] = bool(web_search)
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

    def _registry_for(self, task_id: str) -> ToolRegistry:
        """
        按任务的会话模式 + 联网搜索开关挑注册表。

        read-only            → 只读视图（真强制：写/执行工具根本没注册）
        workspace-write / full-access → 剔除桌面工具的注册表
        desktop              → 完整注册表（含桌面工具；用户显式授权的桌面操控模式）

        ★ 联网搜索开关（对比表 #21）：web_search 工具默认注册在 readonly 组
          （risk=READ），但任务级开关决定它是否可见——关闭时从注册表里剔除
          （服务端真强制，模型看不到这个工具，不会去调）。

        ★ 非法模式这里不拦：它会在 core.run 里抛 ValueError，任务以 failed 收尾，
          这样错误出现在**任务该出现的地方**（而不是调度器里静默降级）。
        """
        mode = (self._mode_of.get(task_id) or "").strip().lower()
        if mode == "read-only" and self._readonly_registry is not None:
            reg = self._readonly_registry
        elif mode == "desktop":
            reg = self._registry
        elif self._no_desktop_registry is not None:
            reg = self._no_desktop_registry
        else:
            reg = self._registry

        # ---- 联网搜索开关：关闭时剔除 web_search 工具 ----
        # ★ 用 without()（保留分组与工具实例），不用 filter()：
        #   filter 会把幸存工具全塞进 group="filtered"，丢掉 readonly/exec 语义。
        if not self._web_search_of.get(task_id, False) and "web_search" in reg.names:
            reg = reg.without("web_search")
        return reg

    # ===================================================================== #
    # Checkpoint 恢复（2026-11-11 接线）
    # ===================================================================== #
    def _checkpoint_store_for(self, workspace: str):
        """按工作区定位 checkpoint 目录（口径与 TaskRuntime 内部一致）。"""
        cfg = getattr(self._config, "checkpoint", None)
        if not cfg or not getattr(cfg, "enabled", False):
            return None
        base = os.path.join(workspace, getattr(cfg, "dir_name", ".loom_checkpoints"))
        return LocalDiskCheckpointStore(base)

    async def inspect_resume(self, task_id: str) -> tuple[Any, Any]:
        """
        装配恢复现场 —— **只读**：不改状态、不入队、不发事件。

        返回 `(plan, report)`；plan 为 None 时看 report.reasons 就知道为什么不能续。
        """
        from ..agent.checkpoint import ResumeReport

        row = await self._store.get_task(task_id)
        if row is None:
            rep = ResumeReport(task_id=task_id)
            rep.reasons.append("任务不存在")
            return None, rep

        cfg = getattr(self._config, "checkpoint", None)
        max_age = float(getattr(cfg, "resume_max_age_s", 0.0) or 0.0)
        entries = await self._store.load_history(task_id)
        store = self._checkpoint_store_for(row["workspace"])
        return plan_resume(
            store, task_id, entries,
            workspace=row["workspace"], max_age_s=max_age,
        )

    async def resume(self, task_id: str) -> dict[str, Any]:
        """
        续跑一个中断的任务：装配现场 → 重新入队（优先插队）。

        ★ 返回结构与其他控制接口一致：`accepted` 表示"请求被接受"，
          不代表任务已经跑完；真正的结果看后续事件/快照。
        """
        rt = self._runtimes.get(task_id)
        if rt is not None and rt.status not in TERMINAL:
            return {"accepted": False, "state": rt.status, "note": "任务正在运行，无需恢复"}

        row = await self._store.get_task(task_id)
        if row is None:
            return {"accepted": False, "state": "missing", "note": "任务不存在"}

        plan, report = await self.inspect_resume(task_id)
        if plan is None:
            return {
                "accepted": False,
                "state": "not_resumable",
                "note": "；".join(report.reasons) or "不可恢复",
                "report": report.to_dict(),
            }

        async with self._lock:
            self._session_of[task_id] = row["session_id"]
            # ★ 模式与范围沿用快照：换成这次的默认值等于偷偷放宽/收紧权限
            self._mode_of[task_id] = plan.state.session_mode
            self._scope_of[task_id] = (
                list(plan.state.allowed_paths), list(plan.state.forbidden_paths),
            )
            self._web_search_of.setdefault(task_id, False)
            self._resume_of[task_id] = plan
            if task_id in self._queue:
                self._queue.remove(task_id)
            self._queue.insert(0, task_id)  # 恢复任务优先于新任务

        # 状态回到 created：真正变 running 由 launch 负责（前端看到"重新受理"）
        # ★ 快照里的 status 也要一起改成 created —— 否则摘要列说 created、
        #   state_json 里还是 running（"列表与快照不一致"是既有审计明确禁止的）。
        snapshot = plan.state.to_dict()
        snapshot["status"] = "created"
        await self._store.save_task(task_id, status="created", state=snapshot)
        log.info(
            "任务 %s 排入恢复队列（checkpoint #%d，第 %d 轮，历史 %d 条）",
            task_id, report.checkpoint_seq, report.resumed_from_turn, report.loaded_messages,
        )
        await self.pump()
        return {
            "accepted": True,
            "state": "queued",
            "note": "已排入恢复队列",
            "report": report.to_dict(),
        }

    async def recover_interrupted(self) -> dict[str, Any]:
        """
        服务启动时处理「看起来还在跑」的任务（running / awaiting_approval / created）。

        ★ 与旧实现的区别（这就是本次要修的债）：
          旧逻辑 `mark_orphans_failed` 一刀切标 failed —— 明明有 checkpoint 也不看，
          任务永远没有第二次机会。现在的顺序是：**先判能不能续，再决定怎么收尾**。
          · 判定通过 + 开了自动恢复 → 重新入队续跑；
          · 判定不通过 → 标 aborted + 把 ResumeReport 写进快照（说明为什么不能续）。

        ★ checkpoint 关闭时退回旧行为（标 failed），不静默改变语义。
        """
        cfg = getattr(self._config, "checkpoint", None)
        ckpt_on = bool(cfg and getattr(cfg, "enabled", False))
        if not ckpt_on:
            fixed = await self._store.mark_orphans_failed("server_restarted")
            return {"scanned": len(fixed), "resumed": [], "marked": fixed, "checkpoint": False}

        rows = await self._store.list_unfinished()
        auto = bool(getattr(cfg, "auto_resume", False))
        summary: dict[str, Any] = {
            "scanned": len(rows), "resumed": [], "marked": [], "checkpoint": True,
        }

        for row in rows:
            tid = row["task_id"]
            plan, report = await self.inspect_resume(tid)

            if plan is not None and auto:
                try:
                    result = await self.resume(tid)
                    if result.get("accepted"):
                        summary["resumed"].append({
                            "task_id": tid,
                            "checkpoint_seq": report.checkpoint_seq,
                            "turn": report.resumed_from_turn,
                        })
                        continue
                    report.reasons.append(str(result.get("note") or "入队被拒绝"))
                except Exception as exc:  # noqa: BLE001 —— 单个任务恢复失败不能拖垮启动
                    log.warning("任务 %s 自动恢复失败：%s: %s", tid, type(exc).__name__, exc)
                    report.reasons.append(f"自动恢复失败：{type(exc).__name__}: {exc}")

            # ---- 不可恢复（或未开自动恢复）：诚实收尾，并留下原因 ----
            try:
                st = json.loads(row.get("state_json") or "{}")
            except (TypeError, ValueError):
                st = {}
            if not isinstance(st, dict):
                st = {}
            now = time.time()
            st["status"] = "aborted"
            st["end_reason"] = "interrupted"
            st["ended_at"] = now
            st["pending_approval"] = None
            st["resume_report"] = report.to_dict()
            await self._store.save_task(
                tid, status="aborted", state=st,
                end_reason="interrupted", ended_at=now,
            )
            summary["marked"].append({"task_id": tid, "report": report.to_dict()})
            log.info(
                "任务 %s 标记为 interrupted（不可续：%s）",
                tid, "；".join(report.reasons) or "未开启自动恢复",
            )

        if summary["resumed"] or summary["marked"]:
            log.info(
                "启动恢复：扫描 %d 个中断任务，续跑 %d 个，收尾 %d 个",
                summary["scanned"], len(summary["resumed"]), len(summary["marked"]),
            )
        return summary

    async def _launch(self, task_id: str) -> None:
        """真正启动一个任务（锁外执行）。"""
        session_id = self._session_of.get(task_id, "")
        allowed_paths, forbidden_paths = self._scope_of.get(task_id, ([], []))
        row = await self._store.get_task(task_id)
        if row is None:
            log.error("任务 %s 在库里不存在，跳过", task_id)
            async with self._lock:
                self._running.discard(task_id)
            return

        # ---- 预算：desktop 模式用专属预算 --------------------------------
        # ★ 桌面控制是"截图→定位→点击→验证"的多轮往返，默认 300s 天然偏紧，
        #   会被墙钟预算中途砍掉。用副本覆盖，**不直接改**共享的 self._config
        #   （否则会污染其他模式的预算）。
        # ★ 三个上限的默认值都是 0 = 不设上限（用户 2026-11-13 要求）。
        #   真正兜底的刹车仍在：软熔断（同参重复）、连续错误、无进展终止。
        mode = self._mode_of.get(task_id, "")
        task_config = self._config
        if mode == "desktop":
            task_config = copy.copy(self._config)
            task_config.max_seconds = getattr(self._config, "desktop_max_seconds", 0.0)
            task_config.max_turns = getattr(self._config, "desktop_max_turns", 0)
            task_config.max_tool_calls = getattr(self._config, "desktop_max_tool_calls", 0)
            log.info(
                "任务 %s 桌面模式：预算覆盖为 max_seconds=%s / max_turns=%s / max_tool_calls=%s"
                "（0 = 不限制；软熔断/连续错误/无进展终止仍然生效）",
                task_id,
                "不限" if task_config.max_seconds <= 0 else f"{task_config.max_seconds:.0f}",
                "不限" if task_config.max_turns <= 0 else task_config.max_turns,
                "不限" if task_config.max_tool_calls <= 0 else task_config.max_tool_calls,
            )

        rt = TaskRuntime(
            task_id=task_id,
            session_id=session_id,
            goal=row["goal"],
            workspace=row["workspace"],
            model=row["model"],
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths,
            session_mode=mode,
            adapter=self._adapter,
            # 步骤 7：read-only 任务拿到的是只读视图（写工具未注册）
            registry=self._registry_for(task_id),
            config=task_config,
            hub=self._hub,
            store=self._store,
            knowledge=self._knowledge,
            spawner=self._spawner,
            # Checkpoint 恢复：非空 = 接着断掉的现场继续跑（一次性）
            resume_plan=self._resume_of.pop(task_id, None),
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
                "note": (
                    f"已请求中止：先让循环在下一个检查点收尾，"
                    f"{FORCE_CANCEL_GRACE_S:.0f}s 内没停下来就强制取消；"
                    f"最终以 status=aborted 为准"
                ) if ok else "取消请求未被接受",
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

    async def resolve_user_input(
        self,
        task_id: str,
        ask_id: str,
        answers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        提交用户对 ask_user 问题的回答。

        ★ 两层校验：
           ① 任务必须在内存里且未在终态
           ② ask_id 必须真的属于这个任务（UserInputManager.resolve 一次性消费）

        ★ 三种结果（对应 DSH 的 answered / late / invalid）：
           · answered —— 有人正挂着 await，回答后立刻继续
           · late     —— 当时超时了（pending），答案作为补充回答送进 Agent 下一步
           · invalid  —— 不存在 / 已回答 / pending 也过期
        """
        rt = self._runtimes.get(task_id)
        if rt is None:
            return {"accepted": False, "reason": "task_not_running",
                    "note": "任务不在运行中（可能已结束或服务重启过）"}

        if rt.status in TERMINAL:
            return {"accepted": False, "reason": "task_finished",
                    "note": f"任务已结束（{rt.status}），无法回答"}

        status = await rt.resolve_user_input(ask_id, answers)
        if status == "answered":
            return {"accepted": True, "status": "answered", "reason": "",
                    "note": "回答已提交，Agent 已恢复执行"}
        if status == "late":
            return {"accepted": True, "status": "late", "reason": "",
                    "note": "已收到。Agent 当时没等到这条回答，会在下一步看到它"}
        return {"accepted": False, "status": "invalid", "reason": "ask_id_invalid",
                "note": "该问题已被回答、已超时过期或不存在"}

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

        # ★ with_state=True：任务结束后 runtime 会从内存里消失，
        #   这时计划和审计只存在于 state_json。不带上它，
        #   前端/调试接口就再也看不到"任务到底做到哪一步"。
        row = await self._store.get_task(task_id, with_state=True)
        if row is None:
            return None

        # ★ 排队中的任务**没有 runtime**，走的是这个分支 ——
        #   所以队列状态必须在这里查，不能硬编码成 False。
        async with self._lock:
            in_queue = task_id in self._queue
            pos = self._queue.index(task_id) + 1 if in_queue else 0

        base = row.get("state") or {}
        # ★ 事件可用性（对比表 #49）：内存里没有不等于"丢了"——
        #   落盘之后要问一句库。只问一条 SQL，describe 不是热路径。
        archived = 0
        try:
            archived = await self._store.events_count(task_id)
        except Exception:  # noqa: BLE001 —— 统计失败不该让快照接口 500
            archived = 0
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
            # ★ 不在内存 = 已结束或服务重启过。事件**可能**还在 hub 缓冲，
            #   也可能只存在于磁盘（重启后就是这样）。两边都算"有"。
            "events_buffered": len(self._hub.buffered_for_task(task_id)),
            "events_archived": archived,
            "events_available": self._hub.has_events_for(task_id) or archived > 0,
        }
        return snap

    async def list_tasks(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """任务摘要列表（不含 state_json）。"""
        rows = await self._store.list_tasks(session_id)
        # 事件条数一次查完（避免每个任务一条 SQL）
        try:
            counts = await asyncio.to_thread(
                self._store.event_counts_sync, [r["task_id"] for r in rows]
            )
        except Exception:  # noqa: BLE001
            counts = {}
        out = []
        for r in rows:
            rt = self._runtimes.get(r["task_id"])
            item = dict(r)
            item["queued"] = r["task_id"] in self._queue
            archived = counts.get(r["task_id"], 0)
            item["events_archived"] = archived
            item["events_available"] = (
                self._hub.has_events_for(r["task_id"]) or archived > 0
            )
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
