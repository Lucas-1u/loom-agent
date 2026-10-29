"""
异步工作流（V1）：Workflow / Step / Job 的数据模型。

★ 为什么要分三层
--------------
    Workflow  用户给的那个目标（一件事）
      └ Step  目标拆出来的一个**可验收**步骤（有依赖、有状态）
           └ Job  真正落在某个执行器上的一次运行（http / tool / shell）

  区分它们的原因：Step 关心"依赖是否满足"，Job 关心"这一跑成没成、能不能重试"。
  一个 Step 可能因为重试产生多个 Job（V1 只保留最近一次的结果，历史在 outbox 里）。

★ 状态机（三条，不要混用）
------------------------
  Workflow: pending → running → completed | failed | cancelled | needs_attention
            running 期间可以进入 paused（等外部/等审批），再回到 running

  Step:     pending → ready（依赖满足）→ running → done | failed | blocked | cancelled
            blocked = 上游有失败/被阻断，自己不会执行

  Job:      pending → running → done | failed | timed_out | cancelled | unknown
            unknown = 进程重启后无法确认是否已执行（**不**自动重跑有副作用的任务）

★ 为什么 Job 要单独记 resumable
------------------------------
  GET 这类幂等操作，重启后重跑是安全的；shell 有副作用，重启后只能标 unknown。
  这一条决定了"能不能自动恢复"，不是拍脑袋选的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"   # 重启后状态不明，等人看


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_STEP = {StepStatus.DONE, StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.CANCELLED}
OK_STEP = {StepStatus.DONE}


@dataclass
class StepSpec:
    """一个步骤的定义（来自模型的规划结果）。"""

    id: str
    title: str = ""
    kind: str = "tool"                       # http | tool | shell
    spec: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    timeout_s: float = 0.0                   # 0 = 用默认值


@dataclass
class Workflow:
    id: str
    goal: str = ""
    status: WorkflowStatus = WorkflowStatus.PENDING
    task_id: str = ""
    workspace: str = ""
    created_at: float = 0.0
    deadline_at: float = 0.0
    cancel_requested: bool = False
    plan_version: int = 1
    error: str = ""


@dataclass
class Step:
    workflow_id: str
    step_id: str
    title: str = ""
    kind: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    timeout_s: float = 0.0
    result: dict[str, Any] | None = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0


@dataclass
class Job:
    id: str
    workflow_id: str
    step_id: str
    kind: str = ""
    status: JobStatus = JobStatus.PENDING
    resumable: bool = False
    attempts: int = 0
    max_attempts: int = 1
    timeout_s: float = 60.0
    created_at: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    result: dict[str, Any] | None = None
    error: str = ""
    external_id: str = ""


@dataclass
class WorkflowView:
    """给工具/接口看的一份快照（Workflow + 所有 Step）。"""

    workflow: Workflow
    steps: list[Step]

    def to_dict(self) -> dict[str, Any]:
        w = self.workflow
        return {
            "workflow_id": w.id,
            "goal": w.goal,
            "status": w.status.value,
            "task_id": w.task_id,
            "created_at": w.created_at,
            "deadline_at": w.deadline_at,
            "cancel_requested": w.cancel_requested,
            "error": w.error,
            "steps": [
                {
                    "step_id": s.step_id,
                    "title": s.title,
                    "kind": s.kind,
                    "status": s.status.value,
                    "depends_on": list(s.depends_on),
                    "attempts": s.attempts,
                    "error": s.error,
                    "elapsed_ms": int((s.ended_at - s.started_at) * 1000) if s.ended_at else 0,
                    "result": s.result,
                }
                for s in self.steps
            ],
        }

    @property
    def running(self) -> list[Step]:
        return [s for s in self.steps if s.status is StepStatus.RUNNING]

    @property
    def pending(self) -> list[Step]:
        return [s for s in self.steps if s.status in (StepStatus.PENDING, StepStatus.READY)]
