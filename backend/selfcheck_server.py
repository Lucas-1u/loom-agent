"""
backend/server 层自检（Phase 3a，全离线）

覆盖：
  [A] TaskStore —— SQLite 持久化、并发写、幂等、重启恢复、schema 版本
  [B] EventHub  —— 双重 id、三重容量限制、截断只影响副本、gap 信号
  [C] Runtime   —— 起任务/收事件/取消/审批/快照/落盘

跑法：python -m backend.selfcheck_server
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from backend.agent.core import AgentConfig
from backend.events import Event
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.server.hub import EventHub
from backend.server.runtime import TaskRuntime
from backend.server.store import SCHEMA_VERSION, StoreError, TaskStore
from backend.tools.registry import build_default_registry

VERBOSE = False
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    if cond:
        if VERBOSE:
            print(f"  [ok] {label}")
    else:
        print(f"  [X]  {label}")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class TmpDB:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="loom_srv_")
        self.path = str(Path(self.dir) / "state.db")

    def store(self) -> TaskStore:
        return TaskStore(self.path)

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def mk_session(sid: str, ws: str = "D:/proj", **kw) -> dict:
    now = time.time()
    return {
        "session_id": sid,
        "title": kw.get("title", "会话"),
        "workspace": ws,
        "created_at": kw.get("created_at", now),
        "updated_at": kw.get("updated_at", now),
    }


def mk_task(tid: str, sid: str, **kw) -> dict:
    return {
        "task_id": tid,
        "session_id": sid,
        "goal": kw.get("goal", "做点事"),
        "status": kw.get("status", "created"),
        "model": kw.get("model", "qwen3.7-plus"),
        "workspace": kw.get("workspace", "D:/proj"),
        "created_at": kw.get("created_at", time.time()),
        "started_at": kw.get("started_at"),
    }


# --------------------------------------------------------------------------- #
# [A] TaskStore
# --------------------------------------------------------------------------- #
async def test_store(db: TmpDB) -> None:
    print("\n[A] TaskStore（SQLite 持久化）")

    store = db.store()
    await store.init()

    # ---- 初始化 ----
    with sqlite3.connect(db.path) as c:
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    check(ver == SCHEMA_VERSION, f"schema 版本 = {ver}")
    check({"sessions", "tasks", "idempotency"} <= tables, f"三张表都建了：{sorted(tables)}")

    # ---- Session ----
    await store.upsert_session(mk_session("s1", title="会话一"))
    await store.upsert_session(mk_session("s2", title="会话二"))
    sessions = await store.list_sessions()
    check(len(sessions) == 2, f"两个会话（实际 {len(sessions)}）")

    await store.upsert_session(mk_session("s1", title="改了名"))
    s1 = [s for s in await store.list_sessions() if s["session_id"] == "s1"][0]
    check(s1["title"] == "改了名", "upsert 是更新而不是插入")

    # ---- 创建任务：created_at 有值，started_at 为 NULL ----
    tid, created = await store.create_task(mk_task("t1", "s1"))
    check(created is True, "新建任务 created=True")

    row = await store.get_task("t1")
    check(row is not None, "任务能读回来")
    check(row["status"] == "created", f"初始状态 created（实际 {row['status']}）")
    check(row["created_at"] > 0, "★ created_at 有值")
    check(row["started_at"] is None, "★★ 排队中 started_at = NULL")
    check(row["state"] is None, "还没有快照")

    # ---- mark_started：填 started_at ----
    await store.mark_started("t1")
    row = await store.get_task("t1")
    check(row["status"] == "running", "mark_started 后状态 running")
    check(row["started_at"] is not None, "★★ started_at 被填上了")
    check(row["started_at"] >= row["created_at"], "started_at >= created_at")

    # ---- save_task：摘要与快照同事务更新 ----
    snap = {"task_id": "t1", "status": "done", "final_answer": "完成", "turn": 2}
    await store.save_task("t1", status="done", state=snap, result="完成", end_reason="agent_done",
                          ended_at=time.time(), turn=2)
    row = await store.get_task("t1")
    check(row["status"] == "done", "摘要 status 更新")
    check(row["status"] == row["state"]["status"], "★★ 摘要 status 与 state_json 一致")
    check(row["result"] == "完成", "result 落库")
    check(row["turn"] == 2, "turn 落库")

    # ---- list_tasks 不带 state_json（列表接口要轻） ----
    tasks = await store.list_tasks("s1")
    check(len(tasks) == 1, "按 session 过滤")
    check("state" not in tasks[0], "★ 列表不返回完整快照（避免列表接口过重）")

    # ---- ★ 并发写：两个任务同时结束，不能丢更新 ----
    for i in range(2, 6):
        await store.create_task(mk_task(f"t{i}", "s1"))
    await asyncio.gather(
        *[
            store.save_task(f"t{i}", status="done", state={"task_id": f"t{i}", "status": "done"},
                            result=f"结果{i}", end_reason="agent_done", ended_at=time.time())
            for i in range(2, 6)
        ]
    )
    rows = await store.list_tasks("s1")
    done = [r for r in rows if r["status"] == "done"]
    check(len(done) == 5, f"★★ 并发写 5 个任务全部落库（实际 {len(done)}/5）")
    check(all(r["result"] for r in done), "★★ 每个任务的结果都在（没有互相覆盖）")

    # ---- ★ 幂等：同一个 key 两次 → 同一个 task_id ----
    a, created_a = await store.create_task(mk_task("tx", "s1"), idem_key="key-1")
    b, created_b = await store.create_task(mk_task("ty", "s1"), idem_key="key-1")
    check(created_a is True, "第一次幂等键 → 新建")
    check(created_b is False, "第二次同 key → 不新建")
    check(a == b == "tx", f"★★ 返回的是原 task_id（{a} == {b}）")
    check(await store.get_task("ty") is None, "★★ 第二个任务根本没被创建")

    # ---- 不同 key → 各自新建 ----
    _, c1 = await store.create_task(mk_task("tz1", "s1"), idem_key="key-2")
    _, c2 = await store.create_task(mk_task("tz2", "s1"), idem_key="key-3")
    check(c1 and c2, "不同 key 各自新建")

    # ---- ★ 事务原子性：任务不合法（session 不存在）→ 幂等键也不该留下 ----
    before = len(await store.list_tasks())
    try:
        await store.create_task(mk_task("tbad", "no_such_session"), idem_key="key-bad")
        check(False, "外键约束应当拒绝无效 session")
    except sqlite3.IntegrityError:
        check(True, "★ 外键约束生效（无效 session 被拒）")
    rows_after = await store.list_tasks()
    check(len(rows_after) == before, "★ 失败的创建没有留下半个任务")
    # 幂等键也不应残留 —— 否则下次带同 key 会被误判为"已完成"
    _, again = await store.create_task(mk_task("tbad2", "s1"), idem_key="key-bad")
    check(again is True, "★★ 失败事务里的幂等键没有残留（可以重新创建）")

    # ---- 幂等清理 ----
    removed = await store.cleanup_idempotency()
    check(removed == 0, "刚写入的幂等键不会被立刻清掉（未过期）")

    # ---- schema 版本比代码新 → 明确报错，不猜 ----
    newer = TaskStore(str(Path(db.dir) / "future.db"))
    await newer.init()
    with sqlite3.connect(newer.db_path) as c:
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    try:
        await newer.init()
        check(False, "库版本高于代码应当报错")
    except StoreError as exc:
        check("高于" in str(exc), f"★ 版本不匹配时明确报错：{str(exc)[:40]}")

    # ---- ★ 重启恢复 ----
    restart = db.store()
    await restart.init()
    orphan_tid, _ = await restart.create_task(mk_task("torphan", "s1", status="running"))
    await restart.save_task("torphan", status="running",
                            state={"task_id": "torphan", "status": "running", "pending_approval": {"x": 1}})
    await restart.mark_started("torphan")

    awaiting_tid, _ = await restart.create_task(mk_task("tawait", "s1"))
    await restart.save_task("tawait", status="awaiting_approval",
                            state={"task_id": "tawait", "status": "awaiting_approval"})

    fixed = await restart.mark_orphans_failed()
    check(set(fixed) >= {"torphan", "tawait"}, f"扫到了未完成任务：{fixed}")

    ro = await restart.get_task("torphan")
    check(ro["status"] == "failed", f"★★ running → failed（实际 {ro['status']}）")
    check(ro["end_reason"] == "server_restarted", f"★★ end_reason=server_restarted（实际 {ro['end_reason']}）")
    check(ro["state"]["status"] == "failed", "★★ state_json 里的 status 也改了（与摘要一致）")
    check(ro["state"]["pending_approval"] is None, "★ 挂起的审批被清空（Future 不可能恢复）")

    ra = await restart.get_task("tawait")
    check(ra["status"] == "failed", "awaiting_approval 也标成 failed")

    still_done = await restart.get_task("t1")
    check(still_done["status"] == "done", "已结束的任务不受重启影响")


# --------------------------------------------------------------------------- #
# [B] EventHub
# --------------------------------------------------------------------------- #
async def test_hub() -> None:
    print("\n[B] EventHub（双重 id + 三重容量限制）")

    hub = EventHub()

    def mk(tid: str, seq_hint: int, big: int = 0) -> Event:
        ev = Event.make("tool.result", {
            "call_id": f"c{seq_hint}", "tool": "read_file", "ok": True,
            "summary": "ok", "duration_ms": 3,
            **({"pad": "x" * big} if big else {}),
        }, task_id=tid)
        ev.seq = seq_hint          # 手动指定，模拟"任务内 seq"
        return ev

    # ---- 全局 id 递增，任务内 seq 原样保留 ----
    r1 = await hub.publish("taskA", "s1", mk("taskA", 1))
    r2 = await hub.publish("taskB", "s1", mk("taskB", 1))     # 另一个任务的 seq 也是 1
    r3 = await hub.publish("taskA", "s1", mk("taskA", 2))
    check(r1["id"] == 1 and r2["id"] == 2 and r3["id"] == 3, "★ 全局 id 连续递增")
    check(r2["event"]["seq"] == 1 and r3["event"]["seq"] == 2, "★★ 任务内 seq 原样保留（撞车也不影响广播）")
    check(r2["id"] != r2["event"]["seq"], "★ 两层 id 互不干扰")

    # ---- 条数上限 ----
    small = EventHub(max_events=5)
    for i in range(1, 9):
        await small.publish("t", "s", mk("t", i))
    check(small.stats()["buffered"] == 5, f"条数上限生效（缓冲 {small.stats()['buffered']}/5）")
    check(small.stats()["evicted"] == 3, f"淘汰了 3 条（实际 {small.stats()['evicted']}）")

    # ---- ★ 淘汰后补发 → gap 信号，而不是静默少给 ----
    sub = small.subscribe()
    small.unsubscribe(sub)
    evs, gap = small.replay(since=0)
    check(evs == [], "已被淘汰的历史不补发")
    check(gap is not None and gap["type"] == "stream.gap", "★★ 返回 stream.gap 信号")
    check(gap["data"]["oldest_available"] == 4, f"★ 告知最旧可用 id（{gap['data']['oldest_available']}）")
    check("快照" in gap["data"]["note"], "★ gap 里说明要用快照恢复")

    # ---- 未淘汰的范围正常补发 ----
    evs, gap = small.replay(since=5)
    check(gap is None and len(evs) == 3, f"从 id=5 之后补发 3 条（实际 {len(evs)}）")

    # ---- ★ 截断只影响缓冲副本，广播仍是原始事件 ----
    size_hub = EventHub(max_event_bytes=2_000)
    q = size_hub.subscribe()
    big_ev = mk("t", 1, big=8_000)
    rec = await size_hub.publish("t", "s", big_ev)

    buffered_data = rec["event"]["data"]
    check(buffered_data.get("_truncated") is True, "★★ 缓冲副本标记 _truncated")
    check("_original_bytes" in buffered_data, f"★ 记录了原始大小（{buffered_data.get('_original_bytes')} B）")

    pushed = q.get_nowait()
    check(pushed["event"]["data"].get("_truncated") is None, "★★ 在线广播收到的仍是原始事件（没被截断）")
    check(len(pushed["event"]["data"].get("pad", "")) == 8_000, "★ 原始 data 完整")

    # ---- 总字节上限 ----
    byte_hub = EventHub(max_events=10_000, max_event_bytes=1_000_000, max_total_bytes=10_000)
    for i in range(1, 11):
        await byte_hub.publish("t", "s", mk("t", i, big=1_500))
    check(byte_hub.stats()["bytes"] <= 10_000, f"总字节不超上限（{byte_hub.stats()['bytes']} <= 10000）")
    check(byte_hub.stats()["buffered"] < 10, f"按字节淘汰生效（剩 {byte_hub.stats()['buffered']} 条）")

    # ---- 订阅广播 ----
    h2 = EventHub()
    q1 = h2.subscribe()
    q2 = h2.subscribe()
    await h2.publish("t", "s", mk("t", 1))
    check(q1.qsize() == 1 and q2.qsize() == 1, "★ 多个订阅者都收到")
    h2.unsubscribe(q2)
    await h2.publish("t", "s", mk("t", 2))
    check(q1.qsize() == 2 and q2.qsize() == 1, "退订后不再收到")


# --------------------------------------------------------------------------- #
# [C] TaskRuntime
# --------------------------------------------------------------------------- #
def make_script():
    """一个最短的剧本：读一次目录 → 给最终回答。"""
    return [
        ModelReply(content="先看看目录"),
        ModelReply(tool_calls=[ToolCall(id="c1", name="list_files",
                                        arguments={"path": "."}, arguments_raw='{"path":"."}')]),
        ModelReply(content="看完了，目录空的。"),
    ]


async def test_runtime(db: TmpDB) -> None:
    print("\n[C] TaskRuntime（起任务 / 事件 / 取消 / 快照 / 落盘）")

    ws_dir = tempfile.mkdtemp(prefix="loom_ws_")
    try:
        store = db.store()
        await store.init()
        await store.upsert_session(mk_session("s1", ws=ws_dir))

        hub = EventHub()
        q = hub.subscribe()

        cfg = AgentConfig(max_turns=4, max_seconds=30, approval_timeout_s=3, min_evidence=1)

        rt = TaskRuntime(
            task_id="task_rt1",
            session_id="s1",
            goal="看看目录",
            workspace=ws_dir,
            model="fake",
            adapter=FakeAdapter(make_script()),
            registry=build_default_registry(),
            config=cfg,
            hub=hub,
            store=store,
        )

        await store.create_task(mk_task("task_rt1", "s1", workspace=ws_dir))
        await rt.start()

        # 等任务结束（最多 10 秒）
        try:
            await asyncio.wait_for(rt.finished.wait(), timeout=10)
        except asyncio.TimeoutError:
            check(False, "任务应当在 10 秒内结束")

        check(rt.status in ("done", "aborted"), f"任务结束，status={rt.status}")
        check(rt.started_at is not None, "★ started_at 在真正开始时才填")

        # ---- 事件确实流到了 hub ----
        collected = []
        while not q.empty():
            collected.append(q.get_nowait())
        types = [c["event"]["type"] for c in collected]
        check("task.start" in types, "★ hub 收到了 task.start")
        check("tool.call" in types, "★ 收到了 tool.call")
        check("task.end" in types, "★ 收到了 task.end")
        check(len(collected) >= 5, f"事件数量合理（{len(collected)} 条）")
        check(all(c["task_id"] == "task_rt1" for c in collected), "★★ 所有事件都归属这个任务")
        check(all(c["session_id"] == "s1" for c in collected), "★★ 都带对的 session_id")

        # ---- 落盘 ----
        row = await store.get_task("task_rt1")
        check(row["status"] == rt.status, "★ 终态落库")
        check(row["started_at"] is not None, "★ started_at 落库")
        check(row["ended_at"] is not None, "★ ended_at 落库")
        check(row["state"]["task_id"] == "task_rt1", "★ state_json 是完整快照")
        check(row["state"]["status"] == row["status"], "★★ 快照与摘要的 status 一致")

        # ---- 快照补上服务层字段 ----
        snap = rt.snapshot()
        check(snap["session_id"] == "s1", "快照带 session_id（core 不知道这个字段）")
        check(snap["goal"] == "看看目录", "快照带 goal")
        check("plan" in snap or "tool_events" in snap, "快照含 core 的完整字段")

        # ---- 已结束的任务不能再取消 ----
        check(rt.cancel() is False, "★ 已结束的任务 cancel 返回 False")

        # ---- 审批接口：无效 id 返回 False ----
        ok = await rt.resolve_approval("apr_nope", "approved")
        check(ok is False, "★ 无效 approval_id → resolve 返回 False（不报假成功）")

    finally:
        shutil.rmtree(ws_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def main() -> int:
    global VERBOSE
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

    print("=" * 78)
    print("Loom Phase 3a · 服务层自检（SQLite + Hub + Runtime，全离线）")
    print("=" * 78)

    db = TmpDB()
    print(f"临时数据库：{db.path}")
    try:
        await test_store(db)
        await test_hub()
        await test_runtime(db)
    finally:
        db.cleanup()

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 78)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print(f"\n失败项（{total - ok} 条）：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 78)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
