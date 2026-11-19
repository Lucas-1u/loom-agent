"""探针：当前主模型端点接不接受 image_url 输入。

背景：要把主对话做成多模态（图片进上下文），前提是模型路由支持。
讯飞 MaaS 的 spark-x2.6 是不是 VL 模型不确定，先打一枪。

判据：
  · 200 且回答里提到了图里的内容      → 支持（可以开）
  · 400 / 报错提到 image / content    → 不支持（只能做开关 + 降级）
  · 200 但明显在瞎编                  → 接受但没真看（等于不支持）

跑法：./.venv/Scripts/python.exe dev/probe_vision_input.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

# 用一张极小的图：纯色方块 + 一个明显的特征（左上角红块），好判断模型有没有真看
def _make_png() -> tuple[bytes, str]:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (240, 120), (245, 245, 240))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 70, 70], fill=(220, 40, 40))       # 左上角红块
    d.rectangle([170, 50, 230, 110], fill=(40, 90, 220))  # 右下角蓝块
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue(), "一张 240x120 的图：左上角有一个红色方块，右下角有一个蓝色方块，其余是浅色背景"


async def main() -> int:
    from openai import AsyncOpenAI

    base = os.environ.get("LOOM_VISION_BASE_URL") or "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    key = os.environ.get("XFYUN_API_KEY", "")
    model = os.environ.get("LOOM_MODEL", "spark-x2.6")
    if not key:
        print("[FAIL] 没有 XFYUN_API_KEY")
        return 1

    png, truth = _make_png()
    b64 = base64.b64encode(png).decode("ascii")
    print(f"模型 = {model}  base_url = {base}\n图 = {len(png)} 字节\n真值 = {truth}\n")

    client = AsyncOpenAI(api_key=key, base_url=base, timeout=60, max_retries=0)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图里有什么？用一句话描述颜色和位置。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 120,
    }
    try:
        resp = await client.chat.completions.create(**payload)
        msg = resp.choices[0].message
        text = (getattr(msg, "content", "") or "").strip()
        print(f"[200] 回复：{text[:300]}")

        # 粗判：回答里有没有红/蓝这两个关键信息
        hit = ("红" in text or "red" in text.lower()) and ("蓝" in text or "blue" in text.lower())
        print("\n判定：")
        if hit:
            print("  ✅ 端点接受图片，且模型真的看到了内容（红/蓝都提到）")
        else:
            print("  ⚠️  端点接受请求，但没答出图里的关键信息 —— 大概率没真看图（等于不支持）")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] {type(exc).__name__}: {str(exc)[:400]}")
        low = str(exc).lower()
        if "image" in low or "content" in low or "400" in low or "支持" in low:
            print("\n判定：❌ 端点不接受图片输入（或格式不符）")
        else:
            print("\n判定：❓ 错误与图片无关，需排查其它原因")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
