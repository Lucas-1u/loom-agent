"""
Checkpoint / 跨进程恢复（对齐 DeepSeek Harness dsh-session-checkpoint-policy）

★ 解决什么问题
----------------
进程崩溃 / 被 kill / 审批 Future 丢失之后，任务状态只存在于内存里，
重启后无法恢复。Checkpoint 在关键执行点之前把 TaskState 完整落盘，
使得进程重启后可以从最后一个 checkpoint 恢复并继续。

★ 与 DSH 的关系
----------------
DSH 的 `dsh-session-checkpoint-policy` 是一个 cordis 插件，在
`llm/stream`、`tools/execute`、`agent/pre-step` 三个事件上安装监听器，
在下游执行前调用 `ctx.sessions.flush(session)`，失败则 fail-closed。

本模块对齐这一策略，但适配 Loom 的架构：
  · 不依赖 cordis 事件系统，直接在 AgentCore 的执行点插入
  · checkpoint 落盘到工作区下的 `.loom_checkpoint/` 目录
  · 失败语义：checkpoint 失败不阻止执行（与 DSH 不同——Loom 选择可用性优先）

★ checkpoint 点
----------------
  1. before_model_call  —— 模型调用前（_act_phase / _call_model）
  2. before_tool_exec   —— 工具执行前（_execute_tools）
  3. at_turn_start      —— 每轮开始前（_loop / node_budget）

这三个点覆盖了所有"有副作用或不可逆"的操作之前的窗口。
"""

from .store import CheckpointStore, LocalDiskCheckpointStore, CheckpointRef, load_checkpoint
from .policy import CheckpointPolicyConfig, save_checkpoint, latest_checkpoint_id

__all__ = [
    "CheckpointStore",
    "LocalDiskCheckpointStore",
    "CheckpointRef",
    "load_checkpoint",
    "CheckpointPolicyConfig",
    "save_checkpoint",
    "latest_checkpoint_id",
]
