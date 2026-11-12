"""
事件流落盘（对比表 #49）

★ 为什么要有这一层，而不是在 `_on_event` 里直接 `await store.append_events(...)`
────────────────────────────────────────────────────────────────
  `EventBus.emit()` 是**同步**的，它的调用点在 Agent 的每一步里；事件到达 hub 之后
  如果串一次数据库写（哪怕 0.6ms/条），thinking 的 delta 流会把事件循环压成锯齿，
  而且 DB 一抖动就会反过来拖慢推理路径 —— 这是纯粹为可观测性付的代价，不该由主流程承担。

  所以这里做三件事：
    ① `enqueue()` 是**同步 O(1)**：塞进 deque 就返回，绝不 await、绝不碰磁盘
    ② 后台 task 每 `flush_interval` 秒把队列整批写出（一个事务 N 条）
    ③ 队列满了丢新事件并计数（`queued_dropped`）—— 宁可少存几条历史，
       **也不许把进程内存吃爆**；丢了会出现在 /api/health 里，不静默

★ seq 缓存（`seq_floor`）
────────────────────────────────────────────────────────────────
  续跑会新建一条 EventBus，seq 从 1 重来；而 task_events 的主键是 (task_id, seq)。
  不接上的话，第二次运行的第 1 条会把第一次运行的第 1 条覆盖掉。
  所以启动时把 `SELECT task_id, MAX(seq) GROUP BY task_id` 读进内存，
  hub 每发一条问一次（纯字典查），新事件就接着涨。

★ 为什么不在 stop() 之外保证不丢
────────────────────────────────────────────────────────────────
  正常退出（uvicorn shutdown）会 flush；`kill -9` 会丢掉最后不到 1 秒的事件。
  这是**明确的取舍**：实时性优先，历史允许"最后几秒缺失"。
  检查点（#50）负责的是"任务能不能续跑"，两者不是一回事。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from .store import TaskStore

log = logging.getLogger("loom.archive")

# 一批最多写多少条（一个事务）
BATCH_SIZE = 64
# 后台刷盘间隔（秒）。0.25 ≈ 最多晚 250ms 可见，对"重启后回看"完全够用
FLUSH_INTERVAL = 0.25
# 队列上限（条）。正常永远到不了；到了说明磁盘写不动了，丢新保命
MAX_QUEUE = 20_000


class EventArchive:
    """事件落盘的写侧 + 读侧的 seq 缓存。单进程单实例。"""

    def __init__(
        self,
        store: TaskStore,
        *,
        enabled: bool = True,
        batch_size: int = BATCH_SIZE,
        flush_interval: float = FLUSH_INTERVAL,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        self._store = store
        self.enabled = enabled
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        self.max_queue = max(1, int(max_queue))

        self._queue: deque[dict[str, Any]] = deque()
        self._max_seq: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._closed = False

        # 统计（/api/health 可见；失败的写不能静默）
        self.written = 0
        self.batches = 0
        self.failed = 0
        self.queued_dropped = 0
        self.last_error = ""

    # ===================================================================== #
    # 生命周期
    # ===================================================================== #
    def load_seq_cache_sync(self) -> int:
        """把每个任务已落盘的最大 seq 读进内存。返回任务条数（启动横幅用）。"""
        self._max_seq = self._store.max_event_seq_sync()
        return len(self._max_seq)

    async def start(self) -> None:
        """启动后台刷盘 task，并预热 seq 缓存。

        ★ 缓存必须在**任何任务开跑之前**载入（见模块头说明）。
        """
        if not self.enabled or self._closed:
            return
        await asyncio.to_thread(self.load_seq_cache_sync)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="loom-event-archive")

    async def stop(self) -> None:
        """停掉后台 task 并把队列里剩下的写完（正常退出路径）。"""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    # ===================================================================== #
    # 写侧（同步！这是整层存在的理由）
    # ===================================================================== #
    def enqueue(self, task_id: str, event_dict: dict[str, Any], gid: int) -> None:
        """把一条事件排进队列。**同步、O(1)、不 await、不碰磁盘。**"""
        if not self.enabled:
            return
        if len(self._queue) >= self.max_queue:
            self.queued_dropped += 1
            if not self.last_error:
                self.last_error = f"归档队列已满（>{self.max_queue}），开始丢事件"
            return

        self._queue.append(
            {
                "task_id": task_id,
                "id": int(gid),
                "seq": int(event_dict.get("seq") or 0),
                "ts": float(event_dict.get("ts") or 0.0),
                "turn": int(event_dict.get("turn") or 0),
                "type": str(event_dict.get("type") or ""),
                "data": event_dict.get("data") or {},
            }
        )
        self._ensure_task()

    def _ensure_task(self) -> None:
        """没有后台 task 就补一个。

        为什么放在 enqueue 里而不是只靠 start()：自检/CLI 里常常没有走 FastAPI 的
        lifespan，事件照样要能落盘。重复判断是幂等的。
        """
        if self._closed or not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 纯同步环境（比如直接调 sync 方法）：等 flush() 手动刷
        self._task = asyncio.create_task(self._run(), name="loom-event-archive")

    async def flush(self) -> int:
        """把队列里的事件按批写出去。返回本次写成功的条数。"""
        total = 0
        while self._queue:
            batch: list[dict[str, Any]] = []
            while self._queue and len(batch) < self.batch_size:
                batch.append(self._queue.popleft())
            if not batch:
                return total
            try:
                await self._store.append_events(batch)
            except Exception as exc:  # noqa: BLE001 —— 落盘失败不能拖垮事件流
                self.failed += len(batch)
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("事件落盘失败（%d 条已丢弃）：%s", len(batch), exc)
                continue
            self.written += len(batch)
            self.batches += 1
            total += len(batch)
            for row in batch:
                tid = row["task_id"]
                if row["seq"] > self._max_seq.get(tid, 0):
                    self._max_seq[tid] = row["seq"]
        return total

    # ===================================================================== #
    # seq 缓存（hub 每发一条会问一次）
    # ===================================================================== #
    def seq_floor(self, task_id: str) -> int:
        """该任务已经落盘的最大 seq；没有就 0。纯内存查。"""
        return self._max_seq.get(task_id, 0)

    # ===================================================================== #
    # 观测
    # ===================================================================== #
    @property
    def pending(self) -> int:
        return len(self._queue)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "written": self.written,
            "batches": self.batches,
            "pending": self.pending,
            "failed": self.failed,
            "queued_dropped": self.queued_dropped,
            "last_error": self.last_error,
            "flush_interval_s": self.flush_interval,
            "batch_size": self.batch_size,
        }
