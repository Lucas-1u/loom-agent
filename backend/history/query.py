"""
会话历史查询（对比表 #25）—— 只读、自带连接。

★ 为什么是独立叶子模块、自己开连接：
  这一层要被两边用 —— server 的 `/api/history`（前端用）和 agent 的
  `session_history` 工具（模型用）。而 `tools/` 不能 import `server/`
  （依赖单向铁律），所以不能复用 `TaskStore`。
  照 RAG 的做法：自己开一个**只读**连接指向同一个库文件（WAL 允许多连接），
  只读也意味着它永远不会和主库的写事务打架。

★ 为什么查 task_projections 而不是扫 task_events：
  投影已经是"从事件流派生好的查询表"（#51），按状态/错误/预算筛选走索引；
  再扫事件流等于把投影白做了。
  但 `goal` / `workspace` 是**任务行**的字段（用户输入、会话属性），不是事件
  派生值，所以 JOIN `tasks` 拿 —— 不塞进投影表，否则"投影 = 纯事件派生"
  这条语义就破了，重建时还要为它们多走一条来源。

★ 只有回放（replay）才真的去读 task_events：那是"看某一次到底发生了什么"，
  本来就需要逐条事件。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

# 列表里每个任务的文本预览上限（完整内容用 replay 看）
PREVIEW_CHARS = 220

# 回放时单条事件 data 的上限：thinking 流式片段、大工具结果可能几万字，
# 整包塞给模型会直接把上下文吃掉。截断并显式标注 truncated。
MAX_EVENT_DATA_CHARS = 1500

# 默认不回放 thinking：它是模型的流式 delta，按 0.15s/80 字节切片，
# 一次任务几百条，回放价值低但体积最大。要看得显式传 kinds=["thinking"]。
DEFAULT_EXCLUDED_KINDS: tuple[str, ...] = ("thinking",)

# 单次查询上限，防止模型要求 "把全部历史列出来"
MAX_LIMIT = 200


class HistoryQuery:
    """只读的会话历史查询器。`usable=False` 时工具/接口应视为不可用。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path or ""

    @property
    def usable(self) -> bool:
        return bool(self.db_path) and os.path.isfile(self.db_path)

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self):
        """
        每次查询开一个只读连接。

        ★ 为什么不复用长连接：sqlite3 的连接不能跨线程，这里会被 asyncio 的
          线程池和工具调用在不同线程里用到；查询本身是毫秒级，开连接的成本
          远低于维护连接池 + 锁的复杂度。只读模式（mode=ro）也不会写 WAL。
        """
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def search(
        self,
        *,
        q: str = "",
        status: str = "",
        workspace: str = "",
        session_id: str = "",
        since: float | None = None,
        until: float | None = None,
        has_error: int | None = None,
        budget_exceeded: int | None = None,
        exclude_task_id: str = "",
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        按条件翻历史任务。返回 `{total, returned, items, truncated}`。

        ★ total 和 items 分开算：模型需要知道"还有多少没显示"，
          否则它会以为列表就是全部，错过更早的记录。
        """
        where: list[str] = []
        args: list[Any] = []

        if q.strip():
            like = f"%{q.strip()}%"
            # 搜目标与最终答复：这两处是"这次做了什么"最浓缩的描述
            where.append("(t.goal LIKE ? OR IFNULL(t.result, '') LIKE ?)")
            args.extend([like, like])
        if status:
            where.append("p.status = ?")
            args.append(status)
        if workspace:
            where.append("t.workspace = ?")
            args.append(workspace)
        if session_id:
            where.append("t.session_id = ?")
            args.append(session_id)
        if since is not None:
            where.append("p.last_ts >= ?")
            args.append(float(since))
        if until is not None:
            where.append("p.last_ts <= ?")
            args.append(float(until))
        if has_error is not None:
            where.append("p.has_error = ?")
            args.append(int(has_error))
        if budget_exceeded is not None:
            where.append("p.budget_exceeded = ?")
            args.append(int(budget_exceeded))
        if exclude_task_id:
            # ★ 排除调用方自己：模型正在做的这件事，搜出来只会浪费一轮
            where.append("p.task_id != ?")
            args.append(exclude_task_id)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        lim = max(1, min(int(limit), MAX_LIMIT))
        off = max(0, int(offset))

        base = (
            "FROM task_projections p JOIN tasks t ON t.task_id = p.task_id" + clause
        )
        with self._conn() as con:
            total = con.execute(f"SELECT COUNT(*) AS n {base}", tuple(args)).fetchone()["n"]
            rows = con.execute(
                "SELECT p.task_id, p.status, p.end_reason, p.tool_call_count,"
                "       p.tool_result_count, p.has_error, p.error_count,"
                "       p.budget_exceeded, p.turn_max, p.event_count,"
                "       p.prompt_tokens, p.completion_tokens, p.reasoning_tokens,"
                "       p.llm_calls, p.first_ts, p.last_ts,"
                "       t.goal, t.workspace, t.session_id, t.created_at, t.result "
                + base
                + f" ORDER BY p.last_ts {direction} LIMIT ? OFFSET ?",
                tuple(args) + (lim, off),
            ).fetchall()

        items = [self._row_to_item(r) for r in rows]
        return {
            "total": int(total),
            "returned": len(items),
            # 还有更多没显示 → 让调用方缩小条件，而不是自己翻页（无游标设计）
            "truncated": int(total) > off + len(items),
            "items": items,
        }

    @staticmethod
    def _row_to_item(r: sqlite3.Row) -> dict[str, Any]:
        goal = (r["goal"] or "").strip()
        result = (r["result"] or "").strip()
        return {
            "task_id": r["task_id"],
            "goal": goal[:PREVIEW_CHARS] + ("…" if len(goal) > PREVIEW_CHARS else ""),
            "status": r["status"],
            "end_reason": r["end_reason"],
            "workspace": r["workspace"],
            "session_id": r["session_id"],
            "created_at": r["created_at"],
            "last_event_ts": r["last_ts"],
            "turn": r["turn_max"],
            "tool_calls": r["tool_call_count"],
            "event_count": r["event_count"],
            "has_error": bool(r["has_error"]),
            "error_count": r["error_count"],
            "budget_exceeded": bool(r["budget_exceeded"]),
            # 对比表 #4：token 用量。全 0 表示"没有 usage 数据"（老任务/端点不返回），
            # 不是"真的没花"。消费方要能区分这两件事。
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "reasoning_tokens": r["reasoning_tokens"],
            "llm_calls": r["llm_calls"],
            "answer_preview": result[:PREVIEW_CHARS] + ("…" if len(result) > PREVIEW_CHARS else ""),
        }

    # ------------------------------------------------------------------ #
    # 单任务
    # ------------------------------------------------------------------ #
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """
        只取任务行（不含事件）。

        ★ 用途：回放前的**授权判定**。要先知道这个任务属于哪个工作区，
          才能决定"允许不允许看"；不能先把事件读出来再判断 ——
          那样数据已经在进程里了，"拒绝"只挡了返回值，不挡读取。
        """
        with self._conn() as con:
            row = con.execute(
                "SELECT task_id, goal, status, end_reason, workspace, session_id,"
                "       created_at, ended_at, turn FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # 统计（对比表 #52）
    # ------------------------------------------------------------------ #
    def stats(self, *, days: int = 30, workspace: str = "") -> dict[str, Any]:
        """
        按时间窗口做聚合统计（给统计面板用）。

        ★ 为什么复用投影表而不是扫事件流：
          投影已经物化了 status / token / 工具数 / 时间跨度，聚合就是一次
          `GROUP BY`；扫事件流要遍历几十万行，还没法按任务去重。
        ★ by_day 用 `last_ts` 的本地日期分组：任务属于"哪一天干的"，
          按最后事件时间算比按创建时间更贴近直觉（跨天的夜活算在收尾那天）。
        """
        span = max(1, min(int(days), 3650))
        since = time.time() - span * 86400
        where = ["p.last_ts >= ?"]
        args: list[Any] = [since]
        if workspace:
            where.append("t.workspace = ?")
            args.append(workspace)
        clause = " WHERE " + " AND ".join(where)
        base = " FROM task_projections p JOIN tasks t ON t.task_id = p.task_id" + clause

        with self._conn() as con:
            head = con.execute(
                "SELECT COUNT(*) AS tasks,"
                "  SUM(CASE WHEN p.status='done'    THEN 1 ELSE 0 END) AS done,"
                "  SUM(CASE WHEN p.status='failed'  THEN 1 ELSE 0 END) AS failed,"
                "  SUM(CASE WHEN p.status='aborted' THEN 1 ELSE 0 END) AS aborted,"
                "  SUM(p.prompt_tokens)     AS pt,"
                "  SUM(p.completion_tokens) AS ct,"
                "  SUM(p.reasoning_tokens)  AS rt,"
                "  SUM(p.llm_calls)         AS lc,"
                "  SUM(p.tool_call_count)   AS tc,"
                "  SUM(p.tool_result_count) AS rc,"
                "  SUM(p.event_count)       AS ec,"
                "  SUM(p.llm_ms)            AS lm,"
                "  SUM(p.tool_ms)           AS tm,"
                "  SUM(p.first_token_ms)    AS ft,"
                "  SUM(CASE WHEN p.last_ts > p.first_ts"
                "          THEN p.last_ts - p.first_ts ELSE 0 END) AS wall_s,"
                "  AVG(p.turn_max)          AS avg_turn,"
                "  AVG(CASE WHEN p.last_ts > p.first_ts"
                "          THEN p.last_ts - p.first_ts ELSE 0 END) AS avg_dur"
                + base,
                tuple(args),
            ).fetchone()

            day_rows = con.execute(
                "SELECT date(p.last_ts,'unixepoch','localtime') AS d,"
                "  COUNT(*) AS tasks,"
                "  SUM(CASE WHEN p.status='done'    THEN 1 ELSE 0 END) AS done,"
                "  SUM(CASE WHEN p.status='failed'  THEN 1 ELSE 0 END) AS failed,"
                "  SUM(CASE WHEN p.status='aborted' THEN 1 ELSE 0 END) AS aborted,"
                "  SUM(p.prompt_tokens)     AS pt,"
                "  SUM(p.completion_tokens) AS ct"
                + base
                + " GROUP BY d ORDER BY d",
                tuple(args),
            ).fetchall()

        total = int(head["tasks"] or 0)
        done = int(head["done"] or 0)
        calls = int(head["lc"] or 0)
        llm_ms = int(head["lm"] or 0)
        tool_ms = int(head["tm"] or 0)
        first_token_sum = int(head["ft"] or 0)
        wall_ms = int(round(float(head["wall_s"] or 0) * 1000))

        def _i(v: Any) -> int:
            return int(v or 0)

        def _f(v: Any, nd: int = 1) -> float:
            return round(float(v or 0.0), nd)

        # 模型 vs 工具的占比：分母用两者之和，而不是任务墙钟。
        # ★ 为什么不用墙钟做分母：墙钟里混着用户等 ask_user、调度、落盘等等，
        #   算进去会让"模型 40%"这种数字失去解释力 —— 你没法从 40% 推出该优化谁。
        busy_ms = llm_ms + tool_ms
        return {
            "range": {"days": span, "since": since},
            "scope": workspace or "all",
            "totals": {
                "tasks": total,
                "done": done,
                "failed": _i(head["failed"]),
                "aborted": _i(head["aborted"]),
                # 成功率只按"已结束的任务"算：running 不该算进分母
                "success_rate": round(done / total, 3) if total else 0.0,
                "prompt_tokens": _i(head["pt"]),
                "completion_tokens": _i(head["ct"]),
                "reasoning_tokens": _i(head["rt"]),
                "llm_calls": calls,
                "tool_calls": _i(head["tc"]),
                "events": _i(head["ec"]),
                "avg_turns": _f(head["avg_turn"]),
                "avg_duration_s": _f(head["avg_dur"]),
                # ---- 墙钟分解（对比表 #52）----
                "llm_ms": llm_ms,
                "tool_ms": tool_ms,
                "busy_ms": busy_ms,
                "wall_ms": wall_ms,
                "avg_llm_ms": round(llm_ms / calls, 1) if calls else 0.0,
                "avg_tool_ms": (
                    # 分母用 tool_result_count：duration_ms 记在结果事件上，
                    # 用调用数做分母会在"调用与结果不成对"的脏数据上算出偏小的均值
                    round(tool_ms / _i(head["rc"]), 1) if _i(head["rc"]) else 0.0
                ),
                # 首 token 延迟看**平均**：总和没有意义（调用次数不同没法比）
                "avg_first_token_ms": round(first_token_sum / calls, 1) if calls else 0.0,
                "first_token_ms_sum": first_token_sum,
                "llm_ratio": round(llm_ms / busy_ms, 3) if busy_ms else 0.0,
                "tool_ratio": round(tool_ms / busy_ms, 3) if busy_ms else 0.0,
            },
            "by_day": [
                {
                    "date": r["d"],
                    "tasks": _i(r["tasks"]),
                    "done": _i(r["done"]),
                    "failed": _i(r["failed"]),
                    "aborted": _i(r["aborted"]),
                    "prompt_tokens": _i(r["pt"]),
                    "completion_tokens": _i(r["ct"]),
                }
                for r in day_rows
            ],
        }

    # ------------------------------------------------------------------ #
    # 回放
    # ------------------------------------------------------------------ #
    def replay(
        self,
        task_id: str,
        *,
        kinds: Iterable[str] | None = None,
        from_turn: int | None = None,
        to_turn: int | None = None,
        limit: int = 80,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        """
        回放一个任务的事件时间线。任务不存在返回 None。

        ★ kinds 为空时默认**排除 thinking**（见模块顶部常量）。
          传 `kinds=["thinking"]` 就只看思考流，传具体类型就只看那几类。
        """
        lim = max(1, min(int(limit), MAX_LIMIT))
        off = max(0, int(offset))

        with self._conn() as con:
            task = con.execute(
                "SELECT task_id, goal, status, end_reason, workspace, session_id,"
                "       created_at, ended_at, turn FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                return None

            where = ["task_id = ?"]
            args: list[Any] = [task_id]
            wanted = [k for k in (kinds or []) if k]
            if wanted:
                where.append(f"type IN ({','.join('?' * len(wanted))})")
                args.extend(wanted)
            else:
                where.append(f"type NOT IN ({','.join('?' * len(DEFAULT_EXCLUDED_KINDS))})")
                args.extend(DEFAULT_EXCLUDED_KINDS)
            if from_turn is not None:
                where.append("turn >= ?")
                args.append(int(from_turn))
            if to_turn is not None:
                where.append("turn <= ?")
                args.append(int(to_turn))

            clause = " WHERE " + " AND ".join(where)
            total = con.execute(
                f"SELECT COUNT(*) AS n FROM task_events{clause}", tuple(args)
            ).fetchone()["n"]
            rows = con.execute(
                f"SELECT seq, type, turn, data_json FROM task_events{clause}"
                " ORDER BY seq LIMIT ? OFFSET ?",
                tuple(args) + (lim, off),
            ).fetchall()

        events = []
        for r in rows:
            raw = r["data_json"] or "{}"
            truncated = len(raw) > MAX_EVENT_DATA_CHARS
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = {"_raw": raw[:MAX_EVENT_DATA_CHARS]}
            if truncated and isinstance(data, dict):
                data = {**data, "_truncated": True}
            if truncated and not isinstance(data, dict):
                data = {"_value": data, "_truncated": True}
            events.append({
                "seq": r["seq"],
                "type": r["type"],
                "turn": r["turn"],
                "data": data,
            })

        return {
            "task": {
                "task_id": task["task_id"],
                "goal": task["goal"],
                "status": task["status"],
                "end_reason": task["end_reason"],
                "workspace": task["workspace"],
                "session_id": task["session_id"],
                "created_at": task["created_at"],
                "ended_at": task["ended_at"],
                "turn": task["turn"],
            },
            "total": int(total),
            "returned": len(events),
            "truncated": int(total) > off + len(events),
            "events": events,
        }
