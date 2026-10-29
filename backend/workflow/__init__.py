"""
通用异步工作流（V1）。

对外只暴露三样：
    WorkflowStore    持久化（workflows / steps / jobs / outbox）
    WorkflowService  调度（DAG、并发、完成推进、重启恢复）
    StepSpec         步骤定义（工具层用它把模型的规划变成可执行结构）

执行器（http / tool / shell）在 `executors.py`，能力声明写在各自的类上。
"""

from .executors import ExecResult, get_executor
from .models import (Job, JobStatus, Step, StepSpec, StepStatus, Workflow, WorkflowStatus,
                     WorkflowView)
from .service import WorkflowError, WorkflowService, validate_steps
from .store import WorkflowStore

__all__ = [
    "ExecResult", "get_executor",
    "Job", "JobStatus", "Step", "StepSpec", "StepStatus", "Workflow", "WorkflowStatus",
    "WorkflowView",
    "WorkflowError", "WorkflowService", "validate_steps",
    "WorkflowStore",
]
