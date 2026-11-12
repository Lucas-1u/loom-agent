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
# G. TaskState.from_dict 往返（恢复的地基）
# --------------------------------------------------------------------------- #
def test_from_dict() -> None:
    print("\n== G. TaskState.from_dict 往返 ==")
    from backend.agent.state import ChangeRecord, Evidence, PlanStep, StepStatus

    st = TaskState(goal="往返", workspace="D:/proj", task_id="task_rt")
    st.turn = 3
    st.plan = [PlanStep(1, "读", StepStatus.DONE, evidence="读了"),
               PlanStep(2, "写", StepStatus.IN_PROGRESS, marks=(2, 1, 0))]
    st.current_step_index = 1
    st.budgets.turns_used = 3
    st.budgets.tool_calls_used = 7
    st.evidence_summary = [Evidence("read_file", "读到 10 行", ref="a.py:1-10")]
    st.change_records = [ChangeRecord("a.py", "edit", before_hash="h1", after_hash="h2", turn=2)]

    back = TaskState.from_dict(st.to_dict())
    check("G1 turn 还原", back.turn == 3, str(back.turn))
    check("G2 计划状态还原", [p.status.value for p in back.plan] == ["done", "in_progress"])
    check("G3★ 步骤证据游标 marks 还原", back.plan[1].marks == (2, 1, 0), str(back.plan[1].marks))
    check("G4 预算用量还原（续跑不吃免费额度）",
          (back.budgets.turns_used, back.budgets.tool_calls_used) == (3, 7))
    check("G5 证据还原", len(back.evidence_summary) == 1 and back.evidence_summary[0].ref == "a.py:1-10")
    check("G6 变更记录还原", back.change_records[0].before_hash == "h1")

    # 容错：脏值/缺字段不能抛
    dirty = {"status": "不存在的状态", "plan": [{"index": "x", "marks": "bad"}], "budgets": {"max_turns": None}}
    try:
        bad = TaskState.from_dict(dirty)
        ok = bad.status == TaskStatus.CREATED and bad.budgets.max_turns > 0
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"       from_dict 抛异常：{exc}")
    check("G7★ 脏快照不抛异常（枚举/类型都退回默认）", ok)
    check("G8 to_dict 的键集合往返后一致", set(st.to_dict()) == set(TaskState.from_dict(st.to_dict()).to_dict()))


# --------------------------------------------------------------------------- #
# H. plan_resume 的安全判据
# --------------------------------------------------------------------------- #
def _mk_resume_fixture(tmp: str):
    """造一份"正常可续"的现场：checkpoint + 历史 + 基线。"""
    from backend.agent.history_log import messages_to_entries
    from backend.models.adapter import Message

    store = LocalDiskCheckpointStore(os.path.join(tmp, ".loom_checkpoints"))
    st = TaskState(goal="续跑", workspace=tmp, task_id="task_res")
    st.status = TaskStatus.RUNNING
    st.turn = 2
    st.baseline_files = {"a.txt": {"size": 1, "sha": "x"}}
    save_checkpoint(store, st, phase="before_model_call", policy=CheckpointPolicyConfig())
    entries = messages_to_entries([Message.assistant("继续吧")], start_seq=1)
    return store, st, entries


def test_resume_gates() -> None:
    print("\n== H. plan_resume 安全判据 ==")
    from backend.agent.checkpoint import plan_resume
    from backend.agent.history_log import messages_to_entries
    from backend.models.adapter import Message, ToolCall

    tmp = tempfile.mkdtemp(prefix="ckpt_gate_")
    store, st, entries = _mk_resume_fixture(tmp)

    plan, rep = plan_resume(store, "task_res", entries, workspace=tmp)
    check("H1★ 完整现场 → safe_to_resume=True", rep.safe_to_resume and plan is not None,
          str(rep.reasons))
    check("H2 基线随现场一起装回（审计参照系）",
          plan is not None and plan.baseline_files.get("a.txt", {}).get("sha") == "x")
    check("H3 报告带上 checkpoint 序号", rep.checkpoint_seq >= 1)

    # 没有 checkpoint
    empty = LocalDiskCheckpointStore(os.path.join(tmp, "empty"))
    plan2, rep2 = plan_resume(empty, "task_none", entries, workspace=tmp)
    check("H4 无 checkpoint → 拒绝（理由明确）",
          plan2 is None and any("checkpoint" in r for r in rep2.reasons), str(rep2.reasons))

    # 工具结果缺口
    gap_entries = messages_to_entries([
        Message.assistant("我调两个工具", tool_calls=[
            ToolCall("c1", "read_file", '{"path":"a.py"}', {"path": "a.py"}),
            ToolCall("c2", "read_file", '{"path":"b.py"}', {"path": "b.py"}),
        ]),
        Message.tool_result("c1", "read_file", "只有一条结果"),
    ], start_seq=1)
    plan3, rep3 = plan_resume(store, "task_res", gap_entries, workspace=tmp)
    check("H5★★ 工具结果缺口 → 拒绝续跑（重跑有副作用）",
          plan3 is None and rep3.tool_call_gaps, str(rep3.reasons))

    # 终态
    st_done = TaskState(goal="已完成", workspace=tmp, task_id="task_done")
    st_done.status = TaskStatus.DONE
    save_checkpoint(store, st_done, phase="at_turn_start", policy=CheckpointPolicyConfig())
    plan4, rep4 = plan_resume(store, "task_done", entries, workspace=tmp)
    check("H6 终态任务 → 拒绝（无需续跑）",
          plan4 is None and any("终态" in r for r in rep4.reasons), str(rep4.reasons))

    # 预算耗尽
    st_broke = TaskState(goal="耗尽", workspace=tmp, task_id="task_broke")
    st_broke.budgets.turns_used = st_broke.budgets.max_turns
    save_checkpoint(store, st_broke, phase="at_turn_start", policy=CheckpointPolicyConfig())
    plan5, rep5 = plan_resume(store, "task_broke", entries, workspace=tmp)
    check("H7 轮次预算耗尽 → 拒绝（续跑也是立刻刹车）",
          plan5 is None and any("预算" in r for r in rep5.reasons), str(rep5.reasons))

    # 工作区不一致
    plan6, rep6 = plan_resume(store, "task_res", entries, workspace=os.path.join(tmp, "other"))
    check("H8 工作区不一致 → 拒绝（文件可能已搬家）",
          plan6 is None and any("工作区" in r for r in rep6.reasons), str(rep6.reasons))

    # 时效
    plan7, rep7 = plan_resume(store, "task_res", entries, workspace=tmp, max_age_s=0.0001, now=1e12)
    check("H9 快照过期 → 拒绝（现场大概率已变）",
          plan7 is None and any("过旧" in r for r in rep7.reasons), str(rep7.reasons))

    # 历史为空
    plan8, rep8 = plan_resume(store, "task_res", [], workspace=tmp)
    check("H10 历史为空 → 拒绝（不知道模型看到过什么）",
          plan8 is None and any("历史为空" in r for r in rep8.reasons), str(rep8.reasons))


# --------------------------------------------------------------------------- #
# I. 端到端续跑（FakeAdapter，不联网）
# --------------------------------------------------------------------------- #
def test_resume_end_to_end() -> None:
    print("\n== I. 端到端续跑（不重跑已执行工具 / 计划与基线与预算沿用） ==")

    from backend.agent.checkpoint import plan_resume
    from backend.agent.core import AgentConfig, AgentCore
    from backend.agent.history_log import MemoryHistorySink, messages_to_entries
    from backend.agent.state import Evidence, PlanStep, StepStatus
    from backend.models.adapter import FakeAdapter, Message, ModelReply
    from backend.tools.registry import build_default_registry

    tmp = tempfile.mkdtemp(prefix="ckpt_e2e_")
    store = LocalDiskCheckpointStore(os.path.join(tmp, ".loom_checkpoints"))
    policy = CheckpointPolicyConfig()

    st = TaskState(goal="续跑：读完文件后写代码", workspace=tmp, task_id="task_e2e")
    st.status = TaskStatus.RUNNING
    st.turn = 2
    st.plan = [PlanStep(1, "读文件", StepStatus.DONE, evidence="读到 10 行"),
               PlanStep(2, "写代码", StepStatus.IN_PROGRESS, marks=(1, 0, 0))]
    st.current_step_index = 1
    st.baseline_files = {"a.txt": {"size": 10, "sha": "abc"}}
    st.evidence_summary = [Evidence("read_file", "读到 a.txt 共 10 行", ref="a.txt:1-10")]
    st.budgets.turns_used = 2
    save_checkpoint(store, st, phase="before_model_call", policy=policy)

    entries = messages_to_entries(
        [Message.assistant("已读完 a.txt，共 10 行。接下来写实现。")], start_seq=1)
    plan, _ = plan_resume(store, "task_e2e", entries, workspace=tmp)
    check("I1 现场装配成功", plan is not None)

    fake = FakeAdapter([ModelReply(content="续跑完成：代码已写好。")])
    core = AgentCore(
        fake, build_default_registry(include_mutating=False),
        config=AgentConfig(use_plan_phase=True, use_reflect_phase=False,
                           orchestrator="legacy", min_evidence=1),
        history_sink=MemoryHistorySink(),
        checkpoint_store=store, checkpoint_policy=policy,
    )

    async def _run():
        return await core.run(st.goal, workspace=tmp, task_id="task_e2e", resume=plan)

    final = asyncio.run(_run())
    check("I2 续跑收敛为 done", final.status == TaskStatus.DONE, final.status.value)
    check("I3★ 轮次从断点继续（2 → 3），不是从头开始", final.turn == 3, str(final.turn))
    check("I4★ 计划与步进沿用 checkpoint（不重规划）",
          final.current_step_index == 1 and [p.index for p in final.plan] == [1, 2],
          str([(p.index, p.status.value) for p in final.plan]))
    check("I5★ 基线沿用（审计不会把中断前的改动当成用户原有状态）",
          final.baseline_files == {"a.txt": {"size": 10, "sha": "abc"}})
    check("I6 历史被装回模型上下文",
          any("已读完 a.txt" in (m.content or "") for m in (fake.calls[0] if fake.calls else [])))
    check("I7 恢复提示已注入（告诉模型别重复执行）",
          any("不要重复执行" in (m.content or "") for m in (fake.calls[0] if fake.calls else [])))
    check("I8 证据沿用（min_evidence 闸门不会把续跑当零证据）", len(final.evidence_summary) == 1)

    # ---- graph 引擎（默认）同样要能续：续跑时不该再走 plan 节点 ----
    from backend.agent.checkpoint import plan_resume as _pr
    tmp2 = tempfile.mkdtemp(prefix="ckpt_e2e_g_")
    store2 = LocalDiskCheckpointStore(os.path.join(tmp2, ".loom_checkpoints"))
    st2 = TaskState(goal="graph 续跑", workspace=tmp2, task_id="task_e2e_g")
    st2.status = TaskStatus.RUNNING
    st2.turn = 2
    st2.plan = [PlanStep(1, "读", StepStatus.DONE, evidence="ok"),
                PlanStep(2, "写", StepStatus.IN_PROGRESS, marks=(1, 0, 0))]
    st2.current_step_index = 1
    st2.evidence_summary = [Evidence("read_file", "读到")]
    save_checkpoint(store2, st2, phase="at_turn_start", policy=policy)
    plan2, _ = _pr(store2, "task_e2e_g", entries, workspace=tmp2)

    fake2 = FakeAdapter([ModelReply(content="graph 续跑完成")])
    core2 = AgentCore(
        fake2, build_default_registry(include_mutating=False),
        config=AgentConfig(orchestrator="graph", min_evidence=1, use_reflect_phase=False),
        history_sink=MemoryHistorySink(),
        checkpoint_store=store2, checkpoint_policy=policy,
    )

    async def _run_graph():
        return await core2.run(st2.goal, workspace=tmp2, task_id="task_e2e_g", resume=plan2)

    gfinal = asyncio.run(_run_graph())
    check("I9 graph 引擎续跑同样收敛", gfinal.status == TaskStatus.DONE, gfinal.status.value)
    check("I10★ 续跑跳过 plan 节点（沿用已有计划）",
          "plan" not in gfinal.graph_trace and gfinal.turn == 3,
          f"{gfinal.graph_trace} / turn={gfinal.turn}")


# --------------------------------------------------------------------------- #
# J. 服务端自动恢复（调度器接线）
# --------------------------------------------------------------------------- #
def test_scheduler_auto_resume() -> None:
    print("\n== J. 服务端启动自动恢复（调度器） ==")

    from backend.agent.core import AgentConfig
    from backend.agent.history_log import messages_to_entries
    from backend.models.adapter import FakeAdapter, Message, ModelReply
    from backend.server.hub import EventHub
    from backend.server.scheduler import TaskScheduler
    from backend.server.store import TaskStore
    from backend.tools.registry import build_default_registry

    async def _scenario():
        tmp = tempfile.mkdtemp(prefix="ckpt_sched_")
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws, exist_ok=True)
        db = os.path.join(tmp, "loom.db")
        store = TaskStore(db)
        await store.init()
        await store.upsert_session({
            "session_id": "s1", "title": "t", "workspace": ws,
            "created_at": 1.0, "updated_at": 1.0,
        })
        tid, created = await store.create_task({
            "task_id": "task_live", "session_id": "s1", "goal": "断掉的任务",
            "status": "running", "model": "fake", "workspace": ws,
            "created_at": 1.0, "started_at": 1.0,
        })
        # 历史 + checkpoint（模拟"跑到第 2 轮被 kill"）
        from backend.agent.history_log import messages_to_entries as m2e
        await store.append_history(tid, m2e(
            [Message.assistant("第一轮已完成观察。")], start_seq=1))

        from backend.agent.state import Evidence
        ck_store = LocalDiskCheckpointStore(os.path.join(ws, ".loom_checkpoints"))
        st = TaskState(goal="断掉的任务", workspace=ws, task_id=tid)
        st.status = TaskStatus.RUNNING
        st.turn = 1
        st.evidence_summary = [Evidence("list_files", "看到 2 个文件")]
        save_checkpoint(ck_store, st, phase="at_turn_start", policy=CheckpointPolicyConfig())

        cfg = AgentConfig(max_turns=5, max_seconds=30, min_evidence=0,
                          use_plan_phase=False, use_reflect_phase=False,
                          orchestrator="legacy")
        from backend.config import CheckpointConfig
        cfg.checkpoint = CheckpointConfig(enabled=True, auto_resume=True)

        sch = TaskScheduler(
            store=store, hub=EventHub(), adapter=FakeAdapter([ModelReply(content="续跑完成")]),
            registry=build_default_registry(include_mutating=False),
            config=cfg, max_global=2,
        )
        summary = await sch.recover_interrupted()
        rt = sch._runtimes.get(tid)
        if rt is not None:
            await asyncio.wait_for(rt.finished.wait(), timeout=20)
        row = await store.get_task(tid)
        return summary, row

    summary, row = asyncio.run(_scenario())
    check("J1 扫描到中断任务", summary.get("scanned") == 1, str(summary))
    check("J2★ 判定可续 → 自动重新入队", len(summary.get("resumed") or []) == 1, str(summary))
    check("J3 任务最终跑完（不是被标 failed）",
          (row or {}).get("status") == "done", str((row or {}).get("status")))
    check("J4 续跑轮次接着原任务（turn >= 2）",
          int((row or {}).get("turn") or 0) >= 2, str((row or {}).get("turn")))


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
    test_from_dict()
    test_resume_gates()
    test_resume_end_to_end()
    test_scheduler_auto_resume()

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
