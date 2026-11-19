"""
`python -m backend.server`

启动流程（顺序很重要）：

    1. 读配置（模型 / 端口 / 数据库路径）
    2. **先检测端口** —— 冲突就立刻退出，不要等 uvicorn 报一堆栈
    3. 初始化 SQLite（建表 / 校验 schema 版本）
    4. **孤儿任务恢复** —— 把上次遗留的 running/awaiting_approval 标成 failed
       必须在接受新任务之前做完，否则前端会看到"永远在跑"的僵尸任务
    5. 装配 hub / scheduler / app
    6. uvicorn 跑起来

★ 数据库路径
    LOOM_DB 环境变量可覆盖，默认 `loom/sandbox/loom.db`。
    放 sandbox/ 下是因为它本来就是"运行期产物"，且已被 .gitignore 覆盖。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time

import uvicorn

from ..agent.core import AgentConfig
from ..config import PROJECT_ROOT, Config, load_dotenv_if_present
from ..models.openai_compat import OpenAICompatAdapter
from ..sandbox import SandboxManager
from ..agent.rag import KnowledgeBase
from ..tools.registry import build_default_registry
from .app import create_app
from .archive import EventArchive
from .hub import EventHub
from .scheduler import TaskScheduler
from .store import StoreError, TaskStore

log = logging.getLogger("loom.server")


def port_available(host: str, port: int) -> bool:
    """试绑一下。比让 uvicorn 去撞更早、更清楚。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend.server", description="Loom Agent HTTP 服务")
    # ★ 默认空字符串 = 交给 .env 的 LOOM_DEFAULT_MODEL 决定；
    #   写死 "bailian" 会把 .env 里的设置覆盖掉（踩过一次：切到 xfyun 却不生效）。
    p.add_argument("-m", "--model", default="",
                   help="模型提供方（bailian / deepseek / xfyun）；留空则用 .env 的 LOOM_DEFAULT_MODEL")
    p.add_argument("--host", default="", help="监听地址，默认 127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="监听端口，默认 8765")
    p.add_argument("--db", default="", help="SQLite 路径，默认 sandbox/loom.db")
    p.add_argument("--max-tasks", type=int, default=2, help="全局同时运行的任务上限")
    p.add_argument("--reload", action="store_true", help="开发模式热重载（会重启进程，任务会中断）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def amain(args: argparse.Namespace) -> int:
    load_dotenv_if_present()

    # ---- 配置 ----
    try:
        cfg = Config.load(args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"[X] 加载配置失败：{exc}")
        return 2

    if not cfg.model.configured:
        print(f"[X] 未配置 {args.model.upper()}_API_KEY —— 请检查 loom/.env")
        return 2

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    db_path = args.db or str(PROJECT_ROOT / "sandbox" / "loom.db")

    # ---- ① 端口冲突（先查，别让 uvicorn 抛栈）----
    if not port_available(host, port):
        print(f"[X] 端口 {host}:{port} 已被占用")
        print("    可能上次的服务还在跑。关掉它，或换端口：")
        print(f"      python -m backend.server --port 8799")
        print(f"      （同时设 LOOM_API_BASE=http://127.0.0.1:8799 给前端）")
        return 3

    # ---- ② 数据库 ----
    store = TaskStore(db_path)
    try:
        await store.init()
    except StoreError as exc:
        print(f"[X] 数据库初始化失败：{exc}")
        return 4

    # ---- ③ 孤儿恢复（必须在接新任务之前）----
    #
    # ★ 这里不再"一刀切标 failed"：能不能续要看 checkpoint（见下面 scheduler 创建后
    #   的 recover_interrupted）。旧实现直接标失败，等于把可恢复的任务判了死刑。
    #   真正的处理推迟到 TaskScheduler 装配完成之后。
    pending_orphans = await store.list_unfinished()
    if pending_orphans:
        print(f"[!] 发现 {len(pending_orphans)} 个上次未完成的任务，正在检查能否续跑……")

    await store.cleanup_idempotency()

    # ---- ④ 装配 ----
    #
    # ★ 事件落盘（对比表 #49）必须在 hub 之前建好：
    #   hub 每次 publish 要问它两件事 —— "这个任务已落盘的最大 seq 是多少"
    #   （续跑接 seq）和"给我把这条塞进队列"。
    archive = EventArchive(
        store,
        enabled=cfg.events.usable,
        flush_interval=cfg.events.flush_interval,
        batch_size=cfg.events.batch_size,
    )
    archived_tasks = 0
    last_event_id = 0
    if archive.enabled:
        # ★ 这两步必须在**任何任务开跑之前**完成（续跑会立刻发事件）：
        #   ① 把每任务的最大 seq 读进内存 → 新事件接着涨，不会覆盖旧事件
        #   ② 用库里的 MAX(id) 给 hub 播种 → ?since= 跨重启仍然对
        archived_tasks = archive.load_seq_cache_sync()
        last_event_id = await store.max_event_id()

    hub = EventHub(archive=archive if archive.enabled else None,
                   start_id=last_event_id,
                   seq_floor=archive.seq_floor if archive.enabled else None)

    # ★ 命令沙箱：先探测，把结果作为**明确的环境状态**告诉用户。
    #   Docker 不可用不影响启动（只读/写文件仍可用），但 run_command 会明确失败，
    #   绝不降级到宿主机执行。
    sandbox = SandboxManager(cfg.sandbox)
    # disabled 时 probe 会立刻返回 unavailable（不碰 docker），
    # 这样 /api/health 里也能看到"已禁用"而不是 null。
    sb = await sandbox.probe()
    if not cfg.sandbox.enabled:
        sb_line = "已禁用（LOOM_SANDBOX_ENABLED=0）"
        sb_warn = "run_command 会被直接拒绝"
    elif sb.ready:
        sb_line = f"docker {sb.version} / {sb.image}"
        sb_warn = ""
    else:
        sb_line = "不可用"
        sb_warn = (
            f"{sb.detail.splitlines()[0] if sb.detail else 'Docker 未就绪'}\n"
            "      run_command 会被拒绝（不会改在宿主机执行）"
        )

    # ★ M3：知识库是**进程级**的（一个 SQLite 文件，按 workspace 隔离数据）。
    #   cfg.rag 不可用时不创建 → registry 不注册 search_docs、core 不做预检索。
    knowledge = KnowledgeBase(cfg.rag) if cfg.rag.usable else None
    rag_info = {
        "enabled": bool(cfg.rag.enabled),
        "available": bool(knowledge is not None),
        "db": cfg.rag.db_path if cfg.rag.usable else "",
        "auto_prefetch": bool(cfg.rag.auto_prefetch),
        "top_k": cfg.rag.top_k,
        "method": "sqlite-fts5-bm25",
    }

    # ★ M4.1：MCP Server 在**服务启动时**连接一次（不是每轮推理启动），
    #   发现到的工具注册进同一个 ToolRegistry；退出时统一关闭子进程。
    mcp_manager = None
    mcp_info: dict = {"enabled": False, "servers": [], "tools": 0}
    if cfg.mcp is not None:
        from ..mcp.client import McpManager
        mcp_manager = McpManager(cfg.mcp)
        await mcp_manager.start()
        mcp_info = {**mcp_manager.status(), "enabled": True}

    # ★ M5：子任务派生器。先建注册表（子任务**复用同一套工具**），再装配 spawner，
    #   最后把 spawn_subtask 注册进去 —— 这样父子任务共用 Policy/审批/闸门，不存在旁路。
    adapter = OpenAICompatAdapter.from_config(cfg.model)
    agent_cfg = AgentConfig.from_budget_config(cfg.budgets, rag_auto=cfg.rag.auto_prefetch)
    # ★ Checkpoint 策略必须挂到 AgentConfig 上：TaskRuntime 读的是 config.checkpoint，
    #   以前没人赋值 → 服务端 checkpoint 从未落盘，恢复也就无从谈起。
    agent_cfg.checkpoint = cfg.checkpoint
    # ★ Skill 技能配置同理：core.run 里按工作区扫描技能库，配置不挂上去就永远为空。
    agent_cfg.skills = cfg.skills

    # 启动时汇总**跨项目层**技能（用户级 / 自定义），供 /api/health 诊断用。
    # ★ 这里 workspace="" 是刻意的：项目级随任务工作区不同；内置目录只在
    #   "工作区就在 Loom 仓库里"时生效（内置技能是 Loom 自己的开发套路），
    #   两者都不该算进"全局"。
    from ..skills import SkillLibrary

    _skill_global = SkillLibrary.from_config(cfg.skills, workspace="")
    skills_info = {
        "enabled": cfg.skills.enabled,
        "project_dir": cfg.skills.project_dir,
        "builtin_dir": cfg.skills.builtin_dir,
        "global": _skill_global.describe(),
    }

    # ★ M6：异步工作流。与 tasks 共用同一个 SQLite 文件（表名 workflow_* 分区）。
    #   allow_local 默认关：http 步骤禁止访问内网/回环（防 SSRF）；本机 mock 测试时才开。
    from ..workflow import WorkflowService, WorkflowStore
    import os as _os

    workflow_store = WorkflowStore(db_path)
    workflow_allow_local = _os.environ.get("LOOM_WORKFLOW_ALLOW_LOCAL", "0").lower() in (
        "1", "true", "on", "yes")
    try:
        workflow_parallel = max(1, int(_os.environ.get("LOOM_WORKFLOW_PARALLEL", "3")))
    except ValueError:
        workflow_parallel = 3

    # ★ Computer-Use 方案2：视觉模型通道（不配置 LOOM_VISION_MODEL 时 vision=None，
    #   vision_look 工具不注册，工具清单与以前完全一致）。
    from ..models.vision import VisionConfig as _VisionConfig
    vision_cfg = _VisionConfig.from_env()
    if vision_cfg.usable:
        log.info("视觉模型已配置：%s（vision_look 工具已注册）", vision_cfg.model)
    else:
        log.info("视觉模型未配置（LOOM_VISION_MODEL 为空）—— vision_look 不注册")

    # ★ 对比表 #21：联网搜索（DeepSeek Search API）。
    #   复用 DEEPSEEK_API_KEY；未配 key 或 LOOM_WEB_SEARCH=0 时不注册 web_search，
    #   工具清单与历史完全一致。
    search_cfg = cfg.web_search
    search_ready = bool(search_cfg.enabled and search_cfg.api_key)
    if search_ready:
        log.info("联网搜索已配置（base_url=%s）—— web_search 工具已注册", search_cfg.base_url)
    else:
        log.info("联网搜索未启用（LOOM_WEB_SEARCH=0 或未配 DEEPSEEK_API_KEY）—— web_search 不注册")

    # ★ 对比表 #25：会话历史查询。只读叶子模块，自己开 mode=ro 连接，
    #   和 RAG 一样不依赖 server 层 —— 所以 registry 和 app 都能安全注入。
    from ..history.query import HistoryQuery

    history = HistoryQuery(db_path)
    if history.usable:
        log.info("会话历史查询已启用（session_history 工具 + /api/history）")
    else:
        log.info("会话历史库不存在（%s）—— session_history 不注册", db_path)

    registry = build_default_registry(
        sandbox=sandbox, knowledge=knowledge, mcp=mcp_manager,
        history=history,
        workflow=workflow_store, workflow_allow_local=workflow_allow_local,
        workflow_parallel=workflow_parallel,
        vision=vision_cfg if vision_cfg.usable else None,
        search=search_cfg if search_ready else None,
        skills=cfg.skills.enabled,
    )

    from ..agent.subtask import SubtaskSpawner
    from ..tools.mutating.spawn_subtask import make as make_spawn

    spawner = SubtaskSpawner(
        store=store, hub=hub, adapter=adapter, registry=registry, config=agent_cfg,
        max_depth=cfg.multiagent.max_depth,
        max_children=cfg.multiagent.max_children,
        child_max_turns=cfg.multiagent.child_max_turns,
        child_max_seconds=cfg.multiagent.child_max_seconds,
        child_max_tool_calls=cfg.multiagent.child_max_tool_calls,
        approval_timeout_s=cfg.multiagent.approval_timeout_s,
        timeout_s=cfg.multiagent.timeout_s,
    )
    registry.register(make_spawn(spawner), group="subagent")

    # ★ 步骤 7：read-only 会话模式的工具子集。
    #   用 readonly_view() 派生（复用同一批工具实例），不是重新 build —
    #   避免 MCP 工具被注册两遍、状态漂移。
    readonly_registry = registry.readonly_view()

    # ★ desktop 模式接入：剔除所有桌面工具（名字含 "desktop"）的注册表。
    #   覆盖两类：MCP 的 mcp__agent_desktop__desktop_*（67 个）和原生
    #   desktop_locate / desktop_click。workspace-write / full-access 用这份，
    #   desktop 模式用完整注册表。capture_screen / vision_look 不剔除
    #   （它们是通用观察工具，READ 风险，read-only 视图里也有）。
    no_desktop_registry = registry.filter(
        names=[n for n in registry.names if "desktop" not in n],
    )

    scheduler = TaskScheduler(
        store=store,
        hub=hub,
        adapter=adapter,
        registry=registry,
        config=agent_cfg,
        max_global=args.max_tasks,
        knowledge=knowledge,
        spawner=spawner,
        readonly_registry=readonly_registry,
        no_desktop_registry=no_desktop_registry,
    )

    # ---- ③b 中断任务恢复（Checkpoint，2026-11-11）----------------------
    #   规则：能续的（checkpoint + 无缺口 + 预算未耗尽 + 未过期）按
    #   LOOM_CKPT_AUTO_RESUME 自动重新入队；不能续的标 aborted 并把原因写进快照。
    recover_summary = await scheduler.recover_interrupted()
    if recover_summary.get("scanned"):
        print(f"[恢复] 扫描 {recover_summary['scanned']} 个中断任务："
              f"续跑 {len(recover_summary.get('resumed') or [])} 个，"
              f"收尾 {len(recover_summary.get('marked') or [])} 个")
        for item in (recover_summary.get("resumed") or [])[:5]:
            print(f"      ↩ {item['task_id']}（checkpoint #{item['checkpoint_seq']}，"
                  f"第 {item['turn']} 轮）")
        for item in (recover_summary.get("marked") or [])[:5]:
            rep = item.get("report") or {}
            why = "；".join(rep.get("reasons") or []) or "未开启自动恢复"
            print(f"      × {item['task_id']}：{why}")

    # ---- ③c 事件保留策略（对比表 #49）----------------------------------
    #   只删"已终态且创建时间早于截止点"的任务事件；运行中/排队中的绝不动
    #   （续跑要接 seq）。0 = 永不清理。
    pruned_events = 0
    if archive.enabled and cfg.events.retention_days > 0:
        try:
            cutoff = time.time() - cfg.events.retention_days * 86400
            pruned_events = await store.prune_events(cutoff)
        except Exception as exc:  # noqa: BLE001 —— 清理失败不该拦住启动
            print(f"[!] 事件清理失败（不影响启动）：{type(exc).__name__}: {exc}")
        if pruned_events:
            print(f"[清理] 事件保留 {cfg.events.retention_days} 天，"
                  f"删掉 {pruned_events} 条过期事件")

    # ---- ③d 会话投影全量重建（#51）-------------------------------------
    #   投影是派生的查询表（从 task_events 推导），不是事实档案。
    #   启动时重建一次，保证投影表与事件流一致 —— 上次运行可能因为
    #   进程被杀、投影维护失败等原因留下滞后的投影行。
    try:
        proj_count = await store.rebuild_projections()
        if proj_count:
            print(f"[投影] 全量重建 {proj_count} 个任务的投影行（#51）")
    except Exception as exc:  # noqa: BLE001 —— 投影重建失败不该拦住启动
        print(f"[!] 投影重建失败（不影响启动，可手动重建）：{type(exc).__name__}: {exc}")

    # ---- 工作流恢复：启动时扫一遍未终结的 Workflow ---------------------
    #   · 幂等步骤（http GET / 只读工具）→ 退回 pending，后台继续跑
    #   · 有副作用的步骤（shell）→ 标 unknown + 工作流转 needs_attention，绝不自动重跑
    from ..agent.policy import make_context
    from ..agent.state import TaskState

    async def _workflow_tool_runner(name: str, wargs: dict, workspace: str) -> dict:
        tool = registry.get(name)
        if tool is None:
            return {"ok": False, "error": f"工具不存在：{name}"}
        st = TaskState(task_id="workflow", goal="(workflow step)", workspace=workspace)
        res = await tool.run(wargs, make_context(st))
        return {"ok": res.ok, "summary": res.summary, "data": res.data, "error": res.error}

    wf_service = WorkflowService(
        workflow_store, tool_runner=_workflow_tool_runner,
        allow_local=workflow_allow_local, max_parallel=workflow_parallel,
    )
    try:
        workflow_recovery = await wf_service.recover()
    except Exception as exc:  # noqa: BLE001 —— 恢复失败不能拖垮启动
        workflow_recovery = {"error": f"{type(exc).__name__}: {exc}"}
    workflow_info = {
        "enabled": True,
        "allow_local": workflow_allow_local,
        "max_parallel": workflow_parallel,
        "recovery": workflow_recovery,
    }

    app = create_app(
        store=store,
        hub=hub,
        scheduler=scheduler,
        provider=cfg.model.provider,
        model=cfg.model.model,
        sandbox=sandbox,
        rag=rag_info,
        mcp=mcp_info,
        workflow_store=workflow_store,
        workflow_info=workflow_info,
        # 对比表 #21：前端据此决定"联网搜索"开关是否可点
        web_search={
            "enabled": search_cfg.enabled,
            "available": search_ready,
            "base_url": search_cfg.base_url if search_ready else "",
        },
        # 对比表 #24：技能（全局层汇总；项目级按任务工作区扫）
        skills=skills_info,
        # 对比表 #49：事件落盘（lifespan 里启动后台刷盘；退出前 flush）
        archive=archive if archive.enabled else None,
        # 对比表 #25：会话历史查询（/api/history、/api/tasks/{id}/replay）
        history=history,
        retention_days=cfg.events.retention_days if archive.enabled else 0,
    )

    print("=" * 62)
    print("  Loom Agent Server")
    print("=" * 62)
    print(f"  地址      http://{host}:{port}")
    print(f"  模型      {cfg.model.provider} / {cfg.model.model}")
    print(f"  编排器    {cfg.budgets.orchestrator}")
    print(f"  数据库    {db_path}")
    print(f"  并发上限  {args.max_tasks}")
    print(f"  命令沙箱  {sb_line}")
    if mcp_manager is not None:
        ready = [x for x in mcp_info.get("servers", []) if x.get("state") == "ready"]
        print(f"  MCP       {len(ready)}/{len(mcp_info.get('servers', []))} 个 Server 就绪，"
              f"{mcp_info.get('tools', 0)} 个工具")
    else:
        print("  MCP       未启用（LOOM_MCP=0）")
    _sk = skills_info["global"]
    _names = f"（{'、'.join(_sk['names'])}）" if _sk["names"] else ""
    print(f"  技能      用户级/自定义 {_sk['count']} 个{_names}")
    print(f"            项目级见各工作区的 {cfg.skills.project_dir}；"
          f"内置仅在 Loom 仓库内生效")
    for bad in _sk["skipped"]:
        print(f"  ! 跳过技能 {bad.get('dir')}：{bad.get('reason')}")
    if archive.enabled:
        _ret = (f"保留 {cfg.events.retention_days} 天"
                if cfg.events.retention_days > 0 else "不清理")
        print(f"  事件落盘  开启（{_ret}）· 已归档 {archived_tasks} 个任务 · "
              f"游标 #{last_event_id}")
    else:
        print("  事件落盘  已关闭（LOOM_EVENTS=0）—— 重启后时间线会丢")
    print(f"  健康检查  http://{host}:{port}/api/health")
    print("=" * 62)
    if sb_warn:
        print(f"[!] {sb_warn}")
        print()

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="debug" if args.verbose else "info",
        access_log=args.verbose,
        reload=False,          # reload 会重启进程，正在跑的任务必然中断
    )
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        # 退出前尽力收尾：取消在跑的任务并落盘
        print("\n正在关闭…")
        await scheduler.shutdown(timeout=5.0)
        # MCP：先关 Client Session 与子进程，再关沙箱/数据库
        if mcp_manager is not None:
            try:
                await mcp_manager.close()
                print("  已关闭 MCP 连接")
            except Exception:  # noqa: BLE001
                log.exception("关闭 MCP 连接失败")
        # 杀掉可能还活着的沙箱容器（--rm 只管正常退出）
        try:
            killed = await sandbox.close()
            if killed:
                print(f"  已清理 {killed} 个沙箱容器")
        except Exception:  # noqa: BLE001
            log.exception("清理沙箱容器失败")
        # 显式关库：连接是长生命周期的，不关会留着 -wal / -shm
        await store.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    import asyncio

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
