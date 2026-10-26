"""
消息历史持久化（M1-B）

★ 解决什么问题
----------------
第六批之前，模型对话历史 <code>AgentCore._history</code> 只存在于进程内存：
任务状态和事件都落了 SQLite，但"模型看到了什么"没有 —— 进程重启后
任务能查、不能续。这个模块把历史变成**任务关联的持久化记录**。

★ 本模块的定位（M1 边界，必须读）
----------------
1. 它是一条 **append-only 的过程记录**：每次追加按 seq 递增，重复写同一 seq
   会覆盖（幂等），压缩发生时追加一条 marker。
   它**不是**内存历史的实时镜像 —— 压缩后内存历史会变短，而日志不会删旧条目。
2. 持久化历史 ≠ 安全续跑。恢复安全边界见
   <code>AgentCore.restore_history</code> 的 RestoreReport：
   只要存在"工具可能已执行但结果不确定"的消息缺口，<b>safe_to_resume 就是 False</b>。
3. 失败不静默：追加失败会计入 <code>state.history_persist_errors</code> 并发
   <code>history_persist_failed</code> 事件 —— 绝不假装保存成功。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import json

from ..models.adapter import Message, ToolCall

log = logging.getLogger("loom.history")


# --------------------------------------------------------------------------- #
# Message ↔ 持久化条目
# --------------------------------------------------------------------------- #
def messages_to_entries(messages: list[Message], *, start_seq: int) -> list[dict[str, Any]]:
    """把消息列表转成持久化条目（seq 从 start_seq 起连续递增）。"""
    out: list[dict[str, Any]] = []
    seq = start_seq
    for m in messages:
        tc_json = ""
        if m.tool_calls:
            tc_json = json.dumps(
                [{"id": tc.id, "name": tc.name, "arguments_raw": tc.arguments_raw}
                 for tc in m.tool_calls],
                ensure_ascii=False,
            )
        out.append({
            "seq": seq,
            "role": m.role,
            "content": m.content or "",
            "reasoning": m.reasoning or "",
            "tool_call_id": m.tool_call_id or "",
            "tool_name": m.tool_name or "",
            "tool_calls_json": tc_json,
        })
        seq += 1
    return out


def entries_to_messages(entries: list[dict[str, Any]]) -> list[Message]:
    """
    把持久化条目还原成消息（字段缺失时保守置空，不猜）。

    ★ tool_calls 必须还原：恢复安全检查（find_tool_call_gaps）依赖它 ——
      丢掉 tool_calls 就等于把"工具可能已执行"的证据抹掉了。
    """
    out: list[Message] = []
    for e in entries:
        tcs: list[ToolCall] = []
        raw = e.get("tool_calls_json") or ""
        if raw:
            try:
                for tc in json.loads(raw):
                    tcs.append(ToolCall(
                        tc.get("id") or "",
                        tc.get("name") or "",
                        tc.get("arguments_raw") or "{}",
                        None,
                    ))
            except (ValueError, TypeError):
                log.warning("tool_calls_json 解析失败，按无调用处理")
        out.append(Message(
            role=str(e.get("role") or "assistant"),
            content=str(e.get("content") or ""),
            reasoning=str(e.get("reasoning") or ""),
            tool_calls=tcs or None,
            tool_call_id=str(e.get("tool_call_id") or "") or None,
            tool_name=str(e.get("tool_name") or "") or None,
        ))
    return out


def find_tool_call_gaps(messages: list[Message]) -> list[str]:
    """
    找出「assistant 声明了 tool_calls，但后面没有对应数量的 tool 结果」的缺口。

    ★ 为什么这是恢复安全性的核心检查：
      消息缺口意味着"工具可能已经执行、结果却没来得及保存" ——
      这种情况下重新执行可能造成重复副作用，跳过执行又会让模型基于不完整
      观察继续决策。M1 的立场：**存在缺口就禁止自动续跑**。
    """
    gaps: list[str] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            expected = len(m.tool_calls)
            got = 0
            j = i + 1
            while j < len(messages) and messages[j].role == "tool":
                got += 1
                j += 1
            if got < expected:
                names = ",".join(tc.name for tc in m.tool_calls)
                gaps.append(f"第 {i + 1} 条消息（{names}）缺少 {expected - got} 条工具结果")
            i = j
        else:
            i += 1
    return gaps


# --------------------------------------------------------------------------- #
# Sink 协议与实现
# --------------------------------------------------------------------------- #
@runtime_checkable
class HistorySink(Protocol):
    """历史落盘的抽象。core 不感知存储细节（SQLite / 测试假件 / 未来的 checkpointer）。"""

    async def append(self, task_id: str, entries: list[dict[str, Any]]) -> None: ...
    async def load(self, task_id: str) -> list[dict[str, Any]]: ...


class StoreHistorySink:
    """基于 <code>server/store.py</code> 的实现（task_history 表）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def append(self, task_id: str, entries: list[dict[str, Any]]) -> None:
        await self._store.append_history(task_id, entries)

    async def load(self, task_id: str) -> list[dict[str, Any]]:
        return await self._store.load_history(task_id)


class MemoryHistorySink:
    """测试/诊断用：存内存，接口一致。"""

    def __init__(self) -> None:
        self.by_task: dict[str, list[dict[str, Any]]] = {}
        self.fail_next: int = 0

    async def append(self, task_id: str, entries: list[dict[str, Any]]) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("模拟历史落盘失败")
        self.by_task.setdefault(task_id, []).extend(entries)

    async def load(self, task_id: str) -> list[dict[str, Any]]:
        return list(self.by_task.get(task_id, []))


def dump_entries(entries: list[dict[str, Any]], *, head: int = 3) -> str:
    """诊断用：只打条目摘要，**绝不输出完整正文**（历史里可能有敏感内容）。"""
    parts = []
    for e in entries[:head]:
        parts.append(f"#{e.get('seq')}:{e.get('role')}({len(str(e.get('content') or ''))}字)")
    more = f" 等 {len(entries)} 条" if len(entries) > head else ""
    return "[" + ", ".join(parts) + more + "]"
