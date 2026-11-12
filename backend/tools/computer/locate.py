"""
desktop_locate —— Computer-Use 方案2 的"看屏幕"环节。

★ 它做什么：
    capture_screen（截图）→ 视觉模型（vision 通道）→ 解析出结构化坐标 JSON。
    不执行任何点击/输入——纯只读。

★ 输入约定（对模型的提示词）：输出坐标 JSON：
    {"action":"click","x":整数,"y":整数,"reason":"简单理由"}
   坐标以截图原始像素为准（2560×1600 物理像素）；找不到则输出 {"action":"none"}，
   绝不伪造。

★ 安全边界：
    · 只读（不按键、不移动鼠标）；
    · 图片读进内存直接发给模型，不落盘日志；
    · vision 未配置时工厂返回 None → 该工具不注册（不注入就不注册）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..readonly.capture_screen import make as make_capture
from ..readonly.vision_query import SYSTEM_PROMPT
from ...agent.state import RiskLevel
from ...models.vision import VisionConfig, VisionError, describe_image


_COORD_RE = re.compile(r'"x"\s*:\s*(\d{1,4})\s*,\s*"y"\s*:\s*(\d{1,4})', re.IGNORECASE)


def _parse_coords(text: str) -> dict[str, Any] | None:
    """稳健解析坐标：优先看嵌套 JSON，兜底正则抓 {"x":123,"y":456}。"""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "x" in data and "y" in data:
            return {"x": int(data["x"]), "y": int(data["y"]),
                    "reason": str(data.get("reason", ""))}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = _COORD_RE.search(text or "")
    if m:
        return {"x": int(m.group(1)), "y": int(m.group(2)), "reason": "正则解析（模型输出非纯 JSON）"}
    return None


class DesktopLocateTool(Tool):
    name = "desktop_locate"
    description = (
        "看屏幕截图，定位某个 UI 元素的像素坐标（纯只读，不执行操作）。"
        "例如「找到左侧边栏的『新建任务』按钮坐标」。"
        "输出为 JSON：{\"action\":\"click\",\"x\":整数,\"y\":整数,\"reason\":\"\"}；"
        "找不到就输出 {\"action\":\"none\"}。坐标以截图原图像素为准。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要找的元素描述，如「屏幕左下角的开始按钮」「右侧边栏里『新建任务』文字的按钮」。",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.READ

    def __init__(self, config: VisionConfig) -> None:
        self.config = config

    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        if not str((args or {}).get("query") or "").strip():
            return "query 不能为空"
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            ws = getattr(ctx, "workspace", "") or os.getcwd()
            query = str((args or {}).get("query") or "").strip()

            # ---- 1) 先截一张图（在拿到模型之前，图片不能直接隐式取最新快照）----
            cap = make_capture()
            ctx_ns = type("Ctx", (), {"workspace": ws})()
            shot = await cap.run({"subdir": "capture"}, ctx_ns)
            if not shot.ok or not shot.data:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    (shot.error or {}).get("message") if isinstance(shot.error, dict)
                    else str(shot.error),
                    hint="capture 失败，无法定位；请先检查桌面分辨率/截图权限。",
                )
            img_path = shot.data["path"]
            with open(img_path, "rb") as fh:
                img_bytes = fh.read()

            # ---- 2) 视觉模型问答，要求 JSON 坐标 ----
            prompt = (
                "请在这张截图中找到：「{q}」。\n"
                "如果它存在，输出 JSON 格式：{{\"action\":\"click\",\"x\":整数,\"y\":整数,\"reason\":\"简短中文理由\"}}\n"
                "坐标请使用截图原始像素坐标；如果找不到它，输出 {{\"action\":\"none\"}}。"
                "只输出 JSON，不要任何其他文字。"
            ).format(q=query)

            try:
                reply = await describe_image(self.config, img_bytes, prompt, system=SYSTEM_PROMPT)
            except VisionError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, str(exc),
                    hint="视觉调用失败：检查 LOOM_VISION_MODEL 配置与额度。",
                )

            coords = _parse_coords(reply.text)
            raw = reply.text.strip().replace("\n", " ")

            if coords is None:
                action = "error"
                msg = f"视觉模型没有给出合法坐标 JSON（原始输出：{raw[:300]}）"
                hint = "视觉模型输出不是约定 JSON 或坐标超出范围——先修 prompt，不要假装成功。"
                payload = {"action": action, "reason": msg}
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, msg,
                                          hint=hint, duration_ms=t.ms)
            if coords.get("action") == "none":
                action = "error"
                msg = "视觉模型在图里没找到目标元素"
                hint = "换更具体的元素名，或先截一张目标明显的图再取。"
                payload = {"action": action, "reason": msg}
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, msg,
                                          hint=hint, duration_ms=t.ms)
            # 找得到 → 输出结构化坐标
            x, y = coords["x"], coords["y"]
            if not (0 <= x < 4000 and 0 <= y < 4000):
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    f"解析出坐标越界（x={x}, y={y}，屏幕 2560×1600）",
                    hint="模型视野问题；把 query 拆细再试一次。",
                    duration_ms=t.ms,
                )
            summary = f"找到目标元素，坐标 ({x},{y})，理由：{coords['reason']}"
            data = {
                "action": "click", "x": x, "y": y,
                "reason": coords["reason"], "image": img_path,
                "model": reply.model, "used_image": img_bytes[:8].hex(),
                "hint": "坐标为截图原始像素；若要用桌面坐标执行点击，需按 DPI 换算。",
            }
            return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)


def make(config: VisionConfig):
    """工厂：vision 配置可用才注册 desktop_locate。"""
    if not config.usable:
        return None
    return DesktopLocateTool(config)
