"""
视觉模型通道（方案2 的地基：截图 → VLM）。

★ 它是什么、不是什么
--------------------
是：一个**最小 OpenAI 兼容图像对话客户端**。输入 PNG 字节 + 提示词，
调用视觉模型（默认百炼 qwen3-vl-plus，可配置）拿回一段文本回答
（通常是 UI 元素坐标或自然语言描述）。

不是：完整的 computer-use loop。动作规划与执行不属于这里 ——
方案2 的"看截图 → 点哪里"闭环在 Agent 的工具链里完成：
    capture_screen（截图）→ vision_look（问 VLM）→ agent-desktop MCP（执行点击）
本模块只负责"喂图收答"这段，保持单一职责。

★ 为什么独立于 OpenAICompatAdapter
----------------------------------
文本主适配器的 Message 契约里没有图像内容块。视觉调用需要
`content: [{type:"image_url", ...}]` 这种混合消息，塞进现有 Message
契约会污染模型层。独立模块的边界：自带 config、自带错误、
不 import agent/（保持 models/ 的方向铁律）。

★ 启用条件（Loom 惯例：不注入就不注册）
--------------------------------------
`backend/tools/readonly/vision_query.py` 只在 VisionConfig.usable
（配置了 LOOM_VISION_MODEL + BAILIAN_API_KEY）时注册为 vision_look 工具。
没配置视觉模型时，工具清单与之前完全一致。
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("loom.vision")


class VisionError(Exception):
    """视觉调用失败（调用方可 catch 后转成 ToolResult 的失败形状）。"""


@dataclass
class VisionReply:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


@dataclass
class VisionConfig:
    model: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    timeout_s: float = 60.0
    temperature: float = 0.2

    @classmethod
    def from_env(cls, _os=None) -> "VisionConfig":
        import os as _os_mod

        os_mod = _os or _os_mod
        return cls(
            model=os_mod.environ.get("LOOM_VISION_MODEL", "").strip(),
            base_url=os_mod.environ.get("LOOM_VISION_BASE_URL",
                                        "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            api_key=os_mod.environ.get("BAILIAN_API_KEY", "").strip(),
        )

    @property
    def usable(self) -> bool:
        """只有显式配置了视觉模型 + 有 API Key 才算可用。"""
        return bool(self.model) and bool(self.api_key)


async def describe_image(cfg: VisionConfig, image_bytes: bytes, prompt: str,
                         *, system: str = "") -> VisionReply:
    """单次图像对话：一张图 + 一句问题 → 模型文本回答。

    ★ 调用约定：调用方负责把坐标提示词组织清楚（见 vision_look 工具的描述）。
      JSON {"x":…,"y":…,"action":"click","reason":…} 是界面控件定位的推荐输出格式，
      各家模板字段不一，模型能力优先，不在这里做强解析。
    """
    if not cfg.usable:
        raise VisionError("视觉模型未配置：检查环境变量 LOOM_VISION_MODEL / BAILIAN_API_KEY")
    if not image_bytes:
        raise VisionError("image 字节为空")
    if not prompt.strip():
        raise VisionError("prompt 不能为空（视觉模型至少要知道它在看什么）")

    from openai import AsyncOpenAI  # 延迟 import：没有视觉配置的机器上不引入 SDK 开销

    client = AsyncOpenAI(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=2,
    )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages: list[dict[str, Any]] = []
    if system.strip():
        messages.append({"role": "system", "content": system.strip()})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    })

    t0 = time.time()
    try:
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=messages,
            temperature=cfg.temperature,
        )
    except Exception as exc:  # noqa: BLE001 —— 统一转成 VisionError
        name = type(exc).__name__
        raise VisionError(f"视觉模型调用失败（{name}）：{str(exc)[:300]}") from exc

    choice = getattr(resp, "choices", None) and resp.choices[0]
    if choice is None:
        raise VisionError("视觉模型返回不含 choices")
    msg = getattr(choice, "message", None)
    text = (getattr(msg, "content", "") or "") if msg is not None else ""
    return VisionReply(
        text=text,
        model=getattr(resp, "model", cfg.model) or cfg.model,
        prompt_tokens=int(getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0),
        duration_ms=int((time.time() - t0) * 1000),
    )
