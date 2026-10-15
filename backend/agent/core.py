"""
Loom Agent v0.1 · Agent Core（五阶段状态机）

★ 这是整个项目的核心。读懂这个文件，就读懂了现代 Agent 的骨架。

────────────────────────────────────────────────────────────────
五阶段循环
────────────────────────────────────────────────────────────────
    用户任务
       │
       ▼
    ┌─ PLAN ────────── 产出计划（不改环境，只看全局）
    │     │
    │     ▼
    │  ┌─ ACT ──────── 决定：调工具 还是 直接作答
    │  │    │
    │  │    ├── 要调工具 ──▶ 审批？──▶ OBSERVE（工具结果回灌）
    │  │    │                    │
    │  │    │                    └─ 被拒/失败 也回灌，让模型知道
    │  │    │
    │  │    └── 不要调工具 ──▶ REFLECT ──▶ 完成？──▶ FINAL ANSWER
    │  │                                        │
    │  └────────────────────────────────────────┘ （未完则回 ACT）
    │
    └──────────────────────────────────────────（其余终止条件）

★ 与老项目的关键差别
────────────────────────────────────────────────────────────────
老项目（stream_handler.py，281 行）是一个「裸 while 循环 + messages 数组」：

    while turn < 30:
        reply = llm(messages, tools)
        if reply.tool_calls:
            for tc in reply.tool_calls: result = dispatch(tc)   # ← 同步阻塞事件循环
            messages.append(...)
        else:
            return reply.content

三个具体缺失：
  ① **没有显式 State**。所有状态都在 messages 里，程序无法据此判断。
  ② **没有 PLAN / REFLECT**。模型走一步看一步，长任务容易迷路。
  ③ **没有审批**。写入类工具要么不提供，要么直接执行。

Loom 的版本把这些补上，并且把「一切过程都变成事件」作为第一原则 ——
你看到的不是一个黑盒在跑，而是一条可以逐帧回放的事件流。

────────────────────────────────────────────────────────────────
终止条件（9 种，全部显式）
────────────────────────────────────────────────────────────────
   1. agent_done          模型明确表示完成 → 正常收敛
   2. no_more_actions     模型既不给结论也没有工具调用（空回复）→ 收尾
   3. max_turns           轮次预算耗尽
   4. max_seconds         时长预算耗尽
   5. max_tool_calls      工具调用总数耗尽
   6. repeated_call       重复指纹熔断
   7. too_many_errors     连续错误过多
   8. model_error         模型层不可重试错误
   9. cancelled           外部取消（用户中断）

★ 每一种都要有明确的 end_reason。调试 Agent 时，
  「它为什么停了」是最高频的问题，含糊其辞会浪费大量时间。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

from ..events import Event, EventBus, EventType
from ..config import MAX_TOOL_RESULT_CHARS
from ..models.adapter import Message, ModelAdapter, ModelError, ToolCall
from ..tools.base import ErrorKind, ToolContext, ToolResult
from ..tools.path_safety import normalize_root
from ..tools.registry import ToolRegistry, execute
from .approval import ApprovalManager, Decision, args_fingerprint, build_pending, make_record
from .context import ContextBuilder, TurnBuilder, phase_hint
from .policy import Policy, make_context
from .state import (
    ApprovalStatus,
    Evidence,
    PlanStep,
    TaskState,
    TaskStatus,
)

log = logging.getLogger("loom.core")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class AgentConfig:
    """AgentCore 的运行参数（与 config.BudgetConfig 解耦，便于测试注入）。"""

    def __init__(
        self,
        *,
        max_turns: int = 20,
        max_seconds: float = 300.0,
        max_tool_calls: int = 40,
        max_same_call_repeat: int = 3,
        max_errors: int = 5,
        approval_timeout_s: float = 180.0,
        min_evidence: int = 1,
        use_plan_phase: bool = True,
        use_reflect_phase: bool = True,
        stream: bool = True,
    ) -> None:
        self.max_turns = max_turns
        self.max_seconds = max_seconds
        self.max_tool_calls = max_tool_calls
        self.max_same_call_repeat = max_same_call_repeat
        self.max_errors = max_errors
        self.approval_timeout_s = approval_timeout_s
        self.min_evidence = min_evidence
        # PLAN / REFLECT 可关：便于对比「有无规划阶段」的效果差异 —— 这是学习项目该有的开关
        self.use_plan_phase = use_plan_phase
        self.use_reflect_phase = use_reflect_phase
        self.stream = stream

    @classmethod
    def from_budget_config(cls, b: Any, **overrides: Any) -> "AgentConfig":
        cfg = cls(
            max_turns=b.max_turns,
            max_seconds=b.max_seconds,
            max_tool_calls=b.max_tool_calls,
            max_same_call_repeat=b.max_same_call_repeat,
            max_errors=b.max_errors,
            approval_timeout_s=b.approval_timeout_s,
            min_evidence=b.min_evidence,
        )
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict[str, Any] | None:
    """
    从模型输出里抽出第一个 JSON 对象。

    ★ 为什么需要这么宽容的抽取？
      即便提示词说「只输出 JSON」，模型（尤其非 strict 模式）也常会：
        · 包一层 ```json 围栏
        · 前面加一句「好的，这是计划：」
        · 结尾加「希望有帮助」
      这些都是**正常现象**，不该导致阶段失败。所以按「容忍」处理。

    策略：先剥围栏 → 再找第一个 { 到最后一个 } → 尝试解析。
    """
    if not text:
        return None
    t = text.strip()

    # 剥 markdown 围栏
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
        if m:
            t = m.group(1).strip()

    # 直接尝试
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 截取第一个 { … 最后一个 }
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def extract_text_answer(text: str) -> str:
    """
    从模型输出里提取「最终答复」正文。

    若模型把答复包在 JSON 里（{"answer": "..."}），取出来；
    否则原样返回。这让 FINAL 阶段对模型的输出格式差异免疫。
    """
    if not text:
        return ""
    obj = extract_json(text)
    if obj and isinstance(obj.get("answer"), str):
        return obj["answer"]
    if obj and isinstance(obj.get("text"), str):
        return obj["text"]
    return text.strip()


# --------------------------------------------------------------------------- #
# Agent Core
# --------------------------------------------------------------------------- #
class AgentCore:
    """
    五阶段 Agent 循环。

    使用：
        core = AgentCore(adapter, registry, config, bus)
        state = await core.run("分析当前项目", workspace="D:/proj")

    ★ 线程/协程模型：全部单协程异步。审批挂起时本协程真的停住，
      但事件循环继续转 —— 于是 CLI 能读输入、HTTP 能收请求。
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        registry: ToolRegistry,
        *,
        config: AgentConfig | None = None,
        bus: EventBus | None = None,
        approvals: ApprovalManager | None = None,
        allowed_tools: set[str] | None = None,
        extra_rules: str = "",
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.config = config or AgentConfig()
        self.bus = bus or EventBus()
        self.approvals = approvals or ApprovalManager(timeout_s=self.config.approval_timeout_s)
        self.policy = Policy(registry, allowed_tools=allowed_tools)
        self.context = ContextBuilder()
        self.extra_rules = extra_rules

        # 运行期状态（每次 run 重置）
        self._state: TaskState | None = None
        self._history: list[Message] = []
        self._system_prompt: str = ""
        self._cancel_event: asyncio.Event | None = None

    # ===================================================================== #
    # 公开属性
    # ===================================================================== #
    @property
    def state(self) -> TaskState | None:
        return self._state

    # ===================================================================== #
    # 取消
    # ===================================================================== #
    def cancel(self) -> None:
        """请求取消当前任务（下一轮循环开始时生效）。"""
        if self._cancel_event is not None:
            self._cancel_event.set()
        # 同时取消等待中的审批，避免它一直挂着
        self.approvals.cancel_all("任务被取消")

    # ===================================================================== #
    # 主循环
    # ===================================================================== #
    async def run(
        self,
        goal: str,
        *,
        workspace: str,
        model_label: str = "",
    ) -> TaskState:
        """
        跑一个完整任务，返回最终 TaskState。

        这是唯一入口。所有阶段在内部串联。
        """
        # ---- 初始化 -----------------------------------------------------
        root = normalize_root(workspace)
        state = TaskState(
            goal=goal.strip(),
            workspace=root,
            model=model_label or getattr(self.adapter, "model", "") or self.adapter.name,
        )
        b = state.budgets
        b.max_turns = self.config.max_turns
        b.max_seconds = self.config.max_seconds
        b.max_tool_calls = self.config.max_tool_calls
        b.max_same_call_repeat = self.config.max_same_call_repeat
        b.max_errors = self.config.max_errors
        b.approval_timeout_s = self.config.approval_timeout_s
        b.min_evidence = self.config.min_evidence

        self._state = state
        self._history = []
        self._cancel_event = asyncio.Event()
        self.bus.bind_task(state.task_id)

        # ---- 系统提示 ---------------------------------------------------
        from ..prompts.system import build_system_prompt, tool_line

        tool_lines = [tool_line(self.registry.get(n)) for n in sorted(self.registry.names)]
        self._system_prompt = build_system_prompt(
            workspace=root,
            tool_lines=tool_lines,
            extra_rules=self.extra_rules,
        )

        # ---- 开始 -------------------------------------------------------
        state.status = TaskStatus.RUNNING
        self.bus.task_start(state.goal, root, state.model)
        log.info("任务开始 %s：%s（工作区 %s）", state.task_id, state.goal[:60], root)

        try:
            await self._loop(state)
        except asyncio.CancelledError:
            state.finish(TaskStatus.ABORTED, self._fallback_answer(state, "cancelled"), "cancelled")
            self.bus.emit(
                Event.make(
                    EventType.TASK_END,
                    {"status": "aborted", "answer": state.final_answer, "reason": "cancelled"},
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底，保证一定有 TASK_END
            log.exception("任务 %s 发生未处理异常", state.task_id)
            state.error = {"kind": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            state.finish(TaskStatus.FAILED, self._fallback_answer(state, "internal_error"), "internal_error")
            self.bus.error("internal_error", f"{type(exc).__name__}: {exc}", fatal=True)
            self.bus.task_end("failed", state.final_answer, "internal_error")

        return state

    # ===================================================================== #
    # 循环主体
    # ===================================================================== #
    async def _loop(self, state: TaskState) -> None:
        # ---- PLAN（第一轮之前）------------------------------------------
        if self.config.use_plan_phase:
            await self._plan_phase(state)

        # ---- 主循环 -----------------------------------------------------
        while True:
            # 取消检查
            if self._cancel_event is not None and self._cancel_event.is_set():
                state.finish(TaskStatus.ABORTED, self._fallback_answer(state, "cancelled"), "cancelled")
                self.bus.task_end("aborted", state.final_answer, "cancelled")
                return

            # 预算检查
            violation = state.budget_violation()
            if violation is not None:
                kind, limit, used = violation
                self.bus.budget_exceeded(kind, limit, used, turn=state.turn)
                reason = f"budget_{kind}"
                state.finish(TaskStatus.ABORTED, self._fallback_answer(state, reason), reason)
                self.bus.task_end("aborted", state.final_answer, reason)
                return

            # 连续错误熔断
            if state.consecutive_errors >= state.budgets.max_errors:
                reason = "too_many_errors"
                self.bus.budget_exceeded("errors", state.budgets.max_errors, state.consecutive_errors)
                state.finish(TaskStatus.ABORTED, self._fallback_answer(state, reason), reason)
                self.bus.task_end("aborted", state.final_answer, reason)
                return

            # ---- 新一轮 -------------------------------------------------
            turn = state.next_turn()

            # ---- ACT：问模型要动作 --------------------------------------
            reply = await self._act_phase(state)

            if reply is None:
                # 模型层失败，已经在 _act_phase 里发过事件
                return

            # ---- 分支 A：模型要调工具 ----------------------------------
            if reply.tool_calls:
                await self._execute_tools(state, reply)

                # ---- REFLECT（每轮工具后）--------------------------------
                if self.config.use_reflect_phase:
                    finished = await self._reflect_phase(state)
                    if finished:
                        # REFLECT 阶段已判定完成并收尾
                        return
                continue

            # ---- 分支 B：模型直接给文本 → 收尾 -------------------------
            answer = extract_text_answer(reply.content)

            if not answer.strip():
                # 既没工具调用也没正文 → 空回复，收尾
                if state.evidence_summary:
                    # ★ 不是直接落模板 —— 已经收集到证据，就该让模型真正作答。
                    #   走 _final_phase 时它自己会 finish + 发 final_answer/task_end。
                    self.bus.error(
                        "empty_reply",
                        "模型未给出正文，转为收尾作答",
                        fatal=False,
                        turn=turn,
                    )
                    await self._final_phase(state, reason="no_more_actions")
                else:
                    state.finish(
                        TaskStatus.ABORTED,
                        "模型没有给出任何内容，也没有调用工具。请换个说法重试，或检查模型配置。",
                        "empty_reply",
                    )
                    self.bus.final_answer(state.final_answer, turn=turn)
                    self.bus.task_end("aborted", state.final_answer, state.end_reason)
                return

            # ---- 证据闸门（C3）------------------------------------------
            if not state.has_enough_evidence() and state.budgets.min_evidence > 0:
                # 模型急着答，但还没观测过环境 → 不采纳，提示它先看
                self.bus.error(
                    "insufficient_evidence",
                    f"模型在没有任何观测的情况下试图作答，已驳回（需至少 {state.budgets.min_evidence} 条证据）",
                    fatal=False,
                    turn=turn,
                )
                self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
                self._history.append(
                    Message.user(
                        "[系统] 你还没有实际查看过任何内容，不能直接下结论。"
                        "请先调用工具观测工作区，再给出结论。"
                    )
                )
                continue

            # ---- FINAL --------------------------------------------------
            state.finish(TaskStatus.DONE, answer, "agent_done")
            self.bus.final_answer(answer, turn=turn)
            self.bus.task_end("done", answer, "agent_done")
            return

    # ===================================================================== #
    # PLAN 阶段
    # ===================================================================== #
    async def _plan_phase(self, state: TaskState) -> None:
        """
        PLAN 阶段：只做规划，不碰环境。

        ★ 为什么单独设一个阶段，而不是让模型在第一轮自己决定要不要规划？
          因为**约束输出空间**比**期待模型自觉**可靠。
          明确告诉它「现在只规划，不许调工具」，它就不会在第一轮
          一边规划一边乱调工具 —— 那种混合输出很难解析，也很难展示。
        """
        from ..prompts.system import PLAN_PROMPT

        state.next_turn()

        msgs = self.context.build(
            state,
            system_prompt=self._system_prompt,
            history=[],
            include_plan=False,
            include_reminder=False,
            phase_hint=phase_hint("plan"),
        )
        msgs.append(Message.system(PLAN_PROMPT))

        try:
            reply = await self._call_model(msgs, turn=state.turn, allow_tools=False)
        except ModelError as exc:
            self._fail_on_model_error(state, exc)
            return

        obj = extract_json(reply.content)
        if obj and isinstance(obj.get("steps"), list):
            steps: list[PlanStep] = []
            for i, raw in enumerate(obj["steps"][:8]):
                if isinstance(raw, dict):
                    title = str(raw.get("title") or raw.get("step") or f"步骤 {i + 1}")
                    detail = str(raw.get("detail") or raw.get("description") or "")
                else:
                    title, detail = str(raw), ""
                steps.append(PlanStep(index=i, title=title[:60], detail=detail[:200]))
            state.set_plan(steps, note=str(obj.get("note") or ""))
            self.bus.plan([s.to_dict() for s in steps], state.plan_note, turn=state.turn)
        else:
            # 模型没按格式输出 → 降级：不设计划，继续跑（不因此失败）
            state.set_plan([], note="模型未给出结构化计划，直接开始执行")
            self.bus.plan([], state.plan_note, turn=state.turn)
            log.warning("PLAN 阶段未解析出 JSON，降级为无计划执行")

        # 把 PLAN 的往返也记进历史（让模型后续能看到自己的计划来源）
        self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))

    # ===================================================================== #
    # ACT 阶段
    # ===================================================================== #
    async def _act_phase(self, state: TaskState) -> Any:
        """ACT 阶段：问模型要下一步动作。返回 ModelReply，失败返回 None。"""
        msgs = self.context.build(
            state,
            system_prompt=self._system_prompt,
            history=self._history,
            phase_hint=phase_hint("act") if not state.tool_events else "",
        )
        try:
            return await self._call_model(msgs, turn=state.turn, allow_tools=True)
        except ModelError as exc:
            self._fail_on_model_error(state, exc)
            return None

    # ===================================================================== #
    # 工具执行（含审批）
    # ===================================================================== #
    async def _execute_tools(self, state: TaskState, reply: Any) -> None:
        """
        执行模型请求的所有工具调用。

        ★ 为什么 for 循环里调 `await` 而不是 gather 并发？
          ① v0.1 的哲学是「**一次一个动作，看清结果再继续**」。
             并发调用会让事件流交错，学习时看不懂。
          ② 审批只能串行（用户一次点一个）。
          ③ 工具之间常有隐含依赖（先列目录才知道读哪个文件）。
          代价是慢一点，收益是行为可预测、事件流可读。这个交换在学习阶段是划算的。
        """
        calls: list[ToolCall] = reply.tool_calls
        tb = TurnBuilder()
        tb.add_assistant(reply.content, calls, reasoning=reply.reasoning)

        name_map = {tc.id: tc.name for tc in calls}

        for tc in calls:
            # ---- 该调用是否已被拒绝/失败（要保证 tool 消息数量对齐）----
            result = await self._one_tool_call(state, tc)
            if result is not None:
                tb.add_tool(tc.id, tc.name, result.to_message_content(MAX_TOOL_RESULT_CHARS))

        # ★ 补齐缺失的 tool 消息 —— 保证 OpenAI 协议不变量
        tb.add_missing_tools(
            name_map,
            "该调用未被执行（被策略拒绝或未获批准）。请换一种方式，或询问用户。",
        )

        self._history.extend(tb.build())

    async def _one_tool_call(self, state: TaskState, tc: ToolCall) -> ToolResult | None:
        """处理单个工具调用：ACT 事件 → Policy → 审批 → 执行 → OBSERVE 事件。"""
        # ---- ACT 事件（宣告要做什么）------------------------------------
        self.bus.act(f"调用 {tc.name}", tool=tc.name, turn=state.turn)
        self.bus.tool_call(tc.id, tc.name, tc.arguments, turn=state.turn)

        # ---- Policy 校验 -------------------------------------------------
        ctx = make_context(state)
        decision = self.policy.check(
            tc.name,
            tc.arguments,
            state,
            ctx,
            parse_error=tc.parse_error,
        )

        # ---- 被拒绝 → 回灌错误 ------------------------------------------
        if not decision.allow and not decision.needs_approval:
            res = decision.result or ToolResult.failure(
                tc.name, ErrorKind.INTERNAL_ERROR, "策略拒绝但未给出原因"
            )
            state.record_tool_call(
                tc.id, tc.name, tc.arguments,
                ok=False, summary=res.summary,
                error_kind=(res.error or {}).get("kind", "policy_denied"),
            )
            self.bus.tool_result(
                tc.id, tc.name, False, res.summary,
                error=res.error, turn=state.turn,
            )
            log.info("策略拒绝 %s：%s", tc.name, decision.reason)
            return res

        # ---- 需要审批 ---------------------------------------------------
        if decision.needs_approval:
            granted = await self._approval_flow(state, tc, decision.meta)
            if granted is None:
                # 被拒绝 / 超时 → 构造失败结果回灌
                return None
            ctx = make_context(state, approval_id=granted, authorized=True)

        # ---- 执行 -------------------------------------------------------
        t0 = time.time()
        res = await execute(self.registry, tc.name, tc.arguments, ctx)
        duration_ms = int((time.time() - t0) * 1000)

        # ---- 留痕 -------------------------------------------------------
        state.record_tool_call(
            tc.id, tc.name, tc.arguments,
            ok=res.ok,
            summary=res.summary,
            error_kind=(res.error or {}).get("kind", ""),
            duration_ms=duration_ms,
        )

        # ---- 证据（成功的只读观测才算）----------------------------------
        if res.ok:
            state.add_evidence(
                Evidence(
                    source=tc.name,
                    summary=res.summary[:160],
                    ref=str((res.data or {}).get("path", "")) if isinstance(res.data, dict) else "",
                    ok=True,
                )
            )
            # 变更记录
            if isinstance(res.data, dict) and "change" in res.data:
                from .state import ChangeRecord

                try:
                    cr = res.data["change"]
                    state.change_records.append(
                        ChangeRecord(
                            path=cr.get("path", ""),
                            action=cr.get("action", ""),
                            before_hash=cr.get("before_hash", ""),
                            after_hash=cr.get("after_hash", ""),
                            added=int(cr.get("added", 0) or 0),
                            approval_id=cr.get("approval_id", ""),
                            backup_path=cr.get("backup_path", ""),
                        )
                    )
                except Exception:  # noqa: BLE001
                    log.exception("变更记录解析失败")

        # ---- OBSERVE 事件 -----------------------------------------------
        self.bus.tool_result(
            tc.id, tc.name, res.ok, res.summary,
            data=self._trim_data(res.data),
            error=res.error,
            duration_ms=duration_ms,
            turn=state.turn,
        )
        self.bus.observe(tc.id, res.summary[:120], turn=state.turn)

        return res

    # ===================================================================== #
    # 审批流程
    # ===================================================================== #
    async def _approval_flow(
        self,
        state: TaskState,
        tc: ToolCall,
        meta: dict[str, Any],
    ) -> str | None:
        """
        走一次完整审批。返回 approval_id（已批准）或 None（拒绝/超时）。

        ★ 事件顺序（这个顺序很重要，前端按它渲染）：
            approval.request  →  （Agent 挂起，用户思考）
            approval.decision →  （恢复，继续执行）
        """
        from .state import RiskLevel as RL

        risk_raw = str(meta.get("risk", "write"))
        try:
            risk = RL(risk_raw)
        except ValueError:
            risk = RL.WRITE

        pending = build_pending(
            tool=tc.name,
            args=tc.arguments,
            risk=risk,
            action=str(meta.get("action", tc.name)),
            target=str(meta.get("target", "")),
            impact=str(meta.get("impact", "")),
            preview=str(meta.get("preview", "")),
            turn=state.turn,
        )

        state.pending_approval = pending
        state.status = TaskStatus.AWAITING_APPROVAL

        # ---- 请求（会挂起）----------------------------------------------
        self.bus.emit(
            Event.make(
                EventType.APPROVAL_REQUEST,
                pending.to_dict(),
                task_id=state.task_id,
                turn=state.turn,
            )
        )

        decision: Decision = await self.approvals.request(pending)

        # ---- 恢复 -------------------------------------------------------
        state.pending_approval = None
        state.status = TaskStatus.RUNNING
        state.approval_history.append(make_record(pending, decision))

        if decision.status is ApprovalStatus.EXPIRED:
            self.bus.emit(
                Event.make(
                    EventType.APPROVAL_EXPIRED,
                    decision.to_dict(),
                    task_id=state.task_id,
                    turn=state.turn,
                )
            )
        else:
            self.bus.emit(
                Event.make(
                    EventType.APPROVAL_DECISION,
                    decision.to_dict(),
                    task_id=state.task_id,
                    turn=state.turn,
                )
            )

        if not decision.approved:
            # ---- 拒绝 → 回灌，让模型知道被拒 ---------------------------
            reason_text = (
                "用户拒绝了这次操作（等待超时）。"
                if decision.status is ApprovalStatus.EXPIRED
                else "用户拒绝了这次操作。"
            )
            res = ToolResult.failure(
                tc.name,
                ErrorKind.PERMISSION_DENIED,
                reason_text + (f" 用户说明：{decision.reason}" if decision.reason else ""),
                hint=(
                    "不要重复发起同样的请求。请向用户说明你的意图，"
                    "询问期望的做法，或改用只读方式完成能完成的部分。"
                ),
            )
            state.record_tool_call(
                tc.id, tc.name, tc.arguments,
                ok=False, summary=res.summary, error_kind="approval_rejected",
            )
            self.bus.tool_result(
                tc.id, tc.name, False, res.summary,
                error=res.error, turn=state.turn,
            )
            return None

        # ---- 批准 → 执行前复检（TOCTOU）--------------------------------
        ctx = make_context(state)
        recheck = self.policy.recheck_after_approval(tc.name, tc.arguments, state, ctx, pending)
        if not recheck.allow:
            res = recheck.result or ToolResult.failure(
                tc.name, ErrorKind.PERMISSION_DENIED, "审批后复检未通过"
            )
            state.record_tool_call(
                tc.id, tc.name, tc.arguments,
                ok=False, summary=res.summary, error_kind="recheck_failed",
            )
            self.bus.tool_result(
                tc.id, tc.name, False, res.summary, error=res.error, turn=state.turn
            )
            return None

        return pending.approval_id

    # ===================================================================== #
    # REFLECT 阶段
    # ===================================================================== #
    async def _reflect_phase(self, state: TaskState) -> bool:
        """
        REFLECT 阶段：让模型自评进展。返回 True 表示任务已在此收敛。

        ★ 这个阶段的价值在哪？
          它把「我做得怎么样了」变成一个**必须显式回答的问题**。
          没有它，模型会一直调用工具直到 token 或轮次耗尽 ——
          这正是老项目那种裸循环的典型失败模式。

          有了它，模型每轮都要面对：「我的 next_action 是什么？」
          当它发现自己答不出具体动作时，就会倾向于收尾。

        代价是每轮多一次模型往返（时间和成本）。
        所以做成可关闭的开关（config.use_reflect_phase），
        便于对比实验 —— 这正是学习项目该提供的。
        """
        from ..prompts.system import REFLECT_PROMPT

        msgs = self.context.build(
            state,
            system_prompt=self._system_prompt,
            history=self._history,
            include_reminder=True,
            phase_hint=phase_hint("reflect"),
        )
        msgs.append(Message.system(REFLECT_PROMPT))

        try:
            reply = await self._call_model(msgs, turn=state.turn, allow_tools=False)
        except ModelError as exc:
            self._fail_on_model_error(state, exc)
            return True   # 已收尾（失败态），主循环应停止

        obj = extract_json(reply.content)
        finished = False

        if obj:
            entry = {
                "turn": state.turn,
                "progress": str(obj.get("progress", ""))[:300],
                "next_action": str(obj.get("next_action", ""))[:200],
                "done": bool(obj.get("done", False)),
                "ts": time.time(),
            }
            state.reflections.append(entry)
            self.bus.reflect(entry["progress"], entry["next_action"], entry["done"], turn=state.turn)

            # ---- 推进计划步 ---------------------------------------------
            # ★ 为什么在这里推进，而不是让模型自己报告「第几步完成」？
            #   因为模型不擅长维护计数器（它会跳步、重复、或编造进度）。
            #   而「每完成一轮工具调用就推进一步」是**程序可判定的事实**。
            #   计划是给人看的路线图，进度由程序记账更可靠。
            if state.plan and state.current_step is not None:
                if entry["done"]:
                    # 收尾时把所有剩余步骤标为完成（避免出现「3 步已完成 0 步」）
                    while state.current_step is not None:
                        state.advance_step()
                else:
                    state.advance_step()
                # ★ 计划状态变了就重发一条 plan 事件 ——
                #   GUI 的计划面板据此实时打勾，不需要自己维护一套进度推断逻辑。
                self.bus.plan([s.to_dict() for s in state.plan], state.plan_note, turn=state.turn)

            # 模型认为完成 且 证据充足 → 提前收敛
            if entry["done"] and state.has_enough_evidence():
                await self._final_phase(state, reason="agent_done")
                finished = True
        else:
            log.debug("REFLECT 未解析出 JSON，跳过本轮自评")

        self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
        return finished

    # ===================================================================== #
    # FINAL 阶段
    # ===================================================================== #
    async def _final_phase(self, state: TaskState, *, reason: str = "agent_done") -> None:
        """
        收尾作答：让模型基于已观测到的证据，真正回答用户的任务。

        ★ 为什么单独一个阶段（这是真实模型跑通后才暴露的问题）
        ────────────────────────────────────────────────────────
        最初的实现里，REFLECT 判定 done=true 就直接
        `finish(状态, self._fallback_answer(...))` 收尾 ——
        于是用户问「分析当前项目结构」，得到的是：

            任务已完成（第 4 轮，共观测 6 条事实）。
            计划共 4 步，已完成 4 步。
            已执行的动作：
              ✓ list_files：...
              ✓ read_file：读取 main.py...

        这是**记账，不是答案**。模型其实已经在思维链里得出了
        「main.py 引用了 services/ 与 db，但这些目录不存在」这种真结论，
        却因为收尾走了模板而被丢弃了。

        所以收尾必须是**一次独立的模型调用**，且明确要求「回答原始任务」，
        并且**不带 tools** —— 强制它收口，而不是又去调工具。

        失败降级：模型调用出错时回落到 `_fallback_answer`（有记账总比空白好）。
        """
        from ..prompts.system import FINAL_ANSWER_PROMPT

        msgs = self.context.build(
            state,
            system_prompt=self._system_prompt,
            history=self._history,
            include_reminder=True,
            phase_hint=phase_hint("final"),
        )
        msgs.append(Message.system(FINAL_ANSWER_PROMPT))

        try:
            reply = await self._call_model(msgs, turn=state.turn, allow_tools=False)
        except ModelError as exc:
            # ★ 模型层失败不能吞掉整个任务 —— 已经收集的证据仍然有价值，
            #   降级成记账式答复并如实说明原因。
            self.bus.error(
                "final_answer_failed",
                f"收尾作答时模型调用失败：{exc}",
                fatal=False,
                turn=state.turn,
            )
            self._fail_on_model_error(state, exc)
            return

        answer = extract_text_answer(reply.content).strip()
        if not answer:
            # 模型给了空回复 → 用记账兜底，但不假装它是完整结论
            answer = self._fallback_answer(state, reason, complete=True)

        state.finish(TaskStatus.DONE, answer, reason)
        self.bus.final_answer(answer, turn=state.turn)
        self.bus.task_end("done", answer, reason)

    # ===================================================================== #
    # 模型调用
    # ===================================================================== #
    async def _call_model(self, msgs: list[Message], *, turn: int, allow_tools: bool):
        """
        调用模型，把流式增量转成 THINKING 事件。

        ★ 这是「可观测性」的关键落点。
          没有它，用户只能看到 Agent 停下来又继续，不知道在等什么。
          有了它，模型的思维链会实时出现在日志区 —— 这是本项目最核心的体验。
        """
        tools = self.registry.to_specs() if allow_tools else None

        if not self.config.stream:
            return await self.adapter.chat(msgs, tools)

        think_buf: list[str] = []
        last_emit = 0.0

        async def on_delta(delta: Any) -> None:
            nonlocal last_emit
            if delta.kind == "reasoning":
                think_buf.append(delta.text)
                now = time.time()
                # 节流：每 0.15s 或累积 80 字符推一次，避免事件洪泛
                if now - last_emit > 0.15 or sum(len(x) for x in think_buf) > 80:
                    last_emit = now
                    self.bus.thinking("".join(think_buf), phase="act", turn=turn, done=False)
                    think_buf.clear()
            elif delta.kind == "content" and delta.text:
                pass  # 正文由最终 reply 统一处理，避免碎片化

        reply = await self.adapter.chat_stream_collect(
            msgs, tools, on_delta=on_delta
        )

        if think_buf:
            self.bus.thinking("".join(think_buf), phase="act", turn=turn, done=False)
        if reply.reasoning:
            self.bus.thinking("", phase="act", turn=turn, done=True)
        return reply

    def _fail_on_model_error(self, state: TaskState, exc: ModelError) -> None:
        """模型层失败的处理：发 ERROR 事件 + 收尾。"""
        self.bus.error(exc.kind, str(exc), fatal=True, turn=state.turn)
        state.error = {"kind": exc.kind, "message": str(exc)}
        state.finish(TaskStatus.FAILED, self._fallback_answer(state, "model_error"), "model_error")
        self.bus.task_end("failed", state.final_answer, "model_error")

    # ===================================================================== #
    # 辅助
    # ===================================================================== #
    def _fallback_answer(self, state: TaskState, reason: str = "", complete: bool = False) -> str:
        """
        兜底答复：当模型没能给出最终答复就结束时，用已观测到的事实拼一段。

        ★ 为什么需要兜底？
          因为「任务被预算掐断」是正常结局（长任务本就该有上限）。
          此时用户不该只看到一句「已中止」，而应该看到
          「Agent 已经看到了什么、做到哪一步」—— 这些信息是有价值的。

        ★ 两个参数的含义（都不能省）：
          reason    结束原因。★ 必须**显式传入**而不是读 state.end_reason ——
                    因为调用点是 `finish(..., self._fallback_answer(state), reason)`，
                    参数在 finish 之前求值，那时 state.end_reason 还是空字符串。
                    写成读 state 会出现「任务在「」处结束」这种荒谬输出。
          complete  是否属于「正常完成」。★ 只有被中断才该说「不是完整结论」；
                    正常收敛却加这句话会误导用户以为任务失败。

        ★ 同时要诚实：中断时必须明确说明这不是完整结论。
          绝不能把半成品包装成结论，那比不给更糟。
        """
        if state.final_answer:
            return state.final_answer

        reason = reason or state.end_reason or "未知"
        lines: list[str] = []

        if complete:
            lines.append(f"任务已完成（第 {state.turn} 轮，共观测 {len([e for e in state.evidence_summary if e.ok])} 条事实）。")
        else:
            lines.append(
                f"任务在「{reason}」处中断（第 {state.turn} 轮，已观测 "
                f"{len([e for e in state.evidence_summary if e.ok])} 条事实）。"
            )

        if state.plan:
            done = [s for s in state.plan if s.status.value == "done"]
            lines.append(f"计划共 {len(state.plan)} 步，已完成 {len(done)} 步。")

        if state.tool_events:
            lines.append("\n已执行的动作：")
            for ev in state.tool_events[-8:]:
                flag = "✓" if ev.ok else "✗"
                lines.append(f"  {flag} {ev.tool}：{ev.summary[:100]}")

        if state.change_records:
            lines.append("\n已修改的文件：")
            for cr in state.change_records:
                lines.append(f"  · {cr.path}（{cr.action}）")

        if not complete:
            lines.append("\n以上为中断时的实际进展，不是完整结论。")
        return "\n".join(lines)

    @staticmethod
    def _trim_data(data: Any, limit: int = 4000) -> Any:
        """裁剪事件里的 data，避免一个巨大文件内容把事件流撑爆。"""
        if data is None:
            return None
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                if k == "content" and isinstance(v, str) and len(v) > limit:
                    out[k] = v[:limit] + f"…（截断，原 {len(v)} 字符）"
                    out["truncated"] = True
                elif isinstance(v, (str, int, float, bool, list, dict)) or v is None:
                    out[k] = v
                else:
                    out[k] = str(v)
            return out
        if isinstance(data, str) and len(data) > limit:
            return data[:limit] + f"…（截断，原 {len(data)} 字符）"
        return data
