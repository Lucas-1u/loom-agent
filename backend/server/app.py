"""
FastAPI 应用

★ 职责边界
────────────────────────────────────────────────────────────────
  这一层**只做协议转换**：HTTP / JSON / SSE ↔ Python 对象。
  不含任何 Agent 逻辑、不判断权限、不碰工具。

★ SSE 的两层 id（复习）
────────────────────────────────────────────────────────────────
  `id:` 行        → hub 的全局递增号，浏览器重连时自动放进 Last-Event-ID
  `data.event.seq` → 任务内序号，前端用它排序 / 去重
  两者都要给，职责不同。

★ 慢客户端
────────────────────────────────────────────────────────────────
  每个订阅者独立队列（`Subscriber`），发布方用 `put_nowait` —— **永不阻塞 Agent**。
  队列满了丢最旧的、保留最新的，并置 `need_gap`；
  生成器下一轮先推 `stream.gap`，明确告诉客户端"中间丢了"。绝不静默。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .hub import EventHub
from .scheduler import TaskScheduler
from .store import TaskStore

log = logging.getLogger("loom.server.app")

SSE_KEEPALIVE_S = 15.0


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class CreateSessionBody(BaseModel):
    title: str = ""
    workspace: str = ""


class UpdateSessionBody(BaseModel):
    """PATCH /api/sessions/{id}：只允许改标题，以及**还没有任务时**改工作目录。"""

    title: str | None = None
    workspace: str | None = None


class CreateTaskBody(BaseModel):
    goal: str = Field(min_length=1)
    session_id: str
    workspace: str = ""
    # 改动范围声明（可选，相对工作区）。默认只允许整个工作区、不禁任何路径。
    # forbidden 里的路径会在 Policy 层直接拦下；allowed 只在事后审计里标记。
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)


class ApprovalBody(BaseModel):
    task_id: str
    decision: str = Field(pattern="^(approved|rejected)$")
    reason: str = ""


# --------------------------------------------------------------------------- #
# SSE 帧
# --------------------------------------------------------------------------- #
def _norm_ws(raw: str | None) -> str:
    """
    比较"两个工作目录是不是同一个"用的规范化键。

    ★ 只处理分隔符 / 尾部分隔符 / 大小写 —— **不解析符号链接、不要求目录存在**。
      这里判的是"两次请求指的是不是同一个目录"，不是安全边界；
      真正的路径安全在 `tools/path_safety.resolve_in_workspace`。
    """
    s = (raw or "").strip().replace("/", "\\")
    if len(s) > 3:          # 保留 `C:\` 这种盘符根
        s = s.rstrip("\\")
    return s.lower()


def sse_frame(event_type: str, data: dict[str, Any], *, eid: int | None = None) -> str:
    """
    拼一个 SSE 帧。

    `eid=None` 时不写 `id:` 行 —— 用于 stream.gap 这类控制帧，
    避免污染浏览器的 Last-Event-ID（否则重连会从 gap 帧的位置开始补发）。
    """
    payload = json.dumps(data, ensure_ascii=False, default=str)
    head = f"id: {eid}\n" if eid is not None else ""
    return f"{head}event: {event_type}\ndata: {payload}\n\n"


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #
def _sandbox_info(sandbox: Any) -> dict[str, Any] | None:
    """
    沙箱状态（给 /api/health 用）。

    用鸭子类型而不是 import SandboxManager：这一层只做协议转换，
    不该依赖具体实现。没装配沙箱时返回 None。
    """
    if sandbox is None:
        return None
    status = sandbox.cached_status() if hasattr(sandbox, "cached_status") else None
    return status.to_dict() if status is not None else None


def create_app(
    *,
    store: TaskStore,
    hub: EventHub,
    scheduler: TaskScheduler,
    provider: str = "",
    model: str = "",
    sandbox: Any = None,
    rag: dict[str, Any] | None = None,
    mcp: dict[str, Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="Loom Agent Server", version="0.1.0")

    # 前端从 Vite 的 5173 访问 8765，需要 CORS。
    # ★ 只放开本机来源 —— 这是本机 Agent，不该让任意网页调用。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.state.store = store
    app.state.hub = hub
    app.state.scheduler = scheduler

    # ===================================================================== #
    # 健康检查
    # ===================================================================== #
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """
        前端启动时先打这个：
          200 → 真实模式
          失败 → 演示模式（前端保留 mock 并提示"后端未启动"）
        """
        return {
            "ok": True,
            "version": "0.1.0",
            # 开发阶段标识（M3 收尾时校准；前端不用它做分支判断）
            "phase": "M3",
            "provider": provider,
            "model": model,
            "time": time.time(),
            "scheduler": scheduler.stats(),
            "hub": hub.stats(),
            # 命令沙箱状态：available=false 时 run_command 会被明确拒绝
            # （不会降级到宿主机执行）。null 表示当前进程没有装配沙箱。
            "sandbox": _sandbox_info(sandbox),
            # M3：项目知识检索（enabled/available/db/method）—— 前端据此提示"检索不可用"
            "rag": rag,
            # M4.1：MCP Server 状态（id/state/tools/error）—— 不含 env 值
            "mcp": mcp,
        }

    # ===================================================================== #
    # Session
    # ===================================================================== #
    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        rows = await store.list_sessions()
        return {"sessions": rows}

    @app.post("/api/sessions")
    async def create_session(body: CreateSessionBody) -> dict[str, Any]:
        now = time.time()
        # ★ 会话身份必须独立生成。
        #   旧实现是 `s_{毫秒}_{会话数}`：两个请求落在同一毫秒、且都读到同样的
        #   会话数时，会算出**同一个 id**；upsert 的 ON CONFLICT 会把先建的那个
        #   会话的标题/工作目录覆盖掉 —— 这就是"会话串线"的一个入口。
        sid = f"s_{uuid.uuid4().hex[:12]}"
        row = {
            "session_id": sid,
            "title": body.title or "新建会话",
            "workspace": body.workspace,
            "created_at": now,
            "updated_at": now,
        }
        await store.upsert_session(row)
        return row

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, body: UpdateSessionBody) -> dict[str, Any]:
        """
        改会话的标题 / 工作目录。

        ★ 工作目录只有在**该会话还没有任何任务**时才允许改。
          理由：会话归属是历史事实。允许事后改挂目录，就会让同一个会话里
          混着两个项目的内容（真实踩过的坑），而历史任务无法判断该信哪个目录。
          要换目录 → 新建会话。
        """
        sess = await store.get_session(session_id)
        if sess is None:
            raise HTTPException(404, f"会话不存在：{session_id}")

        new_ws = sess["workspace"]
        if body.workspace is not None and _norm_ws(body.workspace) != _norm_ws(sess["workspace"]):
            n = await store.count_tasks(session_id)
            if n > 0:
                raise HTTPException(
                    409,
                    detail={
                        "accepted": False,
                        "reason": "session_has_tasks",
                        "note": (
                            f"该会话已有 {n} 个任务，工作目录固定为 {sess['workspace'] or '（未设置）'}，"
                            "不能再改。要换目录请新建会话。"
                        ),
                        "session_workspace": sess["workspace"],
                    },
                )
            new_ws = body.workspace

        row = {
            **sess,
            "title": sess["title"] if body.title is None else body.title,
            "workspace": new_ws,
            "updated_at": time.time(),
        }
        await store.upsert_session(row)
        return row

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str) -> dict[str, Any]:
        """
        完整可读对话：从该会话的所有任务派生。

        每个任务两条：user(goal) + assistant(result)。
        ★ 不是只给最后一个 final_answer —— 多轮对话要能整段还原。
        """
        tasks = await store.list_tasks(session_id)
        msgs: list[dict[str, Any]] = []
        for t in tasks:
            msgs.append(
                {
                    "message_id": f"m_{t['task_id']}_user",
                    "session_id": session_id,
                    "task_id": t["task_id"],
                    "role": "user",
                    "content": t["goal"],
                    "created_at": t["started_at"] or t["created_at"],
                }
            )
            if t.get("result"):
                msgs.append(
                    {
                        "message_id": f"m_{t['task_id']}_ai",
                        "session_id": session_id,
                        "task_id": t["task_id"],
                        "role": "assistant",
                        "content": t["result"],
                        "created_at": t.get("ended_at") or t["created_at"],
                    }
                )
        return {"session_id": session_id, "messages": msgs}

    # ===================================================================== #
    # Task
    # ===================================================================== #
    @app.get("/api/tasks")
    async def list_tasks(session_id: str | None = Query(default=None)) -> dict[str, Any]:
        return {"tasks": await scheduler.list_tasks(session_id)}

    @app.post("/api/tasks")
    async def create_task(
        body: CreateTaskBody,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    ) -> dict[str, Any]:
        """
        创建任务。

        ★ 幂等键：优先 `Idempotency-Key`，其次 `X-Request-Id`，
          再其次 body 里没有就没法幂等（老客户端）。
          命中 → 返回原任务，`idempotent_replay=true`。

        ★ 工作目录**以会话为准**：任务不允许跑到会话之外的目录去。
          旧前端把"切换工作目录"实现成只改本地字段，于是同一个会话里
          混进了两个项目的任务 —— 用户在界面上看到的就是"串线"。
        """
        sess = await store.get_session(body.session_id)
        if sess is None:
            raise HTTPException(404, f"会话不存在：{body.session_id}")

        session_ws = sess["workspace"] or ""
        if session_ws and body.workspace and _norm_ws(session_ws) != _norm_ws(body.workspace):
            raise HTTPException(
                409,
                detail={
                    "accepted": False,
                    "reason": "workspace_mismatch",
                    "note": (
                        f"会话已绑定工作目录 {session_ws}，不能在这里用 {body.workspace} 建任务。"
                        "要换目录请新建会话。"
                    ),
                    "session_workspace": session_ws,
                },
            )
        workspace = session_ws or body.workspace
        if not session_ws and body.workspace:
            # 老数据兼容：会话还没绑目录（历史上允许空），首次建任务时补绑，
            # 之后它就固定下来了 —— 不静默改写已有归属。
            await store.upsert_session({**sess, "workspace": body.workspace, "updated_at": time.time()})

        key = idempotency_key or x_request_id or ""
        snap = await scheduler.submit(
            session_id=body.session_id,
            goal=body.goal,
            workspace=workspace,
            idem_key=key,
            allowed_paths=body.allowed_paths or None,
            forbidden_paths=body.forbidden_paths or None,
        )
        return snap

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        snap = await scheduler.describe(task_id)
        if snap is None:
            raise HTTPException(404, f"任务不存在：{task_id}")
        return snap

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        since: int | None = Query(default=None, description="只返回 id 大于它的记录"),
    ) -> dict[str, Any]:
        """
        任务的历史事件（HTTP 拉取，非 SSE）。

        ★ 三种"不可用"要明确区分，前端据此显示不同文案：
            available=false, reason=evicted      → 缓冲淘汰了
            available=false, reason=unknown_task → 任务不存在
            available=true                       → 正常
          服务重启后 runtime 没了、hub 也没了 → evicted / empty
        """
        snap = await scheduler.describe(task_id)
        if snap is None:
            raise HTTPException(404, f"任务不存在：{task_id}")

        recs = hub.buffered_for_task(task_id)
        if since is not None:
            recs = [r for r in recs if r["id"] > since]

        if not recs:
            return {
                "task_id": task_id,
                "available": False,
                "reason": "evicted" if hub.evicted else "empty",
                "note": "事件历史不在内存缓冲里（服务重启过，或已被容量上限淘汰）。"
                        "任务结论仍在快照的 result 字段中。",
                "events": [],
            }

        return {
            "task_id": task_id,
            "available": True,
            "reason": "",
            "events": [r for r in recs],
        }

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """
        请求取消。

        ★ `accepted=true` 只代表**请求被接受**，任务还没停。
          最终是否停下要看 `status` 是否变成 `aborted`。
        """
        return await scheduler.cancel(task_id)

    # ===================================================================== #
    # 审批
    # ===================================================================== #
    @app.post("/api/approvals/{approval_id}/decision")
    async def approval_decision(approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        """
        提交审批决策。

        ★ 必须带 `task_id` —— 两个任务可能同时在等审批，
          只靠 approval_id 无法保证投到正确的任务上（虽然 id 唯一，
          但显式校验能挡住前端状态错乱导致的误投）。
        """
        result = await scheduler.resolve_approval(
            task_id=body.task_id,
            approval_id=approval_id,
            decision=body.decision,
            reason=body.reason,
        )
        if not result["accepted"]:
            # 409：状态冲突（已决策 / 已超时 / 不属于该任务）
            raise HTTPException(409, detail=result)
        return result

    # ===================================================================== #
    # SSE
    # ===================================================================== #
    @app.get("/api/stream")
    async def stream(
        request: Request,
        since: int | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """
        全局事件流。

        补发优先级：`?since=` > `Last-Event-ID` 头 > 不补发。

        ★ 不补发（全新连接）时客户端应自己调 `/api/tasks` 拉快照 ——
          比从 1 开始补发更快，也不会被"已淘汰"卡住。
        """
        cursor: int | None
        if since is not None:
            cursor = since
        elif last_event_id:
            try:
                cursor = int(last_event_id)
            except ValueError:
                cursor = None
        else:
            cursor = None

        sub = hub.subscribe()

        async def gen() -> AsyncIterator[str]:
            try:
                # ---- 1. 补发 ----
                replay, gap = hub.replay(cursor)
                if gap is not None:
                    yield sse_frame("stream.gap", gap["data"])

                for rec in replay:
                    yield sse_frame(
                        rec["event"]["type"], rec, eid=rec["id"]
                    )

                # ---- 2. 实时 ----
                while True:
                    if await request.is_disconnected():
                        break

                    # 慢客户端丢过事件 → 先明确告知
                    if sub.need_gap:
                        sub.need_gap = False
                        yield sse_frame(
                            "stream.gap",
                            {
                                "type": "stream.gap",
                                "data": {
                                    "reason": "slow_client",
                                    "dropped": sub.dropped,
                                    "latest": hub.latest_id,
                                    "note": "客户端消费过慢，中间事件已被丢弃；"
                                            "请调快照接口重新同步。",
                                },
                            },
                        )

                    try:
                        payload = await asyncio.wait_for(
                            sub.queue.get(), timeout=SSE_KEEPALIVE_S
                        )
                    except asyncio.TimeoutError:
                        # 心跳注释帧：防止中间层把空闲连接掐掉
                        yield ": keepalive\n\n"
                        continue

                    yield sse_frame(
                        payload["event"]["type"], payload, eid=payload["id"]
                    )

            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 断连是常态，不该刷异常栈
                log.debug("SSE 流结束", exc_info=True)
            finally:
                hub.unsubscribe(sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",     # 让 nginx 之类不要缓冲
            },
        )

    return app
