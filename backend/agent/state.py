"""
Loom Agent v0.1 · Agent 运行状态（TaskState）

★ 核心设计命题：State ≠ 对话历史
--------------------------------
老项目（小智）把「喂给模型的消息数组」当成状态的全部。这带来三个具体问题：

  ① **模型看到 ≠ 程序知道**。「我已经读了 3 个文件」这句话只存在于模型的上下文里，
     程序本身无法据此做任何判断（比如「证据是否足够」）。

  ② **无法做预算控制**。轮数上限只能靠循环外部计数器，工具调用次数、同工具重复次数
     散落在各处，无法统一裁定。

  ③ **无法中断/恢复**。审批挂起后，恢复现场全靠重放消息数组，一旦消息被裁剪就丢了。

所以 Loom 把状态显式拆成两层：

    ┌─────────────────────────────────────────────┐
    │ TaskState          ← 程序的状态（本文件）    │
    │   plan / evidence / budgets / approvals ... │
    ├─────────────────────────────────────────────┤
    │ messages[]         ← 模型的状态（喂给 LLM）  │
    │   system / user / assistant / tool ...      │
    └─────────────────────────────────────────────┘

两者的关系：TaskState **派生** messages（`context.py` 负责），而不是反过来。
程序永远以 TaskState 为准；messages 只是为了「让模型也能看到」。

这条边界一旦立住，后面所有能力都是自然生长出来的：
  · 证据校验 → 查 evidence_summary
  · 重复熔断 → 查 tool_events 指纹
  · 审批恢复 → 查 pending_approval + 已授权的 approval_id
  · 变更回溯 → 查 change_records

依赖方向
--------
agent/state.py 只依赖 events.py，不依赖 tools/ server/。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class TaskStatus(str, Enum):
    """任务级状态。注意与「阶段」区分：状态是粗粒度生命周期，阶段是轮内循环。"""

    CREATED = "created"                  # 已创建，未开始
    RUNNING = "running"                  # 循环进行中
    AWAITING_APPROVAL = "awaiting_approval"  # ★ 挂起等待用户批准
    DONE = "done"                          # 正常收敛
    ABORTED = "aborted"                    # 被预算/熔断强制中止
    FAILED = "failed"                      # 不可恢复错误


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"     # 超时视同拒绝，但单独记，便于用户回看「我错过了什么」


class RiskLevel(str, Enum):
    """风险等级。决定审批时展示的警示强度，以及未来「自动批准」策略的分档。"""

    READ = "read"           # 只读，无副作用
    WRITE = "write"         # 写入/修改文件
    DELETE = "delete"       # 删除文件
    EXEC = "exec"           # 执行命令


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
@dataclass
class Budgets:
    """
    任务级预算。所有「刹车」集中在这里，不再散落各处。

    老项目有两道刹车（轮数上限 30 + 重复指纹熔断 3），但硬编码在循环里。
    这里把它们变成**可配置的字段**，并补上时长与工具调用总数。
    """

    max_turns: int = 20                  # 最多几轮 Agent 循环（C2 定稿）
    max_seconds: float = 300.0           # 墙钟总时长上限
    max_tool_calls: int = 40             # 工具调用总数上限
    max_same_call_repeat: int = 3        # 同一工具+同一参数重复几次就熔断
    max_errors: int = 5                  # 连续错误上限
    approval_timeout_s: float = 180.0    # 单次审批等待上限（超时=拒绝）
    min_evidence: int = 1                # 收尾前至少要拿到几条证据（C3 定稿）

    # ---- 用量（运行期累加）------------------------------------------------
    turns_used: int = 0
    tool_calls_used: int = 0
    errors_used: int = 0

    def check(self, started_at: float) -> tuple[str, Any, Any] | None:
        """
        统一预算裁定。

        返回 None 表示全部通过；否则返回 (kind, limit, used)：
            ("turns", 20, 20) / ("seconds", 300, 312.4) / ("tool_calls", 40, 41)
        """
        if self.turns_used >= self.max_turns:
            return ("turns", self.max_turns, self.turns_used)
        if self.tool_calls_used >= self.max_tool_calls:
            return ("tool_calls", self.max_tool_calls, self.tool_calls_used)
        if self.errors_used >= self.max_errors:
            return ("errors", self.max_errors, self.errors_used)
        elapsed = time.time() - started_at
        if elapsed >= self.max_seconds:
            return ("seconds", self.max_seconds, round(elapsed, 1))
        return None


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass
class PlanStep:
    """计划中的一步。刻意保持极简 —— 计划是「给人看的路线图」，不是工作流引擎。"""

    index: int
    title: str
    status: StepStatus = StepStatus.PENDING
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "status": self.status.value,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# 证据
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """
    一条「外部证据」。

    ★ 为什么需要它？
      Coding Agent 的铁律：**模型自称「我看过了/我改好了」不算数**，
      必须有实际观测（读了文件、跑了命令）作支撑。
      C3 决定启用 min_evidence=1，就是为了杜绝「第一轮就凭想象作答」。
    """

    source: str          # 工具名，如 "read_file"
    summary: str         # 一句话摘要，如 "读到 main.py 共 80 行"
    ref: str = ""        # 位置引用，如 "main.py:1-80"
    ok: bool = True
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "summary": self.summary,
            "ref": self.ref,
            "ok": self.ok,
            "ts": self.ts,
        }


# --------------------------------------------------------------------------- #
# 工具调用记录
# --------------------------------------------------------------------------- #
@dataclass
class ToolEvent:
    """
    一次工具调用的完整留痕。

    两个用途：
      ① 熔断指纹：`fingerprint` = 工具名 + 归一化参数，重复计数
      ② 上下文压缩：只把最近 N 条完整结果喂给模型，更早的降级成摘要
    """

    call_id: str
    tool: str
    args: dict[str, Any]
    ok: bool = True
    summary: str = ""
    error_kind: str = ""
    duration_ms: int = 0
    turn: int = 0
    ts: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        """
        调用指纹：同工具 + 同参数 → 同指纹。

        用 sorted(args) 保证 {"a":1,"b":2} 与 {"b":2,"a":1} 指纹一致。
        这是「重复熔断」的判定依据 —— 模型反复用同样参数调同样的工具，
        说明它卡住了，继续下去只会烧 token。
        """
        import json

        try:
            norm = json.dumps(self.args, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            norm = repr(self.args)
        return f"{self.tool}:{norm}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "summary": self.summary,
            "error_kind": self.error_kind,
            "duration_ms": self.duration_ms,
            "turn": self.turn,
            "ts": self.ts,
        }


# --------------------------------------------------------------------------- #
# 审批
# --------------------------------------------------------------------------- #
@dataclass
class PendingApproval:
    """
    一次待审批请求（Agent 挂起时的现场）。

    ★ 为什么要 args_hash？
      防止「批准 A，执行 B」—— 用户在界面上看到的是 read_file("a.py")，
      但如果在挂起期间参数被换掉（或被篡改），就会被执行成别的东西。
      args_hash 在「发起审批」时算好，在「真正执行」前再算一次比对，
      不一致直接拒绝。

    ★ 为什么 approval_id 要一次性？
      防止重放：同一个 approval_id 只能被 resolve 一次（见 approval.py 的 pop）。
    """

    approval_id: str
    tool: str
    args: dict[str, Any]
    args_hash: str
    risk: RiskLevel
    # 面向用户的说明（由工具提供，不是模型自由发挥的文案）
    action: str          # 要做什么，如 "写入文件"
    target: str          # 作用对象，如 "D:/proj/main.py"
    impact: str = ""     # 可能影响
    preview: str = ""    # 预览内容（如 write_file 的内容前若干行）
    turn: int = 0
    created_at: float = field(default_factory=time.time)
    status: ApprovalStatus = ApprovalStatus.PENDING

    @staticmethod
    def new_id() -> str:
        return f"apr_{uuid.uuid4().hex[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "args": self.args,
            "args_hash": self.args_hash,
            "risk": self.risk.value,
            "action": self.action,
            "target": self.target,
            "impact": self.impact,
            "preview": self.preview,
            "turn": self.turn,
            "created_at": self.created_at,
        }


@dataclass
class ApprovalRecord:
    """已决策的审批历史（审计用：谁在什么时候批了什么）。"""

    approval_id: str
    tool: str
    target: str
    status: ApprovalStatus
    args_hash: str
    decided_at: float = field(default_factory=time.time)
    reason: str = ""     # 用户拒绝时可填理由（CLI 里可选输入）

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "target": self.target,
            "status": self.status.value,
            "args_hash": self.args_hash,
            "decided_at": self.decided_at,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# 变更
# --------------------------------------------------------------------------- #
@dataclass
class ChangeRecord:
    """
    一次文件变更留痕。

    v0.1a 只有 write_file（新建/覆盖）。v0.1b 加 edit_file/delete_file 后，
    这里会补上 diff 摘要与 before/after hash，成为「回滚」的依据。
    """

    path: str
    action: str          # "create" | "overwrite" | "edit" | "delete"
    before_hash: str = ""   # "" 表示原本不存在
    after_hash: str = ""
    added: int = 0
    removed: int = 0
    approval_id: str = ""
    backup_path: str = ""   # 覆盖前备份位置（给用户后悔的机会）
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "added": self.added,
            "removed": self.removed,
            "approval_id": self.approval_id,
            "backup_path": self.backup_path,
            "ts": self.ts,
        }


# --------------------------------------------------------------------------- #
# 任务状态（总装配）
# --------------------------------------------------------------------------- #
@dataclass
class TaskState:
    """
    一个任务的完整程序状态。

    这是 Agent Core 的「唯一真相」。每一轮循环都读写它，
    每一轮结束都可以整体序列化（`to_dict`）用于调试页展示。
    """

    # ---- 身份 --------------------------------------------------------------
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    goal: str = ""                     # 用户原始任务
    workspace: str = ""                # 工作区根目录（绝对路径，已 realpath）
    model: str = ""                    # 使用的模型标识（便于对比不同模型表现）

    # ---- 生命周期 ----------------------------------------------------------
    status: TaskStatus = TaskStatus.CREATED
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    end_reason: str = ""               # 结束原因（对应 9 种终止条件之一）

    # ---- 五阶段 ------------------------------------------------------------
    plan: list[PlanStep] = field(default_factory=list)
    plan_note: str = ""                # 计划调整说明（REFLECT 后可能改写计划）
    current_step_index: int = 0
    turn: int = 0                      # 当前轮次（从 1 开始）

    # ---- 观测留痕 ----------------------------------------------------------
    tool_events: list[ToolEvent] = field(default_factory=list)
    evidence_summary: list[Evidence] = field(default_factory=list)
    reflections: list[dict[str, Any]] = field(default_factory=list)

    # ---- 审批 --------------------------------------------------------------
    pending_approval: PendingApproval | None = None   # 非空 = 正在挂起
    approval_history: list[ApprovalRecord] = field(default_factory=list)

    # ---- 变更 --------------------------------------------------------------
    change_records: list[ChangeRecord] = field(default_factory=list)

    # ---- 预算与计数 --------------------------------------------------------
    budgets: Budgets = field(default_factory=Budgets)
    consecutive_errors: int = 0

    # ---- 结果 --------------------------------------------------------------
    final_answer: str = ""
    error: dict[str, Any] | None = None

    # ---- 私有：重复指纹计数 ------------------------------------------------
    _repeat_counter: dict[str, int] = field(default_factory=dict, repr=False)

    # ===================================================================== #
    # 轮次
    # ===================================================================== #
    def next_turn(self) -> int:
        """进入下一轮，返回新的轮次号。"""
        self.turn += 1
        self.budgets.turns_used = self.turn
        return self.turn

    # ===================================================================== #
    # 工具调用留痕
    # ===================================================================== #
    def record_tool_call(
        self,
        call_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        ok: bool,
        summary: str,
        error_kind: str = "",
        duration_ms: int = 0,
    ) -> ToolEvent:
        """
        记录一次工具调用，返回 ToolEvent。

        副作用（重要，别拆出去）：
          · 累加 tool_calls_used
          · 维护重复指纹计数 → 供 should_break_repeat 熔断
          · ok=True 时顺手把摘要升级为一条 Evidence
        """
        ev = ToolEvent(
            call_id=call_id,
            tool=tool,
            args=args,
            ok=ok,
            summary=summary,
            error_kind=error_kind,
            duration_ms=duration_ms,
            turn=self.turn,
        )
        self.tool_events.append(ev)
        self.budgets.tool_calls_used += 1

        fp = ev.fingerprint
        self._repeat_counter[fp] = self._repeat_counter.get(fp, 0) + 1

        if ok:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1
            self.budgets.errors_used += 1

        return ev

    def add_evidence(self, item: Evidence) -> None:
        self.evidence_summary.append(item)

    def repeat_count(self, tool: str, args: dict[str, Any]) -> int:
        """同指纹已出现次数（含即将执行的这次之前的数量）。"""
        ev = ToolEvent(call_id="", tool=tool, args=args)
        return self._repeat_counter.get(ev.fingerprint, 0)

    def failed_before(self, tool: str, args: dict[str, Any]) -> ToolEvent | None:
        """
        这个「工具+参数」之前有没有**确定性失败**过？返回那次失败的 ToolEvent。

        ★ 和 should_break_repeat 的区别（真实模型跑通后才补的）
        ──────────────────────────────────────────────────────
        should_break_repeat 数的是「出现过几次」，阈值默认 3 —— 它是给
        「模型可能换个角度再来一次」留余地的**软熔断**。

        但有一类失败是**确定性的**：参数一模一样、失败原因一模一样，
        再调用一次结果必然相同。典型是策略性拒绝：
          · path_outside_workspace —— 路径越界，重试一万次也还是越界
          · sensitive_file         —— 敏感文件，不看内容就是不看
          · permission_denied      —— 用户已明确拒绝

        实测（用真实 qwen 跑「读取 C:/Windows/.../hosts」）：
        模型连续两次拿同样的参数去撞同一堵墙，第 3 次才会被软熔断拦下。
        虽然最终没死循环，但那两次是纯浪费的 token 和轮次。

        所以这里单独提供「确定性失败」的判定：只要**失败过且不可重试**，
        第二次就直接拒掉，并明确告诉模型「这个错误不可重试，别再试了」。
        阈值从 3 降到 2 —— 既拦住浪费，又不惩罚「第一次探索性调用」。
        """
        if not args:
            return None

        target = ToolEvent(call_id="", tool=tool, args=args).fingerprint
        for ev in reversed(self.tool_events):
            if ev.fingerprint != target:
                continue
            if ev.ok:
                return None       # 最近一次同调用是成功的 → 不算「撞墙」
            # 不可重试的失败 = 确定性失败
            if ev.error_kind and ev.error_kind not in {
                "not_found", "not_a_file", "not_a_dir",
                "invalid_arguments", "timeout", "execution_error",
            }:
                return ev
            return None           # 可重试类错误，交给软熔断数次数
        return None

    # ===================================================================== #
    # 熔断判定
    # ===================================================================== #
    def should_break_repeat(self, tool: str, args: dict[str, Any]) -> bool:
        """
        重复调用熔断：同一工具 + 同一参数已经出现过 >= N 次。
        老项目叫「重复指纹熔断」，这里升级为可配置。
        """
        return self.repeat_count(tool, args) >= self.budgets.max_same_call_repeat

    def budget_violation(self) -> tuple[str, Any, Any] | None:
        return self.budgets.check(self.started_at)

    def has_enough_evidence(self) -> bool:
        """是否已满足最小证据数（C3）。"""
        return len([e for e in self.evidence_summary if e.ok]) >= self.budgets.min_evidence

    # ===================================================================== #
    # 计划
    # ===================================================================== #
    def set_plan(self, steps: list[PlanStep], note: str = "") -> None:
        self.plan = steps
        self.plan_note = note

    def advance_step(self) -> None:
        """把当前步标为完成并推进到下一步（REFLECT 阶段调用）。"""
        if 0 <= self.current_step_index < len(self.plan):
            self.plan[self.current_step_index].status = StepStatus.DONE
        self.current_step_index += 1

    @property
    def current_step(self) -> PlanStep | None:
        if 0 <= self.current_step_index < len(self.plan):
            return self.plan[self.current_step_index]
        return None

    # ===================================================================== #
    # 收尾
    # ===================================================================== #
    def finish(self, status: TaskStatus, answer: str = "", reason: str = "") -> None:
        self.status = status
        self.final_answer = answer
        self.end_reason = reason
        self.ended_at = time.time()
        self.pending_approval = None

    @property
    def elapsed_s(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    # ===================================================================== #
    # 快照
    # ===================================================================== #
    def to_dict(self) -> dict[str, Any]:
        """
        全量序列化。用途：
          · STATE_SNAPSHOT 事件（前端重连后同步）
          · 调试页「当前状态」面板
          · 测试断言
        """
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "workspace": self.workspace,
            "model": self.model,
            "status": self.status.value,
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed_s, 2),
            "end_reason": self.end_reason,
            "turn": self.turn,
            "plan": [s.to_dict() for s in self.plan],
            "plan_note": self.plan_note,
            "current_step_index": self.current_step_index,
            "tool_events": [e.to_dict() for e in self.tool_events],
            "evidence_summary": [e.to_dict() for e in self.evidence_summary],
            "reflections": self.reflections,
            "pending_approval": self.pending_approval.to_dict() if self.pending_approval else None,
            "approval_history": [a.to_dict() for a in self.approval_history],
            "change_records": [c.to_dict() for c in self.change_records],
            "budgets": {
                "max_turns": self.budgets.max_turns,
                "max_seconds": self.budgets.max_seconds,
                "max_tool_calls": self.budgets.max_tool_calls,
                "max_same_call_repeat": self.budgets.max_same_call_repeat,
                "max_errors": self.budgets.max_errors,
                "approval_timeout_s": self.budgets.approval_timeout_s,
                "min_evidence": self.budgets.min_evidence,
                "turns_used": self.budgets.turns_used,
                "tool_calls_used": self.budgets.tool_calls_used,
                "errors_used": self.budgets.errors_used,
            },
            "consecutive_errors": self.consecutive_errors,
            "final_answer": self.final_answer,
            "error": self.error,
        }
