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
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..events import Event, EventBus, EventType
from ..prompts.system import RISK_REVIEW_PROMPT
from ..config import MAX_TOOL_RESULT_CHARS
from ..models.adapter import Message, ModelAdapter, ModelError, ToolCall
from ..tools.base import ErrorKind, ToolContext, ToolResult
from ..tools.path_safety import normalize_root
from ..tools.registry import ToolRegistry, execute
from .approval import ApprovalManager, Decision, args_fingerprint, build_pending, make_record
from .context import ContextBuilder, TurnBuilder, phase_hint
from .policy import Policy, make_context
from .compaction import ContextCompactor, ContextStats, measure
from .history_log import (HistorySink, StoreHistorySink, dump_entries,
                          entries_to_messages, find_tool_call_gaps,
                          messages_to_entries)
from .intent import (classify_intent, detect_risks, detect_scope_conflicts,
                     extract_scoped_allowed, extract_scoped_forbidden, grade_evidence,
                     is_test_config_path)
from .scope import diff_tree, hits as _scope_hits, norm_rel as _scope_norm, scan_tree, within_allowed
from .state import (
    ApprovalStatus,
    Evidence,
    PlanStep,
    TaskState,
    TaskStatus,
)

log = logging.getLogger("loom.core")

# 计划步骤可声明的完成条件类型（第二批）。
# ★ 收敛成三种，是刻意的：模型很容易发明一堆花哨的 check 类型，
#   而程序只可能可靠判定这三种。不认识的类型回落到 observation。
_STEP_CHECKS = ("observation", "change", "command_ok")

# --------------------------------------------------------------------------- #
# M2.2：反思答复的「零进展」处理档位
# --------------------------------------------------------------------------- #
# ★ 这两个常数**不是"反思次数上限"**。判据是「两次反思答复之间有没有真实进展增量」
#   （工具调用 / 证据 / 命令 / 计划步进任一变化 → 有进展，计数归 1）。
#   只有在**连续零进展**时才会升级：
#     第 2 次零进展 → 发一次受控恢复提示（明确要求下一条是工具调用或答复）
#     第 3 次零进展 → 按 no_progress 终止，并在答复里说明判据
#   恢复只做一次：既然"零进展"这个事实没变，第二次恢复提示不会有新信息。
_REFLECT_STREAK_RECOVER = 2
_REFLECT_MAX_RECOVERIES = 1

# 哪些意图算"必须拿出修复证据"的任务（M3 收尾）。
# ★ 只放 repair：implementation / test_authoring 没有"可复现的原始失败"，
#   机械要求改动会误伤（例如"帮我实现一个空函数"本来就可能不需要验证命令）。
#   这也是第四批 §③ 证据关口只对 repair 生效的同一条理由。
_REPAIR_INTENTS = ("repair",)

_REFLECT_RECOVERY_PROMPT = (
    "[系统] 你刚刚又提交了一份进展汇报，而且自上一份以来**没有任何工具执行、"
    "没有新证据、计划也没有推进** —— 这不构成进展。\n"
    "下一条消息只能是以下两种之一：\n"
    "1) 一次真实的工具调用（list_files / read_file / edit_file / run_command 等），去真正推进下一步；\n"
    "2) 面向用户的最终答复正文（做了什么、结果如何、还有什么没做完）。\n"
    "不要再输出 progress/next_action/done 这类 JSON。"
    "如果确实无法继续，请在答复里直接说明卡在哪里，而不是重复汇报。"
)


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
        max_verification_retries: int = 2,
        stream: bool = True,
        # ---- 上下文压缩（第五批）-------------------------------------
        # 阈值是**字符数**（没有可靠 tokenizer，不假装精确 token 数）。
        # 60k 字符 ≈ 40k token 估算，对 128k 窗口的模型留足余量。
        compact_max_chars: int = 60_000,
        compact_keep_turns: int = 3,
        # ---- 预算续期（第六批）-------------------------------------
        # 有界续期：只续一次，幅度固定。出现循环/停滞/冲突时绝不续。
        budget_extend_turns: int = 10,
        budget_extend_enabled: bool = True,
        # ---- 编排引擎（M1）：legacy 默认；graph 未实现，配置层会拒绝 ----
        orchestrator: str = "legacy",
        # ---- 项目知识检索（M3）：任务开始时是否自动检索用户目标 ----------
        # 关掉后只剩 search_docs 工具（模型按需调用），用于 A/B 对比。
        rag_auto: bool = True,
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
        # 完成验证闸门：最后一次命令失败时，最多驳回几次"任务完成"的说法。
        # 0 = 关闭（对照实验用）
        self.max_verification_retries = max_verification_retries
        self.stream = stream
        self.compact_max_chars = compact_max_chars
        self.compact_keep_turns = compact_keep_turns
        self.budget_extend_turns = budget_extend_turns
        self.budget_extend_enabled = budget_extend_enabled
        self.orchestrator = orchestrator
        self.rag_auto = rag_auto

    @classmethod
    def from_budget_config(cls, b: Any, **overrides: Any) -> "AgentConfig":
        cfg = cls(
            orchestrator=getattr(b, "orchestrator", "legacy"),
            compact_max_chars=getattr(b, "compact_max_chars", 60_000),
            compact_keep_turns=getattr(b, "compact_keep_turns", 3),
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


def _as_reflect_payload(text: str) -> dict[str, Any] | None:
    """
    识别「模型把 REFLECT 的 JSON 当成最终答复交上来」的情况。

    ★ 真实 E2E 抓到的（讯飞 spark-x2.6）：修���任务里模型跑完测试、列完目录，
      交上来的是 {"progress": ..., "next_action": ..., "done": false} ——
      这是"我还要继续干"，不是给用户的答复。
      旧实现直接把它当正文收尾，于是 done:false 被记成 done，答复还是一段 JSON。

    ★ 为什么要单独识别：REFLECT 阶段有自己的提示词和 JSON 结构，
      但模型完全可能在 ACT 阶段就用同样的形状说话。程序得认得出来。
    """
    obj = extract_json(text or "")
    if not isinstance(obj, dict):
        return None
    if "progress" in obj and "next_action" in obj:
        return obj
    return None


def _flat_text(value: Any) -> str:
    """把任意值压成单行小写文本（用于比对"是不是同一段话"）。"""
    return " ".join(str(value or "").split()).lower()


def reflect_answer_fingerprint(obj: dict[str, Any]) -> str:
    """
    反思答复的内容指纹：progress + next_action + done。

    ★ 只用来回答"这次是不是又把同一段话交上来了"，
      不参与"要不要终止"的判断（终止看的是进展增量，见下）。
    """
    raw = "|".join(
        (_flat_text(obj.get("progress")), _flat_text(obj.get("next_action")),
         "1" if obj.get("done") else "0")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def progress_key(state: TaskState) -> str:
    """
    任务"真实进展"的指纹：工具调用 / 证据 / 命令 / 计划步进。

    ★ 为什么用这四个而不是 state.turn：
      轮次每轮必然 +1，用它做判据等于"永远有进展"，检测不到空转。
      这四个量只在**真的做了事**时才变（读文件、跑命令、计划推进）。
    """
    return ":".join((
        str(len(state.tool_events)),
        str(len(state.evidence_summary)),
        str(len(state.command_history)),
        str(state.current_step_index),
    ))


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
        history_sink: "HistorySink | None" = None,
        bus: EventBus | None = None,
        approvals: ApprovalManager | None = None,
        allowed_tools: set[str] | None = None,
        extra_rules: str = "",
        knowledge: "KnowledgeBase | None" = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.config = config or AgentConfig()
        self.bus = bus or EventBus()
        self.approvals = approvals or ApprovalManager(timeout_s=self.config.approval_timeout_s)
        self.policy = Policy(registry, allowed_tools=allowed_tools)
        self.context = ContextBuilder()
        self.extra_rules = extra_rules
        # M1：历史落盘的抽象出口（runtime 传 StoreHistorySink；单测不传 → 不持久化）
        self.history_sink = history_sink
        # M3：项目知识库（不传 = 没有 RAG；两条引擎共用同一份实现）
        self.knowledge = knowledge

        # 运行期状态（每次 run 重置）
        self._state: TaskState | None = None
        self._history: list[Message] = []
        self._system_prompt: str = ""
        # 取消事件：__init__ 就创建（cancel() 不依赖 run() 已启动）
        self._cancel_event: asyncio.Event = asyncio.Event()

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
        allowed_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
        task_id: str = "",
    ) -> TaskState:
        """
        跑一个完整任务，返回最终 TaskState。

        这是唯一入口。所有阶段在内部串联。

        `allowed_paths` / `forbidden_paths`（第二批新增，相对工作区）：
          · 默认 `allowed=["."]`、`forbidden=[]` → 行为与之前完全一致
          · forbidden 会在 Policy 层**直接拦下**写入；allowed 只在事后审计里标记

        编排引擎由 <code>self.config.orchestrator</code> 决定（M1）：
          · legacy（默认）= 现有 _loop；graph 尚未实现，进入即报可诊断错误
        """
        # ---- 编排引擎选择（M1/M2）----------------------------------------
        # ★ 合法值校验在配置层（validate_orchestrator）；这里只分派。
        # ★ graph 引擎异常直接向上抛 → 由下方顶层 except 收尾为 failed，
        #   绝不静默切回 legacy 重跑（工具副作用可能已经发生）。
        orch = getattr(self.config, "orchestrator", "legacy")
        if orch not in ("legacy", "graph"):
            raise ValueError(
                f"未知的编排器配置：{orch!r}（合法值：legacy | graph）"
            )

        # ---- 初始化 -----------------------------------------------------
        root = normalize_root(workspace)
        # ★ M2.1：task_id 可以由调用方（TaskRuntime）指定。
        #   不指定时自生成 —— 但那样历史会挂在另一个 id 下，
        #   与 tasks 表 / 前端 / 事件里的 task_id 对不上（真实事故：7 条历史成了孤儿）。
        state = TaskState(
            goal=goal.strip(),
            workspace=root,
            model=model_label or getattr(self.adapter, "model", "") or self.adapter.name,
            **({"task_id": task_id} if task_id else {}),
        )
        state.engine = orch
        state.allowed_paths = list(allowed_paths) if allowed_paths else ["."]
        state.forbidden_paths = list(forbidden_paths) if forbidden_paths else []

        # ---- 第三批：任务意图与用户约束 -------------------------------
        # ★ 为什么在 run 开头就定：意图决定了后面每一步怎么验证
        #   （诊断任务不该被"测试失败"拦，修复任务不能只凭 exit_code=0 收尾）。
        #   分类只用**用户写的文字**，不看文件变化 —— 后者是结果不是原因。
        verdict = classify_intent(state.goal, forbidden_paths=state.forbidden_paths)
        state.intent = verdict.intent
        state.intent_confidence = verdict.confidence
        state.intent_reasons = verdict.reasons
        state.constraints = verdict.constraints
        if state.intent != "unknown":
            log.info("任务 %s 意图判定：%s（%s）%s", state.task_id, state.intent,
                     state.intent_confidence, verdict.reasons)

        # ★ 用户说了"别改测试"而调用方没传 forbidden_paths → 自动补上，
        #   复用现有 Policy 机制，不另建一套审批。
        #   测试编写任务例外：那时改测试正是用户要的交付物。
        if state.constraints.get("no_test_modification") and state.intent != "test_authoring":
            for p in ("tests/", "conftest.py", "pytest.ini", "tox.ini"):
                if not _scope_hits(p, state.forbidden_paths):
                    state.forbidden_paths.append(p)
            state.intent_reasons.append("用户要求不改测试 → 已自动加入禁止修改范围")
        # ★ 「不许修改 <具体路径>」→ 路径约束（第四批：不再误判成全局只读）
        for p in verdict.scoped_forbidden:
            if not _scope_hits(p, state.forbidden_paths):
                state.forbidden_paths.append(p)
                state.intent_reasons.append(f"用户禁止修改 {p} → 已加入禁止范围")
        # ★ 「其余只许改 <路径>」→ 允许范围（超出即越界，会被审计标记）
        #   ★ 取**交集**而不是并集：调用方给的 allowed 是外层硬边界，
        #   用户原话里的范围更具体 → 生效范围是两者中更严的那个。
        #   （并集 = 放宽权限，正是第五批要避免的事。）
        api_allowed = list(allowed_paths) if allowed_paths else []
        goal_allowed = verdict.scoped_allowed
        if goal_allowed:
            effective = [p for p in goal_allowed
                         if not api_allowed or within_allowed(p, api_allowed)]
            if effective:
                state.allowed_paths = effective
                state.intent_reasons.append(f"用户限定只许改 {', '.join(effective)} → 生效允许范围")
            else:
                state.intent_reasons.append(
                    "用户原话的范围与调用方给的允许范围无交集 → 按更严的调用方范围执行")

        # ---- 范围冲突检测（目标 vs 权限）--------------------------------
        # ★ 只检测、只披露，**不自动放开权限**。
        state.scope_conflicts = detect_scope_conflicts(
            state.goal,
            allowed_paths=state.allowed_paths,
            forbidden_paths=state.forbidden_paths,
        )
        if state.scope_conflicts:
            log.info("任务 %s 检测到范围冲突 %d 项：%s", state.task_id,
                     len(state.scope_conflicts),
                     [c.get("path") for c in state.scope_conflicts])
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
        # 上下文压缩器：每轮调用模型前判断规模，超了就压。
        # ★ 摘要器复用同一个 adapter（不新建模型连接、不建第二套 Agent）。
        self.compactor = ContextCompactor(
            max_chars=self.config.compact_max_chars,
            keep_recent_turns=self.config.compact_keep_turns,
            summarizer=self._summarize_history,
        )
        # ---- 历史持久化（M1）：sink 在 __init__ 注入；这里只重置游标 ------
        self._history_persisted = 0
        self._history_seq = 0
        self._cancel_event.clear()
        self.bus.bind_task(state.task_id)

        # ---- 系统提示 ---------------------------------------------------
        from ..prompts.system import build_system_prompt, tool_line

        tool_lines = [tool_line(self.registry.get(n)) for n in sorted(self.registry.names)]
        self._system_prompt = build_system_prompt(
            workspace=root,
            tool_lines=tool_lines,
            extra_rules=self.extra_rules,
            rag=self.knowledge is not None,
        )

        # ---- 开始 -------------------------------------------------------
        state.status = TaskStatus.RUNNING
        # ★ 任务开始时的文件快照：用户在此之前就有的未提交修改**不是** Agent 干的。
        #   没有这个基线，事后审计会把用户的改动算到 Agent 头上。
        state.baseline_files = await asyncio.to_thread(scan_tree, root)
        self.bus.task_start(state.goal, root, state.model)
        log.info("任务开始 %s：%s（工作区 %s，基线文件 %d 个）",
                 state.task_id, state.goal[:60], root, len(state.baseline_files))

        # ---- M3：项目知识预检索（索引 + 一次检索，失败不影响任务）----------
        await self._prefetch_knowledge(state)

        try:
            if self.config.orchestrator == "graph":
                # M2：LangGraph 编排引擎驱动同一组阶段方法与安全检查点
                from .graph_engine import GraphTaskEngine
                await GraphTaskEngine(self).run(state)
            else:
                await self._loop(state)
        except asyncio.CancelledError:
            state.finish(TaskStatus.ABORTED, self._fallback_answer(state, "cancelled"), "cancelled")
            self.bus.emit(
                Event.make(
                    EventType.TASK_END,
                    {"status": "aborted", "answer": state.final_answer, "reason": "cancelled"},
                )
            )
            self._record_audit(state)
            raise
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底，保证一定有 TASK_END
            log.exception("任务 %s 发生未处理异常", state.task_id)
            state.error = {"kind": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            state.finish(TaskStatus.FAILED, self._fallback_answer(state, "internal_error"), "internal_error")
            self.bus.error("internal_error", f"{type(exc).__name__}: {exc}", fatal=True)
            self.bus.task_end("failed", state.final_answer, "internal_error")

        # ---- 最终历史落盘（M1）：保证"任务结束 → 历史已到最新"------------
        try:
            await self._flush_history(state)
        except Exception as exc:  # noqa: BLE001 —— 落盘失败已在 _flush 里记录
            log.warning("任务 %s 最终历史落盘失败：%s", state.task_id, exc)
        self._record_audit(state)
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
                # ---- 第六批：先给耗尽原因分类，再做一次**有界**续期评估 ----
                verdict = self._budget_verdict(state, kind)
                if kind == "turns" and verdict["extend"] and not state.budget_extended:
                    # ★ 续期条件（全部是可观测事实，不是模型自述）：
                    #   有效进展 + 无重复循环 + 无范围冲突 + 允许续期的意图。
                    #   只续一次、有明确上限，并且记录原因 —— 不是无上限加轮次。
                    state.budget_extended = True
                    extra = self.config.budget_extend_turns
                    state.budgets.max_turns += extra
                    state.budget_reason = f"{verdict['label']}（续期 +{extra} 轮）"
                    log.info("任务 %s 预算续期 +%d 轮：%s", state.task_id, extra, verdict["label"])
                    self.bus.error(
                        "budget_extended",
                        f"预算续期 +{extra} 轮：{verdict['label']}（仍在推进且无循环/冲突）",
                        fatal=False, turn=state.turn,
                    )
                    # 续期后不再走终止分支
                else:
                    state.budget_reason = verdict["label"] if kind == "turns" else f"budget_{kind}"
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
                # ★ 先标记当前步骤"开工"并记下证据基线，**再**执行工具 ——
                #   顺序反了的话，基线会把这一轮新产生的证据一起算进去，
                #   步骤就永远看不到"自己这一轮的成果"。
                state.start_current_step()
                await self._execute_tools(state, reply)

                # ---- 计划步骤结算（由证据决定，不由轮次决定）------------
                self._advance_plan_by_evidence(state)

                # ---- REFLECT（每轮工具后）--------------------------------
                if self.config.use_reflect_phase:
                    finished = await self._reflect_phase(state)
                    if finished:
                        # REFLECT 阶段已判定完成并收尾
                        return
                continue

            # ---- 分支 B：模型直接给文本 → 收尾 -------------------------
            # M2：逻辑提取到 _finalize_text_reply（legacy 与 graph 引擎共用），
            # 返回 "continue"（回灌继续）或 "stop"（已收尾）。
            route = await self._finalize_text_reply(state, reply)
            if route != "continue":
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

        msgs = await self._prepare_messages(
            state,
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
                    completion = str(raw.get("completion") or raw.get("done_when") or "")
                    check = str(raw.get("check") or "").strip().lower()
                else:
                    title, detail, completion, check = str(raw), "", "", ""
                # 只接受已知的检查类型，别的回落到默认值（不让模型发明 DSL）
                if check not in _STEP_CHECKS:
                    check = "observation"
                # ---- 第六批：修改/验证类步骤不能只凭"观测"打勾 ----------------
                # 第五批真实数据：模型的计划全是 check=observation，
                # 于是"修改 pipeline/ 源码"被一次 list_files 标成 done，
                # "重跑 pytest 验证"被一次 list_files 标成 done —— 计划面板
                # 显示 4/4 完成，实际一行代码都没改。
                # 修复类/实现类任务里，按步骤标题推断更严格的完成条件。
                if check == "observation" and state.intent in (
                    "repair", "implementation", "test_authoring",
                ):
                    title_and_detail = f"{title} {detail}".lower()
                    if any(k in title_and_detail for k in (
                            "修改", "改 ", "修好", "修复", "实现", "新增", "添加",
                            "写入", "重构", "更新", "fix", "implement", "add ")):
                        check = "change"
                    elif any(k in title_and_detail for k in (
                            "重跑", "重新运行", "运行测试", "跑测试", "跑一遍测试",
                            "验证通过", "验证修复", "verify", "pytest")):
                        check = "command_ok"
                steps.append(
                    PlanStep(
                        index=i,
                        title=title[:60],
                        detail=detail[:200],
                        completion=completion[:200],
                        check=check,
                    )
                )
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
        # ★ M2.2：ACT 的阶段提示改为**每轮都给**。
        #   旧写法是 `if not state.tool_events` —— 第一次工具调用之后就再也不提示
        #   "现在处于 ACT 阶段"，而 PLAN/REFLECT/FINAL 三种提示是无条件给的。
        #   于是模型在 ACT 轮里既没有"该调工具"的信号，又刚看过自己上一轮的
        #   REFLECT JSON，容易顺着那个形状继续输出 → 真实运行里连续 11 次
        #   reflect_as_answer，把轮次烧在空转上。
        #   提示词原文没改，只是不再"只在第一轮出现"。
        msgs = await self._prepare_messages(
            state,
            history=self._history,
            phase_hint=phase_hint("act"),
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
                content = result.to_message_content(MAX_TOOL_RESULT_CHARS)
                # ---- 读取复用（第五批）：同文件同 hash 读过了就别再塞一遍全文 ----
                # 先给读取结果加一行标记（path + sha256 + 行数），
                # 后面靠它判断"上下文里是不是已经有同一份内容了"。
                if tc.name == "read_file" and result.ok and isinstance(result.data, dict):
                    rp = _scope_norm(str(result.data.get("path") or ""))
                    rh = str(result.data.get("hash") or "")
                    if rp and rh:
                        content = (self._read_cache_header(rp, rh, result.data.get("lines"))
                                   + "\n" + content)
                marker = self._read_cache_marker(state, tc, result)
                if marker:
                    content = marker
                    state.read_reuse_hits += 1
                tb.add_tool(tc.id, tc.name, content)

        # ★ 补齐缺失的 tool 消息 —— 保证 OpenAI 协议不变量
        tb.add_missing_tools(
            name_map,
            "该调用未被执行（被策略拒绝或未获批准）。请换一种方式，或询问用户。",
        )

        self._history.extend(tb.build())

    # ===================================================================== #
    # 文件读取复用（第五批）
    # ===================================================================== #
    def _build_read_index(self, state: TaskState) -> list[dict[str, Any]]:
        """
        生成「已读取文件清单」：模型据此决定要不要重读。

        ★ 每个条目回答模型最关心的三个问题：
          ① 这个文件我读过吗？读到哪一轮？
          ② 那份原文**现在还在上下文里吗**？（被压缩掉就得重读）
          ③ 读取之后文件变过吗？（hash 变过 / 被我改过）

        ★ 这个清单只是辅助信息：不扩大 allowed_paths、不替代权限检查，
          模型也始终可以重新读取（不确定就重读是正确行为）。
        """
        out: list[dict[str, Any]] = []
        for path, info in (state.read_cache or {}).items():
            h = str(info.get("hash") or "")
            header = self._read_cache_header(path, h, info.get("lines"))
            in_ctx = any(m.role == "tool" and header in (m.content or "")
                         for m in self._history)
            out.append({
                "path": path,
                "hash": h[:12],
                "turn": info.get("turn"),
                "lines": info.get("lines"),
                "range": info.get("range"),
                "in_context": in_ctx,
            })
        # 最近的排前面，最多 8 条（清单本身也是上下文，不能膨胀）
        out.sort(key=lambda x: -(x.get("turn") or 0))
        return out[:8]

    def _track_read_cache(self, state: TaskState, tc: ToolCall, res: ToolResult) -> None:
        """
        维护任务级读取缓存：读到的记下来，被写/改/删的立刻失效。

        ★ 关键点：**缓存永远不提供内容**，只提供"这个文件上次读到的 hash"。
          内容每次都由 read_file 现场读盘，所以不可能返回过期源码。
          缓存真正的用处有两个：
            ① 识别"重复读取"（同 hash）→ 上下文里不必再放一份全文
            ② 识别"文件在我们读之后变了"（hash 不同）→ 摘要里如实标注
        """
        if not res.ok or not isinstance(res.data, dict):
            return
        data = res.data
        if tc.name == "read_file" and data.get("path"):
            path = _scope_norm(str(data["path"]))
            h = str(data.get("hash") or "")
            prev = state.read_cache.get(path)
            if prev and prev.get("hash") and h and prev["hash"] != h:
                state.cache_notes.append(
                    f"{path} 在第 {prev.get('turn')} 轮之后被改动"
                    f"（hash {prev.get('hash')} → {h}）"
                )
            state.read_cache[path] = {
                "hash": h,
                "lines": data.get("lines"),
                "size": data.get("size"),
                "turn": state.turn,
                "range": data.get("range"),
            }
        # 写类工具改过的文件 → 缓存立即失效
        change = data.get("change") if isinstance(data.get("change"), dict) else None
        if change and change.get("path"):
            path = _scope_norm(str(change["path"]))
            if path in state.read_cache:
                state.cache_notes.append(f"{path} 已被 Agent 修改，读取缓存失效")
                state.read_cache.pop(path, None)

    def _read_cache_marker(self, state: TaskState, tc: ToolCall, res: ToolResult) -> str | None:
        """
        同一文件、同一 hash 之前已经读过，且**那份内容还在上下文里** →
        这一条只放一个命中标记，不再重复塞全文。

        ★ 判断"还在上下文里"的方法：历史里存在带同样读取标记的工具消息。
          标记由 read_file 结果的 header 行提供（见 _read_cache_header）。
          如果那份内容已被压缩掉，标记也就不在了 → 照常给全文（宁可多花 token，
          也不能让模型拿着过期内容去改代码）。
        """
        if tc.name != "read_file" or not res.ok or not isinstance(res.data, dict):
            return None
        path = _scope_norm(str(res.data.get("path") or ""))
        h = str(res.data.get("hash") or "")
        if not path or not h:
            return None
        header = self._read_cache_header(path, h, res.data.get("lines"))
        for m in self._history:
            if m.role == "tool" and header in (m.content or ""):
                prev = state.read_cache.get(path) or {}
                return (
                    f"（读取复用：{path} 与第 {prev.get('turn', '?')} 轮读到的内容完全一致，"
                    f"hash={h}，{res.data.get('lines')} 行。上下文里已有同一份内容，"
                    f"此处不再重复贴出；如需重新查看请指定 offset/limit。）"
                )
        return None

    @staticmethod
    def _read_cache_header(path: str, h: str, lines: Any) -> str:
        """读取结果的标记行（用于识别"上下文里是否已有同一份内容"）。"""
        return f"[读取 {path} | sha256:{h} | {lines} 行]"

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

            # ---- 被禁止路径挡住 → 当前步骤标记 BLOCKED -------------------
            # ★ 为什么单独处理：这是唯一一种"不是模型做不好，而是外部不允许"的失败。
            #   把它算成步骤失败会误导后续策略（模型会去改别的地方）；
            #   标记成 blocked，最终报告里能看到"卡在权限上"。
            if decision.reason == "命中禁止修改路径":
                state.block_current_step(f"被禁止修改的路径挡住：{res.summary[:160]}")
            return res

        # ---- 需要审批 ---------------------------------------------------
        if decision.needs_approval:
            granted = await self._approval_flow(state, tc, decision.meta)
            if granted is None:
                # 被拒绝 / 超时 → 构造失败结果回灌
                return None
            ctx = make_context(
                state,
                approval_id=granted,
                authorized=True,
                # ★ 长耗时工具（run_command）要能感知"用户点了停止"
                cancel_event=self._cancel_event,
            )

        # ---- 执行 -------------------------------------------------------
        # ★ M3：search_docs 的检索事件在这里发（工具的"开始/结束"与其它工具一致），
        #   工具本身不持有 bus，事件形状由 core 统一保证。
        is_rag = tc.name == "search_docs"
        if is_rag:
            self.bus.rag_retrieve(
                {"query": str(tc.arguments.get("query") or ""), "mode": "tool",
                 "k": tc.arguments.get("k") or 0, "index": state.rag_index},
                turn=state.turn,
            )
        t0 = time.time()
        res = await execute(self.registry, tc.name, tc.arguments, ctx)
        duration_ms = int((time.time() - t0) * 1000)
        if is_rag:
            self._note_rag_tool_result(state, tc, res, duration_ms)

        # ---- 留痕 -------------------------------------------------------
        state.record_tool_call(
            tc.id, tc.name, tc.arguments,
            ok=res.ok,
            summary=res.summary,
            error_kind=(res.error or {}).get("kind", ""),
            duration_ms=duration_ms,
        )

        # ---- 执行结果留痕（完成验证闸门的依据）--------------------------
        # ★ 判定标准是**契约**而不是工具名：凡是 data 里带 exit_code 的，
        #   都算"一次可验证的执行"。这样以后加别的执行类工具，闸门自动生效。
        if isinstance(res.data, dict) and "exit_code" in res.data:
            state.note_command(
                self._describe_call(tc),
                res.data.get("exit_code"),
                bool(res.ok),
                turn=state.turn,
                stderr_tail=str(res.data.get("stderr") or ""),
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
                            turn=state.turn,
                        )
                    )
                except Exception:  # noqa: BLE001
                    log.exception("变更记录解析失败")

        # ---- 读取缓存维护（第五批）--------------------------------------
        self._track_read_cache(state, tc, res)

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

        msgs = await self._prepare_messages(
            state,
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

            # ---- 完成验证闸门（必须先判，再决定要不要动计划）------------
            gate, gate_detail = ("ok", "")
            if entry["done"]:
                gate, gate_detail = await self._verification_gate(state)

            if gate == "reject":
                # 驳回：不推进计划、不 finish，把证据回灌给模型继续修
                self.bus.error("unverified_completion", gate_detail, fatal=False, turn=state.turn)
                self._history.append(Message.user(gate_detail))
            else:
                # ---- 计划结算 ---------------------------------------------
                # ★ 只在模型说"完成"时做一次全量兜底结算，且只结算**开过工**
                #   的步骤；PENDING 的步骤从没开工，不能因为要收尾就补勾。
                #   gate == "fail" 时干脆不结算 —— 没通过验证就结束，
                #   剩余步骤必须保持原样，那才是真实进度。
                if gate != "fail" and state.plan and entry["done"]:
                    leftover = state.reconcile_plan()
                    self.bus.plan([s.to_dict() for s in state.plan], state.plan_note, turn=state.turn)
                    if leftover:
                        log.info("收尾时仍有 %d 个步骤未满足完成条件", len(leftover))

                # 模型认为完成 且 证据充足 → 提前收敛
                if entry["done"] and state.has_enough_evidence():
                    if gate == "fail":
                        await self._final_phase(
                            state, reason="verification_failed",
                            status=TaskStatus.ABORTED, note=gate_detail,
                        )
                    else:
                        await self._final_phase(state, reason="agent_done")
                    finished = True
        else:
            log.debug("REFLECT 未解析出 JSON，跳过本轮自评")

        self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
        return finished

    # ===================================================================== #
    # FINAL 阶段
    # ===================================================================== #
    async def _final_phase(
        self,
        state: TaskState,
        *,
        reason: str = "agent_done",
        status: TaskStatus = TaskStatus.DONE,
        note: str = "",
    ) -> None:
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

        msgs = await self._prepare_messages(
            state,
            history=self._history,
            include_reminder=True,
            phase_hint=phase_hint("final"),
        )
        msgs.append(Message.system(FINAL_ANSWER_PROMPT))

        # ---- 把计划进度与改动范围一起交给模型（第二批）------------------
        # ★ 为什么要在这时候给它看：
        #   收尾是模型最后一次总结机会。此时它必须知道
        #   ① 哪些步骤其实没完成 ② 文件实际被改了哪些
        #   否则它很容易凭着记忆写一份"看起来都做完了"的报告。
        leftover = state.incomplete_steps()
        if leftover:
            msgs.append(Message.system(
                "[系统] 计划中以下步骤尚未满足完成条件，请在答复里如实说明它们的状态：\n"
                + "\n".join(
                    f"  · 步骤 {s.index + 1}：{s.title}（{s.status.value}）"
                    + (f" —— 完成条件：{s.completion}" if s.completion else "")
                    for s in leftover
                )
            ))
        audit = self._ensure_audit(state)
        block = self._audit_block(audit)
        if block:
            msgs.append(Message.system(
                "[系统] 本次任务的文件改动范围（机器扫描结果）：\n" + block
                + "\n请在答复里如实列出你改动过的文件；如果上面有你没打算改的，明确指出来。"
            ))
        # ---- 第三批：把意图与证据缺口一起交给模型 ----------------------
        #   收尾是它最后一次总结机会。此时它必须知道：
        #   ① 系统把这个任务理解成了什么类型
        #   ② 证据到哪一级、缺什么
        #   ③ 哪些改动可能影响了验证可信度（需要它解释，而不是掩盖）
        extra: list[str] = []
        if state.intent != "unknown":
            extra.append(f"系统将该任务理解为「{state.intent}」"
                         f"（依据：{'；'.join(state.intent_reasons[:2])}）")
        ev, risks = self._verification_view(state)
        if ev:
            extra.append(f"当前证据等级 {ev.get('grade')}：{'；'.join(ev.get('reasons') or [])}")
            if ev.get("unverified"):
                extra.append(f"尚未验证：{'；'.join(ev['unverified'])}")
        risky = [r for r in risks if r.get("level") in ("medium", "high")]
        if risky:
            extra.append("以下改动可能影响验证可信度，**必须解释**："
                         + "；".join(f"{r['path']}（{'、'.join(r.get('reasons') or [])}）" for r in risky[:5]))
        if extra:
            msgs.append(Message.system(
                "[系统] 收尾前请如实说明：\n· " + "\n· ".join(extra)
                + "\n区分「我做完了」和「我验证过了」；无法验证的部分要写明原因，不要含糊。"
            ))

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

        # ---- 完成门禁（M3 收尾）----------------------------------------
        # ★ 为什么收尾阶段也要过闸门：
        #   空回复路径（no_more_actions）以前**完全绕过**闸门，于是
        #   "repair + 零改动 + 零命令" 会以 done 结束（2026-10-26 真实运行踩到）。
        #   这里已经没有再跑一轮模型的余地，所以 reject/fail 一律按
        #   未通过验证收尾：宁可显示"未通过验证"，也不谎报完成。
        if status is TaskStatus.DONE:
            gate, detail = await self._verification_gate(state)
            if gate != "ok":
                status = TaskStatus.ABORTED
                reason = "verification_failed"
                note = detail

        if status is not TaskStatus.DONE and note:
            # 未通过验证的收尾：把机器判据摆在模型自述前面，两者都保留
            answer = self._stamp_unverified(answer, note)

        # 没做完的步骤要写在答复里，不能只让计划面板自己显示
        answer = self._note_incomplete_steps(state, answer)
        # 意图 / 证据等级 / 验证风险：把"做完"和"验证过"分开说
        answer = self._note_verification(state, answer, status)

        state.finish(status, answer, reason)
        self.bus.final_answer(answer, turn=state.turn)
        self.bus.task_end("done" if status is TaskStatus.DONE else status.value, answer, reason)

    # ===================================================================== #
    # 模型调用
    # ===================================================================== #
    # ===================================================================== #
    # 消息历史持久化（M1）
    # ===================================================================== #
    async def _flush_history(self, state: TaskState) -> None:
        """
        把内存历史里**尚未落盘**的消息追加到存储。

        ★ 语义：
          · append-only 过程记录（不是内存历史的实时镜像）：
            压缩发生时会追加 marker，而旧条目不会删除 —— 读回的是完整过程。
          · 失败**不静默**：计入 state.history_persist_errors 并发事件；
            不更新游标 → 下次 flush 自动重试（seq 不变，幂等覆盖）。
          · 落盘内容是"模型看到了什么"，供诊断与未来恢复机制使用。
        """
        sink = self.history_sink
        if sink is None:
            return
        pending = len(self._history) - self._history_persisted
        if pending <= 0:
            return
        new_msgs = self._history[self._history_persisted:]
        entries = messages_to_entries(new_msgs, start_seq=self._history_seq + 1)
        try:
            await sink.append(state.task_id, entries)
        except Exception as exc:  # noqa: BLE001 —— 不让落盘失败中断任务
            state.history_persist_errors += 1
            log.warning("任务 %s 历史落盘失败（%d 条未持久化）：%s",
                        state.task_id, pending, exc)
            self.bus.error(
                "history_persist_failed",
                f"历史落盘失败（第 {state.history_persist_errors} 次，{pending} 条暂存内存）",
                fatal=False, turn=state.turn,
            )
            return
        self._history_seq += len(entries)
        self._history_persisted = len(self._history)

    async def _persist_marker(self, state: TaskState, seq: int, note: str) -> None:
        """往持久化日志追加一条系统 marker（压缩等编排事件的留痕）。失败不静默但不致命。"""
        sink = self.history_sink
        if sink is None:
            return
        entry = [{"seq": seq, "role": "system", "content": f"[编排事件] {note}",
                  "reasoning": "", "tool_call_id": "", "tool_name": ""}]
        try:
            await sink.append(state.task_id, entry)
        except Exception as exc:  # noqa: BLE001
            state.history_persist_errors += 1
            log.warning("任务 %s 历史事件落盘失败：%s", state.task_id, exc)

    # --------------------------------------------------------------------- #
    # 恢复安全边界（M1-C）：只读恢复，绝不自动续跑
    # --------------------------------------------------------------------- #
    def restore_history(self, state: TaskState, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """
        把持久化历史装载进内存，用于**诊断与未来恢复机制**。

        ★ M1 的安全立场（必须如实理解）：
          · 装载历史 ≠ 可以安全续跑。原因：
            ① 消息缺口 = 工具可能已执行而结果未保存 → 重复执行有副作用风险；
            ② 等待中的审批状态不在历史里（审批是内存 Future）；
            ③ 工具副作用（文件系统）无法从消息重建。
          · 因此本方法**只装配历史并给出诊断报告**，不改任务状态、不触发任何
            工具调用、不解除任何闸门。真正的安全续跑属于后续阶段
            （需要：工具结果持久化 + 副作用对账 + 审批状态持久化）。

        返回 RestoreReport：
          loaded            装载的消息条数
          tool_call_gaps    消息缺口清单（assistant 有 tool_calls 但 tool 结果不足）
          safe_to_resume    M1 恒为 False（除非历史为空）
          reasons           为什么不能自动续跑
        """
        messages = entries_to_messages(entries)
        gaps = find_tool_call_gaps(messages)
        reasons: list[str] = []
        if gaps:
            reasons.append(f"存在 {len(gaps)} 处消息缺口（工具可能已执行但结果未保存）")
        if messages and messages[-1].role != "assistant":
            reasons.append("最后一条不是 assistant 消息，执行位置不明确")
        if entries:
            reasons.append("工具副作用与审批状态无法从消息历史重建（M1 边界）")
        safe = not reasons and not messages

        marker = Message.system(
            f"[恢复] 已从持久化历史装载 {len(messages)} 条消息"
            f"（M1：仅供诊断，不会自动续跑；安全结论见 RestoreReport）"
        )
        self._history = [marker] + messages
        self._history_persisted = len(self._history)
        self._history_seq = (max((int(e.get("seq") or 0) for e in entries), default=0))
        return {
            "loaded": len(messages),
            "tool_call_gaps": gaps,
            "safe_to_resume": safe,
            "reasons": reasons,
        }

    # ===================================================================== #
    # 项目知识检索（M3）
    # ===================================================================== #
    async def _prefetch_knowledge(self, state: TaskState) -> None:
        """
        任务开始时：增量更新索引 + 用用户目标检索一次，命中就注入上下文。

        ★ 为什么自动检索放在"任务开始"而不是每轮：
          每轮都检索会 (a) 反复烧查询时间 (b) 把上下文越塞越乱。
          开始一次覆盖"用户这句话问的是什么"；后续需要更多资料由模型
          自己调 search_docs —— 两种入口共用同一个 KnowledgeBase。
        ★ 失败处理：检索是**增强**，不是前置条件。索引建不起来就记事件、
          把原因留在 state.rag_index，然后照常跑任务（不阻断）。
        ★ 事件先 retrieve 后 result：前端能看到"正在检索"，而不是干等。
        """
        kb = self.knowledge
        if kb is None or not getattr(kb, "usable", False) or not self.config.rag_auto:
            return
        goal = (state.goal or "").strip()
        if not goal:
            return

        try:
            stats = await asyncio.to_thread(kb.ensure_indexed, state.workspace)
            state.rag_index = stats.to_dict()
        except Exception as exc:  # noqa: BLE001 —— 索引失败不能拖垮任务
            state.rag_index = {"error": f"{type(exc).__name__}: {exc}"}
            self.bus.rag_result(
                {"query": goal, "mode": "auto", "count": 0, "found": 0, "chars": 0,
                 "elapsed_ms": 0, "reason": "index_error", "injected": False,
                 "error": state.rag_index["error"], "hits": []},
                turn=state.turn,
            )
            log.warning("任务 %s 建立索引失败：%s", state.task_id, exc)
            return

        await self._retrieve_and_inject(state, goal, mode="auto", k=kb.config.top_k)

    async def _retrieve_and_inject(
        self, state: TaskState, query: str, *, mode: str, k: int
    ) -> None:
        """检索一次并把结果写进 state（含事件留痕）。自动路径用。"""
        kb = self.knowledge
        if kb is None:
            return
        self.bus.rag_retrieve(
            {"query": query, "mode": mode, "k": k, "index": state.rag_index},
            turn=state.turn,
        )
        try:
            outcome = await asyncio.to_thread(kb.retrieve, state.workspace, query, k=k)
        except Exception as exc:  # noqa: BLE001
            self.bus.rag_result(
                {"query": query, "mode": mode, "count": 0, "found": 0, "chars": 0,
                 "elapsed_ms": 0, "reason": "error", "injected": False,
                 "error": f"{type(exc).__name__}: {exc}", "hits": []},
                turn=state.turn,
            )
            return

        injected = bool(outcome.hits)
        payload = outcome.to_event()
        payload.update(mode=mode, injected=injected)
        self.bus.rag_result(payload, turn=state.turn)
        state.rag_queries.append({
            "query": query, "mode": mode, "count": len(outcome.hits),
            "found": outcome.found, "chars": outcome.chars,
            "elapsed_ms": outcome.elapsed_ms, "injected": injected,
            "reason": outcome.reason,
        })
        if injected:
            # ★ 只有"真的进了上下文"才记 rag_hits —— 与"检索到但没注入"区分开
            state.rag_hits = [{**h.to_dict(), "text": h.text} for h in outcome.hits]
            state.rag_context_block = kb.build_context_block(outcome)
            log.info("任务 %s 预检索命中 %d 条（%d 字符），已注入上下文",
                     state.task_id, len(outcome.hits), len(state.rag_context_block))

    def _note_rag_tool_result(
        self, state: TaskState, tc: ToolCall, res: ToolResult, duration_ms: int
    ) -> None:
        """
        模型主动调用 search_docs 之后补一对 RAG 事件。

        ★ 为什么在这里补，而不是让工具自己发事件：
          工具是"无全局状态的纯组件"，不该持有 bus；事件统一由 core 发，
          这样两种引擎、两条调用路径（自动/工具）的事件形状必然一致。
        """
        data = res.data if isinstance(res.data, dict) else {}
        hits = data.get("hits") or []
        payload = {
            "query": data.get("query") or str(tc.arguments.get("query") or ""),
            "mode": "tool",
            "count": len(hits),
            "found": data.get("found", len(hits)),
            "chars": sum(len(str(h.get("text") or "")) for h in hits),
            "elapsed_ms": data.get("elapsed_ms", duration_ms),
            "reason": "" if hits else (data.get("reason") or "no_hit"),
            "injected": bool(res.ok and hits),
            "hits": [
                {k: v for k, v in h.items() if k != "text"} for h in hits
            ],
        }
        if not res.ok:
            payload["reason"] = "error"
            payload["error"] = (res.error or {}).get("message", res.summary)[:300]
        self.bus.rag_result(payload, turn=state.turn)
        state.rag_queries.append({
            "query": payload["query"], "mode": "tool", "count": payload["count"],
            "found": payload["found"], "chars": payload["chars"],
            "elapsed_ms": payload["elapsed_ms"], "injected": payload["injected"],
            "reason": payload["reason"],
        })

    # ===================================================================== #
    # 上下文压缩接线（第五批）
    # ===================================================================== #
    async def _prepare_messages(
        self,
        state: TaskState,
        *,
        history: list[Message] | None = None,
        include_plan: bool = True,
        include_reminder: bool = True,
        phase_hint: str = "",
    ) -> list[Message]:
        """
        构建模型消息；必要时**先压缩历史再构建**。

        ★ 为什么压缩要放在这里而不是别处：
          上下文是**每轮重建**的（ContextBuilder 的设计），所以压缩完立刻重建，
          模型下一次看到的就是压缩后的上下文 —— 不需要"重建+替换"两套逻辑。

        ★ 压缩失败/收益不足时一律保持原样继续跑：
          压缩是优化手段，不能让它变成任务失败的原因。
        """
        hist = self._history if history is None else history

        def _build(h: list[Message]) -> list[Message]:
            return self.context.build(
                state,
                system_prompt=self._system_prompt,
                history=h,
                include_plan=include_plan,
                include_reminder=include_reminder,
                phase_hint=phase_hint,
            )

        # ---- 历史落盘（M1）：每轮构建消息前，把新追加的消息刷进存储 ------
        # 放在这里而不是各 append 点：一次覆盖全部追加路径，顺序有保证。
        await self._flush_history(state)

        # ---- 已读文件清单（第六批）：让模型知道自己手里有什么 ------------
        state.read_index = self._build_read_index(state)

        msgs = _build(hist)
        # 记录上下文规模（实测字符数 + 估算 token），供报告与观测使用
        stats = measure(msgs)
        state.context_last = stats.as_dict()
        state.context_peak_chars = max(state.context_peak_chars, stats.chars)
        compactor = getattr(self, "compactor", None)
        if compactor is None or not self._history:
            return msgs

        need, reason, stats = compactor.should_compact(msgs)
        if not need:
            return msgs

        outcome = await compactor.compact(
            state, self._history, before=stats, reason=reason
        )
        state.compaction_log.append(outcome.as_dict())
        if not outcome.triggered:
            log.info("任务 %s 放弃压缩：%s", state.task_id, "；".join(outcome.notes))
            return msgs

        self._history = outcome.new_history
        # 压缩是历史日志里的一个事件：追加 marker，让持久化日志与内存演进对得上
        self._history_seq += 1
        await self._persist_marker(state, self._history_seq,
                                   f"上下文已压缩（保留最近 {outcome.kept_turns} 轮，"
                                   f"{outcome.summarized_turns} 轮转为摘要）")
        self._history_persisted = len(self._history)
        log.info(
            "任务 %s 上下文已压缩：%s（%d→%d 消息，%d→%d 字符，保留 %d 轮，降级=%s）",
            state.task_id, outcome.reason,
            outcome.before.messages, outcome.after.messages,
            outcome.before.chars, outcome.after.chars,
            outcome.kept_turns, outcome.degraded,
        )
        self.bus.context_compacted(outcome.as_dict(), turn=state.turn)
        if outcome.notes:
            log.info("任务 %s 压缩附注：%s", state.task_id, "；".join(outcome.notes))
        return _build(self._history)

    async def _summarize_history(self, text: str) -> str | None:
        """
        用同一个 adapter 生成过程摘要（不含工具、限时）。

        ★ 失败/超时就返回 None —— 压缩器会降级为"只保留权威状态块"，
          绝不会因为摘要失败就丢掉原始用户约束或伪造成功。
        """
        from .compaction import COMPACT_PROMPT

        msgs = [Message.system(COMPACT_PROMPT), Message.user(text)]
        try:
            # ★ max_tokens 要给足：推理型模型会先花一大段 reasoning_content，
            #   预算太小会导致正文（真正的摘要）是空的 → 摘要降级。
            #   实测（讯飞 spark-x2.6）：600 会出现"正文空、只有推理"的情况。
            reply = await asyncio.wait_for(
                self.adapter.chat(msgs, temperature=0.2, max_tokens=2000),
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001 —— 摘要失败不能毁掉任务
            log.warning("上下文摘要调用失败：%s", exc)
            return None
        out = (getattr(reply, "content", "") or "").strip()
        return out or None

    # ===================================================================== #
    # 预算策略（第六批）：耗尽原因分类 + 有界续期
    # ===================================================================== #
    def _budget_verdict(self, state: TaskState, kind: str) -> dict[str, Any]:
        """
        给预算耗尽一个**基于可观测事实**的原因分类，并决定要不要续期。

        分类（互斥，按优先级）：
          scope_conflict       目标与授权范围冲突（扩预算解决不了，必须用户授权）
          repeated_loop        最近反复出现相同参数的调用 / 相同失败的命令
          stalled_plan         计划长期没有推进（没有步骤状态变化、没有新证据）
          productive_progress  最近仍在产生新证据 / 新命令结果 / 步骤推进
          unknown              证据不足

        ★ 续期规则（全部有界）：
          只对 repair / implementation / test_authoring 续一次；
          diagnostic 不续（诊断拖得久通常是在原地打转）；
          有范围冲突、有重复循环、计划停滞 → 一律不续。
          续期幅度 = budget_extend_turns（默认 +10），只续一次。
        """
        last_n = state.tool_events[-6:]
        # ---- 重复循环：最近 6 次调用里出现 ≥3 次完全相同的 (tool, args) ----
        # ★ tool_events 里是 ToolEvent 对象（不是 dict）
        sigs = [(e.tool, json.dumps(e.args or {}, sort_keys=True,
                                    ensure_ascii=False)) for e in last_n]
        dup = sum(1 for s in set(sigs) if sigs.count(s) >= 3)
        repeated_loop = dup >= 1 and len(last_n) >= 4

        # ---- 计划推进：最近是否有步骤状态变化 / 新证据 / 新命令 ----
        recent_evidence = len(state.evidence_summary[-3:]) if state.evidence_summary else 0
        recent_cmds = [c for c in state.command_history[-3:]]
        new_cmds = len(recent_cmds)
        plan_moving = any(
            s.status.value == "in_progress" or s.evidence for s in state.plan[-2:]
        ) if state.plan else False

        # ---- 有效进展：最近的工具调用里有成功的（而不是连续失败）----
        recent_ok = sum(1 for e in last_n if e.ok)
        productive = recent_ok >= 2 or new_cmds >= 1 or plan_moving
        # ★ 最近 6 次里有 3 次以上完全相同的调用 → 是循环，不算推进。
        #   （连续 2 次相同调用不拦：重复验证是正常工作方式；
        #     真正的连续重复由既有的 same_call_repeat 熔断兜底。）
        if repeated_loop:
            productive = False

        if state.scope_conflicts:
            label, extend = "scope_conflict", False
        elif repeated_loop:
            label, extend = "repeated_loop", False
        elif not productive and recent_evidence == 0 and new_cmds == 0:
            label, extend = "stalled_plan", False
        elif state.intent in ("repair", "implementation", "test_authoring") and productive:
            label, extend = "productive_progress", True
        elif state.intent == "diagnostic":
            label, extend = "verification_or_report", False
        else:
            label, extend = "unknown", False

        return {"label": label, "extend": extend and not state.budget_extended}


    async def _finalize_text_reply(self, state: TaskState, reply: Any) -> str:
        """
        分支 B：模型直接给文本 → 收尾处理（M2 提取，legacy 与 graph 共用）。

        返回路由："continue"（回灌后继续执行）| "stop"（任务已收尾）。
        ★ 提取时不改任何行为：这里的代码就是 legacy _loop 分支 B 的原样搬迁，
          两种编排器走同一份逻辑 → 安全行为不可能分叉。
        """
        turn = state.turn
        # ★ 先看这是不是"REFLECT 形状"的输出（progress/next_action/done）：
        #   是的话它不是答复，不能收尾 —— done:false 记成 done 是虚假完成。
        reflect_obj = _as_reflect_payload(reply.content)
        if reflect_obj is not None:
            return self._handle_reflect_answer(state, reply, reflect_obj)

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
            return "stop"

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
            return "continue"

        # ---- FINAL --------------------------------------------------
        # ★ 完成验证闸门：模型说"完事了"不算数，先看最后一次执行过没过。
        gate, detail = await self._verification_gate(state)
        if gate == "reject":
            self.bus.error("unverified_completion", detail, fatal=False, turn=turn)
            self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
            self._history.append(Message.user(detail))
            return "continue"
        if gate == "fail":
            answer = self._stamp_unverified(answer, detail)
            state.finish(TaskStatus.ABORTED, answer, "verification_failed")
            self.bus.final_answer(answer, turn=turn)
            self.bus.task_end("aborted", answer, "verification_failed")
            return "stop"

        answer = self._note_incomplete_steps(state, answer)
        answer = self._note_verification(state, answer, TaskStatus.DONE)
        # M1：最终答复也进入历史（任务结束 → 持久化历史完整）
        self._history.append(Message.assistant(answer))
        state.finish(TaskStatus.DONE, answer, "agent_done")
        self.bus.final_answer(answer, turn=turn)
        self.bus.task_end("done", answer, "agent_done")
        return "stop"

    # ===================================================================== #
    # 反思答复（M2.2）：识别 + 零进展处理
    # ===================================================================== #
    def _handle_reflect_answer(
        self, state: TaskState, reply: Any, obj: dict[str, Any]
    ) -> str:
        """
        模型把 REFLECT 的 JSON 当答复交上来时的处理。

        返回路由："continue"（继续跑）| "stop"（已按 no_progress 终止）。

        ★ 为什么需要这段（真实运行证据）
        --------------------------------
        旧实现只做一件事：把模型自己的 next_action 复读回去（"继续执行：…"）。
        模型若反复用同一种形状说同一句话，系统就会一直"继续执行"——20 轮预算里
        11 轮烧在这里，而**没有任何一层会判断"它其实没进展"**。

        ★ 判据是"有没有进展"，不是"反思了几次"
        --------------------------------
        每次进来先算 progress_key（工具数 / 证据数 / 命令数 / 计划步进）：
          · 与上次不同 → 中间真的做了事 → streak 归 1，正常回灌；
          · 与上次相同 → 这段反思没伴随任何真实动作 → streak+1。
        只有"零进展"连续出现才升级：第 2 次发一次受控恢复，第 3 次按 no_progress 终止。
        """
        turn = state.turn
        fp = reflect_answer_fingerprint(obj)
        key = progress_key(state)
        same_content = bool(state.reflect_last_fingerprint) and fp == state.reflect_last_fingerprint
        if key == state.reflect_progress_key:
            state.reflect_streak += 1
        else:
            state.reflect_streak = 1
        state.reflect_progress_key = key
        state.reflect_last_fingerprint = fp

        nxt = str(obj.get("next_action") or obj.get("progress") or "").strip()
        self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
        self.bus.error(
            "reflect_as_answer",
            "模型交上来的是进展汇报（REFLECT 格式），不是最终答复",
            fatal=False,
            turn=turn,
        )

        # ---- ① 零进展重复：先给一次受控恢复 --------------------------------
        if (state.reflect_streak >= _REFLECT_STREAK_RECOVER
                and state.reflect_recoveries < _REFLECT_MAX_RECOVERIES):
            state.reflect_recoveries += 1
            self.bus.error(
                "no_progress_recovered",
                f"连续 {state.reflect_streak} 次进展汇报没有任何工具/证据/计划变化，"
                "已发出一次受控恢复提示",
                fatal=False,
                turn=turn,
            )
            self._history.append(Message.user(_REFLECT_RECOVERY_PROMPT))
            return "continue"

        # ---- ② 恢复无效：按无进展终止（如实说明，不伪装成完成）-----------
        if state.reflect_streak > _REFLECT_STREAK_RECOVER:
            answer = self._no_progress_answer(state, same_content=same_content, next_action=nxt)
            self.bus.error(
                "no_progress",
                f"连续 {state.reflect_streak} 次零进展的进展汇报，任务按 no_progress 终止",
                fatal=True,
                turn=turn,
            )
            state.finish(TaskStatus.ABORTED, answer, "no_progress")
            self.bus.final_answer(answer, turn=turn)
            self.bus.task_end("aborted", answer, "no_progress")
            return "stop"

        # ---- ③ 首次出现：正常回灌，但提示"下一步要动手，不是再汇报" --------
        if obj.get("done"):
            self._history.append(Message.user(
                "[系统] 你给的是进展汇报，不是给用户的最终答复。"
                "请直接输出最终答复正文（说明做了什么、结果如何、还有什么没做完）。"
                "不要再用 progress/next_action/done 这类 JSON。"
            ))
        else:
            self._history.append(Message.user(
                f"[系统] 继续执行：{nxt}。请**直接调用工具**执行这一步，"
                "不要再用 progress/next_action 这类 JSON 汇报进展。"
                if nxt else
                "[系统] 你还没有完成本次任务，请调用工具推进"
                "（不要再输出进展汇报 JSON）。"
            ))
        return "continue"

    def _no_progress_answer(
        self, state: TaskState, *, same_content: bool, next_action: str
    ) -> str:
        """无进展终止时的答复：先说清判据，再附上中断时的实际进展。"""
        head = [
            f"任务因「无进展循环」终止（第 {state.turn} 轮）。",
            f"模型连续 {state.reflect_streak} 次把进展汇报（REFLECT 格式）当答复提交，"
            f"{'内容完全相同' if same_content else '内容有变化'}；"
            "但期间没有任何工具执行、没有新证据、计划也没有推进。",
            f"已发出 {state.reflect_recoveries} 次受控恢复提示，模型仍未改变行为。",
        ]
        if next_action:
            head.append(
                f"它最后一次声称的下一步是：「{next_action[:200]}」—— 该动作从未被执行。"
            )
        return "\n".join(head) + "\n\n" + self._fallback_answer(state, "no_progress")

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
        # ★ 记录**实际服务本次调用的模型**：模型轮换后（额度不足自动换下一个），
        #   配置里的主模型名已经不是真相，任务快照必须显示真正用的那个。
        if getattr(reply, "model", ""):
            self._state.model = reply.model
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
    # ===================================================================== #
    # 计划步骤：由证据推进（第二批）
    # ===================================================================== #
    def _advance_plan_by_evidence(self, state: TaskState) -> None:
        """
        每轮工具调用之后结算一次当前计划步骤。

        ★ 与旧实现的区别：旧的是"每轮无条件推进一步"，
          于是跑满 3 轮就等于"计划完成 3 步"，跟实际做成什么无关。
          现在：步骤先进入 IN_PROGRESS，**满足自己的完成条件**才打勾。
        """
        if not state.plan:
            return
        before = [(s.index, s.status) for s in state.plan]
        state.finish_current_step()
        if [(s.index, s.status) for s in state.plan] != before:
            # 状态真的变了才广播 —— GUI 的计划面板据此打勾
            self.bus.plan([s.to_dict() for s in state.plan], state.plan_note, turn=state.turn)

    # ===================================================================== #
    # 改动范围审计（第二批）
    # ===================================================================== #
    def _ensure_audit(self, state: TaskState, *, refresh: bool = False) -> dict[str, Any]:
        """
        算出（并缓存）本次任务的文件改动范围审计。

        `refresh=True` 时重新扫描（收尾确认要看到最新改动），
        并把旧审计里的风险状态（explained/resolved/...）按路径合并回来 ——
        不合并的话，每次重扫都会把 Agent 已给出的解释丢掉。
        """
        if state.audit and not refresh:
            return state.audit
        prev_risks = {r.get("path"): r for r in (state.risks or [])}

        after = scan_tree(state.workspace)
        delta = diff_tree(state.baseline_files, after)
        tool_paths = {_scope_norm(c.path) for c in state.change_records}

        changed: list[dict[str, Any]] = []
        for kind in ("added", "modified", "deleted"):
            for rel in delta[kind]:
                changed.append({
                    "path": rel,
                    "kind": kind,
                    # ★ 归因分三档，不混为一谈：
                    #   tool        工具改的（有 change_records 留痕，确定）
                    #   unattributed 文件变了但没有工具留痕 —— 可能是命令的副作用
                    #               （sed / 脚本 / pip 写文件），也可能是外部修改，
                    #               无法可靠区分，所以标成"来源不明"
                    "via": "tool" if _scope_norm(rel) in tool_paths else "unattributed",
                    "uncertain": bool(after.get(rel, {}).get("uncertain")),
                })

        audit = {
            "baseline_files": len(state.baseline_files),
            "scanned_files": len(after),
            "changed": changed,
            "out_of_scope": [c["path"] for c in changed
                             if not within_allowed(c["path"], state.allowed_paths)],
            "forbidden": [c["path"] for c in changed
                          if _scope_hits(c["path"], state.forbidden_paths)],
            "unattributed": [c["path"] for c in changed if c["via"] == "unattributed"],
            "allowed_paths": state.allowed_paths,
            "forbidden_paths": state.forbidden_paths,
        }

        # ---- 第三批：验证可信度风险 + 证据分级 -------------------------
        # ★ 只在这里算一次并缓存：风险列表是**任务级结论**，
        #   不是每轮都要重算重报的东西（否则事件流会被重复告警淹掉）。
        risks = detect_risks(
            changed,
            constraints=state.constraints,
            forbidden_paths=state.forbidden_paths,
            read_text=lambda rel: self._read_changed_text(state, rel),
        )
        audit["risks"] = risks
        audit["intent"] = state.intent
        audit["intent_confidence"] = state.intent_confidence

        # 合并旧状态：解释与结论不因重扫而丢失（状态属于路径，不属于扫描）
        for r in risks:
            prev = prev_risks.get(r.get("path"))
            if prev and prev.get("status"):
                r["status"] = prev["status"]
                r["explanation"] = prev.get("explanation", "")
                r["resolution"] = prev.get("resolution", "")
                r["changed_verification"] = prev.get("changed_verification", False)
                if prev.get("contradiction_seen"):
                    r["contradiction_seen"] = True   # 历史痕迹跨轮保留

        audit["evidence"] = self._grade_now(state, risks)
        state.audit = audit
        state.risks = risks
        return audit

    def _verification_view(self, state: TaskState) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """
        收尾前统一入口：确保文件审计算过（有缓存），返回 (证据, 风险)。

        ★ 为什么需要它：分支 B（模型直接给正文）以前从不调用 `_ensure_audit`，
          于是那条路径上 `state.risks` 永远是空的 —— 风险检测只对
          "走 FINAL 阶段"的任务生效。两条收尾路径必须一致。
        """
        try:
            self._ensure_audit(state)
        except Exception as exc:  # noqa: BLE001 —— 审计失败不能挡住收尾
            log.warning("任务 %s 审计失败：%s", state.task_id, exc)
        risks = state.risks or (state.audit or {}).get("risks") or []
        return self._grade_now(state, risks), risks

    def _grade_now(self, state: TaskState, risks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """
        按**当前**状态算证据等级。

        ★ 为什么不缓存：文件审计（扫描磁盘）很贵，缓存；
          证据分级只是把 command_history / change_records / risks 折算一下，极便宜，
          而且必须反映"刚刚又跑了一次命令"的变化。
        """
        if risks is None:
            risks = state.risks or (state.audit or {}).get("risks") or []
        ev = grade_evidence(
            command_history=[c.to_dict() for c in state.command_history],
            change_turns=[c.turn for c in state.change_records],
            risks=risks,
            intent=state.intent,
        )
        state.evidence = ev
        return ev

    @staticmethod
    def _read_changed_text(state: TaskState, rel: str, limit: int = 200_000) -> str:
        """读一个改动文件的当前内容（用于风险模式扫描）。读不了返回空串。"""
        try:
            p = Path(state.workspace) / rel
            if not p.is_file() or p.stat().st_size > limit:
                return ""
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _audit_block(audit: dict[str, Any]) -> str:
        """给最终答复用的机器生成审计小结（不替代模型自己的说明）。"""
        if not audit:
            return ""
        changed = audit.get("changed") or []
        lines = [
            "## 文件改动范围审计（机器生成）",
            f"- 工作区基线 {audit.get('baseline_files', 0)} 个文件，"
            f"结束时 {audit.get('scanned_files', 0)} 个",
            f"- 变动 {len(changed)} 个："
            f"新增 {sum(1 for c in changed if c['kind'] == 'added')}、"
            f"修改 {sum(1 for c in changed if c['kind'] == 'modified')}、"
            f"删除 {sum(1 for c in changed if c['kind'] == 'deleted')}",
        ]
        for c in changed[:20]:
            mark = {"added": "+", "modified": "~", "deleted": "-"}.get(c["kind"], "?")
            via = "（工具）" if c["via"] == "tool" else "（来源不明：可能是命令副作用）"
            flag = " ⚠不确定" if c.get("uncertain") else ""
            lines.append(f"  {mark} {c['path']} {via}{flag}")
        if len(changed) > 20:
            lines.append(f"  …以及另外 {len(changed) - 20} 个")

        if audit.get("forbidden"):
            lines.append(f"- ⚠ 命中禁止修改路径：{', '.join(audit['forbidden'])}")
        if audit.get("out_of_scope"):
            lines.append(f"- ⚠ 超出允许范围：{', '.join(audit['out_of_scope'])}")
        if audit.get("unattributed"):
            lines.append(
                "- ⚠ 有变动的文件没有工具留痕（可能是 run_command 的副作用，"
                "也可能是任务期间的外部修改，无法可靠区分）："
                f"{', '.join(audit['unattributed'])}"
            )
        return "\n".join(lines)

    def _record_audit(self, state: TaskState) -> None:
        """
        任务结束后落地审计结果：存进 state、发事件、附到最终答复后面。

        ★ 为什么附在答复**后面**而不是拦在前面：
          模型的自述是"它认为自己做了什么"，机器的审计是"文件实际怎么变的"。
          两者都要保留，顺序上让证据跟在说法后面，读者自己判断。
        """
        try:
            audit = self._ensure_audit(state)
        except Exception as exc:  # noqa: BLE001 —— 审计失败不该让任务看起来失败
            log.exception("改动范围审计失败")
            state.audit = {"error": f"{type(exc).__name__}: {exc}"}
            return

        bad = audit.get("forbidden") or []
        oos = audit.get("out_of_scope") or []
        if bad or oos:
            msg = "改动范围异常："
            if bad:
                msg += f" 命中禁止路径 {bad}"
            if oos:
                msg += f" 超出允许范围 {oos}"
            self.bus.error("scope_audit", msg, fatal=False, turn=state.turn)
            log.warning("任务 %s %s", state.task_id, msg)

        # ---- 第三批：验证可信度风险（只报一次）---------------------------
        # ★ 为什么强调"一次"：风险是**任务级结论**，每轮重报会把事件流
        #   淹掉，还会诱导模型反复解释同一件事（真实回归里见过这类循环）。
        risky = [r for r in (state.risks or []) if r.get("level") in ("medium", "high")]
        if risky:
            high = [r["path"] for r in risky if r["level"] == "high"]
            med = [r["path"] for r in risky if r["level"] == "medium"]
            parts = []
            if high:
                parts.append(f"高风险 {high}")
            if med:
                parts.append(f"需解释 {med}")
            self.bus.error(
                "verification_risk",
                "检测到可能影响验证可信度的改动：" + "；".join(parts)
                + "。测试通过不等于原始问题已修复，请结合改动原因判断。",
                fatal=False,
                turn=state.turn,
            )
            log.warning("任务 %s 验证可信度风险：%s", state.task_id, parts)

        if state.final_answer and (bad or oos or audit.get("unattributed")):
            block = self._audit_block(audit)
            if block and block not in state.final_answer:
                state.final_answer = state.final_answer.rstrip() + "\n\n---\n" + block + "\n"

    # ===================================================================== #
    # 完成验证闸门
    # ===================================================================== #
    @staticmethod
    def _describe_call(tc: Any) -> str:
        """把一次工具调用还原成"人看得懂的命令串"，用于错误提示和留痕。"""
        cmd = str(tc.arguments.get("command") or tc.name)
        args = tc.arguments.get("args") or []
        if isinstance(args, (list, tuple)) and args:
            cmd += " " + " ".join(str(a) for a in args)
        return cmd[:200]

    async def _verification_gate(self, state: TaskState) -> tuple[str, str]:
        """
        收尾前问一句：**这次"完成"到底有没有证据支撑？**

        返回 `(kind, detail)`：

            "ok"     → 可以正常收尾
            "reject" → 驳回这次"完成"的说法，把证据回灌给模型
            "fail"   → 已经提醒过了还是这么说 → 如实按「未通过验证」收尾

        ★ 第三批：三道关口，按顺序过。
          ① 意图关口：diagnostic / verification 任务的失败命令**就是交付物**，
             不该被拦（旧实现用"改没改文件"猜这个，猜不准）。
          ② 命令关口：最后一次执行失败 → 不能算完成。
          ③ 证据关口：命令是绿的，但证据指不回用户的问题（等级 C）→ 也不能算完成。
             exit_code=0 只证明"这条命令跑通了"，不证明"用户的问题解决了"。

        ★ 为什么证据关口只拦 C 不拦 B？
          B 是"部分相关"（比如没观察到失败场景）。这种情况应该**披露**而不是拦截 ——
          拦得太狠会让正常任务反复被驳回，模型开始学会糊弄。

        ★ 为什么不解析模型的话来判断它是不是在"声称成功"？
          那太脆。这里全部用可观测事实：意图 + 改动 + 退出码 + 证据等级。
        """
        if self.config.max_verification_retries <= 0:
            return "ok", ""

        # ---- ① 意图关口 ----
        if state.intent in ("diagnostic", "verification"):
            return "ok", ""

        # ---- ② 命令关口 ----
        rec = state.failed_verification()
        if rec is not None and state.change_records:
            detail = f"命令 `{rec.command}` 退出码 {rec.exit_code}（第 {rec.turn} 轮）"
            tail = (rec.stderr_tail or "").strip()
            if tail:
                detail += "；stderr 末尾：" + " / ".join(tail.splitlines()[-3:])[:300]

            if state.verification_retries < self.config.max_verification_retries:
                state.verification_retries += 1
                return "reject", (
                    f"[系统] 最后一次执行失败了：{detail}。\n"
                    "这个任务已经修改过文件，所以不能按「完成」收尾。二选一：\n"
                    "· 如果这是你要修复的问题 —— 请修复后用 run_command 重新验证；\n"
                    "· 如果这个失败本身无法修复、或正是用户要你报告的结果 —— "
                    "在答复里明确写清楚失败原因、已尝试的方案和现状，不要写成已修复。"
                )
            return "fail", detail

        # ---- ②b 修复意图 + 零改动 + 零命令 = 未验证（M3 收尾）------------
        # ★ 真实运行证据：意图 repair、模型空回复、没有改任何文件、没有跑任何命令，
        #   最终却以 done 结束（GUI 显示"完成"，而答复自称"未修复"）。
        #   原来的注释假设"没动过文件 = 观测/汇报型任务"，但 repair 不是观测任务：
        #   停下不等于修好。所以这里先拦一次。
        # ★ 为什么不机械要求"必须改文件"：如果任务本来就不需要改（问题不存在、
        #   或已被别处修好），agent 应该**跑一条命令给出证据**（例如跑测试）。
        #   命令一旦存在就不走这条分支，交给下面的证据/风险关口判断。
        if (state.intent in _REPAIR_INTENTS
                and not state.change_records
                and not state.command_history):
            detail = (
                "意图是「修复」，但既没有文件改动，也没有任何验证命令 —— 无法证明问题已修复。"
            )
            if state.verification_retries < self.config.max_verification_retries:
                state.verification_retries += 1
                return "reject", (
                    "[系统] 你停下来了，但系统没有看到任何文件改动，也没有看到验证命令。\n"
                    "修复类任务不能只凭「我停下了」判定完成。二选一：\n"
                    "· 真的去改：用 edit_file / write_file 修改目标文件，"
                    "再用 run_command 跑能复现原始问题的命令；\n"
                    "· 如果问题本来就不存在（或已被别处修好）：跑一条命令给出证据"
                    "（例如测试命令与退出码），并在答复里说明为什么不需要改动。"
                )
            return "fail", detail

        # 没动过文件 → 观测/汇报型任务，失败命令的结果就是交付物
        if not state.change_records:
            return "ok", ""

        # ---- ③ 证据关口 ----
        # 只对 repair 生效：只有"修复"才有明确可复现的原始失败场景，
        # 拿"有没有命令成功"去要求它才有意义。实现/写测试/意图不明的任务
        # 没有这样一个场景，强行要求只会造成无意义驳回（回归里抓到过：
        # "写 s.py" 被连驳两次）。这类任务的完成度由计划步骤说话。
        _ev0, _risks0 = self._verification_view(state)
        _has_risky = any(r.get("level") in ("medium", "high") for r in _risks0)
        evidence = _ev0
        if state.intent == "repair" and evidence.get("grade") == "C" and not _has_risky:
            why = "；".join(evidence.get("reasons") or []) or "证据不足"
            missing = "；".join(evidence.get("unverified") or []) or "未说明"
            if state.verification_retries < self.config.max_verification_retries:
                state.verification_retries += 1
                return "reject", (
                    f"[系统] 虽然有命令执行成功，但证据指不回你要解决的问题（等级 C）：{why}。\n"
                    f"还缺：{missing}。\n"
                    "请用**能复现原始问题的那条命令**重新验证；"
                    "如果确实无法验证，就在答复里明确写清楚为什么，不要写成已修复。"
                )
            return "fail", f"证据等级 C：{why}"

        # ---- ④ 风险收尾确认（第四批）--------------------------------
        # ★ 中/高风险必须经过**结构化收尾确认**才能随任务正常完成：
        #   Agent 对风险的解释是新的证据，不是自动消除风险的凭证。
        #   评估规则（确定性，见 _evaluate_risk_explanations）：
        #     high                    → 一律 unresolved（违反用户约束，解释不能消除）
        #     解释承认改变了验证条件  → unresolved（测试通过不能证明原始问题已修复）
        #     解释与审计矛盾          → 标记矛盾并驳回，要求补充独立验证或撤销改动
        #     解释无矛盾 + 证据 A 级  → resolved
        #     解释无矛盾 + 证据 B 级  → unresolved（放行结束，但明确标注「未充分验证」）
        #
        # ★ 流程：只要还有未解决风险且重试额度未用完，就再收集一轮解释
        #   （模型可能已补充证据、修正说法或撤销改动）；额度用完按
        #   「未充分验证」如实收尾，绝不无限循环。
        evidence, risks = self._verification_view(state, refresh=True)
        risky = [r for r in risks if r.get("level") in ("medium", "high")]
        if not risky:
            return "ok", ""

        def _summary(rs):
            return "；".join(f"{r.get('path')}[{r.get('level')}]"
                             for r in rs) or "（无）"

        unresolved = [r for r in risky if r.get("status") != "resolved"]
        if not unresolved:
            return "ok", "风险收尾确认通过"

        if state.risk_review_retries >= self.config.max_verification_retries:
            return "fail", (
                f"风险收尾确认未完成（已达重试上限）：{_summary(unresolved)}。"
                "这些风险将作为「未解决」随任务结束披露，不视为已充分验证。"
            )

        state.risk_review_retries += 1
        await self._risk_review_phase(state, risky)
        unresolved = [r for r in risky if r.get("status") != "resolved"]
        if not unresolved:
            return "ok", "风险收尾确认通过"

        highs = [r for r in unresolved if r.get("level") == "high"]
        if highs:
            return "fail", (
                f"存在违反用户约束的高风险改动，不能按正常完成收尾：{_summary(highs)}。"
                "已在审计与最终答复中作为未解决的约束违反披露。"
            )

        contradictions = [r for r in unresolved if r.get("contradiction")]
        if contradictions:
            return "reject", (
                f"[系统] 以下解释与审计记录矛盾：{_summary(contradictions)}。\n"
                "系统以审计为准。请补充独立验证（针对原始问题加测试、恢复被跳过的验证），"
                "或撤销这些改动。"
            )

        unexplained = [r for r in unresolved if not r.get("explanation")]
        if unexplained:
            return "reject", (
                f"[系统] 以下验证可信度风险还没有解释：{_summary(unexplained)}。\n"
                "请复核这些改动是否必要、是否改变了原本要验证的条件。"
            )

        # 情况 B：解释了、没矛盾，但证据不足 → 允许结束，明确披露「未充分验证」
        return "ok", "存在未解决的风险，将在答复中披露为「未充分验证」"

    # ===================================================================== #
    # 风险收尾确认（第四批）
    # ===================================================================== #
    def _verification_view(self, state: TaskState, *, refresh: bool = False):
        try:
            self._ensure_audit(state, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 —— 审计失败不能挡住收尾
            log.warning("任务 %s 审计失败：%s", state.task_id, exc)
        risks = state.risks or (state.audit or {}).get("risks") or []
        return self._grade_now(state, risks), risks

    async def _risk_review_phase(self, state: TaskState, risky: list[dict[str, Any]]) -> None:
        """
        风险收尾确认：让模型对每条中/高风险给出**结构化**解释。

        ★ 为什么是独立的一次模型调用，而不是让它写在最终答复里：
          ① 结构化（JSON）才能被程序逐条评估，自然语言段落没法对账；
          ② 解释要进入 state 持久化，与审计记录并列，而不是混在答复文本里；
          ③ 复用现有 Agent Loop 的 `_call_model`，不新增工具、不建第二套审查 Agent。

        ★ 解释是**新的证据**，不是免检凭证 —— 评估规则在
          `_evaluate_risk_explanations`，全部是确定性规则。
        """
        msgs = await self._prepare_messages(
            state,
            history=self._history,
            include_reminder=True,
            phase_hint=phase_hint("reflect"),
        )
        listing = "\n".join(
            f"  · [{r.get('level')}] {r.get('path')}（{r.get('kind')}）："
            + ("；".join(r.get("reasons") or []) or "可能影响验证可信度")
            for r in risky
        )
        msgs.append(Message.system(RISK_REVIEW_PROMPT.format(risks=listing)))

        try:
            reply = await self._call_model(msgs, turn=state.turn, allow_tools=False)
        except ModelError as exc:
            log.warning("任务 %s 风险收尾确认模型调用失败：%s", state.task_id, exc)
            return

        obj = extract_json(reply.content)
        explanations = obj.get("explanations") if isinstance(obj, dict) else None
        if not isinstance(explanations, list):
            log.warning("任务 %s 风险收尾确认未解析出结构化解释", state.task_id)
            for r in risky:
                if not r.get("explanation"):
                    r["status"] = "detected"
                    r["resolution"] = "未给出解释（模型未输出结构化说明）"
            state.risk_reviews.append({
                "turn": state.turn, "ok": False, "note": "模型未输出结构化解释",
            })
            return

        self._evaluate_risk_explanations(state, risky, explanations)
        resolved = [r for r in risky if r.get("status") == "resolved"]
        unresolved = [r for r in risky if r.get("status") not in ("resolved",)]
        record = {
            "turn": state.turn,
            "ok": True,
            "resolved": [r.get("path") for r in resolved],
            "unresolved": [(r.get("path"), r.get("status"), r.get("resolution", "")[:80])
                           for r in unresolved],
        }
        state.risk_reviews.append(record)
        self._history.append(Message.assistant(reply.content, reasoning=reply.reasoning))
        self.bus.error(
            "risk_review",
            f"风险收尾确认：已解决 {len(resolved)} 条，未解决 {len(unresolved)} 条"
            + (f"（{[r.get('path') for r in unresolved]}）" if unresolved else ""),
            fatal=False,
            turn=state.turn,
        )
        log.info("任务 %s 风险收尾确认：%s", state.task_id, record)

    def _evaluate_risk_explanations(
        self,
        state: TaskState,
        risky: list[dict[str, Any]],
        explanations: list[Any],
    ) -> None:
        """
        用**确定性规则**评估解释，写回每条风险的 status/resolution。

        ★ 关键：解释与审计记录矛盾时，以审计为准。
          典型矛盾：修复任务里改了 conftest.py、命令恰好由红转绿，
          解释却说"没有改变验证条件" —— 这在事实上不成立。
        """
        by_path = {}
        for e in explanations:
            if isinstance(e, dict) and e.get("path"):
                by_path[_scope_norm(str(e["path"]))] = e
        flipped = self._command_flipped(state)

        for r in risky:
            if r.get("level") not in ("medium", "high"):
                continue
            expl = by_path.get(_scope_norm(r.get("path", "")))
            if expl is None:
                r["status"] = "detected"
                r["resolution"] = "未给出解释"
                continue

            r["status"] = "explained"
            r["explanation"] = str(expl.get("why", ""))[:400]
            r["contradiction"] = False      # 矛盾按轮重新判定，旧标记不携带
            r["still_proves"] = str(expl.get("still_proves", ""))[:300]
            r["independent_verification"] = str(expl.get("independent_verification", ""))[:300]
            changed = bool(expl.get("changed_verification"))

            # ---- 矛盾检测：解释与审计记录冲突 → 以审计为准 ----
            contradiction = ""
            if (not changed and r.get("level") == "medium" and state.intent == "repair"
                    and is_test_config_path(r.get("path", "")) and flipped):
                changed = True
                contradiction = "解释与审计矛盾（修复任务中改了测试配置，且命令由红转绿）"
                r["contradiction"] = True
                r["contradiction_seen"] = True   # 历史痕迹：曾经与审计矛盾（不随轮清除）
            r["changed_verification"] = changed

            if r.get("level") == "high":
                r["status"] = "unresolved"
                r["resolution"] = ("违反用户约束，解释不能消除"
                                   + (f"；{contradiction}" if contradiction else ""))
            elif changed:
                r["status"] = "unresolved"
                r["resolution"] = (
                    (contradiction + "；") if contradiction
                    else "解释承认改变了验证条件，"
                ) + "测试通过不能证明原始问题已修复"
            else:
                grade = (state.evidence or {}).get("grade")
                if grade == "A":
                    r["status"] = "resolved"
                    r["resolution"] = "解释与 A 级证据一致"
                else:
                    r["status"] = "unresolved"
                    r["resolution"] = f"解释尚无独立证据支持（证据等级 {grade}）"

    @staticmethod
    def _command_flipped(state: TaskState) -> bool:
        """是否存在「同一命令由失败转为成功」且其间发生过文件改动。"""
        fails = [c for c in state.command_history if not c.ok]
        oks = [c for c in state.command_history if c.ok]
        if not fails or not oks:
            return False
        last_fail, last_ok = fails[-1], oks[-1]
        if (last_ok.command or "") != (last_fail.command or ""):
            return False
        return any(c.turn <= last_ok.turn for c in state.change_records)

    @staticmethod
    def _stamp_unverified(answer: str, detail: str) -> str:
        """给"未经证实的结论"加机器标注：不篡改模型的话，但把证据摆在前面。

        ★ 措辞要对两种情况都成立：
          ① 模型声称"修好了"（但最后一次执行是红的）→ 提醒用户别信
          ② 模型诚实说"修不了"（最后一条也确实红）→ 说明这确实是未完成的收尾
          所以只说事实，不预设模型在撒谎。
        """
        head = (
            f"⚠ 未通过验证：{detail}。\n"
            "任务按「未完成」收尾。以下是模型在最后一次执行失败的情况下给出的说明：\n\n"
        )
        return head + (answer or "（模型没有给出正文）")

    def _note_verification(self, state: TaskState, answer: str, status: Any) -> str:
        """
        把「意图 / 证据等级 / 验证可信度风险」附到最终答复。

        ★ 为什么要分开写这三行：
          用户最容易被"修复完成"四个字误导。这里把机器判定的事实摆在旁边：
          这个任务被理解成什么类型、证据到哪一级、有哪些改动可能影响了验证。
          模型自述与机器判定并列，读者自己下结论。
        """
        _ev, risks = self._verification_view(state)
        if not state.intent_reasons and not state.evidence and not risks:
            return answer
        lines = [answer.rstrip(), "", "---", "## 机器判定（非模型自述）"]
        lines.append(f"- 任务意图：{state.intent}（置信度 {state.intent_confidence}）")
        ev = state.evidence or {}
        if ev:
            lines.append(f"- 证据等级：{ev.get('grade', '?')} —— "
                         + ("；".join(ev.get("reasons") or []) or "无"))
            if ev.get("unverified"):
                lines.append(f"- 尚未验证：{'；'.join(ev['unverified'])}")
        risky = [r for r in risks if r.get("level") in ("medium", "high")]
        if risky:
            unresolved = [r for r in risky if r.get("status") != "resolved"]
            lines.append(f"- 验证可信度风险 {len(risky)} 项"
                         + (f"（其中 {len(unresolved)} 项未解决）" if unresolved else "（均已确认合理）")
                         + "：")
            for r in risky[:6]:
                line = f"  · [{r['level']}·{r.get('status', 'detected')}] {r['path']}："
                line += "；".join(r.get("reasons") or []) or "可能影响验证可信度"
                if r.get("resolution"):
                    line += f" —— {r['resolution']}"
                lines.append(line)
            if unresolved:
                lines.append("- ⚠ 存在未解决的风险：本任务**不能视为已充分验证**，"
                             "请人工复核上述改动是否正当。")
            elif status is TaskStatus.DONE:
                lines.append("  （风险已经收尾确认：解释与验证证据一致）")
        elif risks:
            lines.append(f"- 文件改动 {len(risks)} 处，未发现影响验证可信度的模式")
        return "\n".join(lines)

    @staticmethod
    def _note_incomplete_steps(state: TaskState, answer: str) -> str:
        """
        把"还没满足完成条件的步骤"附到答复后面。

        ★ 为什么两条收尾路径都要加（分支 B 直接作答 / _final_phase 收尾）：
          模型很擅长写一份"看起来都做完了"的总结。步骤状态是程序记的账，
          必须出现在用户能看到的结论里，而不是只躺在计划面板上。
        """
        leftover = state.incomplete_steps()
        if not leftover:
            return answer
        lines = [answer.rstrip(), "", "---", f"⚠ 计划中仍有 {len(leftover)} 步未完成验证："]
        for s in leftover:
            extra = f" —— {s.evidence}" if s.evidence else ""
            cond = f"（完成条件：{s.completion}）" if s.completion else ""
            lines.append(f"  · 步骤 {s.index + 1}：{s.title}（{s.status.value}）{cond}{extra}")
        return "\n".join(lines)

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
            for s in state.incomplete_steps():
                lines.append(f"  · 步骤 {s.index + 1} 未完成：{s.title}（{s.status.value}）")

        if state.audit:
            block = self._audit_block(state.audit)
            if block:
                lines.append("\n" + block)

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
