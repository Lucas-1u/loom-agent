"""
事件落盘的**真进程**验证（对比表 #49）

为什么不能只靠自检：
    自检里 `[EV]` / `[EV2]` 都是在**同一个进程**里换 store/hub 实例来模拟重启，
    走不到真正的 `python -m backend.server` 装配路径 —— 而 #49 恰恰有一半
    风险在装配上（hub 用 MAX(id) 播种、seq 缓存预热、lifespan 启停归档）。

这个脚本的做法（照搬 dev/_resume_killtest.py 的路子）：
    ① 起一个真 uvicorn 子进程（假模型，不联网），跑一个任务，
       顺便断言"运行中从内存读得到事件"
    ② **硬杀**它（proc.kill，等价于拔电/任务管理器结束进程）
    ③ 用同一个数据库目录再起一个进程
    ④ 断言：那个任务的事件历史**还在**，来源是 disk，顺序不乱

★ 硬杀必然丢掉"最后不到一个刷盘周期"的事件（默认 0.25s；这里调成 0.05s）。
  所以断言写成"至少还在 N-1 条"，而不是逐条相等 —— 这是设计取舍，不是 bug。

跑法：
    ./.venv/Scripts/python.exe dev/_events_killtest.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[bool, str]] = []


def check(cond: bool, label: str, actual: object = "") -> None:
    text = label if actual == "" else f"{label}｜实际：{actual!r}"
    RESULTS.append((bool(cond), text))
    print(("  [ok] " if cond else "  [X]  ") + text)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http(method: str, url: str, payload: dict | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        return resp.status, (json.loads(body) if body else None)


def wait_health(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, _ = http("GET", f"http://127.0.0.1:{port}/api/health", timeout=2.0)
            if status == 200:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- #
# 子进程：真 uvicorn（假模型）
# --------------------------------------------------------------------------- #
def serve() -> None:
    import asyncio

    import uvicorn

    from backend.agent.core import AgentConfig
    from backend.models.adapter import FakeAdapter, ModelReply
    from backend.server.app import create_app
    from backend.server.archive import EventArchive
    from backend.server.hub import EventHub
    from backend.server.scheduler import TaskScheduler
    from backend.server.store import TaskStore
    from backend.tools.registry import build_default_registry

    db = os.environ["KT_DB"]
    port = int(os.environ["KT_PORT"])

    async def main() -> None:
        store = TaskStore(db)
        await store.init()

        # ★ 和 backend/server/__main__.py 的装配顺序保持一字不差：
        #   归档 → 预热 seq 缓存 → 播种 hub 的 gid → 再起调度器
        archive = EventArchive(store, flush_interval=0.05)
        archive.load_seq_cache_sync()
        hub = EventHub(
            archive=archive,
            start_id=await store.max_event_id(),
            seq_floor=archive.seq_floor,
        )

        script = [
            ModelReply(reasoning="先看看目录里有什么，再决定要不要动手。", content=""),
            ModelReply(content="目录看过了，没有需要改的东西。"),
        ]
        scheduler = TaskScheduler(
            store=store,
            hub=hub,
            adapter=FakeAdapter(script),
            registry=build_default_registry(),
            config=AgentConfig(max_turns=3, max_seconds=30, min_evidence=0, use_reflect_phase=False),
            max_global=1,
        )
        app = create_app(
            store=store, hub=hub, scheduler=scheduler,
            archive=archive, retention_days=30,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        await server.serve()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主进程：跑一遍 → 硬杀 → 重启 → 看时间线
# --------------------------------------------------------------------------- #
def start_server(db: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["KT_DB"] = db
    env["KT_PORT"] = str(port)
    env["LOOM_EVENTS"] = "1"
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="loom_evkill_")
    db = str(Path(tmp) / "state.db")
    ws = str(Path(tmp) / "ws")
    Path(ws).mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = start_server(db, port)

    try:
        print("=" * 70)
        print("事件落盘 · 真进程验证（硬杀 → 重启 → 时间线还在）")
        print("=" * 70)
        print(f"临时目录：{tmp}")
        print(f"端口：{port}")

        if not wait_health(port):
            out = b""
            if proc.stdout is not None:
                proc.kill()
                out = proc.stdout.read() or b""
            print("[X] 服务没起来，子进程输出：")
            print(out.decode("utf-8", "ignore")[-2000:])
            return 2
        check(True, "① 服务已启动")

        # 建会话 + 跑一个任务
        _, sess = http("POST", f"http://127.0.0.1:{port}/api/sessions",
                       {"title": "落盘验证", "workspace": ws})
        _, task = http("POST", f"http://127.0.0.1:{port}/api/tasks",
                       {"goal": "看看目录里有什么", "session_id": sess["session_id"], "workspace": ws})
        tid = task["task_id"]
        check(bool(tid), f"② 任务已提交（{tid}）")

        # 等它结束
        status = ""
        t0 = time.time()
        while time.time() - t0 < 60:
            _, snap = http("GET", f"http://127.0.0.1:{port}/api/tasks/{tid}")
            status = snap.get("status", "")
            if status in ("done", "failed", "aborted"):
                break
            time.sleep(0.2)
        check(status == "done", f"③ 任务跑完（status={status}）")

        # 运行中的读取：应该来自内存
        _, ev_live = http("GET", f"http://127.0.0.1:{port}/api/tasks/{tid}/events")
        n_live = len(ev_live.get("events") or [])
        check(ev_live.get("available") is True, "④ 运行中的事件历史可用")
        check(ev_live.get("source") in ("memory", "hybrid"),
              f"   来源是内存（{ev_live.get('source')}）")
        check(n_live >= 4, f"   至少 4 条事件（实际 {n_live}）")

        # ---- 硬杀 ----
        proc.kill()
        proc.wait(timeout=20)
        print("  ···· 已硬杀（进程被杀，没有任何退出清理）")

        # ---- 重启，同一个库 ----
        port2 = free_port()
        proc = start_server(db, port2)
        if not wait_health(port2):
            print("[X] 重启失败")
            return 3
        check(True, "⑤ 服务已重启（新进程、新 hub）")

        _, ev_disk = http("GET", f"http://127.0.0.1:{port2}/api/tasks/{tid}/events")
        events = ev_disk.get("events") or []
        check(ev_disk.get("available") is True,
              f"⑥★ 重启后事件历史仍然可用（reason={ev_disk.get('reason')}）")
        check(ev_disk.get("source") == "disk",
              f"⑦★ 来源是磁盘（{ev_disk.get('source')}）")
        # 硬杀允许丢最后几条（未刷盘），但不能整段消失
        check(len(events) >= max(1, n_live - 2),
              f"⑧★ 事件基本都在（重启后 {len(events)} 条 / 杀之前 {n_live} 条）")

        seqs = [e["event"]["seq"] for e in events]
        check(seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
              f"⑨ 顺序正确且不重复（{seqs[:8]}…）")
        check(events[0]["event"]["type"] == "task.start",
              f"⑩ 第一条是 task.start（{events[0]['event']['type']}）")
        check(any(e["event"]["type"] == "thinking" for e in events),
              "⑪ 思考事件也被存下来了（不只是骨架事件）")

        _, snap2 = http("GET", f"http://127.0.0.1:{port2}/api/tasks/{tid}")
        check(snap2.get("events_available") is True,
              "⑫ 任务快照里的 events_available 也是 true")
        check(snap2.get("events_archived", 0) >= 1,
              f"   快照里带落盘条数（events_archived={snap2.get('events_archived')}）")

        _, health = http("GET", f"http://127.0.0.1:{port2}/api/health")
        ev_health = health.get("events") or {}
        check(ev_health.get("enabled") is True, "⑬ health 里事件落盘开启")
        # 刚重启的进程什么都没跑过，written=0 才是对的；
        # 这里验的是"计数暴露出来了"，不是"它必须大于 0"。
        check("written" in ev_health and "pending" in ev_health,
              f"   health 带落盘计数（written={ev_health.get('written')}, "
              f"pending={ev_health.get('pending')}）")

        # 导出 → 导入
        _, exported = http("GET", f"http://127.0.0.1:{port2}/api/tasks/{tid}/export")
        check(exported.get("format") == "loom.task.v1" and len(exported.get("events") or []) >= 1,
              "⑭ 导出接口可用")
        _, imported = http("POST", f"http://127.0.0.1:{port2}/api/tasks/import", exported)
        check(imported.get("events", 0) >= 1, f"⑮ 导入成功（{imported.get('task_id')}）")
        _, ev_imp = http("GET", f"http://127.0.0.1:{port2}/api/tasks/{imported['task_id']}/events")
        check(len(ev_imp.get("events") or []) == imported.get("events"),
              "⑯ 导入后事件条数一致")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for c, _ in RESULTS if c)
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        for c, label in RESULTS:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 70)
    return 0 if ok == total else 1


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        sys.exit(main())
