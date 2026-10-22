"""
SQLite 持久化层

★ 存什么、不存什么
────────────────────────────────────────────────────────────────
  存：Session、Task（含完整 TaskState 快照）、幂等键
  不存：AgentEvent（体积大、价值随时间衰减；重启后明确告知不可用）
         Message（从 Task 派生：goal → user，result → assistant，存两份必不同步）

★ 并发与线程安全（这是选 SQLite 而不是 JSON 的核心原因）
────────────────────────────────────────────────────────────────
  多个 Task（默认上限 2）可能**同时结束**。JSON 方案里「读-改-写」会丢更新，
  必须自己加全局锁 + 原子写。SQLite 的事务直接解决，代价是每次操作一个短连接。

  具体做法：
    · 每次操作**新建一个连接**（`sqlite3.connect` 对同一文件很快），用完关掉
      —— 绝不在多个协程间共享同一个连接对象（sqlite3 连接不是线程安全的）
    · 同步 API 用 `asyncio.to_thread()` 包一层，不阻塞事件循环
    · 一次操作 = 一个事务（`with conn:` 自动 commit / rollback），**绝不跨操作**
    · WAL 模式：读写不互斥；`synchronous=NORMAL` 在 WAL 下足够安全且更快

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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 幂等键保留多久 / 最多几条（超出按 created_at 从旧到新删）
IDEM_TTL_S = 24 * 3600
IDEM_MAX_ROWS = 1000

SCHEMA_VERSION = 1


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

    # ===================================================================== #
    # 连接与事务
    # ===================================================================== #
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """
        一个短事务。成功 commit，异常 rollback，无论怎样都关连接。

        ★ `with conn:` 在 sqlite3 里是**事务上下文**（不是普通上下文管理器），
          进入时隐式 BEGIN，正常退出 commit，抛异常 rollback。

        ★ 为什么这里不设 `PRAGMA journal_mode=WAL`？
          WAL 是**数据库级持久设置**，写一次就存在文件头里。每次连接都设一遍
          会在并发连接时争抢 WAL 头（实测偶发 `attempt to write a readonly
          database`）。只在 `init_sync()` 里设一次即可。
        """
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

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
                    """
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return

            if ver > SCHEMA_VERSION:
                raise StoreError(
                    f"数据库 schema 版本 {ver} 高于代码支持的 {SCHEMA_VERSION} —— "
                    f"可能是用新版 Loom 建的库。请升级代码，或换个 LOOM_DB 路径。"
                )
            # ver == SCHEMA_VERSION：已是最新

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
                  (task_id, session_id, goal, status, model, workspace,
                   created_at, started_at, ended_at, end_reason, result, turn, state_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '', '', 0, '')
                """,
                (
                    task["task_id"],
                    task["session_id"],
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

    def get_task_sync(self, task_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _task_row_to_dict(row) if row else None

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_task_sync, task_id)

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
    }
    if with_state:
        d["state"] = _loads(row["state_json"])
    return d
