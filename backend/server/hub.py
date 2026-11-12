"""
进程级事件广播

★ 为什么需要它，而不是直接把 EventBus 接到 SSE？
────────────────────────────────────────────────────────────────
  ① **seq 会撞车**。`EventBus.seq` 是**每个实例**从 1 数的。两个任务同时跑，
     它们的 seq 都是 1,2,3…，直接当 SSE 的 `id:` 会让客户端补发出错。
     → 这里维护一个**全局递增 id**：

           SSE id:  1, 2, 3, ...        （全局，用于 Last-Event-ID 补发）
           event.seq: 任务内序号         （原样保留，用于前端排序去重）

  ② **需要缓冲**。客户端断线重连时得能补发。EventBus 不存历史。
     ★ 缓冲只是"最近 3000 条"的热数据。冷数据由 `EventArchive` 落盘
       （`task_events` 表，对比表 #49）。hub 自己**不认识 SQLite**，
       只把每条事件同步塞进归档队列（见 `publish()` 末尾）——
       于是"实时广播"和"落盘"彻底解耦：落盘慢/挂掉都不影响 SSE。

  ③ **需要广播**。多个客户端（刷新页面会短暂有两条连接）要各自收到。

★ 三重容量限制（缺一不可）
────────────────────────────────────────────────────────────────
    MAX_EVENTS      条数上限        —— 防事件数爆炸
    MAX_EVENT_BYTES 单条上限        —— 防一条 tool.result 塞 10MB
    MAX_TOTAL_BYTES 总字节上限      —— 防 3000 条大事件吃光内存

★ 截断只影响「缓冲副本」
────────────────────────────────────────────────────────────────
  在线客户端收到的永远是**原始事件**（完整 data）。
  只有重连补发时，超大的 data 会被替换成占位符并标记 `_truncated: true`。
  这不是"偷偷改数据" —— 前端会看到标记并显示「补发内容已截断」。

★ 淘汰要**明确告知**
────────────────────────────────────────────────────────────────
  客户端要的历史已被淘汰时，补发前先推一条 `stream.gap`，前端据此走快照恢复。
  **绝不静默少给一段。**
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from ..events import Event

# ---- 容量上限 ----
MAX_EVENTS = 3000
MAX_EVENT_BYTES = 64_000                     # 单条 64 KB
MAX_TOTAL_BYTES = 32 * 1024 * 1024           # 32 MB

# ---- 每个订阅者的队列长度（满了说明消费者卡死，丢事件并计数）----
SUB_QUEUE_SIZE = 2000

PREVIEW_CHARS = 400


class Subscriber:
    """
    一个 SSE 消费者。

    ★ 慢客户端策略
    ────────────────────────────────────────────────────────────
    队列满了怎么办？**丢最旧的，保留最新的** —— 因为对"观察 Agent 执行过程"
    这个场景来说，最新的状态比丢失的中间步骤更重要（而且丢了中间步骤还能
    靠 gap 信号 + 快照恢复）。

    同时置 `need_gap=True`：生成器下一次循环会先推一条 `stream.gap`，
    明确告诉客户端"你中间丢了内容"。**绝不静默**。

    ★ 一个客户端慢不能拖累别人：每个订阅者独立队列，
      `put_nowait` 永不阻塞发布方（也就是永远不会阻塞 Agent）。
    """

    __slots__ = ("queue", "need_gap", "dropped")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.need_gap = False
        self.dropped = 0


class EventHub:
    """所有任务的全局事件流。单进程、单实例。"""

    def __init__(
        self,
        *,
        max_events: int = MAX_EVENTS,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        sub_queue_size: int = SUB_QUEUE_SIZE,
        archive: Any = None,
        start_id: int = 0,
        seq_floor: Any = None,
    ) -> None:
        self.max_events = max_events
        self.max_event_bytes = max_event_bytes
        self.max_total_bytes = max_total_bytes
        self.sub_queue_size = sub_queue_size

        # 落盘（#49）：只需 `enqueue(task_id, event_dict, gid)` 和 `seq_floor(task_id)`
        # 两个方法，鸭子类型 —— hub 不认识 SQLite，也不 import store。
        self._archive = archive
        self._seq_floor = seq_floor

        self._gid = int(start_id or 0)
        self._buf: deque[dict[str, Any]] = deque()
        self._bytes = 0
        self._subs: set[Subscriber] = set()
        # 每个任务"最后一次发出"的 seq —— 见 publish 里的归一化说明
        self._last_seq: dict[str, int] = {}

        # 统计（便于测试和 /api/health 观察）
        self.evicted = 0        # 被容量上限挤掉的事件数
        self.dropped = 0        # 因订阅者队列满而丢弃的投递数
        self.seq_normalized = 0  # 被"抬"过的 seq 条数（续跑时才会 > 0）

    # ===================================================================== #
    # 发布
    # ===================================================================== #
    async def publish(self, task_id: str, session_id: str, ev: Event) -> dict[str, Any]:
        """
        收一条事件：分配全局 id → 存缓冲副本 → 广播原始事件。

        返回刚生成的记录（便于测试）。

        ★ 任务内 seq 的**归一化**（2026-11-11，事件落盘时加的）
        ────────────────────────────────────────────────────────────
        前端用 seq 排序去重，`task_events` 的主键也是 (task_id, seq)，
        所以"同一任务内 seq 必须严格递增"是硬约束。而 EventBus 的 seq 是
        每个实例从 1 数的 —— **续跑**会新建一个 bus，seq 又从 1 开始，
        不处理的话新事件要么覆盖旧事件，要么被前端当重复丢掉。

        处理方式：取 `max(本进程见过的上一个 seq, 库里已落盘的最大 seq)` 作地板，
        比地板小的 seq 一律抬成 `地板+1`。正常运行时 `raw > floor` 恒成立，
        这一步是纯 no-op；只有续跑/无 seq 的事件才会被抬。

        没有 seq 的事件（运行时兜底的 error 事件就是）同样走这条路 ——
        以前它靠"前端排序时 NaN 不参与"混过去，落盘之后必须给个真 seq。
        """
        self._gid += 1
        gid = self._gid

        event_dict = ev.to_dict()

        floor = self._last_seq.get(task_id, 0)
        if self._seq_floor is not None:
            try:
                floor = max(floor, int(self._seq_floor(task_id) or 0))
            except Exception:  # noqa: BLE001 —— 缓存坏了不该影响事件流
                pass
        raw_seq = event_dict.get("seq")
        if isinstance(raw_seq, int) and raw_seq > floor:
            seq = raw_seq
        else:
            seq = floor + 1
            if raw_seq is not None:
                self.seq_normalized += 1
        event_dict["seq"] = seq
        self._last_seq[task_id] = seq

        # ---- 缓冲副本（可能截断超大 data）----
        buffered = self._make_buffered_copy(event_dict)
        size = len(json.dumps(buffered, ensure_ascii=False, default=str))

        rec = {
            "id": gid,
            "task_id": task_id,
            "session_id": session_id,
            "size": size,
            "event": buffered,
        }

        self._buf.append(rec)
        self._bytes += size
        self._trim()

        # ---- 广播原始事件（完整 data）----
        payload = {
            "id": gid,
            "task_id": task_id,
            "session_id": session_id,
            "event": event_dict,
        }
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 丢最旧，保留最新；并标记"需要补 gap 通知"
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
                sub.need_gap = True
                sub.dropped += 1
                self.dropped += 1

        # ---- 落盘：同步入队，一微秒都不等（见 archive.py 顶部说明）----
        if self._archive is not None:
            try:
                self._archive.enqueue(task_id, event_dict, gid)
            except Exception:  # noqa: BLE001 —— 落盘侧任何问题都不许影响实时事件
                pass

        return rec

    def _make_buffered_copy(self, event_dict: dict[str, Any]) -> dict[str, Any]:
        """
        超大事件 → 把 data 换成占位符。

        ★ 只改**缓冲里这份**，广播出去的那份不动。

        为什么用占位符而不是"截断字符串"？因为截断后的 JSON 结构可能不合法，
        前端解析会炸。换成固定的占位对象，前端永远能安全解析，且能看出被截断。
        """
        raw = json.dumps(event_dict, ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) <= self.max_event_bytes:
            return event_dict

        preview = raw[:PREVIEW_CHARS]
        trimmed = dict(event_dict)
        trimmed["data"] = {
            "_truncated": True,
            "_original_bytes": len(raw.encode("utf-8")),
            "_preview": preview,
            "_note": "补发副本已截断（超过单条上限）；在线收到的原始事件不受影响。",
        }
        return trimmed

    def _trim(self) -> None:
        """按条数和总字节双重淘汰，从最旧的开始。"""
        while self._buf and (
            len(self._buf) > self.max_events or self._bytes > self.max_total_bytes
        ):
            old = self._buf.popleft()
            self._bytes -= old["size"]
            self.evicted += 1

    # ===================================================================== #
    # 订阅（SSE 消费者）
    # ===================================================================== #
    def subscribe(self) -> Subscriber:
        sub = Subscriber(self.sub_queue_size)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # ===================================================================== #
    # 补发
    # ===================================================================== #
    @property
    def latest_id(self) -> int:
        return self._gid

    def seed_gid(self, value: int) -> int:
        """
        用"库里最大的事件 id"播种全局游标。

        ★ 不这么做的话，重启后 gid 又从 1 开始：客户端拿着上次的 `since=42`
          来重连，新事件 id 只有 1、2、3 —— 要么补不出东西，要么补错。
          落盘之后游标必须跟着库走，否则 `?since=` 的语义就断了。
        """
        if value and int(value) > self._gid:
            self._gid = int(value)
        return self._gid

    @property
    def oldest_id(self) -> int:
        return self._buf[0]["id"] if self._buf else self._gid + 1

    def replay(self, since: int | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """
        计算要补发的事件。

        返回 `(要补发的记录列表, gap 信号或 None)`。

        since=None      → 全新连接，**不补发历史**（让客户端自己调快照接口）
        since=0         → 从头补发（能做到几条给几条）
        since 已淘汰    → 返回空列表 + gap 信号，客户端必须走快照

        ★ 第三种情况是这里最关键的：宁可明确告诉"我给不了"，
          也不能从 oldest 开始给 —— 那会让客户端以为自己拿到的是完整的。
        """
        if since is None:
            return [], None

        # 已淘汰：请求的 id 比缓冲最旧的还小
        if since < self.oldest_id - 1:
            return [], {
                "type": "stream.gap",
                "data": {
                    "requested_since": since,
                    "oldest_available": self.oldest_id,
                    "latest": self._gid,
                    "evicted": self.evicted,
                    "reason": "evicted",
                    "note": "请求的事件已被缓冲淘汰；请调用 GET /api/tasks/{id} 拉快照恢复状态。",
                },
            }

        out = [r for r in self._buf if r["id"] > since]
        return out, None

    # ===================================================================== #
    # 按任务查询
    # ===================================================================== #
    def buffered_for_task(self, task_id: str) -> list[dict[str, Any]]:
        """
        目标任务的缓冲记录（时间正序）。

        O(n) 遍历 —— 缓冲上限 3000，快照调用也不频繁，够用。
        真要优化可以再维护 task → index 的映射，但现在没必要。
        """
        return [r for r in self._buf if r["task_id"] == task_id]

    def has_events_for(self, task_id: str) -> bool:
        return any(r["task_id"] == task_id for r in self._buf)

    # ===================================================================== #
    # 统计
    # ===================================================================== #
    def stats(self) -> dict[str, Any]:
        return {
            "latest_id": self._gid,
            "oldest_id": self.oldest_id,
            "buffered": len(self._buf),
            "bytes": self._bytes,
            "max_events": self.max_events,
            "max_event_bytes": self.max_event_bytes,
            "max_total_bytes": self.max_total_bytes,
            "evicted": self.evicted,
            "dropped": self.dropped,
            "subscribers": self.subscriber_count,
            # >0 说明发生过 seq 归一化（续跑接 seq / 无 seq 事件），不是故障
            "seq_normalized": self.seq_normalized,
        }
