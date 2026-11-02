"""
Checkpoint 策略

★ 三个执行点（对齐 DSH dsh-session-checkpoint-policy）
----------------
DSH 在 `llm/stream`、`tools/execute`、`agent/pre-step` 三个事件上安装监听器。
Loom 对应地在以下位置插入 checkpoint：

  1. `before_model_call`  —— 模型调用前（_act_phase / _plan_phase）
     → 对应 DSH 的 `llm/stream`
  2. `before_tool_exec`   —— 工具执行前（_execute_tools）
     → 对应 DSH 的 `tools/execute`
  3. `at_turn_start`      —— 每轮开始前（_loop 顶部 / node_budget）
     → 对应 DSH 的 `agent/pre-step`

★ 失败语义（与 DSH 不同）
----------------
DSH 的 checkpoint 失败是 fail-closed：下游 adapter/tool 不会被调用。
Loom 选择**可用性优先**：checkpoint 失败只记日志 + 计数，不阻止执行。
理由：Loom 是本地个人 Agent，可用性比严格一致性更重要。
但会在状态里记录 `checkpoint_failures`，用户可以看到。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .store import CheckpointStore, CheckpointRef

log = logging.getLogger("loom.checkpoint")


@dataclass
class CheckpointPolicyConfig:
    """
    Checkpoint 策略配置。

    控制哪些执行点需要 checkpoint、是否启用等。
    """

    enabled: bool = True
    # 哪些阶段做 checkpoint
    before_model_call: bool = True
    before_tool_exec: bool = True
    at_turn_start: bool = True
    # 保留多少个历史 checkpoint（超过就删旧的）
    max_history: int = 20
    # checkpoint 失败是否阻止执行（Loom 默认 False = 不阻止）
    fail_closed: bool = False


def save_checkpoint(
    store: CheckpointStore | None,
    state: Any,
    *,
    phase: str,
    policy: CheckpointPolicyConfig,
) -> CheckpointRef | None:
    """
    在指定执行点保存一个 checkpoint。

    参数：
      store   —— checkpoint 存储（None = 不启用）
      state   —— TaskState 实例
      phase   —— checkpoint 点标识（before_model_call / before_tool_exec / at_turn_start）
      policy  —— 策略配置

    返回：
      CheckpointRef 如果成功保存；None 如果未启用或失败。

    ★ 不抛异常：失败记日志 + 返回 None。调用方不应因为 checkpoint 失败而中断。
    """
    if store is None or not policy.enabled:
        return None

    # 检查当前 phase 是否应该 checkpoint
    if phase == "before_model_call" and not policy.before_model_call:
        return None
    if phase == "before_tool_exec" and not policy.before_tool_exec:
        return None
    if phase == "at_turn_start" and not policy.at_turn_start:
        return None

    try:
        payload = {
            "task_id": state.task_id,
            "phase": phase,
            "state": state.to_dict(),
        }
        ref = store.save(payload)
        # 清理旧 checkpoint
        _cleanup_old(store, state.task_id, policy.max_history)
        log.debug(
            "任务 %s checkpoint[%s] seq=%d 已保存",
            state.task_id, phase, ref.seq,
        )
        return ref
    except Exception as exc:  # noqa: BLE001 —— checkpoint 失败绝不影响主流程
        log.warning(
            "任务 %s checkpoint[%s] 保存失败：%s: %s",
            state.task_id, phase, type(exc).__name__, exc,
        )
        # 计数（在 state 上累加，但不写回 DB）
        if hasattr(state, "_checkpoint_failures"):
            state._checkpoint_failures += 1
        return None


def _cleanup_old(
    store: CheckpointStore,
    task_id: str,
    max_history: int,
) -> None:
    """删除超出 max_history 的旧 checkpoint。"""
    if max_history <= 0:
        return
    refs = store.list(task_id)
    if len(refs) <= max_history:
        return
    # 保留最新的 max_history 个
    to_delete = refs[:-max_history]
    for ref in to_delete:
        store.delete(ref)


def latest_checkpoint_id(store: CheckpointStore, task_id: str) -> str | None:
    """获取任务最新的 checkpoint 文件路径（用于恢复）。"""
    ref = store.latest(task_id)
    return ref.path if ref else None
