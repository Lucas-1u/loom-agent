"""
vision_look —— 把截图喂给视觉模型，拿回"图里有什么/点哪里"的答案。

★ 它是什么（方案2 的第二块砖）
--------------------------------
输入：一张 PNG（工作区内路径，或省略=自动找最新的截图）+ 一个问题。
输出：视觉模型的文本回答。典型用法是问"界面上这个按钮的坐标在哪里"，
让模型按 JSON 格式回答（坐标体系以截图像素为准）。

★ 依赖 config（Loom 惯例：不注入就不注册）
------------------------------------------
  LLM_VISION_MODEL 环境变量 + BAILIAN_API_KEY。
  未配置时**整个工具不注册**，工具清单与之前完全一致 —— 
  这也是"没有视觉模型时环境照常运行"的实现方式。

★ 安全边界：
  · 只读工具（无副作用），RiskLevel.READ；
  · 读到的图片路径必须在工作区内（realpath 前缀校验，防符号链接逃逸）；
  · 图片字节只进模型请求，不落盘日志；回答原样回传给模型，不被改写。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel
from ...models.vision import describe_image, VisionConfig, VisionError

MAX_IMAGE_BYTES = 30 * 1024 * 1024   # 30MB：截图入模前的体积上限

SYSTEM_PROMPT = (
    "你在帮一个桌面自动化 Agent 看屏幕截图。「看」的目的是准确回答屏幕上的问题，"
    "比如某个控件的像素坐标、界面文字内容、按钮位置关系。"
    "回答坐标时以截图原始像素坐标为准，并给出简短理由。"
)


def _realpath_within(workspace: str, target: str) -> bool:
    """realpath 前缀校验：目标必须在工作区内（解析符号链接防逃逸）。"""
    ws = os.path.realpath(workspace or ".")
    root = ws + os.sep if not ws.endswith(os.sep) else ws
    tp = os.path.realpath(target)
    return tp.startswith(root)


class VisionLookTool(Tool):
    name = "vision_look"
    description = (
        "把一张截图（PNG/JPG）交给视觉模型看，回答你的问题。"
        "典型场景：① 问某个界面元素在屏幕上的像素坐标（让模型输出 JSON "
        "{\"action\":\"click\",\"x\":…,\"y\":…,\"reason\":…} 格式）；"
        "② 问屏幕上显示了什么文字/按钮；③ 找某个图标的位置。"
        "省略 image 参数时自动使用工作区 capture/ 目录里的最新截图"
        "（通常紧跟 capture_screen 之后调用）。"
        "注意：坐标以截图像素为准，不同缩放/多屏下可能需要换算。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": "工作区内图片路径（PNG/JPG；绝对或相对工作区）。省略则用 capture/ 里最新的图。",
            },
            "prompt": {
                "type": "string",
                "description": "要问视觉模型的问题/任务（中文或英文）。问坐标时直接给动作指令，如「主屏幕上『确定』按钮的像素坐标」。",
            },
        },
        "required": ["prompt"],
    }
    risk = RiskLevel.READ

    def __init__(self, config: VisionConfig) -> None:
        self.config = config

    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        if not str((args or {}).get("prompt") or "").strip():
            return "prompt 不能为空"
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            workspace = getattr(ctx, "workspace", "") or os.getcwd()

            # ---- 定位图片 -------------------------------------------------
            image_arg = str((args or {}).get("image") or "").strip()
            if image_arg:
                path = image_arg if os.path.isabs(image_arg) else os.path.join(workspace, image_arg)
                if not os.path.isfile(path):
                    return ToolResult.failure(
                        self.name, ErrorKind.NOT_FOUND, f"图片不存在：{image_arg}",
                        hint="先跑 capture_screen，或检查路径。",
                    )
                if not _realpath_within(workspace, path):
                    return ToolResult.failure(
                        self.name, ErrorKind.PERMISSION_DENIED,
                        "图片路径指向工作区之外（安全限制）",
                        hint="图片必须位于工作区内；必要时先拷入工作区。",
                    )
            else:
                capture_dir = os.path.join(workspace, "capture")
                files = []
                if os.path.isdir(capture_dir):
                    files = [os.path.join(capture_dir, f) for f in os.listdir(capture_dir)
                             if f.lower().endswith((".png", ".jpg", ".jpeg"))]
                files.sort(key=os.path.getmtime, reverse=True)
                if not files:
                    return ToolResult.failure(
                        self.name, ErrorKind.NOT_FOUND, "没有可用截图",
                        hint="先跑 capture_screen 截一张，再问视觉模型。",
                    )
                path = files[0]

            with open(path, "rb") as fh:
                image_bytes = fh.read()
            if not image_bytes:
                return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, "图片文件为空")
            if len(image_bytes) > MAX_IMAGE_BYTES:
                return ToolResult.failure(
                    self.name, ErrorKind.TOO_LARGE,
                    f"图片过大（{len(image_bytes)} 字节 > {MAX_IMAGE_BYTES}）",
                    hint="截图分辨率太高；用小窗口截图或压缩后重试。",
                )

            prompt = str((args or {}).get("prompt") or "").strip()
            try:
                reply = await describe_image(self.config, image_bytes, prompt, system=SYSTEM_PROMPT)
            except VisionError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, str(exc),
                    hint="视觉模型不可用：确认 LOOM_VISION_MODEL / BAILIAN_API_KEY 配置与额度。",
                )

            size_kb = len(image_bytes) // 1024
            return ToolResult.success(
                self.name,
                f"视觉模型回答（{reply.model}，{reply.prompt_tokens}+{reply.completion_tokens} tokens）：\n{reply.text}",
                data={
                    "image": path, "model": reply.model,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "image_kb": size_kb, "hint": "坐标以截图像素为准。",
                },
                duration_ms=t.ms,
            )


def make(config: VisionConfig):
    """工厂：usable 时返回工具；不可用返回 None（注册表不注册）。"""
    if not config.usable:
        return None
    return VisionLookTool(config)
