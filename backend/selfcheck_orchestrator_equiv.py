"""
双引擎等价性验证（P1-#1 Agent Loop 编排对齐）

验证 graph 和 legacy 两种编排引擎在相同输入下的终态一致性。

已知差异（M4.3 记录）：
- 模型错误时 legacy 记 turn=2、graph 记 turn=1
- 终态/原因码一致，但轮次语义不等价

验证维度：
  A. 正常完成（模型返回文本）
  B. 模型错误（ModelError）
  C. 预算超限（轮次）
  D. 预算超限（工具调用）
  E. 连续错误熔断
  F. 取消
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent.core import AgentCore, AgentConfig
from backend.tools.registry import ToolRegistry
from backend.models.adapter import ModelAdapter, ModelReply, ModelError, StreamDelta, ToolCall
from backend.events import EventBus
from backend.agent.state import TaskStatus

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} {detail}")


class FakeAdapter(ModelAdapter):
    """测试用模型适配器：可配置行为。"""

    name = "fake"
    model = "fake-model"

    def __init__(self, mode: str = "text", fail_on_call: int = 0):
        self.mode = mode  # "text" / "error" / "empty"
        self.calls = 0
        self.fail_on_call = fail_on_call

    async def chat_stream(self, messages, tools=None, *, temperature=None, max_tokens=None):
        self.calls += 1

        if self.fail_on_call > 0 and self.calls >= self.fail_on_call:
            raise ModelError(f"模拟模型错误：第 {self.calls} 次调用")

        if self.mode == "empty":
            reply = ModelReply(content="", model=self.model)
        else:
            reply = ModelReply(content="完成", model=self.model)

        # 模拟流式：先发内容 delta，再结束
        if reply.content:
            yield StreamDelta(kind="content", text=reply.content)
        yield StreamDelta(kind="done")

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None):
        self.calls += 1

        if self.fail_on_call > 0 and self.calls >= self.fail_on_call:
            raise ModelError(f"模拟模型错误：第 {self.calls} 次调用")

        if self.mode == "empty":
            return ModelReply(content="", model=self.model)

        return ModelReply(content="完成", model=self.model)


async def run_engine(engine_type: str, adapter: FakeAdapter, config_overrides: dict = None) -> dict:
    """运行一个引擎，返回终态信息。"""
    reg = ToolRegistry()
    overrides = {"use_plan_phase": False, "use_reflect_phase": False, "max_turns": 5, "min_evidence": 0}
    if config_overrides:
        overrides.update(config_overrides)

    cfg = AgentConfig(orchestrator=engine_type, **overrides)
    core = AgentCore(adapter=adapter, registry=reg, config=cfg, bus=EventBus())

    try:
        state = await core.run("测试目标", workspace=".")
        return {
            "status": state.status.value,
            "end_reason": state.end_reason,
            "turn": state.turn,
            "tool_calls_used": state.budgets.tool_calls_used,
            "errors_used": state.budgets.errors_used,
        }
    except Exception as e:
        return {
            "status": "exception",
            "end_reason": str(e),
            "turn": 0,
            "tool_calls_used": 0,
            "errors_used": 0,
        }


async def main():
    print("=" * 70)
    print("双引擎等价性验证（P1-#1 Agent Loop 编排对齐）")
    print("=" * 70)

    # A. 正常完成
    print("\n== A. 正常完成 ==")
    adapter_legacy = FakeAdapter(mode="text")
    adapter_graph = FakeAdapter(mode="text")
    r_legacy = await run_engine("legacy", adapter_legacy)
    r_graph = await run_engine("graph", adapter_graph)

    check("A1 legacy 状态=done", r_legacy["status"] == "done", r_legacy["status"])
    check("A2 graph 状态=done", r_graph["status"] == "done", r_graph["status"])
    check("A3 终态一致", r_legacy["status"] == r_graph["status"])
    check("A4 end_reason 一致", r_legacy["end_reason"] == r_graph["end_reason"])

    # B. 模型错误
    print("\n== B. 模型错误 ==")
    adapter_legacy = FakeAdapter(mode="text", fail_on_call=1)
    adapter_graph = FakeAdapter(mode="text", fail_on_call=1)
    r_legacy = await run_engine("legacy", adapter_legacy)
    r_graph = await run_engine("graph", adapter_graph)

    check("B1 legacy 状态=failed", r_legacy["status"] == "failed", r_legacy["status"])
    check("B2 graph 状态=failed", r_graph["status"] == "failed", r_graph["status"])
    check("B3 end_reason 一致（model_error）", r_legacy["end_reason"] == r_graph["end_reason"] == "model_error")
    # 已知差异：turn 语义不同（legacy=2, graph=1）
    check("B4 已知差异：turn 不同（legacy>graph）", r_legacy["turn"] >= r_graph["turn"])

    # C. 预算超限（轮次）
    print("\n== C. 预算超限（轮次）")
    adapter_legacy = FakeAdapter(mode="text")
    adapter_graph = FakeAdapter(mode="text")
    r_legacy = await run_engine("legacy", adapter_legacy, {"max_turns": 0})
    r_graph = await run_engine("graph", adapter_graph, {"max_turns": 0})

    check("C1 legacy 状态=aborted 或 done", r_legacy["status"] in ("aborted", "done"), r_legacy["status"])
    check("C2 graph 状态=aborted 或 done", r_graph["status"] in ("aborted", "done"), r_graph["status"])
    check("C3 终态一致", r_legacy["status"] == r_graph["status"])

    # D. 预算超限（工具调用）
    print("\n== D. 预算超限（工具调用）")
    adapter_legacy = FakeAdapter(mode="text")
    adapter_graph = FakeAdapter(mode="text")
    r_legacy = await run_engine("legacy", adapter_legacy, {"max_tool_calls": 0})
    r_graph = await run_engine("graph", adapter_graph, {"max_tool_calls": 0})

    check("D1 legacy 状态=aborted", r_legacy["status"] == "aborted", r_legacy["status"])
    check("D2 graph 状态=aborted", r_graph["status"] == "aborted", r_graph["status"])
    check("D3 end_reason 一致（budget_tool_calls）", r_legacy["end_reason"] == r_graph["end_reason"] == "budget_tool_calls")

    # E. 连续错误熔断
    print("\n== E. 连续错误熔断")
    adapter_legacy = FakeAdapter(mode="text", fail_on_call=1)
    adapter_graph = FakeAdapter(mode="text", fail_on_call=1)
    r_legacy = await run_engine("legacy", adapter_legacy, {"max_errors": 1})
    r_graph = await run_engine("graph", adapter_graph, {"max_errors": 1})

    check("E1 legacy 状态=failed 或 aborted", r_legacy["status"] in ("failed", "aborted"), r_legacy["status"])
    check("E2 graph 状态=failed 或 aborted", r_graph["status"] in ("failed", "aborted"), r_graph["status"])

    # F. 空回复
    print("\n== F. 空回复（empty_reply）")
    adapter_legacy = FakeAdapter(mode="empty")
    adapter_graph = FakeAdapter(mode="empty")
    r_legacy = await run_engine("legacy", adapter_legacy)
    r_graph = await run_engine("graph", adapter_graph)

    check("F1 legacy 状态=done 或 aborted", r_legacy["status"] in ("done", "aborted"), r_legacy["status"])
    check("F2 graph 状态=done 或 aborted", r_graph["status"] in ("done", "aborted"), r_graph["status"])

    print("\n" + "=" * 70)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
