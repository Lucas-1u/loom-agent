"""小智 AI 聊天机器人 —— 后端服务入口。

FastAPI + SSE 架构，提供流式对话与工具调用能力。
"""

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from .services.llm_client import LLMClient
from .services.stream_handler import StreamHandler
from .services.tool_registry import REGISTRY
from .db import database

app = FastAPI(title="小智 AI", version="1.0.0")

llm = LLMClient()
handler = StreamHandler(llm, REGISTRY)


@app.get("/health")
async def health():
    """健康检查，供 Nginx 与部署脚本探测。"""
    return {"status": "ok", "model": llm.model}


@app.post("/api/chat")
async def chat(request: Request):
    """主对话入口，走 SSE 流式返回。"""
    body = await request.json()
    session_id = body.get("session_id", "default")
    message = body.get("message", "")

    async def event_stream():
        async for chunk in handler.run(session_id, message):
            yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/sessions")
async def list_sessions():
    return {"sessions": await database.list_sessions()}


app.mount("/", StaticFiles(directory="frontend", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
