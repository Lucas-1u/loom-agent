"""
desktop_locate —— Computer-Use 方案2 的"看屏幕"环节。

★ 它做什么：
    capture_screen（截图）→ 视觉模型（vision 通道）→ 解析出结构化坐标 JSON。
    不执行任何点击/输入——纯只读。

★ 输入约定（对模型的提示词）：输出坐标 JSON：
    {"action":"click","x":整数,"y":整数,"reason":"简单理由"}
   坐标以截图原始像素为准（capture_screen 存的 PNG 尺寸，逻辑像素；
   DPI 缩放换算由 desktop_click 内部完成，这里不需要管）；
   找不到则输出 {"action":"none"}，绝不伪造。

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

# 发给视觉模型的图片上限（比各家 API 的硬上限更保守一点，见 _fit_size 的注释）
MAX_MODEL_SIDE = 1280
MAX_MODEL_PIXELS = 1_000_000


def _png_size(data: bytes) -> tuple[int, int] | None:
    """
    从 PNG 头读宽高（纯标准库，不引入 PIL）。

    ★ 为什么还要这一步：capture_screen 现在会回传尺寸，但它解析的是 PowerShell
      的 `OK w h` 输出；万一那句话没打出来（老版本脚本/输出被截断），这里能兜住。
      给视觉模型的坐标基准不能是"大概"——没有尺寸它就只能瞎猜。
    """
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))
    return None


def _fit_size(w: int, h: int, *, max_side: int = MAX_MODEL_SIDE,
              max_pixels: int = MAX_MODEL_PIXELS) -> tuple[int, int, float]:
    """
    算出"该发给视觉模型"的目标尺寸，返回 `(nw, nh, 缩放比)`。

    ★ 为什么必须自己先缩（2026-11-13 事故的直接结论）：
      主流 VLM 的图片入口都有像素上限（Claude 4.6 家族：长边 ≤1568 且 ≤1.15MP），
      **超限的图会被服务端悄悄降采样**。模型是按它实际看到的那张缩小图数格子的，
      而 harness 按原图分辨率去点 —— 坐标空间对不上，就出现"系统性偏移"。
      本机实测：1707×1067 = 1.82MP 超限，两次定位都偏到同一个空白点（约 65px）。
      自己缩到上限内，坐标空间才是确定的、可反算的。

    阈值取得比 Claude 更保守（1280 / 1MP），因为百炼、讯飞等国内入口的上限
    通常比 Anthropic 更低。宁可多缩一点，也不要让服务端替我们缩。
    """
    if w <= 0 or h <= 0:
        return w, h, 1.0
    scale = 1.0
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
    if w * h * scale * scale > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
    if scale >= 1.0:
        return w, h, 1.0
    return max(1, int(w * scale)), max(1, int(h * scale)), scale


def _shrink_png(data: bytes, size: tuple[int, int]) -> bytes | None:
    """
    把 PNG 重编码到目标尺寸；Pillow 不可用/解码失败时返回 None（调用方降级）。

    ★ 降级而不是抛异常：没装 Pillow 的机器上定位功能应该继续能用（只是精度差些），
      不能因为一个可选优化把工具整个搞挂。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 —— 可选依赖，缺了就退回原图
        return None
    import io

    try:
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("RGB").resize(size, Image.LANCZOS)
            buf = io.BytesIO()
            small.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:  # noqa: BLE001 —— 解码失败同样退回原图
        return None


def _parse_coords(text: str) -> dict[str, Any] | None:
    """
    稳健解析坐标：优先看嵌套 JSON，兜底正则抓 {"x":123,"y":456}。

    ★ 必须把 `action` 一起带出来：调用方要判 `action == "none"`（模型说"没找到"）。
      早先这里只返回 x/y，导致那个分支永远不成立，模型说"没有"会被误报成
      "输出格式不合法"——错误信息指向错的地方，排障时要多绕一圈。
    """
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if "x" in data and "y" in data:
                return {"action": str(data.get("action") or "click").lower(),
                        "x": int(data["x"]), "y": int(data["y"]),
                        "reason": str(data.get("reason", ""))}
            if str(data.get("action") or "").lower() == "none":
                return {"action": "none", "reason": str(data.get("reason", ""))}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = _COORD_RE.search(text or "")
    if m:
        return {"action": "click", "x": int(m.group(1)), "y": int(m.group(2)),
                "reason": "正则解析（模型输出非纯 JSON）"}
    return None


class DesktopLocateTool(Tool):
    name = "desktop_locate"
    description = (
        "看屏幕截图，定位某个 UI 元素的像素坐标（纯只读，不执行操作）。"
        "输出为 JSON：{\"action\":\"click\",\"x\":整数,\"y\":整数,\"reason\":\"\"}；"
        "找不到就输出 {\"action\":\"none\"}。坐标以截图原图像素为准。"
        "★ 只用于 UIA 树里没有的元素（画布、游戏画面、H5 渲染区）。"
        "**不要用它找桌面图标**：实测有系统性偏差（连续两次都偏到同一个空白点位约 65px），"
        "点了要么落空、要么误中旁边的程序。启动程序请用 desktop_launch。"
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

            # ---- 2) 视觉模型问答，要求 JSON 坐标（★ 先把图缩到上限内，再告诉它尺寸）----
            width = int((shot.data or {}).get("width") or 0)
            height = int((shot.data or {}).get("height") or 0)
            if width <= 0 or height <= 0:
                wh = _png_size(img_bytes)
                if wh:
                    width, height = wh

            # 发送图（可能被缩小）与它的坐标系。模型只认这张图，坐标按它反算。
            send_bytes, send_w, send_h = img_bytes, width, height
            shrink_note = ""
            if width > 0 and height > 0:
                nw, nh, sc = _fit_size(width, height)
                if sc < 1.0:
                    shrunk = _shrink_png(img_bytes, (nw, nh))
                    if shrunk is not None:
                        send_bytes, send_w, send_h = shrunk, nw, nh
                        shrink_note = f"（原图 {width}×{height} → 发送 {nw}×{nh}）"
                    else:
                        shrink_note = "（Pillow 不可用，按原图发送，可能被服务端降采样）"

            if send_w and send_h:
                size_line = (
                    f"这张截图的像素尺寸是 {send_w}×{send_h}（宽×高）。"
                    f"你输出的坐标必须**以这张 {send_w}×{send_h} 图片的左上角为原点**，"
                    f"取值范围 x∈[0,{send_w - 1}]、y∈[0,{send_h - 1}]。"
                    f"不要按缩放后的显示尺寸估，也不要输出归一化比例。\n"
                )
            else:
                size_line = "坐标请使用截图的原始像素（左上角为原点）。\n"

            prompt = (
                size_line
                + "请在这张截图中找到：「{q}」。\n".format(q=query)
                + "如果它存在，输出 JSON 格式："
                  "{{\"action\":\"click\",\"x\":整数,\"y\":整数,\"reason\":\"简短中文理由\"}}\n"
                  "坐标请给出**目标元素的中心点**；如果找不到它，输出 {{\"action\":\"none\"}}。"
                  "只输出 JSON，不要任何其他文字。"
            )

            try:
                reply = await describe_image(self.config, send_bytes, prompt, system=SYSTEM_PROMPT)
            except VisionError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, str(exc),
                    hint="视觉调用失败：检查 LOOM_VISION_MODEL 配置与额度。",
                )

            coords = _parse_coords(reply.text)
            raw = reply.text.strip().replace("\n", " ")

            if coords is None:
                msg = f"视觉模型没有给出合法坐标 JSON（原始输出：{raw[:300]}）"
                hint = "视觉模型输出不是约定 JSON 或坐标超出范围——先修 prompt，不要假装成功。"
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, msg,
                                          hint=hint, duration_ms=t.ms)
            if coords.get("action") == "none":
                msg = "视觉模型在图里没找到目标元素"
                hint = "换更具体的元素名，或先截一张目标明显的图再取。"
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, msg,
                                          hint=hint, duration_ms=t.ms)

            # ---- 3) 模型坐标系（可能是缩小图）→ 截图原图坐标系 ----
            mx, my = coords["x"], coords["y"]
            rescaled = bool(send_w and send_h and width and height
                            and (send_w, send_h) != (width, height))
            if rescaled:
                x = int(round(mx * width / send_w))
                y = int(round(my * height / send_h))
            else:
                x, y = mx, my

            limit_w, limit_h = (width or 4000), (height or 4000)
            if not (0 <= x < limit_w and 0 <= y < limit_h):
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    f"解析出坐标越界（x={x}, y={y}；截图空间 {limit_w}×{limit_h}）",
                    hint="模型视野问题；把 query 拆细再试一次。",
                    duration_ms=t.ms,
                )

            summary = (
                f"找到目标元素，坐标 ({x},{y})"
                + (f"（模型在 {send_w}×{send_h} 图上给的是 ({mx},{my})，已按比例还原）"
                   if rescaled else "")
                + f"，理由：{coords['reason']}"
            )
            data = {
                "action": "click", "x": x, "y": y,
                "model_x": mx, "model_y": my,
                "model_size": [send_w, send_h],
                "source_size": [width, height],
                "shrink": shrink_note,
                "reason": coords["reason"], "image": img_path,
                "model": reply.model, "used_image": img_bytes[:8].hex(),
                "hint": "坐标为截图原始像素；desktop_click_at 会自动做 DPI 换算，无需手动处理。",
            }
            return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)


def make(config: VisionConfig):
    """工厂：vision 配置可用才注册 desktop_locate。"""
    if not config.usable:
        return None
    return DesktopLocateTool(config)
