"""
SQLite 持久化层

★ 存什么、不存什么
────────────────────────────────────────────────────────────────
  存：Session、Task（含完整 TaskState 快照）、幂等键、消息历史、**事件流**（#49）
  不存：Message（从 Task 派生：goal → user，result → assistant，存两份必不同步）

★ 事件流为什么也要存（2026-11-11，对比表 #49）
────────────────────────────────────────────────────────────────
  以前事件只活在 `EventHub` 的内存环形缓冲里，重启即丢 —— 界面上表现为
  "任务的事件历史不可用（empty）"，执行流时间线是空的，只剩一句结论。
  现在落进 `task_events` 表：
    · 写入不进 emit 热路径（`EventArchive` 收进队列，后台批量写，见 archive.py）
    · 读的时候内存优先、磁盘兜底，两侧按 seq 合并去重
    · 保留策略见 `prune_events_sync`（默认 30 天，0 = 永不清理）

★ 并发与线程安全（这是选 SQLite 而不是 JSON 的核心原因）
────────────────────────────────────────────────────────────────
  多个 Task（默认上限 2）可能**同时结束**。JSON 方案里「读-改-写」会丢更新，
  必须自己加全局锁 + 原子写。SQLite 的事务直接解决。

  具体做法：
    · **进程内复用同一个连接**（懒创建，`check_same_thread=False`），
      所有访问经 `_DB_LOCK` 串行化 —— 同一时刻只有一个线程在用它
    · 同步 API 用 `asyncio.to_thread()` 包一层，不阻塞事件循环
    · 一次操作 = 一个事务（`with conn:` 自动 commit / rollback），**绝不跨操作**
    · WAL 模式：读写不互斥；`synchronous=NORMAL` 在 WAL 下足够安全且更快

★ 为什么不再"每次操作新建连接"（2026-10-25 修复）
────────────────────────────────────────────────────────────────
  WAL 的 `-wal` / `-shm` 由**最后一个关闭的连接**负责 checkpoint 并删除。
  「每次操作一个短连接」意味着每次写都在做「创建 → 拆除」这套动作；
  多线程并发下，A 正在拆 `-shm`、B 同时打开同一文件，B 会拿不到 wal-index
  而被 SQLite 降级为只读，抛 `SQLITE_READONLY: attempt to write a readonly
  database`。实测（8 线程并发写）：短连接 5/100 失败，单线程 0/100；
  改成复用连接后 1200 次并发写 0 失败，且单次写从 54ms 降到 0.6ms
  （短连接每次关闭都要 checkpoint，这才是那 54ms 的来源）。

  ⚠️ 注意：WAL 本身没问题，**问题在连接生命周期**。所以这里保留 WAL，
     改的是「不再反复开关连接」。

★ created_at 与 started_at 分开
────────────────────────────────────────────────────────────────
  排队中的任务 `started_at = NULL` —— 只有真正拿到执行槽位才填。
  这样「创建了但还没开始」和「开始后跑了多久」是两个可区分的概念，
  不会出现"排了 3 分钟，看起来任务跑了 3 分钟"的错觉。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 幂等键保留多久 / 最多几条（超出按 created_at 从旧到新删）
IDEM_TTL_S = 24 * 3600
IDEM_MAX_ROWS = 1000

# M5：tasks 增加 parent_task_id（子任务指向父任务；根任务为 NULL）
# v3：新增 task_events（事件流落盘，#49）
# v4：新增 task_projections（会话投影，#51）——从事件流派生的可重建查询表
# v5：投影表补 token 用量列（对比表 #4）——来自 llm.usage 事件
# v6：投影表补墙钟分解列（对比表 #52）——llm_ms / tool_ms / first_token_ms
SCHEMA_VERSION = 6

# 导出文件格式标识（改结构就换它，导入端据此拒绝旧文件）
EXPORT_FORMAT = "loom.task.v1"

# --------------------------------------------------------------------------- #
# 进程级数据库锁
# --------------------------------------------------------------------------- #
# 为什么是模块级而不是每个 TaskStore 一把？
#   测试 / 自检里会同时存在多个指向同一个文件的 TaskStore 实例
#   （比如 `restart = db.store()`）。锁必须覆盖**文件**，不是实例。
#
# 为什么用 RLock？
#   同步方法之间理论上可能嵌套调用（当前没有），RLock 让「重入」不会自锁死。
#
# 代价：所有读写被串行化。本应用的写频率是「每任务 3~5 次」，
#       单次 0.6ms，串行化完全不是瓶颈；换来的是不再有连接生命周期竞争。
_DB_LOCK = threading.RLock()

# --------------------------------------------------------------------------- #
# 事件流建表语句（v3）
# --------------------------------------------------------------------------- #
# ★ 为什么单独抽成常量：初始化、迁移、以及"老库补表"三条路径都用同一段 SQL。
#   抄三份的下场是改一处漏两处 —— task_history 当年就是这么丢的。
#
# ★ 主键为什么是 (task_id, seq) 而不是 (task_id, id)：
#   前端按 seq 排序去重（`mergeEvents`），seq 才是"事件在任务内的位置"这一语义。
#   id 是全局 SSE 游标（重连补发用），只存不约束。
#   ⚠️ 由此推出一条硬约束：**续跑时 seq 必须接着涨**，否则新事件会覆盖旧事件。
#      这件事由 EventHub 统一归一化（见 hub.publish），不指望调用方自觉。
EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS task_events (
  id        INTEGER NOT NULL,              -- 全局 SSE id（跨重启单调，启动时由 MAX(id) 播种）
  task_id   TEXT    NOT NULL,
  seq       INTEGER NOT NULL,              -- 任务内序号（前端排序/去重靠它）
  ts        REAL    NOT NULL,              -- 事件发生时间
  turn      INTEGER NOT NULL DEFAULT 0,
  type      TEXT    NOT NULL,              -- 事件类型（thinking / tool.call / …）
  data_json TEXT    NOT NULL DEFAULT '{}', -- 载荷 JSON
  PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_task_events_id ON task_events(id);
"""

# --------------------------------------------------------------------------- #
# 会话投影建表语句（v4，#51）
# --------------------------------------------------------------------------- #
# ★ 投影 ≠ 事实档案（#49 的 task_events 才是事实）：
#   投影是**从事实派生**的查询表，删掉它随时能从 task_events 全量重放重建，
#   重建后逐行一致 —— 这是验收标准，也是"投影"和"快照"的本质区别。
#
# ★ 为什么要投影：
#   列表页/统计要频繁按 status、tool_call 数、是否有 final_answer 等条件筛任务。
#   每次都从 task_events 全量重放推导太重；投影表把这些派生字段物化下来，
#   查询走索引，O(1) 拿到。代价是多一张表 + 一个维护点，换来的是查询快。
#
# ★ 派生字段全部可从事件流推导，不存任何"只有当时才知道"的信息：
#   - status / end_reason / result：task.end 事件（data.status / data.reason / data.answer）
#   - tool_call_count / tool_result_count：tool.call / tool.result 事件计数
#   - has_final_answer / final_answer_len：final_answer 事件
#   - has_error / error_count：error 事件
#   - turn_max / event_count：事件外壳的 turn / 总数
#   - first_ts / last_ts：事件时间跨度
#   - budget_exceeded：budget.exceeded 是否出现过
#
# ★ rebuilt_at 是元数据（记录上次重建时间），**不参与一致性验收**：
#   两次重建的 rebuilt_at 必然不同，若把它算进"逐行一致"就永远过不了。
PROJECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS task_projections (
  task_id            TEXT PRIMARY KEY,
  status             TEXT NOT NULL DEFAULT '',
  end_reason         TEXT NOT NULL DEFAULT '',
  result             TEXT NOT NULL DEFAULT '',
  tool_call_count    INTEGER NOT NULL DEFAULT 0,
  tool_result_count  INTEGER NOT NULL DEFAULT 0,
  has_final_answer   INTEGER NOT NULL DEFAULT 0,
  final_answer_len   INTEGER NOT NULL DEFAULT 0,
  has_error          INTEGER NOT NULL DEFAULT 0,
  error_count        INTEGER NOT NULL DEFAULT 0,
  budget_exceeded    INTEGER NOT NULL DEFAULT 0,
  turn_max           INTEGER NOT NULL DEFAULT 0,
  event_count        INTEGER NOT NULL DEFAULT 0,
  -- v5（对比表 #4）：token 用量。来自 llm.usage 事件，provider 返回的精确值。
  -- ★ 不存"估算值"：估算和精确混在一列里，就再也分不清哪个能信了。
  prompt_tokens      INTEGER NOT NULL DEFAULT 0,
  completion_tokens  INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
  llm_calls          INTEGER NOT NULL DEFAULT 0,
  -- v6（对比表 #52）：墙钟时间分解。任务"慢"到底慢在等模型还是跑工具，
  -- 只能靠这两列回答；单看任务总时长会把两种完全不同的瓶颈混成一个数。
  llm_ms             INTEGER NOT NULL DEFAULT 0,
  tool_ms            INTEGER NOT NULL DEFAULT 0,
  first_token_ms     INTEGER NOT NULL DEFAULT 0,
  first_ts           REAL NOT NULL DEFAULT 0,
  last_ts            REAL NOT NULL DEFAULT 0,
  rebuilt_at         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_projections_status
  ON task_projections(status);
"""



class StoreError(RuntimeError):
    """持久化层错误（schema 不匹配、写失败等）。"""


class TaskStore:
    """
    所有方法都是 async —— 内部用 `asyncio.to_thread` 调同步实现。

    为什么不用 aiosqlite？
      它是个新依赖，而 `sqlite3` 内置。这里写入频率很低（每任务 3~5 次），
      to_thread 的开销完全可接受，没必要为它引包。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 懒创建：第一次真正用到时才 connect（init 里会用到）
        self._conn: sqlite3.Connection | None = None

    # ===================================================================== #
    # 连接与事务
    # ===================================================================== #
    def _connect_locked(self) -> sqlite3.Connection:
        """
        取（必要时创建）本实例的连接。**调用方必须已持有 `_DB_LOCK`**。

        `check_same_thread=False` 是必需的：`asyncio.to_thread` 会把同步实现
        派到线程池里，不同操作可能落在不同线程上。
        这么做的安全性来自两处：
          ① 每次使用都被 `_DB_LOCK` 串行化，不存在并发访问同一个连接；
          ② CPython 自带的 SQLite 是 serialized 模式（threadsafety=3），
             即使真并发也只是串行执行，不会破坏内存。
        """
        if self._conn is None:
            conn = sqlite3.connect(
                self.db_path, timeout=5.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """
        一个短事务。成功 commit，异常 rollback。

        ★ `with conn:` 在 sqlite3 里是**事务上下文**（不是普通上下文管理器），
          进入时隐式 BEGIN，正常退出 commit，抛异常 rollback。
          连接本身**不关闭** —— 复用连接，避免 WAL 反复拆除（见模块头说明）。

        ★ 为什么这里不设 `PRAGMA journal_mode=WAL`？
          WAL 是**数据库级持久设置**，写一次就存在文件头里。只在 `init_sync()`
          里设一次即可；每次连接都设一遍会在并发连接时争抢 WAL 头。
        """
        with _DB_LOCK:
            conn = self._connect_locked()
            with conn:
                yield conn

    # ------------------------------------------------------------------ #
    # 消息历史（M1）：append-only，按 (task_id, seq) 幂等
    # ------------------------------------------------------------------ #
    def append_history_sync(self, task_id: str, entries: list[dict[str, Any]]) -> int:
        """
        追加历史条目。重复写同一 (task_id, seq) 会**覆盖** —— 幂等语义，
        供"重放同一段历史"与部分失败后的补写使用。返回写入条数。
        """
        if not entries:
            return 0
        now = time.time()
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO task_history"
                " (task_id, seq, role, content, reasoning, tool_call_id, tool_name,"
                "  tool_calls_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (task_id, int(e["seq"]), str(e.get("role") or ""),
                     str(e.get("content") or ""), str(e.get("reasoning") or ""),
                     str(e.get("tool_call_id") or ""), str(e.get("tool_name") or ""),
                     str(e.get("tool_calls_json") or ""), now)
                    for e in entries
                ],
            )
        return len(entries)

    def load_history_sync(self, task_id: str) -> list[dict[str, Any]]:
        """按 seq 升序读取某任务的全部历史条目。"""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT seq, role, content, reasoning, tool_call_id, tool_name,"
                " tool_calls_json, created_at"
                " FROM task_history WHERE task_id = ? ORDER BY seq ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def history_count_sync(self, task_id: str) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_history WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    async def append_history(self, task_id: str, entries: list[dict[str, Any]]) -> int:
        return await asyncio.to_thread(self.append_history_sync, task_id, entries)

    async def load_history(self, task_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.load_history_sync, task_id)

    # ------------------------------------------------------------------ #
    # 事件流（#49）：append-only，按 (task_id, seq) 幂等
    # ------------------------------------------------------------------ #
    def append_events_sync(self, rows: list[dict[str, Any]]) -> int:
        """
        批量写入事件（一个事务）。每行：`{id, task_id, seq, ts, turn, type, data}`。

        ★ `INSERT OR REPLACE`：队列重放、续跑补写、导入重号都不会炸。
          重复的 (task_id, seq) 以后写的为准 —— 语义是"这个位置的事件就是它"。
        """
        if not rows:
            return 0
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO task_events"
                " (id, task_id, seq, ts, turn, type, data_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        int(r.get("id") or 0),
                        str(r["task_id"]),
                        int(r["seq"]),
                        float(r.get("ts") or 0.0),
                        int(r.get("turn") or 0),
                        str(r.get("type") or ""),
                        json.dumps(r.get("data") or {}, ensure_ascii=False, default=str),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    async def append_events(self, rows: list[dict[str, Any]]) -> int:
        return await asyncio.to_thread(self.append_events_sync, rows)

    def load_events_sync(
        self,
        task_id: str,
        *,
        since_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """按 seq 升序读某任务的事件（`since_id` 按全局 id 过滤，和内存侧同义）。"""
        sql = (
            "SELECT id, task_id, seq, ts, turn, type, data_json"
            " FROM task_events WHERE task_id = ?"
        )
        args: list[Any] = [task_id]
        if since_id is not None:
            sql += " AND id > ?"
            args.append(int(since_id))
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))

        with self._tx() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_event_row_to_dict(r) for r in rows]

    async def load_events(self, task_id: str, **kw: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.load_events_sync, task_id, **kw)

    def events_count_sync(self, task_id: str) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    async def events_count(self, task_id: str) -> int:
        return await asyncio.to_thread(self.events_count_sync, task_id)

    def event_counts_sync(self, task_ids: list[str]) -> dict[str, int]:
        """一次查出多个任务各有几条事件（列表接口用，避免 N+1）。"""
        ids = [t for t in task_ids if t]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT task_id, COUNT(*) AS n FROM task_events"
                f" WHERE task_id IN ({marks}) GROUP BY task_id",
                tuple(ids),
            ).fetchall()
        return {r["task_id"]: int(r["n"]) for r in rows}

    def max_event_id_sync(self) -> int:
        """库里最大的全局事件 id —— 启动时用它给 hub 播种，保证 id 跨重启单调。"""
        with self._tx() as conn:
            row = conn.execute("SELECT MAX(id) AS m FROM task_events").fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    async def max_event_id(self) -> int:
        return await asyncio.to_thread(self.max_event_id_sync)

    def max_event_seq_sync(self) -> dict[str, int]:
        """
        每个任务已经落盘的最大 seq。

        ★ 用途只有一个：**续跑时接上 seq**。
          续跑会新建一条 EventBus，它的 seq 又从 1 开始；如果直接写库，
          (task_id, 1) 会把第一次运行的第 1 条事件覆盖掉 —— 时间线会被搅乱。
          EventArchive 启动时把这张表读进内存，hub 每发一条问它一次（纯字典查）。
        """
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT task_id, MAX(seq) AS m FROM task_events GROUP BY task_id"
            ).fetchall()
        return {r["task_id"]: int(r["m"]) for r in rows}

    def prune_events_sync(self, before_ts: float) -> int:
        """
        保留策略：删掉"创建时间早于 before_ts 且已终态"的任务的事件。

        ★ 只删终态任务：运行中/排队中的任务事件一定还要用（续跑要接 seq）。
        ★ 顺带清理孤儿事件（任务行已经没了的事件）——
          `sessions` 删除是 ON DELETE CASCADE，tasks 会跟着删，
          但 task_events 没有外键（跨表级联会让写入变慢），所以会留下孤儿。
        ★ 删事件时**同步删投影行**：投影的契约是"可从事实重建"，
          事实被删了，留着投影行就是假数据 —— 删事件必须删投影。
        """
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM task_events WHERE task_id IN ("
                "  SELECT task_id FROM tasks"
                "   WHERE created_at < ? AND status IN ('done', 'aborted', 'failed')"
                ")",
                (float(before_ts),),
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur2 = conn.execute(
                "DELETE FROM task_events"
                " WHERE task_id NOT IN (SELECT task_id FROM tasks)"
            )
            if cur2.rowcount and cur2.rowcount > 0:
                deleted += cur2.rowcount
            # 投影表同步清理：被删事件的任务，其投影行也必须删
            conn.execute(
                "DELETE FROM task_projections"
                " WHERE task_id NOT IN (SELECT task_id FROM tasks)"
            )
            conn.execute(
                "DELETE FROM task_projections"
                " WHERE task_id IN (SELECT task_id FROM tasks"
                "   WHERE created_at < ? AND status IN ('done', 'aborted', 'failed'))",
                (float(before_ts),),
            )
        return deleted

    async def prune_events(self, before_ts: float) -> int:
        return await asyncio.to_thread(self.prune_events_sync, before_ts)

    # ------------------------------------------------------------------ #
    # 会话投影（#51）：从 task_events 派生的可重建查询表
    # ------------------------------------------------------------------ #
    # ★ 设计原则：投影行**不存任何事实之外的东西**。
    #   每一列都能从 task_events 全量重放推导出来 —— 删掉整张表，
    #   调 rebuild_projections_sync() 就能逐行重建回来（验收标准）。
    #   rebuilt_at 是元数据，不参与一致性验收（两次重建必然不同）。
    def _derive_projection(
        self, task_id: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        从一条任务的事件流推导投影行。

        纯函数：只读 events，不碰数据库。这是"可重建"的根基 ——
        重建 = 重新调用本方法，输入相同则输出逐字段一致。
        """
        p: dict[str, Any] = {
            "task_id": task_id,
            "status": "",
            "end_reason": "",
            "result": "",
            "tool_call_count": 0,
            "tool_result_count": 0,
            "has_final_answer": 0,
            "final_answer_len": 0,
            "has_error": 0,
            "error_count": 0,
            "budget_exceeded": 0,
            "turn_max": 0,
            "event_count": len(events),
            # v5：token 用量（对比表 #4）。全为 0 表示"这个任务没有 usage 数据"，
            # 不能当成"真的没花 token"——查的时候要能区分这两种。
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "llm_calls": 0,
            # v6：墙钟分解（毫秒）。first_token_ms 记的是**每次调用的 TTFT 之和**，
            # 平均值要除以 llm_calls —— 直接看总和没有意义。
            "llm_ms": 0,
            "tool_ms": 0,
            "first_token_ms": 0,
            "first_ts": 0.0,
            "last_ts": 0.0,
        }
        if not events:
            return p

        first_ts = float("inf")
        last_ts = 0.0
        for ev in events:
            ts = float(ev.get("ts") or 0.0)
            if ts < first_ts:
                first_ts = ts
            if ts > last_ts:
                last_ts = ts
            turn = int(ev.get("turn") or 0)
            if turn > p["turn_max"]:
                p["turn_max"] = turn

            type_ = ev.get("type") or ""
            data = ev.get("data") or {}
            if type_ == "task.end":
                p["status"] = str(data.get("status") or "")
                p["end_reason"] = str(data.get("reason") or "")
                p["result"] = str(data.get("answer") or "")
            elif type_ == "tool.call":
                p["tool_call_count"] += 1
            elif type_ == "tool.result":
                p["tool_result_count"] += 1
                # v6：工具墙钟。旧数据没有 duration_ms 就是 0（不猜）
                try:
                    p["tool_ms"] += int(data.get("duration_ms") or 0)
                except (TypeError, ValueError):
                    pass
            elif type_ == "final_answer":
                p["has_final_answer"] = 1
                p["final_answer_len"] = len(str(data.get("text") or ""))
            elif type_ == "error":
                p["has_error"] = 1
                p["error_count"] += 1
            elif type_ == "budget.exceeded":
                p["budget_exceeded"] = 1
            elif type_ == "llm.usage":
                # 对比表 #4：累计一次模型调用的用量。
                # 字段转换用 try 兜住 —— 导入外部任务时 data 可能不是纯数字，
                # 不能因为一条脏数据就让整个投影重建失败。
                p["llm_calls"] += 1
                for _k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                    try:
                        p[_k] += int(data.get(_k) or 0)
                    except (TypeError, ValueError):
                        pass
                # v6：模型墙钟 + 首 token 延迟（累加，平均要除 llm_calls）
                for _k in ("duration_ms", "first_token_ms"):
                    try:
                        p["llm_ms" if _k == "duration_ms" else "first_token_ms"] += int(
                            data.get(_k) or 0
                        )
                    except (TypeError, ValueError):
                        pass

        p["first_ts"] = first_ts if first_ts != float("inf") else 0.0
        p["last_ts"] = last_ts
        return p

    def upsert_projection_sync(self, p: dict[str, Any]) -> None:
        """
        写入/更新一条投影行。

        ★ 用 `INSERT ... ON CONFLICT DO UPDATE`：重建时重复写同一 task_id
          是幂等的，不会因为"已存在"而报错，也不会残留旧列。
        """
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO task_projections
                  (task_id, status, end_reason, result, tool_call_count,
                   tool_result_count, has_final_answer, final_answer_len,
                   has_error, error_count, budget_exceeded, turn_max,
                   event_count, prompt_tokens, completion_tokens,
                   reasoning_tokens, llm_calls, llm_ms, tool_ms, first_token_ms,
                   first_ts, last_ts, rebuilt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                  status            = excluded.status,
                  end_reason        = excluded.end_reason,
                  result            = excluded.result,
                  tool_call_count   = excluded.tool_call_count,
                  tool_result_count = excluded.tool_result_count,
                  has_final_answer  = excluded.has_final_answer,
                  final_answer_len  = excluded.final_answer_len,
                  has_error         = excluded.has_error,
                  error_count       = excluded.error_count,
                  budget_exceeded   = excluded.budget_exceeded,
                  turn_max          = excluded.turn_max,
                  event_count       = excluded.event_count,
                  prompt_tokens     = excluded.prompt_tokens,
                  completion_tokens = excluded.completion_tokens,
                  reasoning_tokens  = excluded.reasoning_tokens,
                  llm_calls         = excluded.llm_calls,
                  llm_ms            = excluded.llm_ms,
                  tool_ms           = excluded.tool_ms,
                  first_token_ms    = excluded.first_token_ms,
                  first_ts          = excluded.first_ts,
                  last_ts           = excluded.last_ts,
                  rebuilt_at        = excluded.rebuilt_at
                """,
                (
                    p["task_id"],
                    p.get("status", ""),
                    p.get("end_reason", ""),
                    p.get("result", ""),
                    int(p.get("tool_call_count", 0)),
                    int(p.get("tool_result_count", 0)),
                    int(p.get("has_final_answer", 0)),
                    int(p.get("final_answer_len", 0)),
                    int(p.get("has_error", 0)),
                    int(p.get("error_count", 0)),
                    int(p.get("budget_exceeded", 0)),
                    int(p.get("turn_max", 0)),
                    int(p.get("event_count", 0)),
                    int(p.get("prompt_tokens", 0)),
                    int(p.get("completion_tokens", 0)),
                    int(p.get("reasoning_tokens", 0)),
                    int(p.get("llm_calls", 0)),
                    int(p.get("llm_ms", 0)),
                    int(p.get("tool_ms", 0)),
                    int(p.get("first_token_ms", 0)),
                    float(p.get("first_ts", 0.0)),
                    float(p.get("last_ts", 0.0)),
                    float(p.get("rebuilt_at", 0.0)),
                ),
            )

    async def upsert_projection(self, p: dict[str, Any]) -> None:
        await asyncio.to_thread(self.upsert_projection_sync, p)

    def rebuild_projection_sync(self, task_id: str, *, rebuilt_at: float | None = None) -> dict[str, Any] | None:
        """
        重建单个任务的投影行：从 task_events 全量重放 → 推导 → 写入。

        返回重建出来的投影行（dict），任务不存在或没有事件时返回 None。
        这是"投影可重建"的核心实现，验收测试直接调它。
        """
        events = self.load_events_sync(task_id)
        if not events:
            return None
        p = self._derive_projection(task_id, events)
        p["rebuilt_at"] = rebuilt_at if rebuilt_at is not None else time.time()
        self.upsert_projection_sync(p)
        return p

    async def rebuild_projection(self, task_id: str, **kw: Any) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.rebuild_projection_sync, task_id, **kw)

    def rebuild_projections_sync(self, *, rebuilt_at: float | None = None) -> int:
        """
        全量重建所有任务的投影表。

        遍历 tasks 表里每个任务，从它的 task_events 重放推导，逐行写入。
        返回重建的任务数。启动时调一次，保证投影表与事实同步；
        也是验收测试"删掉 → 重建 → 逐行一致"的标准入口。
        """
        with self._tx() as conn:
            task_ids = [
                r["task_id"]
                for r in conn.execute("SELECT task_id FROM tasks").fetchall()
            ]
        now = rebuilt_at if rebuilt_at is not None else time.time()
        count = 0
        for tid in task_ids:
            p = self.rebuild_projection_sync(tid, rebuilt_at=now)
            if p is not None:
                count += 1
        return count

    async def rebuild_projections(self, **kw: Any) -> int:
        return await asyncio.to_thread(self.rebuild_projections_sync, **kw)

    def get_projection_sync(self, task_id: str) -> dict[str, Any] | None:
        """读单条投影行。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM task_projections WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    async def get_projection(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_projection_sync, task_id)

    def list_projections_sync(
        self,
        *,
        status: str = "",
        session_id: str = "",
        has_final_answer: int | None = None,
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        按条件查投影表（列表/统计接口用）。

        ★ 支持按 status / session_id / 是否有 final_answer 过滤，
          这是投影存在的意义 —— 不用扫事件流就能筛任务。
        """
        sql = "SELECT * FROM task_projections"
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if has_final_answer is not None:
            where.append("has_final_answer = ?")
            args.append(int(has_final_answer))
        if session_id:
            where.append("task_id IN (SELECT task_id FROM tasks WHERE session_id = ?)")
            args.append(session_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        direction = "DESC" if str(order).lower() == "desc" else "ASC"
        sql += f" ORDER BY last_ts {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        if offset:
            sql += " OFFSET ?"
            args.append(int(offset))
        with self._tx() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    async def list_projections(self, **kw: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_projections_sync, **kw)

    def projections_count_sync(self) -> int:
        with self._tx() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM task_projections").fetchone()
        return int(row["n"]) if row else 0

    async def projections_count(self) -> int:
        return await asyncio.to_thread(self.projections_count_sync)

    def clear_projections_sync(self) -> int:
        """清空投影表（验收测试用：删掉 → 重建 → 逐行一致）。返回删除行数。"""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM task_projections")
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return n

    async def clear_projections(self) -> int:
        return await asyncio.to_thread(self.clear_projections_sync)


    # ------------------------------------------------------------------ #
    # 导出 / 导入（一个任务的全套材料：行 + 快照 + 历史 + 事件）
    # ------------------------------------------------------------------ #
    def export_task_sync(self, task_id: str) -> dict[str, Any] | None:
        """
        导出一个任务。

        ★ 为什么连 session 一起导：任务行有 `session_id` 外键，
          导到别人的库里如果会话不存在，插入会被外键拒掉。
        """
        row = self.get_task_sync(task_id, with_state=True)
        if row is None:
            return None
        return {
            "format": EXPORT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "session": self.get_session_sync(row["session_id"]),
            "task": row,
            "history": self.load_history_sync(task_id),
            "events": self.load_events_sync(task_id),
        }

    async def export_task(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.export_task_sync, task_id)

    def import_task_sync(self, payload: dict[str, Any], *, task_id: str = "") -> dict[str, Any]:
        """
        把 `export_task` 的产物写回库。返回 `{task_id, source_task_id, events, history}`。

        ★ 撞号策略：默认保留原 task_id；库里已经有了就换一个新 id（导入不是"覆盖"）。
          想强制指定就用参数 `task_id`。
        ★ 事件 id 重新编号（从库里 MAX(id)+1 起），因为 id 是**全局**游标，
          两个库混在一起时旧 id 可能和别人重复。
        """
        if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
            raise StoreError(f"不是 Loom 任务导出文件（缺少 format={EXPORT_FORMAT}）")

        src = payload.get("task") or {}
        if not src.get("task_id"):
            raise StoreError("导出文件里没有 task.task_id")

        target_id = task_id or str(src["task_id"])
        auto_renamed = False
        with self._tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (target_id,)
            ).fetchone()
        if exists:
            if task_id:
                raise StoreError(f"任务已存在：{target_id}（换一个 task_id 或先删掉）")
            target_id = _make_import_id(str(src["task_id"]))
            auto_renamed = True

        session = payload.get("session") or {}
        session_id = str(src.get("session_id") or session.get("session_id") or "")
        if not session_id:
            raise StoreError("导出文件里没有 session_id")

        state = src.get("state") or {}
        if isinstance(state, dict) and state.get("task_id"):
            state = {**state, "task_id": target_id}

        now = time.time()
        with self._tx() as conn:
            # 会话不存在就补一个：任务行的外键指着它
            if not conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone():
                conn.execute(
                    "INSERT INTO sessions (session_id, title, workspace, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        session.get("title") or "导入的会话",
                        session.get("workspace") or "",
                        session.get("created_at") or now,
                        session.get("updated_at") or now,
                    ),
                )

            conn.execute(
                "INSERT INTO tasks"
                " (task_id, session_id, parent_task_id, goal, status, model, workspace,"
                "  created_at, started_at, ended_at, end_reason, result, turn, state_json)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target_id,
                    session_id,
                    str(src.get("goal") or "(导入的任务)"),
                    str(src.get("status") or "done"),
                    str(src.get("model") or ""),
                    str(src.get("workspace") or ""),
                    float(src.get("created_at") or now),
                    src.get("started_at"),
                    src.get("ended_at"),
                    str(src.get("end_reason") or ""),
                    str(src.get("result") or ""),
                    int(src.get("turn") or 0),
                    json.dumps(state, ensure_ascii=False, default=str) if state else "",
                ),
            )

            history = payload.get("history") or []
            if history:
                conn.executemany(
                    "INSERT OR REPLACE INTO task_history"
                    " (task_id, seq, role, content, reasoning, tool_call_id, tool_name,"
                    "  tool_calls_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            target_id,
                            int(h.get("seq") or 0),
                            str(h.get("role") or ""),
                            str(h.get("content") or ""),
                            str(h.get("reasoning") or ""),
                            str(h.get("tool_call_id") or ""),
                            str(h.get("tool_name") or ""),
                            str(h.get("tool_calls_json") or ""),
                            float(h.get("created_at") or now),
                        )
                        for h in history
                    ],
                )

            events = payload.get("events") or []
            if events:
                base = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
                ).fetchone()["m"]
                conn.executemany(
                    "INSERT OR REPLACE INTO task_events"
                    " (id, task_id, seq, ts, turn, type, data_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            int(base) + i + 1,
                            target_id,
                            int(e.get("seq") or 0),
                            float(e.get("ts") or 0.0),
                            int(e.get("turn") or 0),
                            str(e.get("type") or ""),
                            json.dumps(e.get("data") or {}, ensure_ascii=False, default=str),
                        )
                        for i, e in enumerate(events)
                    ],
                )

        # 事务已提交，事件已落盘 —— 现在重建该任务的投影行
        # （投影是派生的，导入事件后必须同步重建，否则查询不到）
        self.rebuild_projection_sync(target_id)

        return {
            "task_id": target_id,
            "source_task_id": str(src["task_id"]),
            "events": len(payload.get("events") or []),
            "history": len(payload.get("history") or []),
            # true 只表示"因为撞号**自动**改名"；显式指定 id 不算改名
            "renamed": auto_renamed,
        }

    async def import_task(self, payload: dict[str, Any], *, task_id: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self.import_task_sync, payload, task_id=task_id)

    def close_sync(self) -> None:
        """
        关闭连接（进程退出 / 测试清理时调用）。

        ★ 必须显式关：Windows 上文件被打开的连接占着时目录删不掉，
          WAL 的 `-wal` / `-shm` 也会留着。
        """
        with _DB_LOCK:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def close(self) -> None:
        await asyncio.to_thread(self.close_sync)

    # ===================================================================== #
    # 初始化 / 升级
    # ===================================================================== #
    def init_sync(self) -> None:
        with self._tx() as conn:
            # WAL 是**持久设置**，写一次就够（详见 _tx 的说明）
            conn.execute("PRAGMA journal_mode=WAL")
            ver = conn.execute("PRAGMA user_version").fetchone()[0]

            if ver == 0:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                      session_id TEXT PRIMARY KEY,
                      title      TEXT NOT NULL DEFAULT '',
                      workspace  TEXT NOT NULL DEFAULT '',
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                      task_id    TEXT PRIMARY KEY,
                      session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                      parent_task_id TEXT,          -- M5：NULL = 根任务；否则指向父任务
                      goal       TEXT NOT NULL,
                      status     TEXT NOT NULL,
                      model      TEXT NOT NULL DEFAULT '',
                      workspace  TEXT NOT NULL DEFAULT '',
                      created_at REAL NOT NULL,
                      started_at REAL,
                      ended_at   REAL,
                      end_reason TEXT NOT NULL DEFAULT '',
                      result     TEXT NOT NULL DEFAULT '',
                      turn       INTEGER NOT NULL DEFAULT 0,
                      state_json TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_tasks_session
                      ON tasks(session_id, created_at);

                    CREATE TABLE IF NOT EXISTS idempotency (
                      key        TEXT PRIMARY KEY,
                      task_id    TEXT NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_idem_created
                      ON idempotency(created_at);

                    -- M1：消息历史（append-only 过程记录，按 (task_id, seq) 幂等）
                    CREATE TABLE IF NOT EXISTS task_history (
                      task_id     TEXT NOT NULL,
                      seq         INTEGER NOT NULL,
                      role        TEXT NOT NULL,
                      content     TEXT NOT NULL DEFAULT '',
                      reasoning   TEXT NOT NULL DEFAULT '',
                      tool_call_id TEXT NOT NULL DEFAULT '',
                      tool_name   TEXT NOT NULL DEFAULT '',
                      tool_calls_json TEXT NOT NULL DEFAULT '',
                      created_at  REAL NOT NULL,
                      PRIMARY KEY (task_id, seq)
                    );
                    """
                )
                # ★ 新库这条路径**必须**也建事件表：下面那些"任何版本都跑一次"的
                #   补齐语句在 `return` 之后，走不到。少了这一句，全新安装的第一启动
                #   会在 `EventArchive.load_seq_cache_sync()` 直接
                #   `sqlite3.OperationalError: no such table: task_events`。
                conn.executescript(EVENTS_DDL)
                # v4：投影表（#51）—— 新库走这条路径，必须一并建好
                conn.executescript(PROJECTIONS_DDL)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return

            if ver > SCHEMA_VERSION:
                raise StoreError(
                    f"数据库 schema 版本 {ver} 高于代码支持的 {SCHEMA_VERSION} —— "
                    f"可能是用新版 Loom 建的库。请升级代码，或换个 LOOM_DB 路径。"
                )
            # ---- M5 迁移：给旧库补 parent_task_id（就地 ALTER，不重建库）----
            if ver < 2:
                cols = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
                }
                if "parent_task_id" not in cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)"
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            # ---- v3 迁移：事件流落盘（#49）-----------------------------------
            if ver < 3:
                conn.executescript(EVENTS_DDL)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            # ---- 补齐历史表（任何版本都跑一次）--------------------------------
            # ★ 为什么不能只放在 ver == 0 分支：
            #   task_history 是 v1 才加进建表脚本的，之前建的库永远停在 v1 且没有这张表；
            #   后来的 v1→v2 迁移只补了 parent_task_id —— 于是"用了一段时间的老库"
            #   会静默地每条历史都写失败（core 只记 history_persist_errors，不致命，
            #   但界面上历史消息/回放是空的）。这里无条件补一次，成本为零。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_history (
                  task_id     TEXT NOT NULL,
                  seq         INTEGER NOT NULL,
                  role        TEXT NOT NULL,
                  content     TEXT NOT NULL DEFAULT '',
                  reasoning   TEXT NOT NULL DEFAULT '',
                  tool_call_id TEXT NOT NULL DEFAULT '',
                  tool_name   TEXT NOT NULL DEFAULT '',
                  tool_calls_json TEXT NOT NULL DEFAULT '',
                  created_at  REAL NOT NULL,
                  PRIMARY KEY (task_id, seq)
                );
                """
            )

            # ---- 补齐事件表（同样任何版本都跑一次）----------------------------
            # v3 才加的表；老库可能停在 v2，迁移分支只在"当时没跑过"时生效，
            # 所以这里也无条件补一次（成本为零，`IF NOT EXISTS`）。
            conn.executescript(EVENTS_DDL)

            # ---- 补齐投影表（任何版本都跑一次）--------------------------------
            # v4 才加的表（#51）；老库停在 v3 时上面两个迁移分支都不触发，
            # 必须在这里无条件补一次，否则启动后投影查询直接 "no such table"。
            conn.executescript(PROJECTIONS_DDL)

            # ---- v5 迁移：投影表补 token 列（对比表 #4）-----------------------
            # ★ 用"缺列就补"而不是 `if ver < 5`：
            #   ① `executescript(PROJECTIONS_DDL)` 用的是 `IF NOT EXISTS`，
            #      表已存在时**不会**补新列 —— 老库必须走 ALTER；
            #   ② 无条件判断天然幂等，也顺手能修好被手工改过结构的库。
            proj_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(task_projections)").fetchall()
            }
            if proj_cols:
                for _col, _ddl in (
                    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
                    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
                    ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
                    ("llm_calls", "INTEGER NOT NULL DEFAULT 0"),
                    ("llm_ms", "INTEGER NOT NULL DEFAULT 0"),
                    ("tool_ms", "INTEGER NOT NULL DEFAULT 0"),
                    ("first_token_ms", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if _col not in proj_cols:
                        conn.execute(
                            f"ALTER TABLE task_projections ADD COLUMN {_col} {_ddl}"
                        )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    async def init(self) -> None:
        await asyncio.to_thread(self.init_sync)

    # ===================================================================== #
    # Session
    # ===================================================================== #
    def upsert_session_sync(self, s: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, title, workspace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  title      = excluded.title,
                  workspace  = excluded.workspace,
                  updated_at = excluded.updated_at
                """,
                (
                    s["session_id"],
                    s.get("title", ""),
                    s.get("workspace", ""),
                    s.get("created_at", time.time()),
                    s.get("updated_at", time.time()),
                ),
            )

    async def upsert_session(self, s: dict[str, Any]) -> None:
        await asyncio.to_thread(self.upsert_session_sync, s)

    def list_sessions_sync(self) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sessions_sync)

    def get_session_sync(self, session_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_session_sync, session_id)

    def count_tasks_sync(self, session_id: str) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    async def count_tasks(self, session_id: str) -> int:
        return await asyncio.to_thread(self.count_tasks_sync, session_id)

    # ===================================================================== #
    # Task
    # ===================================================================== #
    def create_task_sync(
        self,
        task: dict[str, Any],
        *,
        idem_key: str = "",
    ) -> tuple[str, bool]:
        """
        创建任务。返回 `(task_id, created)`：

            created=True  → 新建
            created=False → 幂等键命中，返回**原来那个** task_id，没有新建

        ★ 建任务 + 登记幂等键在**同一个事务**里 —— 否则可能出现
          "任务建了但幂等键没记"，网络重试就又跑一遍。
        """
        now = time.time()

        with self._tx() as conn:
            # 幂等键命中 → 直接返回原任务
            if idem_key:
                row = conn.execute(
                    "SELECT task_id FROM idempotency WHERE key = ?", (idem_key,)
                ).fetchone()
                if row:
                    return row["task_id"], False

            conn.execute(
                """
                INSERT INTO tasks
                  (task_id, session_id, parent_task_id, goal, status, model, workspace,
                   created_at, started_at, ended_at, end_reason, result, turn, state_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', '', 0, '')
                """,
                (
                    task["task_id"],
                    task["session_id"],
                    task.get("parent_task_id") or None,   # M5：None = 根任务
                    task["goal"],
                    task.get("status", "created"),
                    task.get("model", ""),
                    task.get("workspace", ""),
                    task.get("created_at", now),
                    task.get("started_at"),          # 排队时为 None
                ),
            )

            if idem_key:
                conn.execute(
                    "INSERT INTO idempotency (key, task_id, created_at) VALUES (?, ?, ?)",
                    (idem_key, task["task_id"], now),
                )

            return task["task_id"], True

    async def create_task(
        self, task: dict[str, Any], *, idem_key: str = ""
    ) -> tuple[str, bool]:
        return await asyncio.to_thread(self.create_task_sync, task, idem_key=idem_key)

    def mark_started_sync(self, task_id: str, started_at: float | None = None) -> None:
        """任务真正拿到执行槽位。填 started_at + 改状态，同一个事务。"""
        ts = started_at if started_at is not None else time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE task_id = ?",
                (ts, task_id),
            )

    async def mark_started(self, task_id: str) -> None:
        await asyncio.to_thread(self.mark_started_sync, task_id)

    def save_task_sync(
        self,
        task_id: str,
        *,
        status: str,
        state: dict[str, Any] | None = None,
        end_reason: str = "",
        result: str = "",
        ended_at: float | None = None,
        turn: int | None = None,
    ) -> None:
        """
        保存一次关键状态转换。

        ★ 摘要列（status / result / turn）与 `state_json` 快照在**同一 UPDATE** 里改，
          保证两者永远一致 —— 否则会出现"列表显示 done、快照里还是 running"。
        """
        sets = ["status = ?"]
        args: list[Any] = [status]

        if state is not None:
            sets.append("state_json = ?")
            args.append(json.dumps(state, ensure_ascii=False, default=str))

        if end_reason:
            sets.append("end_reason = ?")
            args.append(end_reason)

        if result:
            sets.append("result = ?")
            args.append(result)

        if ended_at is not None:
            sets.append("ended_at = ?")
            args.append(ended_at)

        if turn is not None:
            sets.append("turn = ?")
            args.append(turn)

        args.append(task_id)

        with self._tx() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", tuple(args))

    async def save_task(self, task_id: str, **kwargs: Any) -> None:
        await asyncio.to_thread(self.save_task_sync, task_id, **kwargs)

    def get_task_sync(self, task_id: str, *, with_state: bool = False) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _task_row_to_dict(row, with_state=with_state) if row else None

    async def get_task(self, task_id: str, *, with_state: bool = False) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_task_sync, task_id, with_state=with_state)

    def list_tasks_sync(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._tx() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at ASC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at ASC"
                ).fetchall()
        # 列表不返回 state_json（太大）
        return [_task_row_to_dict(r, with_state=False) for r in rows]

    async def list_tasks(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_tasks_sync, session_id)

    def delete_session_sync(self, session_id: str) -> dict[str, int]:
        """
        删除一个会话及其**全部派生数据**（不碰工作区文件）。

        ★ 为什么每张表都显式删，而不是只删 sessions 靠 CASCADE：
          `tasks.session_id` 有 `ON DELETE CASCADE`，但 `task_events`、
          `task_projections`、`task_history` **都没有外键**。
          只删 sessions 会留下孤儿事件；更糟的是投影重建时会把已删的任务
          重新"算"出来（投影的行是 task_id 驱动的，不看 tasks 是否存在）。
        ★ 顺序从叶子到根：先删派生数据，再删任务，最后删会话。
          中途失败也不会出现"会话没了但任务还挂着"的悬挂状态。
        ★ 先 COUNT 再 DELETE：sqlite3 的 `rowcount` 对 DELETE 不一定可靠，
          这里要的是**能报给用户的准确数字**。
        """
        with self._tx() as conn:
            ids = [
                r["task_id"]
                for r in conn.execute(
                    "SELECT task_id FROM tasks WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            counts = {"tasks": len(ids), "events": 0, "projections": 0, "history": 0}
            if ids:
                ph = ",".join("?" * len(ids))
                args = tuple(ids)
                for key, table in (
                    ("events", "task_events"),
                    ("projections", "task_projections"),
                    ("history", "task_history"),
                ):
                    counts[key] = int(
                        conn.execute(
                            f"SELECT COUNT(*) AS n FROM {table} WHERE task_id IN ({ph})", args
                        ).fetchone()["n"]
                    )
                    conn.execute(f"DELETE FROM {table} WHERE task_id IN ({ph})", args)
            conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            counts["sessions"] = int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)
        return counts

    async def delete_session(self, session_id: str) -> dict[str, int]:
        return await asyncio.to_thread(self.delete_session_sync, session_id)

    # ===================================================================== #
    # M5：父子任务
    # ===================================================================== #
    def count_children_sync(self, parent_task_id: str) -> int:
        """某个父任务已经派生了多少子任务（用于数量上限）。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_task_id = ?",
                (parent_task_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    async def count_children(self, parent_task_id: str) -> int:
        return await asyncio.to_thread(self.count_children_sync, parent_task_id)

    def parent_of_sync(self, task_id: str) -> str | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT parent_task_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return (row["parent_task_id"] if row else None) or None

    async def parent_of(self, task_id: str) -> str | None:
        return await asyncio.to_thread(self.parent_of_sync, task_id)

    # ===================================================================== #
    # 重启恢复
    # ===================================================================== #
    def mark_orphans_failed_sync(self, reason: str = "server_restarted") -> list[str]:
        """
        把所有「看起来还在跑」的任务标成失败。

        ★ 服务重启后，挂起的协程和审批 Future 都没了 —— 它们永远不可能再前进。
          与其假装它们还在 running（前端会一直转圈），不如诚实标成 failed。
          同时把 state_json 里的 status 一起改掉，保证快照与摘要一致。
        """
        now = time.time()
        fixed: list[str] = []

        with self._tx() as conn:
            rows = conn.execute(
                "SELECT task_id, state_json FROM tasks "
                "WHERE status IN ('running', 'awaiting_approval', 'created')"
            ).fetchall()

            for row in rows:
                tid = row["task_id"]
                state = _loads(row["state_json"])
                if isinstance(state, dict):
                    state["status"] = "failed"
                    state["end_reason"] = reason
                    state["ended_at"] = now
                    state["pending_approval"] = None
                    conn.execute(
                        """
                        UPDATE tasks
                           SET status = 'failed', end_reason = ?, ended_at = ?, state_json = ?
                         WHERE task_id = ?
                        """,
                        (reason, now, json.dumps(state, ensure_ascii=False, default=str), tid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE tasks SET status = 'failed', end_reason = ?, ended_at = ?
                         WHERE task_id = ?
                        """,
                        (reason, now, tid),
                    )
                fixed.append(tid)

        return fixed

    async def mark_orphans_failed(self, reason: str = "server_restarted") -> list[str]:
        return await asyncio.to_thread(self.mark_orphans_failed_sync, reason)

    def list_unfinished_sync(self) -> list[dict[str, Any]]:
        """
        列出「看起来还在跑」的任务（服务重启后要逐个决定：续跑还是收尾）。

        ★ 与 mark_orphans_failed 的分工：那个是"一刀切标失败"的旧策略；
          现在先由调度器用 checkpoint 判断能不能续（recover_interrupted），
          判不了的才退回标失败。
        """
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, goal, workspace, model, status, turn, state_json "
                "  FROM tasks "
                " WHERE status IN ('running', 'awaiting_approval', 'created') "
                " ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_unfinished(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_unfinished_sync)

    # ===================================================================== #
    # 幂等键清理
    # ===================================================================== #
    def cleanup_idempotency_sync(self, ttl_s: int = IDEM_TTL_S, max_rows: int = IDEM_MAX_ROWS) -> int:
        """
        清理策略：
          ① 超过 TTL（默认 24h）的删掉
          ② 仍超过 max_rows 的，按 created_at 从旧到新删到上限以内

        为什么需要上限：长期运行下这张表会无限增长，而它的作用只是
        "挡住短时间内的重复提交"，不需要永久保留。
        """
        cutoff = time.time() - ttl_s
        removed = 0

        with self._tx() as conn:
            cur = conn.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
            removed += cur.rowcount if cur.rowcount > 0 else 0

            total = conn.execute("SELECT COUNT(*) AS n FROM idempotency").fetchone()["n"]
            if total > max_rows:
                conn.execute(
                    """
                    DELETE FROM idempotency WHERE key IN (
                      SELECT key FROM idempotency ORDER BY created_at ASC LIMIT ?
                    )
                    """,
                    (total - max_rows,),
                )
                removed += total - max_rows

        return removed

    async def cleanup_idempotency(self) -> int:
        return await asyncio.to_thread(self.cleanup_idempotency_sync)


# --------------------------------------------------------------------------- #
# 行 → dict
# --------------------------------------------------------------------------- #
def _loads(s: str) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """事件行 → API/导出用的形状（data_json 在这里解回 dict）。"""
    raw = row["data_json"] or "{}"
    return {
        "id": int(row["id"]),
        "task_id": row["task_id"],
        "seq": int(row["seq"]),
        "ts": row["ts"],
        "turn": int(row["turn"]),
        "type": row["type"],
        "data": _loads(raw) or {},
        # 载荷字节数（只用于展示；内存路径里是整条记录的序列化长度）
        "size": len(raw.encode("utf-8")),
    }


def _make_import_id(source_id: str) -> str:
    """
    导入撞号时的新 id。

    ★ 保留原 id 前缀：一眼能看出"它是从哪个任务导出来的"，
      排查时比 `task_9f3a1c` 这种纯随机 id 有用得多。
    """
    base = source_id[:40] if len(source_id) > 40 else source_id
    return f"{base}_imp{uuid.uuid4().hex[:6]}"


def _task_row_to_dict(row: sqlite3.Row, *, with_state: bool = True) -> dict[str, Any]:
    d = {
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "goal": row["goal"],
        "status": row["status"],
        "model": row["model"],
        "workspace": row["workspace"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "result": row["result"],
        "turn": row["turn"],
        # M5：父子关系（根任务为 None）
        "parent_task_id": row["parent_task_id"] if "parent_task_id" in row.keys() else None,
    }
    if with_state:
        d["state"] = _loads(row["state_json"])
    return d
