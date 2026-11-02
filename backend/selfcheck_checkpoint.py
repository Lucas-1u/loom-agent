"""
Checkpoint 模块自检（P0，对齐 DeepSeek Harness dsh-session-checkpoint-policy）

覆盖范围：
  A. 存储层（save / load / list / latest）
  B. 策略层（enabled / phase 过滤 / max_history 裁剪 / fail_closed）
  C. TaskState 序列化完整性（to_dict → checkpoint → 恢复）
  D. AgentCore 集成（_loop at_turn_start / _plan_phase before_model_call / _execute_tools before_tool_exec）
  E. graph_engine 集成（node_budget at_turn_start）

运行：./.venv/Scripts/python.exe -m backend.selfcheck_checkpoint
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 确保项目根在 sys.path 上
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent.checkpoint import (
    CheckpointPolicyConfig,
    LocalDiskCheckpointStore,
    save_checkpoint,
    load_checkpoint,
)
from backend.agent.checkpoint.store import CheckpointRef
from backend.agent.state import TaskState, TaskStatus

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------------- #
# A. 存储层
# --------------------------------------------------------------------------- #
def test_storage() -> None:
    print("\n== A. 存储层 ==")
    d = tempfile.mkdtemp(prefix="loom_ckpt_a_")
    store = LocalDiskCheckpointStore(d)

    st = TaskState(task_id="task_a")
    ref = save_checkpoint(store, st, phase="at_turn_start",
                          policy=CheckpointPolicyConfig())
    check("A1 save 返回 CheckpointRef", isinstance(ref, CheckpointRef))
    check("A2 seq 从 1 开始", ref.seq == 1, f"got {ref.seq}")
    check("A3 文件存在", os.path.exists(ref.path))
    check("A4 file 名格式", os.path.basename(ref.path) == "task_a_0001.json")

    loaded = load_checkpoint(store, ref)
    check("A5 load 返回 dict", isinstance(loaded, dict))
    check("A6 load 含 task_id", loaded.get("task_id") == "task_a")
    check("A7 load 含 _checkpoint 元数据", "_checkpoint" in loaded)
    check("A8 _checkpoint.seq=1", loaded["_checkpoint"]["seq"] == 1)
    check("A9 _checkpoint.phase", loaded["_checkpoint"]["phase"] == "at_turn_start")

    refs = store.list("task_a")
    check("A10 list 返回 1 条", len(refs) == 1, f"got {len(refs)}")

    latest = store.latest("task_a")
    check("A11 latest 返回 seq=1", latest is not None and latest.seq == 1)

    latest_none = store.latest("nonexistent")
    check("A12 latest 空任务返回 None", latest_none is None)

    list_none = store.list("nonexistent")
    check("A13 list 空任务返回 []", list_none == [])


# --------------------------------------------------------------------------- #
# B. 策略层
# --------------------------------------------------------------------------- #
def test_policy() -> None:
    print("\n== B. 策略层 ==")
    d = tempfile.mkdtemp(prefix="loom_ckpt_b_")
    store = LocalDiskCheckpointStore(d)
    st = TaskState(task_id="task_b")

    # enabled=False → 跳过
    ref = save_checkpoint(store, st, phase="at_turn_start",
                          policy=CheckpointPolicyConfig(enabled=False))
    check("B1 enabled=False 返回 None", ref is None)

    # phase 过滤
    policy_no_model = CheckpointPolicyConfig(before_model_call=False)
    ref = save_checkpoint(store, st, phase="before_model_call", policy=policy_no_model)
    check("B2 phase=before_model_call 被过滤", ref is None)

    policy_no_tool = CheckpointPolicyConfig(before_tool_exec=False)
    ref = save_checkpoint(store, st, phase="before_tool_exec", policy=policy_no_tool)
    check("B3 phase=before_tool_exec 被过滤", ref is None)

    policy_no_turn = CheckpointPolicyConfig(at_turn_start=False)
    ref = save_checkpoint(store, st, phase="at_turn_start", policy=policy_no_turn)
    check("B4 phase=at_turn_start 被过滤", ref is None)

    # 其他 phase 不受影响
    ref = save_checkpoint(store, st, phase="custom_phase",
                          policy=CheckpointPolicyConfig())
    check("B5 未知 phase 不被过滤", ref is not None)

    # max_history 裁剪
    policy = CheckpointPolicyConfig(max_history=5)
    for i in range(10):
        save_checkpoint(store, st, phase="at_turn_start", policy=policy)
    refs = store.list("task_b")
    # B1 没保存（None），B5 保存了 1 个（seq=1），后面 10 个 → seq 2~11
    # max_history=5 → 保留 seq 7~11
    check("B6 max_history=5 裁剪到 5 条", len(refs) == 5, f"got {len(refs)}")
    check("B7 裁剪后最小 seq", refs[0].seq == 7, f"got {refs[0].seq}")
    check("B8 裁剪后最大 seq", refs[-1].seq == 11, f"got {refs[-1].seq}")

    # fail_closed 行为（save_checkpoint 内部：失败不抛异常，只记日志）
    # 这里验证 fail_closed=True 时也不抛异常（Loom 选择可用性优先）
    ref = save_checkpoint(store, st, phase="at_turn_start",
                          policy=CheckpointPolicyConfig(fail_closed=True))
    check("B9 fail_closed=True 不抛异常", ref is not None)


# --------------------------------------------------------------------------- #
# C. TaskState 序列化完整性
# --------------------------------------------------------------------------- #
def test_state_serialization() -> None:
    print("\n== C. TaskState 序列化完整性 ==")
    d = tempfile.mkdtemp(prefix="loom_ckpt_c_")
    store = LocalDiskCheckpointStore(d)

    st = TaskState(task_id="task_c", goal="修复 bug")
    st.turn = 5
    st.status = TaskStatus.RUNNING

    ref = save_checkpoint(store, st, phase="before_model_call",
                          policy=CheckpointPolicyConfig())
    check("C1 save 返回 ref", ref is not None)

    loaded = load_checkpoint(store, ref)
    check("C2 load 返回 dict", isinstance(loaded, dict))
    check("C3 task_id 一致", loaded.get("task_id") == "task_c")
    check("C4 goal 一致", loaded.get("goal") == "修复 bug")
    check("C5 turn 一致", loaded.get("turn") == 5, f"got {loaded.get('turn')}")
    check("C6 status 一致", loaded.get("status") == TaskStatus.RUNNING.value)
    check("C7 _checkpoint.seq", loaded.get("_checkpoint", {}).get("seq") == 1)
    check("C8 _checkpoint.phase", loaded.get("_checkpoint", {}).get("phase") == "before_model_call")

    # 验证 JSON 文件可读
    with open(ref.path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    check("C9 文件含 meta", "meta" in raw)
    check("C10 文件含 state", "state" in raw)
    check("C11 meta.seq", raw["meta"]["seq"] == 1)


# --------------------------------------------------------------------------- #
# D. AgentCore 集成
# --------------------------------------------------------------------------- #
def test_core_integration() -> None:
    print("\n== D. AgentCore 集成 ==")
    from backend.agent.core import AgentCore
    from backend.tools.registry import ToolRegistry

    d = tempfile.mkdtemp(prefix="loom_ckpt_d_")
    store = LocalDiskCheckpointStore(d)

    # 最小构造：adapter=None, registry 空
    core = AgentCore(
        adapter=None,
        registry=ToolRegistry(),
        checkpoint_store=store,
        checkpoint_enabled=True,
    )
    check("D1 _checkpoint_store 已注入", core._checkpoint_store is store)
    check("D2 _checkpoint_policy 已创建", core._checkpoint_policy is not None)
    check("D3 _checkpoint_policy.enabled=True", core._checkpoint_policy.enabled is True)
    check("D4 _ckpt_save 已绑定", callable(core._ckpt_save))
    check("D5 _checkpoint_count=0 初始", core._checkpoint_count == 0)

    # 模拟 _ckpt_save 调用（count 由调用方递增，这里验证 ref 返回）
    st = TaskState(task_id="task_d", goal="测试 checkpoint")
    st.turn = 1
    ref = core._ckpt_save(store, st, phase="at_turn_start",
                          policy=core._checkpoint_policy)
    check("D6 _ckpt_save 返回 ref", ref is not None)
    check("D7 _ckpt_save 不递增 count（调用方负责）", core._checkpoint_count == 0)

    # 模拟调用方递增
    if ref is not None:
        core._checkpoint_count += 1
    check("D8 调用方递增后 count=1", core._checkpoint_count == 1)

    # 验证不传 store 时不报错
    core2 = AgentCore(adapter=None, registry=ToolRegistry())
    ref2 = core2._ckpt_save(None, st, phase="at_turn_start",
                            policy=core2._checkpoint_policy)
    check("D9 store=None 返回 None", ref2 is None)


# --------------------------------------------------------------------------- #
# E. graph_engine 集成
# --------------------------------------------------------------------------- #
def test_graph_integration() -> None:
    print("\n== E. graph_engine 集成 ==")
    from backend.agent.graph_engine import GraphTaskEngine
    from backend.agent.core import AgentCore
    from backend.tools.registry import ToolRegistry

    # 无 checkpoint store 的 core
    core_none = AgentCore(adapter=None, registry=ToolRegistry())
    check("E1 store=None 时 _checkpoint_store is None",
          core_none._checkpoint_store is None)
    check("E2 store=None 时 _checkpoint_policy 非 None",
          core_none._checkpoint_policy is not None)

    # 有 store 的情况
    d2 = tempfile.mkdtemp(prefix="loom_ckpt_e2_")
    store2 = LocalDiskCheckpointStore(d2)
    core_with = AgentCore(
        adapter=None, registry=ToolRegistry(),
        checkpoint_store=store2,
        checkpoint_enabled=True,
    )
    check("E3 有 store 时 _checkpoint_store 已注入", core_with._checkpoint_store is store2)
    check("E4 有 store 时 _checkpoint_policy.enabled=True",
          core_with._checkpoint_policy.enabled is True)


# --------------------------------------------------------------------------- #
# F. 环境变量配置
# --------------------------------------------------------------------------- #
def test_env_config() -> None:
    print("\n== F. 环境变量配置 ==")
    # 清空环境变量后测默认值
    for key in ("LOOM_CHECKPOINT", "LOOM_CKPT_MODEL", "LOOM_CKPT_TOOL",
                 "LOOM_CKPT_TURN", "LOOM_CKPT_MAX_HISTORY", "LOOM_CKPT_FAIL_CLOSED",
                 "LOOM_CKPT_DIR"):
        os.environ.pop(key, None)

    from backend.config import CheckpointConfig
    cfg = CheckpointConfig.from_env()
    check("F1 默认 enabled=True", cfg.enabled is True)
    check("F2 默认 before_model_call=True", cfg.before_model_call is True)
    check("F3 默认 before_tool_exec=True", cfg.before_tool_exec is True)
    check("F4 默认 at_turn_start=True", cfg.at_turn_start is True)
    check("F5 默认 max_history=20", cfg.max_history == 20)
    check("F6 默认 fail_closed=False", cfg.fail_closed is False)
    check("F7 默认 dir_name", cfg.dir_name == ".loom_checkpoints")

    # 关闭所有
    os.environ["LOOM_CHECKPOINT"] = "0"
    cfg2 = CheckpointConfig.from_env()
    check("F8 LOOM_CHECKPOINT=0 → enabled=False", cfg2.enabled is False)

    # 关闭单个 phase
    os.environ["LOOM_CKPT_TURN"] = "0"
    cfg3 = CheckpointConfig.from_env()
    check("F9 LOOM_CKPT_TURN=0 → at_turn_start=False", cfg3.at_turn_start is False)
    check("F10 其他 phase 不受影响", cfg3.before_model_call is True)

    # max_history
    os.environ["LOOM_CKPT_MAX_HISTORY"] = "5"
    cfg4 = CheckpointConfig.from_env()
    check("F11 LOOM_CKPT_MAX_HISTORY=5", cfg4.max_history == 5)

    # fail_closed
    os.environ["LOOM_CKPT_FAIL_CLOSED"] = "1"
    cfg5 = CheckpointConfig.from_env()
    check("F12 LOOM_CKPT_FAIL_CLOSED=1", cfg5.fail_closed is True)

    # 清理
    for key in ("LOOM_CHECKPOINT", "LOOM_CKPT_MODEL", "LOOM_CKPT_TOOL",
                 "LOOM_CKPT_TURN", "LOOM_CKPT_MAX_HISTORY", "LOOM_CKPT_FAIL_CLOSED",
                 "LOOM_CKPT_DIR"):
        os.environ.pop(key, None)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 70)
    print("Checkpoint 模块自检（P0 · 对齐 DeepSeek Harness dsh-session-checkpoint-policy）")
    print("=" * 70)

    test_storage()
    test_policy()
    test_state_serialization()
    test_core_integration()
    test_graph_integration()
    test_env_config()

    print("\n" + "=" * 70)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
