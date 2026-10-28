"""
spawn_subtask —— 父 Agent 派生一个子任务（M5，首版串行）。

★ 它不是"另一个执行框架"
--------------------
这个工具只是把一个**普通的 AgentCore 任务**跑起来并等它结束：
子任务有自己的 task_id、自己的事件、自己的审批、自己的（更小的）预算，
但用的是同一套 ToolRegistry / Policy / 审批 / 闸门 / 审计。
父 Agent 拿到的不是"子 Agent 的说法"，而是一个**可追溯的结果信封**。

★ 为什么 risk=WRITE（要审批）
------------------------
派生子任务本身不改文件，但它马上会去改。按"副作用不明确就按保守处理"的原则，
首版一律要求人工审批；审批被拒绝时子任务**根本不会被创建**（可证伪测试覆盖了这点）。

★ 结果信封里有什么
------------------
task_id / status / end_reason / summary / evidence（复用既有证据分级）
/ tools / commands / changes —— 父任务据此判断"能不能采纳"，
而不是因为子任务说"已完成"就相信。
"""

from __future__ import annotations

from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel


class SpawnSubtaskTool(Tool):
    name = "spawn_subtask"
    description = (
        "派生一个**子任务**去完成一个明确的子目标，并**等待它结束**后返回结果。"
        "适合把一件可以独立验收的事（例如：读某个目录并汇报、改一个小地方并跑测试）交给子 Agent。"
        "返回内容包含子任务 ID、终态、结果摘要、以及**证据**（命令退出码、文件改动、证据等级）——"
        "请依据证据判断是否采纳，不要因为子任务自称完成就相信。"
        "约束：串行执行；每层任务的子任务数量与派生层级都有上限；子任务不能绕过审批与预算。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "子任务目标：一句话说清要做什么、做到什么算完成。",
            },
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "（可选）进一步收窄子任务可写的路径；不传则继承父任务的约束。",
            },
        },
        "required": ["goal"],
    }
    risk = RiskLevel.WRITE

    def __init__(self, spawner: Any) -> None:
        self.spawner = spawner

    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        goal = str(args.get("goal") or "")[:200]
        return {
            "action": "派生一个子任务并等待它执行完毕",
            "target": f"子目标：{goal}",
            "impact": (
                "子任务会以自己的 task_id 运行，使用**同一套工具与权限**，"
                "但预算更小；它可能读写文件或执行命令，因此会产生与父任务同级的副作用。"
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

            goal = str(args.get("goal") or "").strip()
            if not goal:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, "goal 不能为空",
                    hint="给出具体的子目标，例如『读取 docs/ 下的说明并总结三点』。",
                    duration_ms=t.ms,
                )

            result = await spawner.spawn(
                goal=goal,
                parent_task_id=ctx.task_id,
                workspace=ctx.workspace,
                allowed_paths=list(args.get("allowed_paths") or []) or None,
                forbidden_paths=None,
                cancel_event=getattr(ctx, "cancel_event", None),
            )
            data = result.to_dict()

            if result.status == "rejected":
                return ToolResult.failure(
                    self.name, ErrorKind.PERMISSION_DENIED, result.error,
                    hint="派生层级或数量已达上限（防止无限派生）；把事情拆给当前任务自己做。",
                    data=data, duration_ms=t.ms,
                )

            if not result.ok:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"子任务未完成：{result.status}/{result.end_reason or 'unknown'}"
                    + ("（超时）" if result.timed_out else "")
                    + ("（被取消）" if result.cancelled else ""),
                    hint="不要把它当成已验证结果；可以看 evidence 决定重试、换方法或如实上报。",
                    data=data, duration_ms=t.ms,
                )

            return ToolResult.success(
                self.name,
                f"子任务 {result.task_id} 完成（{result.status}，{result.turn} 轮）：{result.summary[:200]}",
                data=data, duration_ms=t.ms,
            )


def make(spawner: Any) -> SpawnSubtaskTool:
    return SpawnSubtaskTool(spawner)
