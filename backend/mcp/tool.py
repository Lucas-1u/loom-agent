"""
MCP 工具 → Loom 工具 的适配层（M4.1）。

★ 命名规则
----------
    mcp__<server_id>__<tool_name>         例：mcp__demo__read_note

为什么带前缀：不同 Server 可能有同名工具，内置工具也可能叫 `read_file`。
前缀保证**唯一且可追溯**；但来源追踪不靠字符串解析 —— 每个工具实例都带
`source = {"kind": "mcp", "server": ..., "tool": ...}`，事件与审批都用这份元数据。

★ 风险等级
----------
**默认 WRITE = 需要人工审批**。只有配置文件里显式写 `trust: "read"` 或把工具列进
`read_tools`，才会被当作只读。工具自己返回的描述/schema 是**不可信输入**，
不参与风险判定（Server 说"我很安全"不等于安全）。

★ 结果归一化
----------
    MCP: {content:[{type:text,...}], structuredContent, isError}
    Loom: ToolResult(ok, summary, data, error{kind,message,hint})

失败要分得清：超时 → TIMEOUT；Server 不可用/断开 → EXECUTION_ERROR（消息里写原因）；
Server 明确返回业务错误 → EXECUTION_ERROR + `is_error=True` 标记；
无法安全转换的内容 → 失败，不伪装成成功。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..agent.state import RiskLevel
from ..tools.base import ErrorKind, Tool, ToolContext, ToolResult, timed
from .client import CallOutcome, DiscoveredTool, McpManager

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_]+")
MAX_NAME_LEN = 64


def qualify(server_id: str, tool: str) -> str:
    """生成注册名：稳定、可追溯、不超长。"""
    safe_tool = _UNSAFE_NAME.sub("_", tool).strip("_") or "tool"
    name = f"mcp__{server_id}__{safe_tool}"
    if len(name) <= MAX_NAME_LEN:
        return name
    keep = MAX_NAME_LEN - len(f"mcp__{server_id}__")
    return f"mcp__{server_id}__{safe_tool[:keep]}"


def ensure_object_schema(schema: dict[str, Any], *, tool: str) -> dict[str, Any]:
    """
    校验并原样返回 inputSchema。

    ★ 不做"宽松化"：缺 type / 不是 object 一律拒绝注册，
      而不是补一个 {"type":"object"} 让模型瞎猜参数。
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"工具「{tool}」的 inputSchema 不是 object，拒绝注册（避免放宽成任意参数）")
    props = schema.get("properties", {})
    if props is not None and not isinstance(props, dict):
        raise ValueError(f"工具「{tool}」的 inputSchema.properties 不是对象，拒绝注册")
    required = schema.get("required", [])
    if required and not isinstance(required, list):
        raise ValueError(f"工具「{tool}」的 inputSchema.required 不是数组，拒绝注册")
    return schema


def _redact_args(args: dict[str, Any]) -> str:
    """审批预览里的参数：像密钥的键打码，整体截断。"""
    def mask(d: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in d.items():
            upper = str(k).upper()
            if any(h in upper for h in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
                out[k] = "***"
            elif isinstance(v, dict):
                out[k] = mask(v)
            else:
                out[k] = v
        return out

    try:
        text = json.dumps(mask(args or {}), ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return text[:600]


class McpTool(Tool):
    """把 Server 的一个工具包装成 Loom 的 Tool。"""

    def __init__(self, manager: McpManager, spec: DiscoveredTool, *, risk: RiskLevel) -> None:
        self.manager = manager
        self.spec = spec
        self.server_id = spec.server_id
        self.original_name = spec.name
        self.name = qualify(spec.server_id, spec.name)
        desc = (spec.description or "").strip().replace("\n", " ")
        self.description = f"[MCP·{spec.server_id}] {desc[:300] or spec.name}"
        self.parameters = ensure_object_schema(spec.input_schema, tool=spec.name)
        # ★ 风险等级**只**由配置决定，不读 Server 给的 annotations
        self.risk = risk
        self.source = dict(spec.source)

    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        return {
            "action": f"调用外部 MCP 工具「{self.original_name}」（Server：{self.server_id}）",
            "target": f"{self.server_id} / {self.original_name}",
            "impact": (
                "MCP Server 是运行在本机的独立进程，拥有与 Loom 相同的操作系统权限；"
                "Loom 不对它做沙箱隔离，也无法保证它只做描述里说的事。"
            ),
            "preview": _redact_args(args),
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # ★ 任务取消：只在"还没发出去"时生效。已经发出去的调用没法安全中断
        #   （SDK 没有取消 in-flight 工具调用的接口），但至少不会在取消后再发起新的副作用。
        cancel = getattr(ctx, "cancel_event", None)
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            return ToolResult.failure(
                self.name, ErrorKind.EXECUTION_ERROR, "任务已取消，MCP 工具调用未执行",
                hint="不要重试这个调用；等用户重新发起任务。",
            )
        with timed() as t:
            outcome = await self.manager.call(
                self.server_id, self.original_name, args or {}, timeout=None
            )
            return self._to_result(outcome, t.ms)

    # ---- 结果归一化 --------------------------------------------------------
    def _to_result(self, outcome: CallOutcome, duration_ms: int) -> ToolResult:
        data = {
            "source": self.source,
            "content_types": outcome.raw_types,
            "structured": outcome.structured,
            "truncated": outcome.truncated,
        }
        if outcome.kind == "timeout":
            return ToolResult.failure(
                self.name, ErrorKind.TIMEOUT, outcome.error or "MCP 工具调用超时",
                hint="不要盲目重试（可能有副作用）；换个思路或告诉用户 Server 无响应。",
                data=data, duration_ms=duration_ms,
            )
        if outcome.kind in ("unavailable", "disconnected", "protocol"):
            return ToolResult.failure(
                self.name, ErrorKind.EXECUTION_ERROR, outcome.error or "MCP Server 不可用",
                hint="这个工具当前不可用，不要重复调用；在答复里说明 Server 状态。",
                data=data, duration_ms=duration_ms,
            )
        if outcome.is_error:
            # Server 说"这次调用是错误"：原样回传，不当作成功
            text = outcome.text or "（MCP 工具返回错误，且没有文字说明）"
            data["is_error"] = True
            return ToolResult.failure(
                self.name, ErrorKind.EXECUTION_ERROR, text,
                hint="这是 Server 返回的业务错误，按内容调整参数或换方法。",
                data=data, duration_ms=duration_ms,
            )
        summary = outcome.text or "（MCP 工具成功返回，但没有文本内容）"
        if len(summary) > 400:
            summary = summary[:400] + "…"
        return ToolResult.success(self.name, summary, data=data, duration_ms=duration_ms)


def register_mcp_tools(registry: Any, manager: McpManager) -> dict[str, Any]:
    """
    把已发现的 MCP 工具注册进 ToolRegistry。

    返回一份报告：注册了哪些、跳过了哪些（名字冲突 / schema 不合法），
    供启动日志与健康检查使用 —— 跳过必须**可见**，不能静默消失。
    """
    report: dict[str, Any] = {"registered": [], "skipped": [], "servers": []}
    for spec in manager.tools():
        risk = manager.risk_for(spec.server_id, spec.name) or RiskLevel.WRITE
        try:
            tool = McpTool(manager, spec, risk=risk)
        except ValueError as exc:
            report["skipped"].append({"server": spec.server_id, "tool": spec.name,
                                      "reason": str(exc)})
            continue

        if registry.get(tool.name) is not None:
            report["skipped"].append({"server": spec.server_id, "tool": spec.name,
                                      "reason": f"工具名冲突：{tool.name}"})
            continue
        try:
            registry.register(tool, group="mcp")
        except Exception as exc:  # noqa: BLE001 —— 注册失败不能拖垮启动
            report["skipped"].append({"server": spec.server_id, "tool": spec.name,
                                      "reason": f"{type(exc).__name__}: {exc}"})
            continue
        report["registered"].append({"name": tool.name, "server": spec.server_id,
                                     "tool": spec.name, "risk": risk.value,
                                     "qualified": tool.name})
    report["servers"] = [s for s in manager.status().get("servers", [])]
    return report
