"""
P1 契约层自测 —— events.py + state.py + config.py

跑法（在 loom/ 目录下）：
    ./.venv/Scripts/python.exe -m backend.selfcheck_p1

注意：这里**不打网络**，纯粹验证数据结构与事件总线行为。
"""

from __future__ import annotations

import asyncio
import json
import sys

from backend.agent.state import (
    ApprovalRecord,
    ApprovalStatus,
    Budgets,
    ChangeRecord,
    Evidence,
    PendingApproval,
    PlanStep,
    RiskLevel,
    StepStatus,
    TaskState,
    TaskStatus,
)
from backend.config import Config, ModelConfig
from backend.events import Event, EventBus, EventType, collect


PASS = "  [ok]"
FAIL = "  [!!]"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(f"{PASS if cond else FAIL} {label}")


async def main() -> int:
    print("=" * 72)
    print("P1 契约层自测")
    print("=" * 72)

    # ------------------------------------------------------------------ #
    print("\n[1] EventBus 分配 seq / 扇出")
    bus = EventBus()
    bus.bind_task("task_demo")
    got = collect(bus)

    bus.task_start("分析当前项目", "D:/proj", "qwen3.7-plus")
    bus.plan([{"index": 0, "title": "先看目录结构", "status": "pending", "detail": ""}])
    bus.tool_call("call_1", "list_files", {"path": "."}, turn=1)
    bus.tool_result("call_1", "list_files", True, "列出 12 项", data={"count": 12}, turn=1)
    await bus.drain()

    check(len(got) == 4, f"4 个事件都被收到（实际 {len(got)}）")
    check([e.seq for e in got] == [1, 2, 3, 4], f"seq 单调递增 1..4（实际 {[e.seq for e in got]}）")
    check(all(e.task_id == "task_demo" for e in got), "默认 task_id 自动注入")
    check(got[2].turn == 1 and got[3].turn == 1, "turn 正确携带")
    check(got[0].type == EventType.TASK_START.value, "type 落为字符串值")

    # ------------------------------------------------------------------ #
    print("\n[2] 订阅者抛异常不影响其他订阅者与 Agent")
    bus2 = EventBus()
    good = collect(bus2)

    async def bad(ev: Event) -> None:
        raise RuntimeError("我是坏订阅者")

    bus2.subscribe(bad)
    bus2.error("tool_error", "读文件失败")
    await bus2.drain()
    check(len(good) == 1, "坏订阅者抛错，好订阅者仍收到事件")
    check(bus2.subscribers is not None, "总线未因异常崩坏")
    check(bus2.delivery_errors == 1,
          f"★ 订阅者异常被准确计数（实际 {bus2.delivery_errors}，应为 1）")

    # ------------------------------------------------------------------ #
    print("\n[3] Event.to_sse 帧格式")
    ev = Event.make(EventType.TOOL_RESULT, {"tool": "read_file", "ok": True}, task_id="t1", turn=2, seq=7)
    frame = ev.to_sse()
    check(frame.startswith("event: tool.result\n"), "SSE 首行 event: <type>")
    check(frame.endswith("\n\n"), "SSE 以空行结尾")
    body = [l for l in frame.split("\n") if l.startswith("data: ")][0][6:]
    parsed = json.loads(body)
    check(parsed["seq"] == 7 and parsed["turn"] == 2, "SSE data 可 JSON 解析且字段完整")
    check("中文" not in body or "\\u" not in body, "中文未被转义（ensure_ascii=False）")
    check("data" in parsed and parsed["data"]["ok"] is True, "data 载荷透传")

    # ------------------------------------------------------------------ #
    print("\n[4] TaskState 轮次与预算")
    st = TaskState(goal="分析项目", workspace="D:/proj", model="qwen3.7-plus")
    st.budgets = Budgets(max_turns=3, max_tool_calls=5, max_same_call_repeat=2, min_evidence=1)
    check(st.status == TaskStatus.CREATED, "初始状态 created")
    for _ in range(3):
        st.next_turn()
    check(st.turn == 3 and st.budgets.turns_used == 3, "turn 与 turns_used 同步")
    v = st.budget_violation()
    check(v is not None and v[0] == "turns", f"轮数耗尽被裁定（{v}）")

    # ------------------------------------------------------------------ #
    print("\n[5] 工具留痕 + 重复指纹熔断")
    st2 = TaskState(goal="x", workspace="D:/proj")
    st2.budgets = Budgets(max_same_call_repeat=2)
    check(not st2.should_break_repeat("read_file", {"path": "a.py"}), "首次调用不熔断")
    st2.record_tool_call("c1", "read_file", {"path": "a.py"}, ok=True, summary="80 行")
    check(not st2.should_break_repeat("read_file", {"path": "a.py"}), "第 2 次仍不熔断（阈值 2）")
    st2.record_tool_call("c2", "read_file", {"path": "a.py"}, ok=True, summary="80 行")
    check(st2.should_break_repeat("read_file", {"path": "a.py"}), "第 2 次记录后触发熔断")
    check(not st2.should_break_repeat("read_file", {"path": "b.py"}), "换参数不熔断")
    # 参数顺序无关
    st2.record_tool_call("c3", "grep", {"a": 1, "b": 2}, ok=True, summary="s")
    st2.record_tool_call("c4", "grep", {"b": 2, "a": 1}, ok=True, summary="s")
    check(st2.should_break_repeat("grep", {"a": 1, "b": 2}), "参数字典顺序不同 → 同指纹")

    # ------------------------------------------------------------------ #
    print("\n[6] 证据收集与 min_evidence")
    st3 = TaskState(goal="x", workspace="D:/w")
    st3.budgets = Budgets(min_evidence=1)
    check(not st3.has_enough_evidence(), "无证据时不足")
    st3.record_tool_call("c1", "read_file", {"path": "a.py"}, ok=True, summary="读到 80 行")
    st3.add_evidence(Evidence(source="read_file", summary="读到 80 行", ref="a.py:1-80"))
    check(st3.has_enough_evidence(), "拿到 1 条证据即满足（C3）")
    st4 = TaskState(goal="x", workspace="D:/w")
    st4.add_evidence(Evidence(source="read_file", summary="失败", ok=False))
    check(not st4.has_enough_evidence(), "失败的证据不算数")

    # ------------------------------------------------------------------ #
    print("\n[7] 计划推进（第二批：由证据决定）")
    st5 = TaskState(goal="x", workspace="D:/w")
    st5.set_plan(
        [
            PlanStep(0, "看目录", check="observation"),
            PlanStep(1, "读关键文件", check="observation"),
            PlanStep(2, "给结论", check="observation"),
        ],
        note="先摸清结构",
    )
    check(st5.current_step.title == "看目录", "当前步 = 第 0 步")
    st5.start_current_step()
    check(st5.current_step.status == StepStatus.IN_PROGRESS, "开工后是 in_progress")
    check(st5.finish_current_step()[0] is False, "★ 没有新证据不能算完成（不再按轮次打勾）")
    st5.add_evidence(Evidence(source="list_files", summary="看到目录", ok=True))
    check(st5.finish_current_step()[0] is True, "★ 有新证据才标记完成")
    check(st5.plan[0].status == StepStatus.DONE, "第 0 步标记完成")
    check(st5.current_step.title == "读关键文件", "推进到第 1 步")
    st5.fail_current_step("读不到")
    check(st5.current_step.status == StepStatus.FAILED, "可以标记失败")
    check(st5.current_step_index == 1, "★ 失败不推进索引")
    st5.current_step_index = 3
    check(st5.current_step is None, "越界返回 None（不抛异常）")

    # ------------------------------------------------------------------ #
    print("\n[8] 审批字段（v0.1a 核心）")
    st6 = TaskState(goal="x", workspace="D:/w")
    check(st6.pending_approval is None, "初始无待审批")
    pa = PendingApproval(
        approval_id=PendingApproval.new_id(),
        tool="write_file",
        args={"path": "a.py", "content": "print(1)"},
        args_hash="abc123",
        risk=RiskLevel.WRITE,
        action="写入文件",
        target="D:/w/a.py",
        impact="覆盖原有内容（已备份）",
        preview="print(1)",
    )
    st6.pending_approval = pa
    st6.status = TaskStatus.AWAITING_APPROVAL
    d = st6.to_dict()
    check(d["status"] == "awaiting_approval", "快照里状态为 awaiting_approval")
    check(d["pending_approval"]["args_hash"] == "abc123", "快照含 args_hash 指纹")
    check(d["pending_approval"]["risk"] == "write", "风险等级序列化正确")

    st6.approval_history.append(
        ApprovalRecord(
            approval_id=pa.approval_id,
            tool="write_file",
            target="D:/w/a.py",
            status=ApprovalStatus.APPROVED,
            args_hash="abc123",
        )
    )
    st6.change_records.append(
        ChangeRecord(path="D:/w/a.py", action="create", after_hash="deadbeef", added=1, approval_id=pa.approval_id)
    )
    d2 = st6.to_dict()
    check(len(d2["approval_history"]) == 1 and len(d2["change_records"]) == 1, "审批历史与变更记录入快照")

    # ------------------------------------------------------------------ #
    print("\n[9] finish() 收尾")
    st6.finish(TaskStatus.DONE, answer="项目是一个 FastAPI 服务", reason="agent_done")
    check(st6.status == TaskStatus.DONE, "状态置 done")
    check(st6.pending_approval is None, "收尾时清空挂起审批")
    check(st6.elapsed_s >= 0, "elapsed_s 可计算")
    check(st6.to_dict()["final_answer"] == "项目是一个 FastAPI 服务", "最终答复入快照")

    # ------------------------------------------------------------------ #
    print("\n[10] 快照可 JSON 序列化（前端契约硬要求）")
    big = TaskState(goal="g", workspace="D:/w", model="m")
    big.next_turn()
    big.record_tool_call("c1", "read_file", {"path": "a.py"}, ok=True, summary="ok")
    big.add_evidence(Evidence("read_file", "读到 80 行", "a.py:1-80"))
    try:
        s = json.dumps(big.to_dict(), ensure_ascii=False)
        check(True, f"TaskState.to_dict() 可 JSON 化（{len(s)} 字节）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"JSON 化失败：{exc}")

    # ------------------------------------------------------------------ #
    print("\n[11] Config 装配")
    cfg = Config.load()
    check(cfg.server.host == "127.0.0.1", f"服务绑定 127.0.0.1（实际 {cfg.server.host}）")
    check(cfg.server.port == 8765, f"默认端口 8765（实际 {cfg.server.port}）")
    check(cfg.budgets.max_turns == 20, f"max_turns=20（C2，实际 {cfg.budgets.max_turns}）")
    check(cfg.budgets.min_evidence == 1, f"min_evidence=1（C3，实际 {cfg.budgets.min_evidence}）")
    # ★ 断言改成"在 presets 里"，而不是写死名单：
    #   加新供应商（xfyun）时这条测试不该跟着改。
    check(cfg.model.provider in ModelConfig().PRESETS,
          f"供应商合法（{cfg.model.provider}）")
    check(cfg.model.models and cfg.model.models[0] == cfg.model.model,
          f"候选列表首项即主模型（{cfg.model.models[:2]}）")
    check(isinstance(cfg.model.configured, bool), f"API Key 是否配置：{cfg.model.configured}")

    # ------------------------------------------------------------------ #
    print("\n[12] 枚举取值与设计文档一致")
    # 第五批新增 context.compacted（压缩）→ 17；M3 新增 rag.retrieve/rag.result → 19
    check(len(EventType) == 20, f"事件类型共 20 种（实际 {len(EventType)}）")
    check(EventType.RAG_RETRIEVE.value == "rag.retrieve"
          and EventType.RAG_RESULT.value == "rag.result",
          "★ M3 检索事件已注册（命名遵循 <域>.<动作>）")
    check(EventType.CONTEXT_COMPACTED.value == "context.compacted",
          "★ 上下文压缩事件已注册")
    check(EventType.APPROVAL_REQUEST.value == "approval.request", "审批事件命名 <域>.<动作>")
    check(EventType.TOOL_CALL.value == "tool.call", "工具事件命名 <域>.<动作>")

    # ------------------------------------------------------------------ #
    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print("\n失败项：")
        for c, label in results:
            if not c:
                print(f"  - {label}")
    print("=" * 72)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
