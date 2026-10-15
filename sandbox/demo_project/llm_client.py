"""LLM 客户端 —— 封装百炼 API 调用。

注意：这里 verify=False 是历史遗留，新项目不要照抄。
"""

import httpx
from typing import AsyncIterator

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class LLMClient:
    def __init__(self, api_key: str, model: str = "qwen3.7-max-preview"):
        self.api_key = api_key
        self.model = model
        self.timeout = httpx.Timeout(60.0)

    async def chat_stream(self, messages: list) -> AsyncIterator[str]:
        """流式调用。百炼推理模型必须走 stream 才能拿到 reasoning_content。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
            async with client.stream("POST", DASHSCOPE_URL,
                                     json=payload, headers=headers) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield line[6:]

    async def chat(self, messages: list) -> dict:
        """非流式调用，用于工具调用判定。"""
        payload = {"model": self.model, "messages": messages}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
            resp = await client.post(DASHSCOPE_URL, json=payload, headers=headers)
            return resp.json()
