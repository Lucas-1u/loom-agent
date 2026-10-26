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

import uvicorn

from ..agent.core import AgentConfig
from ..config import PROJECT_ROOT, Config, load_dotenv_if_present
from ..models.openai_compat import OpenAICompatAdapter
from ..sandbox import SandboxManager
from ..tools.registry import build_default_registry
from .app import create_app
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
    orphans = await store.mark_orphans_failed("server_restarted")
    if orphans:
        print(f"[!] 发现 {len(orphans)} 个上次未完成的任务，已标记为 failed(server_restarted)：")
        for t in orphans[:5]:
            print(f"      {t}")
        if len(orphans) > 5:
            print(f"      …以及另外 {len(orphans) - 5} 个")

    await store.cleanup_idempotency()

    # ---- ④ 装配 ----
    hub = EventHub()

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

    scheduler = TaskScheduler(
        store=store,
        hub=hub,
        adapter=OpenAICompatAdapter.from_config(cfg.model),
        registry=build_default_registry(sandbox=sandbox),
        config=AgentConfig.from_budget_config(cfg.budgets),
        max_global=args.max_tasks,
    )

    app = create_app(
        store=store,
        hub=hub,
        scheduler=scheduler,
        provider=cfg.model.provider,
        model=cfg.model.model,
        sandbox=sandbox,
    )

    print("=" * 62)
    print("  Loom Agent Server")
    print("=" * 62)
    print(f"  地址      http://{host}:{port}")
    print(f"  模型      {cfg.model.provider} / {cfg.model.model}")
    print(f"  数据库    {db_path}")
    print(f"  并发上限  {args.max_tasks}")
    print(f"  命令沙箱  {sb_line}")
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
