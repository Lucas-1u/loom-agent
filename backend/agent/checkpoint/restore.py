"""
Checkpoint 恢复规划（把「只写不读」变成「能读、能判、能续」）

★ 背景
----------------
`checkpoint/` 此前只做写：每个执行点存一份 TaskState 快照，但**没有任何代码读它**。
`AgentCore.restore_history` 的 `safe_to_resume` 恒为 False（"M1 边界"），
于是"进程断了怎么办"的答案是"从头再来"—— 长任务的核心承诺（可恢复）没落地。

本模块补上中间那一层：把「最新 checkpoint + 持久化历史 + 基线快照」装配成
一份 **ResumePlan**，并给出**有真实判据的**安全结论。

★ 安全判据（宁可拒绝，不可错续）
----------------
| 判据 | 不满足时 |
|---|---|
| 有 checkpoint 且能读 | 拒绝：没有可恢复的执行点 |
| 工作区一致 | 拒绝：文件可能已不在原位置 |
| 历史非空 | 拒绝：不知道模型此前看到过什么 |
| 无 tool_call 缺口 | 拒绝：工具可能已执行但结果没保存 → 重跑有副作用 |
| 预算未耗尽 | 拒绝：续跑会立刻被刹车 |
| 未超过时效 | 拒绝：太久以前的快照，现场大概率已变 |

★ 为什么"无缺口"就够安全
----------------
恢复不重放历史动作：messages 原样回灌给模型，已执行过的工具**不会**再跑一遍。
真正的风险只有一种 —— 模型声明了 tool_calls、结果却没落盘（缺口），此时
无论重跑还是跳过都可能出错。所以缺口检查就是恢复安全的核心闸门。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..history_log import entries_to_messages, find_tool_call_gaps
from ..state import TaskState, TaskStatus
from .store import CheckpointRef, CheckpointStore, load_checkpoint

# 已经结束的状态：再"续跑"就是把一个完成的任务重开，语义上不允许
_TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED}


def _norm(path: str) -> str:
    """路径比较用（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.normpath(path or ""))


@dataclass
class ResumeReport:
    """恢复可行性报告（给 API / 日志 / 前端看，不含任何密钥或正文）。"""

    task_id: str
    found: bool = False
    checkpoint_seq: int = 0
    checkpoint_phase: str = ""
    checkpoint_age_s: float = 0.0
    resumed_from_turn: int = 0
    loaded_messages: int = 0
    tool_call_gaps: list[str] = field(default_factory=list)
    safe_to_resume: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "found": self.found,
            "checkpoint_seq": self.checkpoint_seq,
            "checkpoint_phase": self.checkpoint_phase,
            "checkpoint_age_s": round(self.checkpoint_age_s, 1),
            "resumed_from_turn": self.resumed_from_turn,
            "loaded_messages": self.loaded_messages,
            "tool_call_gaps": list(self.tool_call_gaps),
            "safe_to_resume": self.safe_to_resume,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass
class ResumePlan:
    """一份可以直接交给 AgentCore 的恢复现场。"""

    state: TaskState
    entries: list[dict[str, Any]]
    messages: list[Any]
    baseline_files: dict[str, Any]
    ref: CheckpointRef | None
    report: ResumeReport

    @property
    def downtime_s(self) -> float:
        """停机时长（checkpoint 写入 → 现在），用于把这段时间还给预算。"""
        return self.report.checkpoint_age_s


def plan_resume(
    store: CheckpointStore | None,
    task_id: str,
    entries: list[dict[str, Any]],
    *,
    workspace: str = "",
    max_age_s: float = 0.0,
    now: float | None = None,
) -> tuple[ResumePlan | None, ResumeReport]:
    """
    装配恢复现场并判定安全性。

    返回 `(plan, report)`：
      · plan 非空 ⇔ report.safe_to_resume 为 True（不安全时只给报告，不给现场）
      · 无论成功失败都返回 report —— 调用方要能告诉用户"为什么不能续"
    """
    now = time.time() if now is None else now
    report = ResumeReport(task_id=task_id)

    if store is None:
        report.reasons.append("checkpoint 未启用（LOOM_CHECKPOINT=0）")
        return None, report

    ref = store.latest(task_id)
    if ref is None:
        report.reasons.append("没有 checkpoint 快照（任务在第一个执行点之前就中断了）")
        return None, report

    report.found = True
    report.checkpoint_seq = ref.seq
    report.checkpoint_phase = ref.phase
    report.checkpoint_age_s = max(0.0, now - float(ref.created_at or now))

    state_dict = load_checkpoint(store, ref)
    if not isinstance(state_dict, dict) or not state_dict:
        report.reasons.append(f"checkpoint #{ref.seq} 文件损坏或不可读")
        return None, report

    # ---- ① 状态：终态不再续跑 ----
    try:
        status = TaskStatus(str(state_dict.get("status") or ""))
    except ValueError:
        status = TaskStatus.CREATED
    if status in _TERMINAL_STATUSES:
        report.reasons.append(f"任务已是终态（{status.value}），无需续跑")
        return None, report

    # ---- ② 工作区必须一致 ----
    saved_ws = str(state_dict.get("workspace") or "")
    if workspace and saved_ws and _norm(saved_ws) != _norm(workspace):
        report.reasons.append(
            f"快照的工作区（{saved_ws}）与当前任务工作区（{workspace}）不一致，"
            "文件可能已不在原位置"
        )
        return None, report

    # ---- ③ 历史：必须存在，且不能有工具结果缺口 ----
    messages = entries_to_messages(entries)
    report.loaded_messages = len(messages)
    report.resumed_from_turn = int(state_dict.get("turn") or 0)
    if not messages:
        report.reasons.append("持久化历史为空：无法确认模型此前看到过什么，续跑会重复执行")
        return None, report

    gaps = find_tool_call_gaps(messages)
    report.tool_call_gaps = gaps
    if gaps:
        report.reasons.append(
            f"存在 {len(gaps)} 处工具结果缺口（工具可能已执行但结果未保存），"
            "重跑有副作用风险"
        )

    last_role = str(getattr(messages[-1], "role", "") or "")
    if last_role in ("user", "system"):
        report.warnings.append(f"最后一条消息是 {last_role}，执行位置不够明确（仍可续，但需模型重新决策）")

    # ---- ④ 预算：耗尽了就别假装能续 ----
    budgets = state_dict.get("budgets") or {}
    try:
        max_turns = int(budgets.get("max_turns") or 0)
        used_turns = int(budgets.get("turns_used") or 0)
        max_calls = int(budgets.get("max_tool_calls") or 0)
        used_calls = int(budgets.get("tool_calls_used") or 0)
    except (TypeError, ValueError):
        max_turns = used_turns = max_calls = used_calls = 0
    if max_turns and used_turns >= max_turns:
        report.reasons.append(f"轮次预算已耗尽（{used_turns}/{max_turns}）")
    if max_calls and used_calls >= max_calls:
        report.reasons.append(f"工具调用预算已耗尽（{used_calls}/{max_calls}）")

    # ---- ⑤ 时效 ----
    if max_age_s and report.checkpoint_age_s > max_age_s:
        report.reasons.append(
            f"快照过旧（{report.checkpoint_age_s / 3600:.1f} 小时 > 上限 "
            f"{max_age_s / 3600:.1f} 小时），现场大概率已变"
        )

    # ---- ⑥ 其他提示（不阻断）----
    if state_dict.get("pending_approval"):
        report.warnings.append("快照里仍挂着待审批请求（审批机制已移除），已忽略")
    baseline = None
    loader = getattr(store, "load_baseline", None)
    if loader is not None:
        baseline = loader(task_id)
    if not baseline:
        report.warnings.append("缺少基线快照：续跑后的改动审计只能覆盖恢复之后的改动")

    report.safe_to_resume = not report.reasons
    if not report.safe_to_resume:
        return None, report

    state = TaskState.from_dict(state_dict)
    state.baseline_files = dict(baseline or {})
    return ResumePlan(
        state=state,
        entries=list(entries),
        messages=messages,
        baseline_files=dict(baseline or {}),
        ref=ref,
        report=report,
    ), report
