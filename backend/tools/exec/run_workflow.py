"""
run_workflow —— 把一个目标拆成**带依赖的多个步骤**，并行推进，等结果齐了再继续。

★ 它解决什么问题
------------
原来的 Agent 是"一步一停"：一个慢操作（等接口、跑长命令）会把整轮推理卡住。
这个工具把步骤交给调度器，**互不依赖的步骤同时跑**，依赖别人的步骤等前置完成，
全部结束后把结构化结果还给模型——模型不需要自己轮询。

★ 与 spawn_subtask 的区别
---------------------
  spawn_subtask  起一个**子 Agent**（有自己的推理、预算、审批）
  run_workflow   起若干**具体操作**（http / 只读工具 / 本机命令），没有推理，只有依赖

要"多个子 Agent 并行"，现版本仍不支持；要"多个操作并行"，用这个。

★ 安全
----
· 整个工具 `risk=exec`，**一次审批覆盖整条工作流**（审批卡片里会列出每个步骤的类型）；
· `shell` 步骤在本机执行，等价于 `terminal`，因此卡片里明确写"会在宿主机跑命令"；
· `tool` 步骤只允许只读工具（read_file / list_files / search_docs）；
· `http` 只允许 GET，禁止内网/回环（防 SSRF），响应有上限、不跟随重定向。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel
from ...workflow import StepSpec, WorkflowError, WorkflowService, WorkflowStore, WorkflowStatus


class RunWorkflowTool(Tool):
    name = "run_workflow"
    description = (
        "把一个目标拆成多个**带依赖**的步骤并发执行，返回每个步骤的结果。"
        "适合：多个互不依赖的查询/文件分析同时做；某个步骤要等外部接口返回时，"
        "先跑另一条不依赖它的分支。步骤类型：http（GET 外部 URL）、tool（只读工具）、"
        "shell（本机命令）。depends_on 声明前置步骤，调度器保证依赖顺序；"
        "互不依赖的步骤会真正并行。全部步骤结束（或失败/超时）后才返回。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "这组步骤要达成的目标（一句话）。"},
            "steps": {
                "type": "array",
                "description": "步骤列表；id 唯一，depends_on 引用其它步骤的 id。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "步骤 id（如 s1）。"},
                        "title": {"type": "string", "description": "一句话说明这步做什么。"},
                        "kind": {"type": "string", "enum": ["http", "tool", "shell"]},
                        "spec": {
                            "type": "object",
                            "description": (
                                "http: {url, method=GET, headers?}；"
                                "tool: {tool: read_file|list_files|search_docs, args:{...}}；"
                                "shell: {command, cwd?, shell?}"
                            ),
                        },
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "timeout_s": {"type": "number", "description": "单步超时（默认 60s）。"},
                    },
                    "required": ["id", "kind", "spec"],
                },
            },
            "deadline_s": {"type": "number", "description": "整条工作流的截止时间（秒，默认 300）。"},
        },
        "required": ["steps"],
    }
    risk = RiskLevel.EXEC

    def __init__(self, store: WorkflowStore, registry: Any, *,
                 allow_local: bool = False, max_parallel: int = 3) -> None:
        self.store = store
        self.registry = registry
        self.allow_local = allow_local
        self.max_parallel = max_parallel

    # ---- 审批展示：把每个步骤摊开给用户看 ----
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        steps = args.get("steps") or []
        lines = []
        for s in steps[:8]:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "?")
            kind = str(s.get("kind") or "?")
            spec = s.get("spec") or {}
            if kind == "shell":
                detail = f"shell: {spec.get('command', '')[:80]}"
            elif kind == "http":
                detail = f"http GET: {spec.get('url', '')[:100]}"
            else:
                detail = f"tool: {spec.get('tool', '')}"
            deps = ",".join(s.get("depends_on") or []) or "-"
            lines.append(f"{sid}（依赖 {deps}）{detail}")
        return {
            "action": f"并行执行 {len(steps)} 个步骤的工作流（最多同时 {self.max_parallel} 个）",
            "target": str(args.get("goal") or "")[:120],
            "impact": (
                "工作流中的 shell 步骤会在**宿主机**上执行命令（与 terminal 相同权限）；"
                "http 步骤只允许 GET 外网地址；tool 步骤只允许只读工具。"
                "本次批准覆盖工作流内全部步骤。"
            ),
            "preview": "\n".join(lines),
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS,
                                      "steps 必须是非空数组",
                                      hint="给出至少一个 {id, kind, spec} 步骤。")
        steps: list[StepSpec] = []
        for i, item in enumerate(raw_steps):
            if not isinstance(item, dict):
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS,
                                          f"第 {i + 1} 个步骤不是对象")
            steps.append(StepSpec(
                id=str(item.get("id") or f"s{i + 1}"),
                title=str(item.get("title") or ""),
                kind=str(item.get("kind") or "tool"),
                spec=dict(item.get("spec") or {}),
                depends_on=[str(d) for d in (item.get("depends_on") or [])],
                timeout_s=float(item.get("timeout_s") or 0),
            ))

        goal = str(args.get("goal") or "").strip() or "工作流"
        deadline = float(args.get("deadline_s") or 0) or 300.0

        # ---- 工具执行器：只读工具走当前 ctx（同一工作区、同一取消信号）----
        async def runner(name: str, sargs: dict[str, Any], workspace: str) -> dict[str, Any]:
            tool = self.registry.get(name) if self.registry is not None else None
            if tool is None:
                return {"ok": False, "error": f"工具不存在：{name}"}
            sub_ctx = dataclasses.replace(ctx, workspace=workspace or ctx.workspace)
            res = await tool.run(sargs, sub_ctx)
            return {"ok": res.ok, "summary": res.summary, "data": res.data,
                    "error": res.error}

        emit = None
        if getattr(ctx, "bus", None) is not None:
            def emit(payload: dict[str, Any]) -> Any:      # noqa: ANN401
                return ctx.bus.workflow_state(payload, turn=ctx.turn)

        service = WorkflowService(
            self.store, tool_runner=runner, allow_local=self.allow_local,
            max_parallel=self.max_parallel,
        )
        with timed() as t:
            try:
                view = await service.run(
                    goal=goal, steps=steps, task_id=ctx.task_id,
                    workspace=ctx.workspace, cancel_event=getattr(ctx, "cancel_event", None),
                    deadline_s=deadline, emit=emit,
                )
            except WorkflowError as exc:
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, str(exc),
                                          hint="检查步骤 id、depends_on 与类型；依赖不能成环。",
                                          duration_ms=t.ms)

        data = view.to_dict()
        done = sum(1 for s in view.steps if s.status.value == "done")
        total = len(view.steps)
        data["elapsed_ms"] = t.ms

        if view.workflow.status is WorkflowStatus.COMPLETED:
            return ToolResult.success(
                self.name,
                f"工作流 {view.workflow.id} 完成：{done}/{total} 步成功（{t.ms}ms）",
                data=data, duration_ms=t.ms,
            )
        bad = [f"{s.step_id}:{s.status.value}" for s in view.steps if s.status.value != "done"]
        return ToolResult.failure(
            self.name, ErrorKind.EXECUTION_ERROR,
            f"工作流 {view.workflow.id} 未全部成功（{view.workflow.status.value}）："
            f"{done}/{total} 步成功；异常步骤 {', '.join(bad[:6])}",
            data=data, duration_ms=t.ms,
            hint="看 data.steps 里各步骤的状态与 error；依赖失败的下游会被标 blocked，不会自动执行。",
        )


def make(store: WorkflowStore, registry: Any, *, allow_local: bool = False,
         max_parallel: int = 3) -> RunWorkflowTool:
    return RunWorkflowTool(store, registry, allow_local=allow_local,
                           max_parallel=max_parallel)
