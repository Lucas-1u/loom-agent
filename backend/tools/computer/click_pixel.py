"""
desktop_click —— Computer-Use 方案2 的"动手"环节。

★ 它做什么：把 desktop_locate 给出来的像素坐标变成一次真实鼠标点击，
   通过 agent-desktop 的 MCP 执行（desktop_mouse_click，xy 字符串参数）。

★ 依赖注入：
    · 需要 MCP manager（agent-desktop 在线）；
    · mcp_servers.json 里 agent-desktop 要带 `--headed`（像素点击需要物理指针）；
    · 未注入/未连接 → 工厂返回 None → 工具不注册。

★ 风险：EXEC（物理动作，与 run_command/terminal 同级）。read-only 意图拦截只拦
    WRITE，不拦 EXEC——这是 Loom 策略层对"命令类只读时"的既有取舍（见 policy ⑤b2），
    桌面点击不在命令内，真实执行边界由 full-access 模式 + MCP trust 配置承担。

★ 验证语义：返回 MCP envelope 的 data.action/post_state；不成功 → 失败，不伪装。
"""

from __future__ import annotations

from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel

# 截图物理像素：2560×1600（desktop_screenshot 元数据），点击坐标必须落在该空间
FLOOR_W, FLOOR_H = 2560, 1600
CLICK_TOOL = "desktop_mouse_click"


class DesktopClickTool(Tool):
    name = "desktop_click"
    description = (
        "在屏幕像素位置执行一次真实鼠标点击（配合 desktop_locate 的坐标）。"
        "坐标 (x, y) 来自截图原始像素（2560×1600 物理坐标系）。"
        "内部经过 agent-desktop 的 desktop_mouse_click。"
        "失败（超时/断连/不可用）会如实返回失败——点没点到绝不假装成功。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "点击 X 坐标（0-2559）。"},
            "y": {"type": "integer", "description": "点击 Y 坐标（0-1599）。"},
            "button": {"type": "string", "enum": ["left", "right", "middle"],
                       "description": "鼠标按键，默认 left。"},
            "count": {"type": "integer", "description": "点击次数，默认 1。"},
        },
        "required": ["x", "y"],
    }
    risk = RiskLevel.EXEC

    def __init__(self, mcp: Any, *, server_id: str = "agent_desktop") -> None:
        self.mcp = mcp
        self.server_id = server_id

    def _has_desktop(self) -> bool:
        """MCP 里有没有 desktop_mouse_click（未连接/没发现→没有）。"""
        return any(t.name == CLICK_TOOL for t in getattr(self.mcp, "tools", lambda: [])())

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            raw_x, raw_y = (args or {}).get("x"), (args or {}).get("y")
            try:
                x, y = int(raw_x), int(raw_y)
            except (TypeError, ValueError):
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    "x/y 必须是整数像素坐标",
                    hint="用 desktop_locate 拿坐标，不要自己拍脑袋。",
                )
            if not (0 <= x < FLOOR_W and 0 <= y < FLOOR_H):
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    f"坐标越界（x={x}, y={y}）；截图空间 {FLOOR_W}×{FLOOR_H}",
                    hint="屏幕是 2560×1600；坐标必须落在这个范围里。",
                )

            if not self._has_desktop():
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "desktop_click 需要的 agent-desktop 未连接或工具未注册",
                    hint="检查 LOOM_MCP 与 mcp_servers.json 配置；必要时重启服务。",
                )

            call_args: dict[str, Any] = {
                "xy": f"{x},{y}",
                "button": str((args or {}).get("button") or "left"),
                "count": int((args or {}).get("count") or 1),
            }
            outcome = await self.mcp.call(self.server_id, CLICK_TOOL, call_args, timeout=30.0)

            if outcome.ok:
                summary = f"点击成功 (x={x}, y={y})；耗时 {t.ms}ms"
                data = dict(getattr(outcome, "structured", None) or {})
                data.setdefault("x", x)
                data.setdefault("y", y)
                return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)
            return ToolResult.failure(
                self.name, ErrorKind.EXECUTION_ERROR,
                outcome.error or "desktop_click 执行失败（点击未确认送达）",
                hint="可能只是桌面目标不在预期窗口内——重新 locate 后再试。",
                data={"outcome_kind": outcome.kind, "x": x, "y": y},
                duration_ms=t.ms,
            )


def make(mcp: Any, *, server_id: str = "agent_desktop"):
    """工厂：MCP 有桌面工具才注册 desktop_click。"""
    tools = list(getattr(mcp, "tools", lambda: [])())
    if not any(t.name == CLICK_TOOL for t in tools):
        return None
    return DesktopClickTool(mcp, server_id=server_id)
