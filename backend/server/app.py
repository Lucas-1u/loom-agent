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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .archive import EventArchive
from .hub import EventHub
from .scheduler import TaskScheduler
from .store import StoreError, TaskStore

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
    # 步骤 6：权限模式（read-only / workspace-write / full-access / desktop）
    # desktop = 可操控桌面模式（完整权限 + 桌面工具 + 桌面操作协议）
    # 空 = 默认 workspace-write；非法值在 core.run 里抛 ValueError → 任务 failed
    mode: str = ""
    # 对比表 #21：联网搜索开关。True = 该任务允许 Agent 调用 web_search 工具；
    # False（默认）= 注册表里不注册 web_search，模型看不到、也调不到（服务端真强制）。
    web_search: bool = False


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
    workflow_store: Any = None,
    workflow_info: dict[str, Any] | None = None,
    web_search: dict[str, Any] | None = None,
    skills: dict[str, Any] | None = None,
    archive: EventArchive | None = None,
    retention_days: int = 0,
) -> FastAPI:
    """
    ★ `archive` / `retention_days`（对比表 #49）：
      传了就把事件流落盘能力接上 —— lifespan 里启动后台刷盘、退出前 flush；
      `/api/tasks/{id}/events` 改成"内存优先、磁盘兜底"。
      传 None 时行为完全回到改造前（自检里就有这么用的用例）。
    """
    # 保留期在闭包里先落成局部值：下面的 prune 接口参数会遮蔽同名变量
    default_retention = int(retention_days or 0)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # ★ 必须在这里 start：后台刷盘 task 要绑在**真正 serving 的那个 loop** 上。
        #   在 __main__ 里 asyncio.create_task 出来的 task，uvicorn 一换 loop 就废了。
        if archive is not None:
            await archive.start()
        try:
            yield
        finally:
            if archive is not None:
                # 正常退出路径：把队列里剩下的写完（kill -9 除外，见 archive.py）
                try:
                    await archive.stop()
                except Exception:  # noqa: BLE001 —— 退出阶段别再抛
                    log.exception("事件归档停止失败")

    app = FastAPI(title="Loom Agent Server", version="0.1.0", lifespan=_lifespan)

    # 前端从 Vite 的 5173 访问 8765，需要 CORS。
    # ★ 只放开本机来源 —— 这是本机 Agent，不该让任意网页调用。
    #
    # ★ 为什么要带 tauri.localhost（2026-11-11 补）：
    #   打包成桌面应用后，WebView 的 origin **不再是 localhost:5173**，而是
    #   Tauri 自定义协议域 —— Windows 上是 `http://tauri.localhost`
    #   （开了 `app.useHttpsScheme` 则是 https；v1 时代还有 `tauri://localhost`）。
    #   少了这一条，`tauri dev` 一切正常（origin 是 5173），但**一打包就连不上后端**：
    #   所有 /api 请求和 SSE 都会被 Chromium 当跨域拦掉，界面表现为"后端未启动"。
    #
    # ★ 这为什么没放宽安全边界：浏览器里网页**无法伪造** Origin 头，
    #   恶意站点仍然是 `https://evil.com` → 照旧被拒。能发出 `tauri.localhost`
    #   的只有本机的 Tauri 应用，而本机进程本来就能直连 127.0.0.1:8765。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"(https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?)"
            r"|(tauri://localhost)"
        ),
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
            # M6：异步工作流（enabled / 恢复摘要）
            "workflow": workflow_info or {"enabled": False},
            # 对比表 #21：联网搜索（enabled=总开关 / available=真的能搜）。
            # 前端据此决定"联网搜索"开关是否可点 —— 不能让用户勾了却什么都搜不到。
            "web_search": web_search or {"enabled": False, "available": False},
            # 对比表 #24：技能。count 只算**全局层**（用户级/自定义/内置），
            # 项目级 `.loom/skills` 是每个工作区自己的，随任务加载、不在这里汇总。
            "skills": skills or {"enabled": False},
            # 对比表 #49：事件落盘。written/failed/pending 让"有没有在正常落盘"
            # 一眼可见 —— 写失败必须能看出来，不能默默少一半历史。
            "events": {
                **(archive.stats() if archive is not None else {"enabled": False}),
                "retention_days": retention_days,
            },
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
            mode=body.mode,
            web_search=body.web_search,
        )
        return snap

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        snap = await scheduler.describe(task_id)
        if snap is None:
            raise HTTPException(404, f"任务不存在：{task_id}")
        return snap

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        """工作流快照（步骤/依赖/状态）。前端时间线是主视图，这个接口用于刷新与排障。"""
        if workflow_store is None:
            raise HTTPException(status_code=503, detail="workflow 未启用")
        view = workflow_store.get(workflow_id)
        if view is None:
            raise HTTPException(status_code=404, detail=f"没有这个工作流：{workflow_id}")
        return {"ok": True, **view.to_dict()}

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        since: int | None = Query(default=None, description="只返回 id 大于它的记录"),
    ) -> dict[str, Any]:
        """
        任务的事件历史（HTTP 拉取，非 SSE）。

        ★ 从 2026-11-11（对比表 #49）起，事件是**落盘**的：
          内存缓冲优先（最新、未被截断）→ 磁盘兜底（重启后、被淘汰后）。
          两侧按全局 id 合并去重，所以「实时 + 历史」不会重也不会缺。

        ★ 「不可用」必须说清是哪一种，前端据此显示不同文案：
            available=false, reason=pruned   → 过了保留期被清掉了
            available=false, reason=empty    → 这个任务从来没落过盘（老库 / 关闭了归档）
            available=true,  source=memory|disk|hybrid
          绝不把"拿不到"说成"没有"。
        """
        snap = await scheduler.describe(task_id)
        if snap is None:
            raise HTTPException(404, f"任务不存在：{task_id}")

        # ---- ① 内存：最近的热数据 ----
        mem = hub.buffered_for_task(task_id)
        if since is not None:
            mem = [r for r in mem if r["id"] > since]

        # ---- ② 磁盘：冷数据 ----
        disk: list[dict[str, Any]] = []
        archived = 0
        if archive is not None:
            try:
                # 读之前先刷一次：刚发生的事件可能还在队列里（最多晚 250ms）
                await archive.flush()
                events = await store.load_events(task_id, since_id=since)
            except Exception:  # noqa: BLE001 —— 磁盘读失败不能让实时历史也拿不到
                log.exception("读取落盘事件失败：%s", task_id)
                events = []
            archived = len(events)
            session_id = str(snap.get("session_id") or "")
            disk = [
                {
                    "id": e["id"],
                    "task_id": e["task_id"],
                    "session_id": session_id,
                    "size": e.get("size", 0),
                    "event": {
                        "seq": e["seq"],
                        "ts": e["ts"],
                        "task_id": e["task_id"],
                        "turn": e["turn"],
                        "type": e["type"],
                        "data": e["data"],
                    },
                }
                for e in events
            ]

        # ---- ③ 合并：同 id 以内存为准（内存里是完整 data，磁盘只是副本）----
        merged: dict[int, dict[str, Any]] = {r["id"]: r for r in disk}
        for r in mem:
            merged[r["id"]] = r
        recs = [merged[k] for k in sorted(merged)]

        if not recs:
            if since is not None:
                # 客户端只是"没有更新的事件"，不是"历史丢了"
                return {"task_id": task_id, "available": True, "reason": "",
                        "source": "none", "events": [], "note": "没有更新的事件"}
            reason = "empty"
            note = "这个任务没有落盘的事件（老库未迁移，或 LOOM_EVENTS=0 关闭了归档）。"
            if archive is not None and archive.enabled and retention_days > 0:
                created = float(snap.get("created_at") or 0)
                if created and created < time.time() - retention_days * 86400:
                    reason = "pruned"
                    note = f"事件已超过保留期（{retention_days} 天）被清理；任务结论仍在快照里。"
            return {"task_id": task_id, "available": False, "reason": reason,
                    "source": "none", "note": note, "events": []}

        source = "memory" if not disk else ("disk" if not mem else "hybrid")
        return {
            "task_id": task_id,
            "available": True,
            "reason": "",
            "source": source,
            "archived": archived,
            "events": recs,
        }

    # ===================================================================== #
    # 导出 / 导入 / 保留策略（对比表 #49）
    # ===================================================================== #
    @app.get("/api/tasks/{task_id}/export")
    async def export_task(task_id: str) -> dict[str, Any]:
        """
        导出一个任务的全套材料（行 + 状态快照 + 消息历史 + 事件流）。

        ★ 用途：把一次失败的运行原样发给别人复现 —— 光有结论没有过程是没用的。
        ★ 文件里带 `format`，导入端据此拒绝不认识的格式。
        """
        data = await store.export_task(task_id)
        if data is None:
            raise HTTPException(404, f"任务不存在：{task_id}")
        return data

    @app.post("/api/tasks/import")
    async def import_task(
        payload: dict[str, Any] = Body(...),
        task_id: str = Query(default="", description="指定新 id（默认沿用导出文件里的）"),
    ) -> dict[str, Any]:
        """
        导入 `GET /api/tasks/{id}/export` 的产物。

        ★ task_id 撞车时自动改名（导入不是覆盖），返回里的 `renamed` 会说清楚。
        ★ 事件 id 会重新编号 —— 它是**全局**游标，两个库混一起时旧 id 可能重复。
        """
        try:
            return await store.import_task(payload, task_id=task_id)
        except StoreError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/maintenance/prune")
    async def prune_events(
        days: int | None = Query(default=None, description="留几天，0=不删"),
    ) -> dict[str, Any]:
        """
        手动触发事件清理（服务启动时也会按配置自动跑一次）。

        ★ 只删"已终态且创建时间早于截止点"的任务的事件：
          运行中/排队中的任务事件还要用（续跑要接 seq）。
        ★ 参数名不能叫 retention_days —— 会遮蔽外层闭包里的同名配置值，
          于是"不传参数"永远落进 None 分支（这里踩过）。
        """
        keep = default_retention if days is None else int(days)
        if keep <= 0:
            return {"deleted": 0, "retention_days": 0,
                    "note": "保留期为 0（永不清理），没有删除任何事件"}
        cutoff = time.time() - keep * 86400
        deleted = await store.prune_events(cutoff)
        return {"deleted": deleted, "retention_days": keep, "cutoff": cutoff}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """
        请求取消。

        ★ `accepted=true` 只代表**请求被接受**，任务还没停。
          最终是否停下要看 `status` 是否变成 `aborted`。
        """
        return await scheduler.cancel(task_id)

    # ===================================================================== #
    # Checkpoint 恢复（2026-11-11）
    # ===================================================================== #
    @app.get("/api/tasks/{task_id}/resume-check")
    async def resume_check(task_id: str) -> dict[str, Any]:
        """
        只读探测：这个任务能不能续跑？（不改状态、不入队）

        返回 `report`：safe_to_resume 由真实判据得出 ——
        checkpoint 是否存在、工作区是否一致、历史有没有工具结果缺口、
        预算是否耗尽、快照是否过旧。
        """
        plan, report = await scheduler.inspect_resume(task_id)
        out = report.to_dict()
        out["resumable"] = plan is not None
        return out

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        """
        从最近 checkpoint 续跑。

        ★ `accepted=true` 只代表"已排入恢复队列"，不代表已跑完；
          最终结果看后续事件与任务状态。
        """
        return await scheduler.resume(task_id)

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
    # 询问用户（ask_user）
    # ===================================================================== #
    @app.post("/api/tasks/{task_id}/user-input")
    async def user_input_answer(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """
        提交用户对 ask_user 问题的回答。

        body（新）: {"ask_id": "...", "answers": [{"id": "...", "selected": ["A"], "custom": ""}]}
        body（旧）: {"ask_id": "...", "answer": "一段自由文本"}   ← 兼容，转成 custom

        ★ `selected` 为空且没有 `custom` = 用户明确跳过该问题（不是没提交）。
        """
        ask_id = str(body.get("ask_id") or "").strip()
        if not ask_id:
            raise HTTPException(400, detail={"reason": "ask_id_required"})

        raw_answers = body.get("answers")
        answers: list[dict[str, Any]] = []

        if isinstance(raw_answers, list) and raw_answers:
            for item in raw_answers:
                if not isinstance(item, dict):
                    raise HTTPException(400, detail={"reason": "answers_item_must_be_object"})
                qid = str(item.get("id") or "").strip()
                if not qid:
                    raise HTTPException(400, detail={"reason": "answers_item_id_required"})
                selected = item.get("selected") or []
                if not isinstance(selected, list):
                    raise HTTPException(400, detail={"reason": "selected_must_be_array"})
                answer_item: dict[str, Any] = {
                    "id": qid,
                    "selected": [str(s) for s in selected],
                }
                custom = str(item.get("custom") or "").strip()
                if custom:
                    answer_item["custom"] = custom
                answers.append(answer_item)
        else:
            # ---- 旧格式：单个自由文本回答 ----
            text = str(body.get("answer") or "").strip()
            if not text:
                raise HTTPException(400, detail={"reason": "answer_required"})
            answers = [{"id": str(body.get("question_id") or "q1"),
                        "selected": [], "custom": text}]

        result = await scheduler.resolve_user_input(
            task_id=task_id, ask_id=ask_id, answers=answers,
        )
        if not result["accepted"]:
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
