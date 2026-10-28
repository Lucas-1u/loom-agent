"""
Graph 编排引擎（M2）：用 LangGraph StateGraph 驱动 AgentCore 的阶段方法。

★ 设计原则（对应集成方案的三个约束）
----------------
1. **不复制状态**：任务真相始终在 <code>AgentCore._state</code>（TaskState），
   图状态（GraphState）只承载**路由信息**（phase/route/note）。
   节点直接操作 core 上的共享对象 —— 两套状态不一致的问题在结构上不存在。
2. **不复制安全机制**：节点调用的是 AgentCore 上的同一组检查点方法
   （预算 <code>_budget_verdict</code>、闸门 <code>_verification_gate</code>、
   工具链 <code>_execute_tools</code>、审计 <code>_record_audit</code>）——
   Policy/审批/作用域对 graph 路径自动生效。
3. **图管控制流**：轮次循环、条件分支（工具/文本）、终止（完成/预算/取消/失败）
   全部由图的条件边表达 —— 这就是"控制流确实由 LangGraph 管理"的含义。

★ 与 legacy <code>_loop</code> 的对应关系
----------------
    budget  节点  ≙  _loop 顶部的 取消/预算/熔断 检查
    plan    节点  ≙  _loop 开头的 _plan_phase（可关闭）
    act     节点  ≙  _act_phase（模型调用）
    tools   节点  ≙  _execute_tools + _advance_plan_by_evidence
    reflect 节点  ≙  _reflect_phase（可能内部收尾 → END）
    finalize节点  ≙  _finalize_text_reply（分支 B：文本答复的三道关口）
每轮的节点序列：budget → plan(仅首轮) → act → (tools → reflect | finalize) → budget → …
M4.3 起 graph 是**默认引擎**；legacy 仍可通过 LOOM_ORCHESTRATOR=legacy 显式启用（兜底，暂不删除）。

★ 图状态为什么这么小
----------------
ModelReply、消息历史、工具结果都是**不可序列化或大对象**，
塞进图状态既破坏 checkpointer 兼容也不必要 —— 它们的真相在 core 上。
图状态里放的是「下一步去哪」这一决策的路由痕迹。
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from .state import TaskState, TaskStatus

log = logging.getLogger("loom.graph")

try:  # ★ 延迟导入：legacy 模式不需要安装 langgraph
    from langgraph.graph import END, START, StateGraph
except ImportError as _e:  # pragma: no cover —— 仅在未安装时触发
    StateGraph = None
    _LANGGRAPH_IMPORT_ERROR: Exception | None = _e


class GraphState(TypedDict, total=False):
    """
    图路由状态。

    字段来源与生命周期：
      task_id  任务标识（进入图时写入，只读）
      phase    最近经过的节点名（每个节点更新）
      route    最近一次条件路由的决定（条件边读取）
      note     诊断备注（终止原因 / 续期原因等）
      turns    已消费的模型轮次快照（诊断用，与 Budgets.turns_used 同源）
    """

    task_id: str
    phase: str
    route: str
    note: str
    turns: int


def _finished(state: TaskState) -> bool:
    """任务是否已经收尾（finish() 会写 end_reason）。"""
    return state.end_reason != ""


def build_task_graph(core, engine):
    """
    构建任务编排图。

    参数 core 是 <code>AgentCore</code> 实例 —— 节点是它的方法绑定闭包，
    所有安全机制（预算/审批/审计/闸门）都在节点内部经由 core 复用。
    """
    if StateGraph is None:
        raise RuntimeError(
            "Graph 编排引擎需要安装 langgraph："
            ".venv/Scripts/python.exe -m pip install 'langgraph>=0.2,<0.7'"
            f"（导入失败原因：{_LANGGRAPH_IMPORT_ERROR}）"
        )

    g = StateGraph(GraphState)
    g.add_node("budget", engine.node_budget)
    g.add_node("plan", engine.node_plan)
    g.add_node("act", engine.node_act)
    g.add_node("tools", engine.node_tools)
    g.add_node("reflect", engine.node_reflect)
    g.add_node("finalize", engine.node_finalize)

    g.add_edge(START, "budget")
    # ★ path_map 必须显式声明所有可能目标（含 END）：
    #   LangGraph 0.6 对未声明的路由目标会静默忽略 —— 终止分支会被吞掉
    #   （实测：预算耗尽/取消/验证失败都变成了 done，是 M2 最危险的坑）。
    g.add_conditional_edges(
        "budget", engine.route_budget,
        {"plan": "plan", "act": "act", "END": END},
    )
    # ★ M4.3：plan 必须是**条件边**。以前这里是无条件边 add_edge("plan","budget")，
    #   于是 node_plan 返回的 route="END"（例如模型认证失败、任务已收尾）被静默吞掉，
    #   任务会一直 plan→budget→plan 空转直到轮次耗尽（实测：model_error 变成 budget_turns）。
    #   这与上面 budget/act/reflect/finalize 的 path_map 教训是同一类坑。
    g.add_conditional_edges(
        "plan", engine.route_plan,
        {"budget": "budget", "END": END},
    )
    g.add_conditional_edges(
        "act", engine.route_act,
        {"tools": "tools", "finalize": "finalize", "END": END},
    )
    g.add_edge("tools", "reflect")
    g.add_conditional_edges(
        "reflect", engine.route_reflect,
        {"budget": "budget", "END": END},
    )
    g.add_conditional_edges(
        "finalize", engine.route_finalize,
        {"budget": "budget", "END": END},
    )
    return g.compile()


class GraphTaskEngine:
    """LangGraph 编排引擎：与 legacy <code>_loop</code> 共享同一组 AgentCore 检查点。"""

    def __init__(self, core) -> None:
        self.core = core
        self.app = build_task_graph(core, self)
        # 最近一次模型回复挂在引擎上（ModelReply 不序列化，不进图状态）；
        # 节点间是同步顺序调用，不存在并发竞争。
        self._last_reply: Any = None
        self.node_trace: list[str] = []

    # ------------------------------------------------------------------ #
    # 节点
    # ------------------------------------------------------------------ #
    async def node_budget(self, state: GraphState) -> dict[str, Any]:
        """每轮入口：取消 / 预算（含有界续期）/ 错误熔断 —— 与 legacy 循环顶部一致。"""
        core, st = self.core, self.core._state
        self._trace("budget")

        # ---- 取消（优先级最高，不可被续期覆盖）--------------------------
        if core._cancel_event is not None and core._cancel_event.is_set():
            st.finish(TaskStatus.ABORTED,
                      core._fallback_answer(st, "cancelled"), "cancelled")
            core.bus.task_end("aborted", st.final_answer, "cancelled")
            return {"phase": "budget", "route": "END", "note": "cancelled"}

        # ---- 预算（含一次有界续期）--------------------------------------
        violation = st.budget_violation()
        if violation is not None:
            kind, limit, used = violation
            verdict = core._budget_verdict(st, kind)
            if kind == "turns" and verdict["extend"] and not st.budget_extended:
                st.budget_extended = True
                extra = core.config.budget_extend_turns
                st.budgets.max_turns += extra
                st.budget_reason = f"{verdict['label']}（续期 +{extra} 轮）"
                core.bus.error("budget_extended",
                               f"预算续期 +{extra} 轮：{verdict['label']}",
                               fatal=False, turn=st.turn)
                return {"phase": "budget", "route": "act",
                        "note": verdict["label"], "turns": st.turn}
            st.budget_reason = verdict["label"] if kind == "turns" else f"budget_{kind}"
            core.bus.budget_exceeded(kind, limit, used, turn=st.turn)
            st.finish(TaskStatus.ABORTED, core._fallback_answer(st, f"budget_{kind}"),
                      f"budget_{kind}")
            core.bus.task_end("aborted", st.final_answer, f"budget_{kind}")
            return {"phase": "budget", "route": "END", "note": f"budget_{kind}"}

        # ---- 连续错误熔断 ------------------------------------------------
        if st.consecutive_errors >= st.budgets.max_errors:
            core.bus.budget_exceeded("errors", st.budgets.max_errors, st.consecutive_errors)
            st.finish(TaskStatus.ABORTED, core._fallback_answer(st, "too_many_errors"),
                      "too_many_errors")
            core.bus.task_end("aborted", st.final_answer, "too_many_errors")
            return {"phase": "budget", "route": "END", "note": "too_many_errors"}

        # ---- 新一轮 ------------------------------------------------------
        # PLAN 只跑一次：plan 或 plan_note 已设置就不再规划
        #（legacy 语义：use_plan_phase 时 _plan_phase 自消耗 1 轮）
        if core.config.use_plan_phase and not st.plan and not st.plan_note:
            return {"phase": "budget", "route": "plan", "turns": st.turn}
        st.next_turn()
        return {"phase": "budget", "route": "act", "turns": st.turn}

    async def node_plan(self, state: GraphState) -> dict[str, Any]:
        """首轮规划（可关闭）。模型失败时任务已在节点内收尾 → END。"""
        core, st = self.core, self.core._state
        self._trace("plan")
        if core.config.use_plan_phase:
            await core._plan_phase(st)   # 内部自消耗 1 轮（与 legacy 一致）
            if _finished(st):
                return {"phase": "plan", "route": "END", "note": st.end_reason}
        return {"phase": "plan", "route": "budget"}

    async def node_act(self, state: GraphState) -> dict[str, Any]:
        """调用模型 → 决定走工具分支还是文本收尾分支。"""
        core, st = self.core, self.core._state
        self._trace("act")
        reply = await core._act_phase(st)
        if reply is None:
            # 模型层失败：任务已在 _act_phase 内收尾（failed）
            return {"phase": "act", "route": "END", "note": "model_error"}
        self._last_reply = reply
        return {"phase": "act",
                "route": "tools" if reply.tool_calls else "finalize"}

    async def node_tools(self, state: GraphState) -> dict[str, Any]:
        """执行工具 + 按证据结算计划步骤（与 legacy 完全同一份代码）。"""
        core, st = self.core, self.core._state
        self._trace("tools")
        st.start_current_step()
        await core._execute_tools(st, self._last_reply)
        core._advance_plan_by_evidence(st)
        return {"phase": "tools", "route": "reflect"}

    async def node_reflect(self, state: GraphState) -> dict[str, Any]:
        """REFLECT 自评：可能内部收尾（done → final）→ END；否则回 budget。"""
        core, st = self.core, self.core._state
        self._trace("reflect")
        if core.config.use_reflect_phase:
            finished = await core._reflect_phase(st)
            if finished:
                return {"phase": "reflect", "route": "END", "note": "reflect_done"}
        return {"phase": "reflect", "route": "budget"}

    async def node_finalize(self, state: GraphState) -> dict[str, Any]:
        """文本答复的收尾三道关口（与 legacy 共用 _finalize_text_reply）。"""
        core, st = self.core, self.core._state
        self._trace("finalize")
        route = await core._finalize_text_reply(st, self._last_reply)
        if route != "continue":
            return {"phase": "finalize", "route": "END", "note": route}
        return {"phase": "finalize", "route": "budget", "note": "rejected"}

    # ------------------------------------------------------------------ #
    # 条件路由
    # ------------------------------------------------------------------ #
    def route_plan(self, state: GraphState) -> str:
        """plan 出口：正常回预算检查；节点判定已收尾则直接 END。"""
        return "END" if state.get("route") == "END" else "budget"

    def route_budget(self, state: GraphState) -> str:
        return state.get("route", "act")

    def route_after_plan(self, state: GraphState) -> str:
        return state.get("route", "budget")

    def route_act(self, state: GraphState) -> str:
        return state.get("route", "finalize")

    def route_reflect(self, state: GraphState) -> str:
        return state.get("route", "budget")

    def route_finalize(self, state: GraphState) -> str:
        return state.get("route", "budget")

    # ------------------------------------------------------------------ #
    def _trace(self, name: str) -> None:
        """节点轨迹：进入 TaskState（快照可见）+ 日志，供"确认经过节点和边"验收。"""
        self.node_trace.append(name)
        st = self.core._state
        if st is not None and hasattr(st, "graph_trace"):
            st.graph_trace.append(name)

    # ------------------------------------------------------------------ #
    async def run(self, state: TaskState) -> None:
        """
        用图驱动一个完整任务。

        ★ 异常语义与 legacy 一致：图内未捕获的异常向上抛，
          由 <code>AgentCore.run</code> 的顶层 try/except 统一收尾 ——
          <b>绝不静默切回 legacy 重跑</b>（工具副作用可能已发生）。
        """
        limit = max(64, state.budgets.max_turns * 8)
        await self.app.ainvoke(
            {"task_id": state.task_id, "phase": "start", "route": "", "note": "",
             "turns": 0},
            config={"recursion_limit": limit},
        )
