"""
Loom Agent v0.1 · 事件系统（★ 前后端唯一契约来源）

设计要点
--------
1. **Agent 不许 print 日志**。Agent 运行过程中一切「发生了什么」都表达为一个结构化事件。
   GUI / CLI / 未来任何消费者都只订阅事件流来还原 Agent 内部状态。

2. **事件是事实，不是渲染**。事件里不出现任何面向人类的措辞（不含「正在思考…」这种文案），
   只描述「发生了什么事、携带什么数据」。渲染文案由前端决定。

3. **envelope 统一封装**。所有事件共享同一外壳：
       {seq, ts, task_id, turn, type, data}
   - seq      : 单调递增序号，用于前端排序 / 断线重连补发 / 发现丢事件
   - ts       : 事件产生时刻（epoch 秒，float）
   - task_id  : 归属的任务。v0.1 单任务，但字段先留出来（多任务/并发时不用改协议）
   - turn     : 归属的 Agent 轮次（从 1 开始）。非轮次内事件（如 session 级）为 0
   - type     : 事件类型，取值见 EventType
   - data     : 该类型专属载荷（dict）

4. 改协议只改这个文件 + frontend/src/types/events.ts（镜像文件）。

依赖方向
--------
events.py 位于契约层，**被 server/ 与 agent/ 双方 import**，但它自己不 import 任何项目内模块。
这保证它是纯粹的「数据结构定义」，不会形成循环依赖。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, AsyncIterator, Callable, Awaitable


def _subscriber_name(sub: Any) -> str:
    """
    给日志用的订阅者名字。

    订阅者通常是 `self._on_event` 这类 bound method，直接 `%r` 只会打出
    `<bound method TaskRuntime._on_event of <...object at 0x...>>` ——
    既看不出是哪个类，也看不出是哪个实例。这里取 `__qualname__`。
    """
    return getattr(sub, "__qualname__", None) or getattr(sub, "__name__", None) or repr(sub)


# --------------------------------------------------------------------------- #
# 事件类型（16 种）
# --------------------------------------------------------------------------- #
class EventType(str, Enum):
    """
    事件类型全集。

    好处：用 Enum 而不是裸字符串 —— 拼错会立刻报错，而不是静默丢事件。
    """

    # ---- 任务生命周期 -------------------------------------------------------
    TASK_START = "task.start"          # 任务受理，Agent 开始工作
    TASK_END = "task.end"              # 任务结束（成功/失败/中止，由 data.status 区分）

    # ---- 五阶段状态机的阶段标志 --------------------------------------------
    PLAN = "plan"                      # ★ PLAN 阶段：产出/更新计划
    THINKING = "thinking"              # 模型推理片段（含思维链 delta）
    ACT = "act"                        # ★ ACT 阶段：决定要调哪个工具（工具调用前的宣告）
    OBSERVE = "observe"                # ★ OBSERVE 阶段：工具结果已到手，准备喂回模型

    # ---- 工具调用 -----------------------------------------------------------
    TOOL_CALL = "tool.call"            # 工具被真正调用（含参数）
    TOOL_RESULT = "tool.result"        # 工具返回（含结果或错误）

    # ---- 审批（C4 人机协作）-------------------------------------------------
    APPROVAL_REQUEST = "approval.request"    # 请求用户批准（Agent 在此挂起）
    APPROVAL_DECISION = "approval.decision"  # 用户已决策（approved / rejected）
    APPROVAL_EXPIRED = "approval.expired"    # 等待超时，视同拒绝

    # ---- 收敛与答复 ---------------------------------------------------------
    REFLECT = "reflect"                # ★ REFLECT 阶段：自评进展
    FINAL_ANSWER = "final_answer"      # 最终答复

    # ---- 异常与预算 ---------------------------------------------------------
    ERROR = "error"                    # 可恢复/不可恢复错误
    BUDGET_EXCEEDED = "budget.exceeded"  # 预算（轮数/时长/重复）耗尽，强制收敛

    # ---- 上下文管理 ---------------------------------------------------------
    CONTEXT_COMPACTED = "context.compacted"  # 上下文被压缩（前后规模、降级情况）

    # ---- 项目知识检索（M3）-------------------------------------------------
    # ★ 两个事件成对出现：retrieve=开始（查询/模式/索引状态），result=结束（命中/耗时/是否注入）
    #   分开的理由：检索可能很慢（首次建索引），前端要先看到"在检索"，
    #   而不是干等一个 result。injected 字段回答"这批命中到底进没进模型上下文"。
    RAG_RETRIEVE = "rag.retrieve"      # 开始检索
    RAG_RESULT = "rag.result"          # 检索结束（含无结果/失败）

    # ---- 前端可用的辅助信号 -------------------------------------------------
    STATE_SNAPSHOT = "state.snapshot"  # 全量状态快照（重连/初始化时同步用）

    # ---- 异步工作流（V1）---------------------------------------------------
    WORKFLOW_STATE = "workflow.state"  # 工作流/步骤状态快照（每个状态变更发一次）


# --------------------------------------------------------------------------- #
# 事件外壳
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    """
    统一事件外壳。

    用法：
        ev = Event.make(type=EventType.TOOL_CALL, task_id=tid, turn=2,
                        data={"tool": "read_file", "args": {"path": "a.py"}})

    注意：seq 由 EventBus 统一分配，Event.make 里为 None，序列化时若为 None 则跳过该字段
    （避免消费者看到一个假 seq）。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    ts: float = field(default_factory=time.time)
    task_id: str = ""
    turn: int = 0

    # ---- 构造 --------------------------------------------------------------
    @classmethod
    def make(
        cls,
        type: "EventType | str",
        data: dict[str, Any] | None = None,
        *,
        task_id: str = "",
        turn: int = 0,
        seq: int | None = None,
    ) -> "Event":
        return cls(
            type=type.value if isinstance(type, EventType) else str(type),
            data=data or {},
            seq=seq,
            task_id=task_id,
            turn=turn,
        )

    # ---- 序列化 ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["seq"] is None:
            d.pop("seq")
        return d

    def to_sse(self) -> str:
        """
        序列化为 SSE 帧。格式：
            event: <type>\\n
            data: <json>\\n\\n

        为什么 SSE 要单列 event 字段？
          → 浏览器 EventSource 可以 addEventListener("tool.call", ...) 精确订阅，
            不必在 JS 里自己 switch(type)。这让「事件类型」在传输层就有意义。
        """
        import json

        payload = json.dumps(self.to_dict(), ensure_ascii=False, default=str)
        return f"event: {self.type}\ndata: {payload}\n\n"


# --------------------------------------------------------------------------- #
# 事件总线
# --------------------------------------------------------------------------- #
# 订阅者签名：收到 Event，异步处理（不许阻塞 Agent 循环）
Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """
    极简异步事件总线。

    角色：Agent 循环产出事件 → EventBus 扇出给所有订阅者。
      - CLI 订阅者：把事件按标签打印到终端
      - SSE 订阅者：把事件写进 HTTP 流
      - 测试订阅者：把事件收进 list 做断言

    ★ 设计约束：**订阅者慢不能拖慢 Agent**。
      这里用 `asyncio.create_task` 异步投递，并给每个订阅者一个队列缓冲。
      老项目「dispatch() 同步调用阻塞事件循环」就是踩了这个坑，此处显式避免。

    为什么不用现成的 pub/sub 库？
      → 单进程、单任务、消费者不超过 5 个。自写 60 行比引依赖更容易看懂，
        符合「可理解性 > 代码量」。
    """

    def __init__(self) -> None:
        self._seq: int = 0
        self._subs: list[Subscriber] = []
        self._tasks: set[asyncio.Task[None]] = set()
        # 订阅者抛异常的累计次数。正常应恒为 0；
        # 非 0 说明"有事件没被某个消费者看到"，属于必须排查的状态。
        self.delivery_errors: int = 0

    # ---- 订阅管理 ----------------------------------------------------------
    def subscribe(self, sub: Subscriber) -> None:
        self._subs.append(sub)

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    @property
    def subscribers(self) -> list[Subscriber]:
        return list(self._subs)

    # ---- 发射 --------------------------------------------------------------
    def emit(self, ev: Event) -> Event:
        """
        发射事件。同步返回（分配 seq 后立刻返回），投递异步进行。

        调用方（Agent）因此永远不会被订阅者拖慢。
        """
        self._seq += 1
        ev.seq = self._seq
        if not ev.task_id:
            ev.task_id = self._default_task_id
        for sub in self._subs:
            t = asyncio.create_task(self._deliver(sub, ev))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return ev

    async def emit_and_wait(self, ev: Event) -> Event:
        """
        发射并等待所有订阅者处理完。

        什么时候需要它？—— 需要「事件已经到达消费者」才能继续的场合，例如：
          · TASK_END 之后要关流，必须确保最后一帧已写出
          · 测试里要断言事件序列
        平时一律用 emit()。
        """
        self._seq += 1
        ev.seq = self._seq
        if not ev.task_id:
            ev.task_id = self._default_task_id
        for sub in self._subs:
            await self._deliver(sub, ev)
        return ev

    async def drain(self) -> None:
        """等待所有在途的异步投递完成（关流前调用）。"""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _deliver(self, sub: Subscriber, ev: Event) -> None:
        """
        投递给单个订阅者。

        单个订阅者抛异常绝不能影响其他订阅者，也不能影响 Agent 循环 ——
        所以这里隔离异常。但**隔离 ≠ 静默**：
          · 计数到 self.delivery_errors（正常恒为 0）
          · 日志带 type / task_id / seq / 订阅者名字 / 完整堆栈
            （之前只有 `subscriber <bound method ...>` 和一个事件名，
             既不知道是哪个任务，也不知道是第几条事件）
        """
        try:
            await sub(ev)
        except Exception as exc:  # noqa: BLE001 —— 刻意宽泛：订阅者故障必须隔离
            import logging

            self.delivery_errors += 1
            logging.getLogger("loom.events").error(
                "订阅者处理事件失败：type=%s task_id=%s seq=%s subscriber=%s → %s: %s",
                ev.type,
                ev.task_id or "-",
                ev.seq,
                _subscriber_name(sub),
                type(exc).__name__,
                exc,
                exc_info=exc,
            )

    # ---- 任务 id -----------------------------------------------------------
    _default_task_id: str = ""

    def bind_task(self, task_id: str) -> None:
        """绑定默认 task_id，此后 emit 的事件自动带上它。"""
        self._default_task_id = task_id

    @staticmethod
    def new_task_id() -> str:
        """生成短 task_id（便于终端里肉眼阅读）。"""
        return f"task_{uuid.uuid4().hex[:8]}"

    # ---- 便捷发射器 --------------------------------------------------------
    # 以下方法把 EventType 的取值收拢到一处，Agent 里调用更短、更不易拼错。
    # 注意：它们都只是 emit 的薄封装，**不承载业务语义**。

    def task_start(self, goal: str, workspace: str, model: str) -> Event:
        return self.emit(
            Event.make(
                EventType.TASK_START,
                {"goal": goal, "workspace": workspace, "model": model},
            )
        )

    def task_end(self, status: str, answer: str, reason: str = "") -> Event:
        return self.emit(
            Event.make(
                EventType.TASK_END,
                {"status": status, "answer": answer, "reason": reason},
            )
        )

    def plan(self, steps: list[dict[str, Any]], note: str = "", turn: int = 0) -> Event:
        return self.emit(Event.make(EventType.PLAN, {"steps": steps, "note": note}, turn=turn))

    def thinking(self, text: str, phase: str = "", turn: int = 0, done: bool = False) -> Event:
        return self.emit(
            Event.make(EventType.THINKING, {"text": text, "phase": phase, "done": done}, turn=turn)
        )

    def act(self, intent: str, tool: str = "", turn: int = 0) -> Event:
        return self.emit(Event.make(EventType.ACT, {"intent": intent, "tool": tool}, turn=turn))

    def tool_call(self, call_id: str, tool: str, args: dict[str, Any], turn: int = 0,
                  source: dict[str, Any] | None = None) -> Event:
        """工具调用开始。source 非空时标明来源（M4.1：MCP 工具带 server/tool 元数据）。

        ★ 为什么用附加字段而不是新事件类型：调用开始/结果/审批的语义没变，
          变的只是"这个工具从哪来"。加一种事件会让时间线多一条没有信息量的记录。
        """
        payload: dict[str, Any] = {"call_id": call_id, "tool": tool, "args": args}
        if source:
            payload["source"] = source
        return self.emit(Event.make(EventType.TOOL_CALL, payload, turn=turn))

    def tool_result(
        self,
        call_id: str,
        tool: str,
        ok: bool,
        summary: str,
        data: Any = None,
        error: dict[str, Any] | None = None,
        duration_ms: int = 0,
        turn: int = 0,
    ) -> Event:
        return self.emit(
            Event.make(
                EventType.TOOL_RESULT,
                {
                    "call_id": call_id,
                    "tool": tool,
                    "ok": ok,
                    "summary": summary,
                    "data": data,
                    "error": error,
                    "duration_ms": duration_ms,
                },
                turn=turn,
            )
        )

    def observe(self, call_id: str, digest: str, turn: int = 0) -> Event:
        return self.emit(Event.make(EventType.OBSERVE, {"call_id": call_id, "digest": digest}, turn=turn))

    def reflect(self, progress: str, next_action: str, done: bool, turn: int = 0) -> Event:
        return self.emit(
            Event.make(
                EventType.REFLECT,
                {"progress": progress, "next_action": next_action, "done": done},
                turn=turn,
            )
        )

    def final_answer(self, text: str, turn: int = 0) -> Event:
        return self.emit(Event.make(EventType.FINAL_ANSWER, {"text": text}, turn=turn))

    def error(self, kind: str, message: str, fatal: bool = False, turn: int = 0) -> Event:
        return self.emit(
            Event.make(EventType.ERROR, {"kind": kind, "message": message, "fatal": fatal}, turn=turn)
        )

    def context_compacted(self, payload: dict[str, Any], turn: int = 0) -> Event:
        """上下文压缩事件（前后规模、保留轮数、降级情况）。"""
        return self.emit(
            Event.make(EventType.CONTEXT_COMPACTED, payload, turn=turn)
        )

    def rag_retrieve(self, payload: dict[str, Any], turn: int = 0) -> Event:
        """检索开始（查询、模式 auto|tool、索引统计）。"""
        return self.emit(Event.make(EventType.RAG_RETRIEVE, payload, turn=turn))

    def rag_result(self, payload: dict[str, Any], turn: int = 0) -> Event:
        """检索结束（命中数、路径、耗时、是否注入、无结果原因）。"""
        return self.emit(Event.make(EventType.RAG_RESULT, payload, turn=turn))

    def workflow_state(self, payload: dict[str, Any], turn: int = 0) -> Event:
        """工作流状态快照（包含 event 名、workflow 状态、每个 step 的状态）。"""
        return self.emit(Event.make(EventType.WORKFLOW_STATE, payload, turn=turn))

    def budget_exceeded(self, kind: str, limit: Any, used: Any, turn: int = 0) -> Event:
        return self.emit(
            Event.make(
                EventType.BUDGET_EXCEEDED,
                {"kind": kind, "limit": limit, "used": used},
                turn=turn,
            )
        )


# --------------------------------------------------------------------------- #
# 便捷工具：收集事件（测试 / 调试用）
# --------------------------------------------------------------------------- #
def collect(bus: EventBus) -> list[Event]:
    """
    把一个 list 挂成订阅者并返回它。用于测试里断言事件序列：

        events = collect(bus)
        await agent.run("读一下 main.py")
        assert any(e.type == EventType.TOOL_CALL for e in events)
    """
    bucket: list[Event] = []

    async def _sub(ev: Event) -> None:
        bucket.append(ev)

    bus.subscribe(_sub)
    return bucket


async def iterate(bus: EventBus) -> AsyncIterator[Event]:
    """
    把事件流转成 async 迭代器（基于队列），便于 `async for ev in iterate(bus)`。
    队列无界 —— 消费端必须及时读取，否则内存会涨（v0.1 事件量小，可接受）。
    """
    q: asyncio.Queue[Event] = asyncio.Queue()

    async def _sub(ev: Event) -> None:
        await q.put(ev)

    bus.subscribe(_sub)
    while True:
        yield await q.get()
