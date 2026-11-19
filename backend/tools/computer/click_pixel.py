"""
desktop_click —— Computer-Use 方案2 的"动手"环节。

★ 它做什么：把 desktop_locate 给出来的像素坐标变成一次真实鼠标点击，
   通过 agent-desktop 的 MCP 执行（desktop_mouse_click，xy 字符串参数）。

★ 坐标空间（★ 关键，之前踩过坑）：
    · desktop_locate 给的是**截图原始像素**——capture_screen 存的 PNG 尺寸，
      在 150% DPI 缩放的屏上是逻辑像素（实测 1707×1067）；
    · agent-desktop 的 desktop_mouse_click 要的是**物理像素**——它自己
      list-displays 返回的 bounds（实测 2560×1600）；
    · 两者差一个 DPI 缩放比（physical / logical，实测 1.5）。
    · 本工具负责这个换算：输入按截图像素理解，乘 scale 转成物理像素再点，
      **模型不需要自己算 DPI**——之前模型就是卡在 DPI 换算上烧光了预算。

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

# 物理像素地板（兜底值）：agent-desktop 的 desktop_mouse_click 在物理坐标系工作。
# 真实物理分辨率优先从 list-displays 读，读不到才用这个兜底。
FLOOR_W, FLOOR_H = 2560, 1600
CLICK_TOOL = "desktop_mouse_click"
DISPLAYS_TOOL = "desktop_list_displays"


class DesktopClickTool(Tool):
    # ★ 名字里必须带 `_at`：agent-desktop 原生的 `desktop_click` 收的是 `ref_id`
    #   （按 UIA 元素点，点元素中心），本工具收的是 (x, y) 截图像素坐标。
    #   两者同名时模型几乎必然选错那个要坐标的——坐标里有视觉误差，点偏过。
    name = "desktop_click_at"
    description = (
        "按**屏幕像素坐标**执行一次真实鼠标点击（配合 desktop_locate 的坐标）。"
        "输入的 (x, y) 是**截图原始像素**（desktop_locate 返回的坐标）；"
        "本工具会自动按屏幕 DPI 缩放换算成物理像素再点，你不需要自己换算。"
        "★ 只用于 UIA 树里没有的元素（画布 / 游戏画面 / H5 渲染区）。"
        "如果目标在 UIA 树里有 ref（`desktop_snapshot`/`desktop_find` 能看到），"
        "请改用 `mcp__agent_desktop__desktop_click` 传 ref_id —— 点元素中心，更准且不受缩放影响。"
        "失败（超时/断连/不可用）会如实返回失败——点没点到绝不假装成功。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "点击 X 坐标（截图像素，来自 desktop_locate）。"},
            "y": {"type": "integer", "description": "点击 Y 坐标（截图像素，来自 desktop_locate）。"},
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
        self._bounds: tuple[int, int] | None = None   # 物理分辨率 (w, h)，惰性获取缓存
        self._scale: float | None = None              # DPI 缩放比，惰性获取缓存

    def _has_desktop(self) -> bool:
        """MCP 里有没有 desktop_mouse_click（未连接/没发现→没有）。"""
        return any(t.name == CLICK_TOOL for t in getattr(self.mcp, "tools", lambda: [])())

    async def _get_display(self) -> tuple[tuple[int, int], float]:
        """
        拿主显示器的 (物理分辨率, DPI 缩放比)，惰性获取并缓存。

        从 agent-desktop 自己的 list-displays 读——它最清楚自己的坐标系。
        避免每次点击都多一次 MCP 往返。读不到就保守回退到 (2560×1600, 1.0)。
        """
        if self._bounds is not None and self._scale is not None:
            return self._bounds, self._scale
        bounds = (FLOOR_W, FLOOR_H)
        scale = 1.0
        try:
            outcome = await self.mcp.call(
                self.server_id, DISPLAYS_TOOL, {}, timeout=15.0)
            if outcome.ok and outcome.structured:
                data = outcome.structured.get("data") or []
                pick = None
                for d in data:
                    if d.get("is_primary"):
                        pick = d
                        break
                if pick is None and data:
                    pick = data[0]
                if pick is not None:
                    b = pick.get("bounds") or {}
                    bw = int(b.get("width") or 0)
                    bh = int(b.get("height") or 0)
                    if bw > 0 and bh > 0:
                        bounds = (bw, bh)
                    sc = pick.get("scale")
                    if sc and float(sc) > 0:
                        scale = float(sc)
        except Exception:  # noqa: BLE001 —— 拿不到就回退，不因查询失败而拒绝点击
            pass
        self._bounds = bounds
        self._scale = scale
        return bounds, scale

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

            if not self._has_desktop():
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "desktop_click 需要的 agent-desktop 未连接或工具未注册",
                    hint="检查 LOOM_MCP 与 mcp_servers.json 配置；必要时重启服务。",
                )

            # ---- 截图像素 → 物理像素（DPI 缩放换算）----
            (pw, ph), scale = await self._get_display()
            px = int(round(x * scale))
            py = int(round(y * scale))

            if not (0 <= px < pw and 0 <= py < ph):
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    f"换算后的物理坐标越界（{px},{py}）；物理屏幕空间 {pw}×{ph}"
                    f"（缩放比 {scale}，输入截图坐标 {x},{y}）",
                    hint=f"截图坐标乘 {scale} 后超出屏幕，说明 locate 给的坐标不对，重新 locate。",
                )

            call_args: dict[str, Any] = {
                "xy": f"{px},{py}",
                "button": str((args or {}).get("button") or "left"),
                "count": int((args or {}).get("count") or 1),
            }
            outcome = await self.mcp.call(self.server_id, CLICK_TOOL, call_args, timeout=30.0)

            if outcome.ok:
                summary = (f"点击成功（截图坐标 {x},{y} → 物理坐标 {px},{py}，"
                           f"scale={scale}）；耗时 {t.ms}ms")
                data = dict(getattr(outcome, "structured", None) or {})
                data.setdefault("x", x)
                data.setdefault("y", y)
                data.setdefault("physical_x", px)
                data.setdefault("physical_y", py)
                data.setdefault("scale", scale)
                return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)
            return ToolResult.failure(
                self.name, ErrorKind.EXECUTION_ERROR,
                outcome.error or "desktop_click 执行失败（点击未确认送达）",
                hint="可能只是桌面目标不在预期窗口内——重新 locate 后再试。",
                data={"outcome_kind": outcome.kind, "x": x, "y": y,
                      "physical_x": px, "physical_y": py},
                duration_ms=t.ms,
            )


def make(mcp: Any, *, server_id: str = "agent_desktop"):
    """工厂：MCP 有桌面工具才注册 desktop_click。"""
    tools = list(getattr(mcp, "tools", lambda: [])())
    if not any(t.name == CLICK_TOOL for t in tools):
        return None
    return DesktopClickTool(mcp, server_id=server_id)
