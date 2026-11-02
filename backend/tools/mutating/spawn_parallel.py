"""
spawn_parallel —— 父 Agent 并行派生多个子任务（P1-#42）。

★ 与 spawn_subtask 的区别
--------------------
spawn_subtask     派生**一个**子任务，串行等待完成 → 返回单个结果信封
spawn_parallel    派生**多个**子任务，并发执行 → 等待全部完成 → 聚合结果

★ 为什么仍要审批
----------------
和 spawn_subtask 一样，并行派生会创建多个子任务，每个都可能读写文件或执行命令。
按"副作用不明确就按保守处理"的原则，需要人工审批。

★ 结果聚合
----------
返回 ParallelSpawnResult，包含每个子任务的完整结果 + 聚合统计
（done / aborted / rejected / timed_out / cancelled 计数）。

★ 数量上限
----------
并行派生的数量受 max_children 限制：已派生 + 本次请求 ≤ max_children。
超出的部分会被拒绝（返回 rejected 状态）。
"""

from __future__ import annotations

from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel


class SpawnParallelTool(Tool):
    name = "spawn_parallel"
    description = (
        "**并行派生多个子任务**去完成多个可以独立验收的子目标。"
        "所有子任务同时启动、并发执行，等待全部完成后返回聚合结果。"
        "每个子任务有自己的 task_id、预算、审批和证据。"
        "返回内容包含每个子任务的结果信封 + 聚合统计（完成数/拒绝数/超时数）。"
        "约束：并行数量受上限约束；子任务不能绕过审批与预算。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子任务目标列表：每个元素是一句话说清要做什么、做到什么算完成。",
            },
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "（可选）进一步收窄子任务可写的路径；不传则继承父任务的约束。",
            },
        },
        "required": ["goals"],
    }
    risk = RiskLevel.WRITE

    def __init__(self, spawner: Any) -> None:
        self.spawner = spawner

    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        goals = args.get("goals") or []
        goal_preview = "; ".join(str(g)[:100] for g in goals[:3])
        if len(goals) > 3:
            goal_preview += f" ...（共 {len(goals)} 个）"
        return {
            "action": f"并行派生 {len(goals)} 个子任务并等待全部完成",
            "target": f"子目标：{goal_preview}",
            "impact": (
                f"{len(goals)} 个子任务会以各自的 task_id 并发运行，"
                "使用**同一套工具与权限**，但预算更小；"
                "它们可能读写文件或执行命令，因此会产生与父任务同级的副作用。"
            ),
            "preview": "",
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            spawner = getattr(ctx, "spawner", None) or self.spawner
            if spawner is None:
                return ToolResult.failure(
                    self.name, ErrorKind.NOT_IMPLEMENTED,
                    "当前运行方式没有启用子任务能力（服务端才会装配 spawner）",
                    hint="可以自己用现有工具一步步做，不要假装派生了子任务。",
                    duration_ms=t.ms,
                )

            goals_raw = args.get("goals") or []
            goals = [str(g).strip() for g in goals_raw if str(g).strip()]
            if not goals:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, "goals 不能为空",
                    hint="给出具体的子目标列表，例如 [『读取 docs/ 下的说明并总结三点』, 『检查测试覆盖率』]。",
                    duration_ms=t.ms,
                )

            result = await spawner.spawn_parallel(
                goals=goals,
                parent_task_id=ctx.task_id,
                workspace=ctx.workspace,
                allowed_paths=list(args.get("allowed_paths") or []) or None,
                forbidden_paths=None,
                cancel_event=getattr(ctx, "cancel_event", None),
            )
            data = result.to_dict()

            # 全部被拒绝 → failure
            if result.rejected_count == result.total:
                return ToolResult.failure(
                    self.name, ErrorKind.PERMISSION_DENIED,
                    f"所有 {result.total} 个子任务派生被拒绝",
                    hint="派生层级或数量已达上限；把事情拆给当前任务自己做。",
                    data=data, duration_ms=t.ms,
                )

            # 部分完成 → success（但数据里包含拒绝/超时信息，父 Agent 自行判断）
            summary = (
                f"并行派生 {result.total} 个子任务：完成 {result.done_count}，"
                f"拒绝 {result.rejected_count}，超时 {result.timed_out_count}，"
                f"取消 {result.cancelled_count}，中止 {result.aborted_count}"
            )
            return ToolResult.success(
                self.name, summary,
                data=data, duration_ms=t.ms,
            )


def make(spawner: Any) -> SpawnParallelTool:
    return SpawnParallelTool(spawner)
