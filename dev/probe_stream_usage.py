"""探针：流式端点认不认 `stream_options.include_usage`。

背景：`_call_model` 默认走真流式（chat_stream_collect）。OpenAI 兼容接口的流式
响应**默认不带 usage**，要显式请求。但国内几家是否实现这个字段不确定 ——
这个实验决定 token 计量是"精确"还是"只能字符估算"。

判据：
  · 加 stream_options 后最后一帧带 usage      → 精确计量可行
  · 请求 400/报错                              → 不支持该字段
  · 正常返回但始终没有 usage                   → 静默忽略，同样只能估算

跑法：./.venv/Scripts/python.exe dev/probe_stream_usage.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

PROMPT = "用一句话介绍你自己。"


async def probe(provider: str, model: str, base_url: str, api_key: str,
                with_options: bool) -> dict:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=40, max_retries=0)
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "max_tokens": 40,
    }
    if with_options:
        payload["stream_options"] = {"include_usage": True}

    usage = None
    chunks = 0
    text = 0
    try:
        stream = await client.chat.completions.create(**payload)
        async for chunk in stream:
            chunks += 1
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = {
                    "prompt": getattr(u, "prompt_tokens", None),
                    "completion": getattr(u, "completion_tokens", None),
                }
            ch = getattr(chunk, "choices", None)
            if ch:
                d = getattr(ch[0], "delta", None)
                if d is not None and getattr(d, "content", None):
                    text += len(d.content)
        return {"ok": True, "chunks": chunks, "usage": usage, "chars": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await client.close()


async def main() -> int:
    # ★ 不用 ModelConfig.from_env：它会被 LOOM_MODEL 覆盖，
    #   结果 bailian 也拿着 spark-x2.6 的模型名去请求（404）。
    #   这里显式指定各自端点的模型，避免交叉污染。
    wanted = [
        ("xfyun", os.environ.get("LOOM_MODEL", "spark-x2.6"),
         "https://maas-api.cn-huabei-1.xf-yun.com/v2", os.environ.get("XFYUN_API_KEY", "")),
        ("bailian", os.environ.get("LOOM_VISION_MODEL", "qwen3.7-plus"),
         "https://dashscope.aliyuncs.com/compatible-mode/v1", os.environ.get("BAILIAN_API_KEY", "")),
    ]
    targets = [(p, m, u, k) for p, m, u, k in wanted if k]

    if not targets:
        print("[FAIL] 没有任何可用的 API Key")
        return 1

    print("=" * 74)
    for provider, model, base_url, key in targets:
        print(f"\n### {provider}  model={model}\n    {base_url}")
        base = await probe(provider, model, base_url, key, with_options=False)
        print(f"  1) 不加 stream_options      → {base}")

        opt = await probe(provider, model, base_url, key, with_options=True)
        print(f"  2) 加 include_usage         → {opt}")

        if opt.get("ok") and opt.get("usage"):
            print(f"  ✅ {provider}：支持精确 usage")
        elif not opt.get("ok") and "400" in str(opt.get("error", "")):
            print(f"  ❌ {provider}：直接拒绝该字段（400）")
        elif opt.get("ok") and base.get("usage"):
            print(f"  ⚠️  {provider}：不加也有 usage（说明它默认就返回）")
        else:
            print(f"  ⚠️  {provider}：接受请求但不返回 usage → 只能估算")
    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
