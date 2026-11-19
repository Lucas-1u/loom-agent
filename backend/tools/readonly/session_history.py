"""
session_history —— 让模型翻自己过去的任务（对比表 #25，对齐 DSH `dsh-tool-session-query`）。

★ 它做什么：两个动作，一个工具入口。
    · search  —— 按关键词/状态/时间翻历史任务（查 task_projections JOIN tasks）
    · replay  —— 回放某一次任务的事件时间线（读 task_events）

★ 为什么合成一个工具而不是拆成五个（DSH 拆了 5 个）：
   DSH 面向多租户部署，需要细粒度授权面；Loom 是单人本地 Agent，
   "翻历史"就是一个动作的两种粒度（列表 → 详情）。合成一个工具省工具槽位，
   也避免模型在 5 个近义工具之间反复试错。

★ 安全边界（三条，都是硬规则）：
   1. **只读**：risk=READ，不写任何东西（连接都是 mode=ro）。
   2. **默认只查当前工作区**：scope=workspace 时，搜索加 workspace 过滤，
      回放先查任务行做工作区比对，不一致直接拒。跨项目必须显式 scope=all。
   3. **回放先授权再读**：先 `get_task()` 拿 workspace 判断，再读事件 ——
      不能读完再拒，那样数据已经进进程了。

★ 为什么不给分页游标：结果超过 limit 时返回 truncated=true，让调用方缩小条件
   或加大 limit（上限 200）。模型能控上限，但控不到"无限翻页"。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel
from ...history.query import HistoryQuery

SEARCH_DEFAULT_LIMIT = 20
REPLAY_DEFAULT_LIMIT = 60


def _norm(path: str) -> str:
    """工作区比较口径：大小写不敏感 + 去尾分隔符（Windows 上必须这么比）。"""
    p = os.path.normpath(path or "")
    return os.path.normcase(p).rstrip("\\/")


def _ago(ts: float, now: float) -> str:
    """把时间戳说成"多久以前"，比 epoch 数字好读。"""
    if not ts:
        return "未知"
    d = max(0.0, now - ts)
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


class SessionHistoryTool(Tool):
    name = "session_history"
    description = (
        "翻自己过去做过什么（只读）。两个动作："
        "action=search —— 按关键词/状态/时间翻历史任务，返回目标、结局、轮次、工具数、是否报错；"
        "action=replay —— 回放某一次任务的事件时间线（计划、工具调用与结果、反思、最终答复）。"
        "默认只查当前工作区的任务；要看别的项目必须显式 scope=all。"
        "搜索结果会自动排除当前正在执行的任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "replay"],
                "description": "search=翻任务列表；replay=回放某个任务的事件。",
            },
            "query": {
                "type": "string",
                "description": "关键词，匹配任务目标与最终答复（仅 search）。",
            },
            "status": {
                "type": "string",
                "enum": ["done", "failed", "aborted", "cancelled"],
                "description": "只看某个结局（仅 search）。",
            },
            "days": {
                "type": "integer",
                "description": "只看最近 N 天内结束的任务（仅 search）。",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "all"],
                "description": "workspace=只查当前工作区（默认）；all=跨所有项目。",
            },
            "task_id": {
                "type": "string",
                "description": "要回放的任务 id（replay 必填，可从 search 结果里取）。",
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只回放这些事件类型，如 [\"tool.call\",\"tool.result\"]（replay）。"
                               "不传默认排除 thinking。",
            },
            "limit": {
                "type": "integer",
                "description": f"最多返回多少条（默认 search {SEARCH_DEFAULT_LIMIT} / replay {REPLAY_DEFAULT_LIMIT}，上限 200）。",
            },
        },
        "required": ["action"],
    }
    risk = RiskLevel.READ

    def __init__(self, history: HistoryQuery) -> None:
        self.history = history

    # ------------------------------------------------------------------ #
    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        a = args or {}
        action = str(a.get("action") or "").strip()
        if action not in ("search", "replay"):
            return "action 只能是 search / replay"
        if action == "replay" and not str(a.get("task_id") or "").strip():
            return "action=replay 时必须给 task_id"
        if a.get("limit") is not None:
            try:
                n = int(a["limit"])
            except (TypeError, ValueError):
                return "limit 必须是整数"
            if n < 1 or n > 200:
                return "limit 必须在 1..200 之间"
        if a.get("days") is not None:
            try:
                d = int(a["days"])
            except (TypeError, ValueError):
                return "days 必须是整数"
            if d < 1 or d > 3650:
                return "days 必须在 1..3650 之间"
        return ""

    # ------------------------------------------------------------------ #
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            a = args or {}
            action = str(a.get("action") or "").strip()
            scope = str(a.get("scope") or "workspace").strip()
            if scope not in ("workspace", "all"):
                scope = "workspace"
            now = time.time()

            if action == "search":
                return await self._search(a, ctx, scope, now, t)
            return await self._replay(a, ctx, scope, now, t)

    # ------------------------------------------------------------------ #
    async def _search(self, a: dict[str, Any], ctx: ToolContext,
                      scope: str, now: float, t: Any) -> ToolResult:
        limit = int(a.get("limit") or SEARCH_DEFAULT_LIMIT)
        since = None
        if a.get("days") is not None:
            since = now - int(a["days"]) * 86400

        res = self.history.search(
            q=str(a.get("query") or ""),
            status=str(a.get("status") or ""),
            # ★ 默认只查当前工作区：模型不该在别的项目里翻东西
            workspace="" if scope == "all" else (ctx.workspace or ""),
            since=since,
            # ★ 排除自己：正在做的这件事搜出来只会浪费一轮
            exclude_task_id=ctx.task_id or "",
            limit=limit,
        )

        items = res["items"]
        head = (
            f"找到 {res['returned']} 条历史任务"
            f"（共 {res['total']} 条匹配{'，已截断' if res['truncated'] else ''}）"
            f"｜范围：{'全部工作区' if scope == 'all' else '当前工作区'}"
        )
        lines = [head]
        for it in items[:5]:
            lines.append(
                f"· [{it['status']}] {it['goal'][:60]}"
                f"（{it['turn']} 轮 / {it['tool_calls']} 次工具 / {_ago(it['last_event_ts'], now)}"
                f"{' / 有错误' if it['has_error'] else ''}）"
                f" task_id={it['task_id']}"
            )
        if not items:
            lines.append("（没有匹配的历史任务；换个关键词，或把 days 放宽）")

        data = {
            "action": "search",
            "scope": scope,
            "total": res["total"],
            "returned": res["returned"],
            "truncated": res["truncated"],
            "items": items,
        }
        return ToolResult.success(self.name, "\n".join(lines), data=data, duration_ms=t.ms)

    # ------------------------------------------------------------------ #
    async def _replay(self, a: dict[str, Any], ctx: ToolContext,
                      scope: str, now: float, t: Any) -> ToolResult:
        task_id = str(a.get("task_id") or "").strip()
        limit = int(a.get("limit") or REPLAY_DEFAULT_LIMIT)

        # ---- 先授权，再读 ----
        task = self.history.get_task(task_id)
        if task is None:
            return ToolResult.failure(
                self.name, ErrorKind.NOT_FOUND,
                f"没有这个任务：{task_id}",
                hint="先用 action=search 查一下有哪些 task_id。",
                duration_ms=t.ms,
            )
        if scope != "all" and _norm(task.get("workspace") or "") != _norm(ctx.workspace or ""):
            return ToolResult.failure(
                self.name, ErrorKind.PERMISSION_DENIED,
                f"任务 {task_id} 属于另一个工作区（{task.get('workspace')}），"
                f"当前工作区是 {ctx.workspace}",
                hint="确实要看别的项目，显式传 scope=all。",
                duration_ms=t.ms,
            )

        kinds = a.get("kinds")
        if kinds is not None and not isinstance(kinds, list):
            kinds = [str(kinds)]
        res = self.history.replay(task_id, kinds=kinds, limit=limit)
        if res is None:      # 授权检查后任务被删（极端情况）：如实说
            return ToolResult.failure(
                self.name, ErrorKind.NOT_FOUND, f"任务在读取前消失了：{task_id}", duration_ms=t.ms,
            )

        evs = res["events"]
        seq_desc = " → ".join(e["type"] for e in evs[:8])
        summary = (
            f"回放 {task_id}（{res['task']['status']}"
            f"{' / ' + res['task']['end_reason'] if res['task']['end_reason'] else ''}，"
            f"共 {res['total']} 个事件，返回 {res['returned']}"
            f"{'，已截断' if res['truncated'] else ''}）\n"
            f"事件序列：{seq_desc}{' …' if len(evs) > 8 else ''}"
        )
        data = {
            "action": "replay",
            "scope": scope,
            "task": res["task"],
            "total": res["total"],
            "returned": res["returned"],
            "truncated": res["truncated"],
            "events": evs,
        }
        return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)


def make(history: HistoryQuery):
    """工厂：历史查询不可用（库不存在）就不注册。"""
    if history is None or not getattr(history, "usable", False):
        return None
    return SessionHistoryTool(history)
