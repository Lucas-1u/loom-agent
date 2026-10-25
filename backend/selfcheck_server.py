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


_UNSET = object()


def check(cond: bool, label: str, actual: object = _UNSET) -> None:
    """`actual` 给出时追加到标签后面 —— 失败报告里直接能看到实际值。"""
    text = label if actual is _UNSET else f"{label}｜实际：{actual!r}"
    results.append((bool(cond), text))
    if cond:
        if VERBOSE:
            print(f"  [ok] {text}")
    else:
        print(f"  [X]  {text}")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class TmpDB:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="loom_srv_")
        self.path = str(Path(self.dir) / "state.db")
        # TaskStore 现在复用长连接（见 store.py 顶部说明）——
        # Windows 上文件被打开时目录删不掉，所以清理前必须显式关。
        self._stores: list[TaskStore] = []

    def store(self) -> TaskStore:
        s = TaskStore(self.path)
        self._stores.append(s)
        return s

    def cleanup(self) -> None:
        for s in self._stores:
            s.close_sync()
        self._stores.clear()
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
# [A2] TaskStore 并发写 —— SQLITE_READONLY 回归
# --------------------------------------------------------------------------- #
async def test_store_concurrent_sqlite(db: TmpDB) -> None:
    """
    ★ 回归用例：WAL + 并发短连接曾偶发
        sqlite3.OperationalError: attempt to write a readonly database

    根因是「每次操作新建连接」：WAL 的 -wal/-shm 由最后一个关闭的连接
    checkpoint 并删除，并发下另一个线程正好在这个窗口打开文件，就会被降级成只读。
    修法是连接复用 + 全局串行化（见 store.py 顶部）。

    这里直接用**真实 TaskStore**（不另写一套 sqlite 逻辑），
    断言三件事：
      ① 一次都不能抛异常
      ② 每一次写都真的落库（turn 等于最后一次写的序号，不能有丢失）
      ③ 仍然是 WAL —— 修复不能靠偷偷降级 journal_mode 绕过问题
    """
    print("\n[A2] TaskStore 并发写（SQLITE_READONLY 回归）")

    store = db.store()
    await store.init()
    await store.upsert_session(mk_session("sc", title="并发"))

    n_tasks, n_writes = 12, 40          # 480 次写：足以把 1% 量级的偶发错误逼出来
    for i in range(n_tasks):
        await store.create_task(mk_task(f"c{i}", "sc"))

    errors: list[str] = []

    def hammer(tid: str) -> None:
        for n in range(n_writes):
            try:
                store.save_task_sync(tid, status="running", turn=n + 1)
            except Exception as exc:  # noqa: BLE001 —— 这里就是要抓到它
                errors.append(f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(asyncio.to_thread(hammer, f"c{i}") for i in range(n_tasks)))

    check(not errors, f"★★ 并发写 {n_tasks * n_writes} 次零异常",
          actual=(errors[:3] if errors else "无"))
    check(len(errors) == 0 or all("readonly" not in e.lower() for e in errors),
          "★★ 没有出现 SQLITE_READONLY")

    rows = await store.list_tasks("sc")
    turns = {r["task_id"]: r["turn"] for r in rows}
    missing = [f"c{i}" for i in range(n_tasks) if turns.get(f"c{i}") != n_writes]
    check(not missing,
          f"★★ 每次写都落库（每个任务 turn 应为 {n_writes}）",
          actual=(missing if missing else sorted(set(turns.values()))))

    # 用独立连接读 journal_mode：确认还是 WAL
    c = sqlite3.connect(db.path)
    try:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()
    check(str(mode).lower() == "wal", "★ 仍然是 WAL（没有靠降级 journal 模式绕过）",
          actual=mode)


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

    pushed = q.queue.get_nowait()
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
    check(q1.queue.qsize() == 1 and q2.queue.qsize() == 1, "★ 多个订阅者都收到")
    h2.unsubscribe(q2)
    await h2.publish("t", "s", mk("t", 2))
    check(q1.queue.qsize() == 2 and q2.queue.qsize() == 1, "退订后不再收到")


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
        while not q.queue.empty():
            collected.append(q.queue.get_nowait())
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
# [C2] 落盘失败时的投递可靠性
# --------------------------------------------------------------------------- #
async def test_runtime_persist_failure(db: TmpDB) -> None:
    """
    ★ 回归用例：落盘失败曾经会**吞掉事件**。

    旧实现 `_on_event` 是「先 await 落盘，再 hub.publish」：
    落盘一抛异常，publish 根本不会执行，`EventBus._deliver` 又把异常吞掉，
    结果是任务内 seq 出现空洞（实测 approval.decision 直接消失），
    前端永远等不到，且日志里只有一行看不懂的 `subscriber <bound method ...>`。

    现在要求：
      ① 落盘失败时事件照样广播
      ② 故障要能在 `snapshot()` 里查到（stage / 错误原文 / 次数）
      ③ 终态落盘失败时任务仍要正常结束，不能挂住
    """
    print("\n[C2] 落盘失败不能吞事件（投递可靠性回归）")

    ws = tempfile.mkdtemp(prefix="loom_pf_")
    store = db.store()
    await store.init()
    await store.upsert_session(mk_session("sp", ws=ws))

    hub = EventHub()
    q = hub.subscribe()

    rt = TaskRuntime(
        task_id="task_pf",
        session_id="sp",
        goal="看看目录",
        workspace=ws,
        model="fake",
        adapter=FakeAdapter(make_script()),
        registry=build_default_registry(),
        config=AgentConfig(max_turns=4, max_seconds=20, approval_timeout_s=3, min_evidence=0),
        hub=hub,
        store=store,
    )
    await store.create_task(mk_task("task_pf", "sp", workspace=ws))

    # ---- ① 直接驱动 _on_event：落盘必失败，事件仍必须到 hub ----
    real_save_task = store.save_task

    async def boom(*_a, **_kw):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    store.save_task = boom  # type: ignore[method-assign]
    try:
        ev = Event.make("approval.decision", {"approved": True, "approval_id": "apr_x"},
                        task_id="task_pf")
        ev.seq = 7
        await rt._on_event(ev)
    finally:
        store.save_task = real_save_task  # type: ignore[method-assign]

    got = []
    while not q.queue.empty():
        got.append(q.queue.get_nowait())
    check([g["event"]["type"] for g in got] == ["approval.decision"],
          "★★ 落盘失败时事件仍然广播（不再丢失）",
          actual=[g["event"]["type"] for g in got])
    check(rt.status == "running", "★ 内存状态照常推进（approval.decision → running）",
          actual=rt.status)
    check(rt.persist_failures == 1, "★ 故障被计数", actual=rt.persist_failures)
    check(rt.persist_error is not None
          and rt.persist_error["stage"] == "state:running"
          and "readonly" in rt.persist_error["error"].lower(),
          "★★ 故障带 stage + 错误原文（可诊断）", actual=rt.persist_error)
    check(rt.snapshot().get("persist_error") == rt.persist_error,
          "★ 快照能查到落盘故障（/api/tasks/{id} 的排查入口）")
    check(rt.bus.delivery_errors == 0,
          "★★ 事件不是靠「吞异常」送到的（订阅者零异常）",
          actual=rt.bus.delivery_errors)

    # ---- ② 终态落盘失败：任务仍要结束，且不能假装成功 ----
    # ★ 必须换一个新的 runtime：上面 phase ① 已经把 rt.status 推到 running，
    #   而 start() 在 status != created 时是直接 return 的。
    rt2 = TaskRuntime(
        task_id="task_pf2",
        session_id="sp",
        goal="看看目录",
        workspace=ws,
        model="fake",
        adapter=FakeAdapter(make_script()),
        registry=build_default_registry(),
        config=AgentConfig(max_turns=4, max_seconds=20, approval_timeout_s=3, min_evidence=0),
        hub=hub,
        store=store,
    )
    await store.create_task(mk_task("task_pf2", "sp", workspace=ws))

    store.save_task = boom  # type: ignore[method-assign]
    try:
        await rt2.start()
        try:
            await asyncio.wait_for(rt2.finished.wait(), timeout=10)
            finished = True
        except asyncio.TimeoutError:
            finished = False
    finally:
        store.save_task = real_save_task  # type: ignore[method-assign]

    check(finished, "★★ 终态落盘失败也不能把任务挂住")
    check(rt2.status == "done", "★ Agent 本身仍然跑完了", actual=rt2.status)
    check(rt2.persist_error is not None and rt2.persist_error["stage"] == "final",
          "★★ 终态故障单独记录 stage=final", actual=rt2.persist_error)

    row = await store.get_task("task_pf2")
    # mark_started 是先写成功的，所以库里停在 running；
    # 关键是**没有**假的 done —— 落盘失败就是失败，不静默改成成功。
    check(row["status"] == "running",
          "★★ 库里没有留下假的 done（终态没写进去就是没写进去）",
          actual=row["status"])

    # ---- ③ 落盘顺序：慢的旧写入不能把新状态盖回去 ----
    # ★ 广播前置之后，两次落盘是并发的。审批请求那次如果慢（DB 抖动），
    #   可能晚于审批决策那次才写完 —— 库里的状态就回退了。
    rt3 = TaskRuntime(
        task_id="task_pf3",
        session_id="sp",
        goal="看看目录",
        workspace=ws,
        model="fake",
        adapter=FakeAdapter(make_script()),
        registry=build_default_registry(),
        config=AgentConfig(max_turns=4, max_seconds=20, approval_timeout_s=3, min_evidence=0),
        hub=hub,
        store=store,
    )
    await store.create_task(mk_task("task_pf3", "sp", workspace=ws))

    real_save_task2 = store.save_task

    async def slow_awaiting(*_a, **_kw):
        # 只有 awaiting_approval 这次写"很慢" —— 模拟一次磁盘/系统调用抖动
        if _kw.get("status") == "awaiting_approval":
            await asyncio.sleep(0.3)
        return await real_save_task2(*_a, **_kw)

    store.save_task = slow_awaiting  # type: ignore[method-assign]
    try:
        req_ev = Event.make("approval.request", {"approval_id": "a2"}, task_id="task_pf3")
        req_ev.seq = 1
        dec_ev = Event.make("approval.decision", {"approved": True}, task_id="task_pf3")
        dec_ev.seq = 2
        await asyncio.gather(rt3._on_event(req_ev), rt3._on_event(dec_ev))
    finally:
        store.save_task = real_save_task2  # type: ignore[method-assign]

    row3 = await store.get_task("task_pf3")
    check(row3["status"] == rt3.status,
          "★★ 慢的旧写入不会把库里的状态写回退（awaiting_approval 不能盖掉 running）",
          actual=row3["status"])
    check((row3["state"] or {}).get("status") == rt3.status,
          "★★ state_json 与内存状态一致", actual=(row3["state"] or {}).get("status"))

    shutil.rmtree(ws, ignore_errors=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def run_async_suites(db: TmpDB) -> None:
    """这几节都是纯 async，跑在同一个 loop 里。"""
    await test_store(db)
    await test_store_concurrent_sqlite(db)
    await test_hub()
    await test_runtime(db)
    await test_runtime_persist_failure(db)
    await test_scheduler(db)


def main() -> int:
    global VERBOSE
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

    print("=" * 78)
    print("Loom 服务层自检（Phase 3a + 3b，全离线）")
    print("=" * 78)

    db = TmpDB()
    print(f"临时数据库：{db.path}")
    try:
        asyncio.run(run_async_suites(db))
        # ★ 同步跑 [F]：TestClient 会开自己的 loop，
        #   如果此时已经在 asyncio.run 里，会报 "another loop is running"
        test_http_and_sse(db)
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




# =========================================================================== #
# Phase 3b：调度器 / SSE / HTTP
# =========================================================================== #
from backend.models.adapter import ModelAdapter, StreamDelta  # noqa: E402
from backend.server.app import create_app  # noqa: E402
from backend.server.scheduler import TaskScheduler  # noqa: E402


class SlowAdapter(ModelAdapter):
    """每次模型调用慢一点，用来制造真实的并发与排队窗口。"""

    name = "slow"

    def __init__(self, delay: float = 0.2, rounds: int = 30) -> None:
        self.model = "slow-1"
        self.delay = delay
        self._inner = FakeAdapter([ModelReply(content="做完了。") for _ in range(rounds)])

    async def chat_stream(self, messages, tools=None, *, temperature=None, max_tokens=None):
        await asyncio.sleep(self.delay)
        async for d in self._inner.chat_stream(messages, tools=tools):
            yield d


async def wait_until(pred, timeout: float = 10.0, interval: float = 0.05) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def test_scheduler(db: TmpDB) -> None:
    print("\n[D] TaskScheduler（并发上限 / 排队 / 槽位释放 / 审批校验）")

    ws = tempfile.mkdtemp(prefix="loom_ws_")
    try:
        store = db.store()
        await store.init()
        for sid in ("s1", "s2", "s3"):
            await store.upsert_session(mk_session(sid, ws=ws))

        hub = EventHub()
        sch = TaskScheduler(
            store=store,
            hub=hub,
            adapter=SlowAdapter(delay=0.6),
            registry=build_default_registry(),
            config=AgentConfig(max_turns=2, max_seconds=20, approval_timeout_s=2, min_evidence=0),
            max_global=2,
        )

        base_count = len(await store.list_tasks())

        # ---- ① 全局上限 2 ----
        a = await sch.submit(session_id="s1", goal="任务A", workspace=ws)
        b = await sch.submit(session_id="s2", goal="任务B", workspace=ws)
        c = await sch.submit(session_id="s3", goal="任务C", workspace=ws)

        check(a["status"] == "running", f"A 立即 running（{a['status']}）")
        check(b["status"] == "running", f"B 立即 running（{b['status']}）")
        check(c["status"] == "created", f"★★ C 排队（status=created，实际 {c['status']}）")
        check(c["queued"] is True and c["queue_position"] == 1, f"★ 排队位置 1（实际 {c.get('queue_position')}）")
        check(sch.stats()["running"] == 2, f"全局 running = 2（实际 {sch.stats()['running']}）")

        # ---- ② 同 Session 上限 1 ----
        d = await sch.submit(session_id="s1", goal="任务D", workspace=ws)   # s1 已有 A
        check(d["status"] == "created", "★★ 同 session 已有任务在跑 → 新任务排队")
        check(sch.stats()["running"] == 2, "没有突破全局上限")

        # ---- ③ 排队任务自动启动 ----
        ok = await wait_until(lambda: sch.stats()["running"] >= 1 and sch.stats()["queued"] < 2, timeout=15)
        check(ok, "★ 有槽位释放后自动拉起排队任务")

        # 等本批 4 个都进终态（不看全局计数，避免被历史残留干扰）
        mine_ids = {a["task_id"], b["task_id"], c["task_id"], d["task_id"]}

        def _all_terminal() -> bool:
            rows = store.list_tasks_sync()
            st = {r["task_id"]: r["status"] for r in rows}
            return all(st.get(tid) in ("done", "aborted", "failed") for tid in mine_ids)

        ok = await wait_until(_all_terminal, timeout=50)
        check(ok, f"★ 本批 4 个任务全部进入终态（running={sch.stats()['running']}, queued={sch.stats()['queued']}）")

        all_rows = await store.list_tasks()
        statuses = {r["task_id"]: r["status"] for r in all_rows}
        # 只看本测试新建的 4 个（db 是共用的，前面 store 测试留下的 failed 不算）
        mine = {a["task_id"], b["task_id"], c["task_id"], d["task_id"]}
        bad = [(r["task_id"], r["status"], r["end_reason"]) for r in all_rows
               if r["task_id"] in mine and r["status"] not in ("done", "aborted")]
        check(not bad, f"★ 本次 4 个任务全部正常收尾（异常={bad[:2]}）")
        check(len(statuses) - base_count == 4, f"4 个任务都创建了（实际新增 {len(statuses) - base_count}）")

        # ---- ④ 幂等 ----
        e1 = await sch.submit(session_id="s1", goal="幂等任务", workspace=ws, idem_key="K1")
        e2 = await sch.submit(session_id="s1", goal="幂等任务", workspace=ws, idem_key="K1")
        check(e1["task_id"] == e2["task_id"], "★★ 同幂等键 → 同一个 task_id")
        check(e2.get("idempotent_replay") is True, "★ 标记为幂等重放")
        check(len(await store.list_tasks()) - base_count == 5, "★ 没有多创建任务")

        # ---- ⑤ 取消：排队中的任务可以直接出队 ----
        # ★ 先等所有任务停下来 —— 否则同 session 限制会让 f1 也被排进队列
        drained = await wait_until(_all_terminal, timeout=40)
        check(drained, "★ 上一批任务已收尾（为并发测试腾出干净状态）")
        # ★ 再等全局放空 —— 幂等任务 e1 不属于 mine_ids，要单独等它结束，
        #   否则 f1(s1) 会被同 session 限制排进队列，取消测试就打不到运行中的任务
        await wait_until(lambda: sch.stats()["running"] == 0, timeout=30)
        await asyncio.sleep(0.2)

        # 先占满两个槽位
        f1 = await sch.submit(session_id="s1", goal="占用1", workspace=ws)
        f2 = await sch.submit(session_id="s2", goal="占用2", workspace=ws)
        g = await sch.submit(session_id="s3", goal="排队待取消", workspace=ws)
        check(g["queued"] is True, "G 在排队")

        r = await sch.cancel(g["task_id"])
        check(r["accepted"] is True, "★ 排队任务取消被接受")
        check(r["state"] == "aborted", f"★ 直接进入 aborted（实际 {r['state']}）")
        check("未执行任何步骤" in r["note"], "★ 说明它一步都没跑")

        # ---- ⑥ 取消运行中的任务：accepted 但终态要等 ----
        r2 = await sch.cancel(f1["task_id"])
        check(r2["accepted"] is True, f"★ 运行中的任务取消被接受（accepted={r2['accepted']}, state={r2['state']}, note={r2['note'][:40]}）")
        check("循环边界" in r2["note"] or "aborted" in r2["note"],
              f"★ 明确说明要到终态才算停（note={r2['note'][:46]}）")
        # 轮询真实状态
        stopped = await wait_until(
            lambda: asyncio.get_event_loop().is_running() and _status_of(sch, store, f1["task_id"]) == "aborted",
            timeout=10,
        )
        check(stopped, "★★ 最终状态确实变成 aborted（不是 accept 就算停）")

        # ---- ⑦ 已结束任务不能取消 / 审批 ----
        done_id = a["task_id"]
        r3 = await sch.cancel(done_id)
        check(r3["accepted"] is False, "★ 已结束的任务 cancel 返回 False")

        r4 = await sch.resolve_approval(done_id, "apr_x", "approved")
        check(r4["accepted"] is False and r4["reason"] in ("task_not_running", "task_finished"),
              f"★ 已结束任务审批被拒（{r4.get('reason')}）")

        # ---- ⑧ 审批 id 不匹配 ----
        # 用一个假 runtime 直接测校验分支（不起真审批，节省时间）
        r5 = await sch.resolve_approval("no_such_task", "apr_x", "approved")
        check(r5["accepted"] is False, "★ 不存在的任务审批被拒")

        await sch.shutdown(timeout=3)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _status_of(sch, store, task_id: str) -> str:
    rt = sch.runtime_of(task_id)
    if rt is not None:
        return rt.status
    row = store.get_task_sync(task_id)
    return row["status"] if row else ""




def test_http_and_sse(db: TmpDB) -> None:
    """HTTP 端点 + SSE（同步 TestClient，它内部有自己的 loop）。"""
    import asyncio as _aio
    from fastapi.testclient import TestClient
    from backend.server.app import sse_frame

    print("\n[F] HTTP 端点 + SSE")

    ws = tempfile.mkdtemp(prefix="loom_ws_")
    try:
        store = db.store()
        store.init_sync()
        store.upsert_session_sync(mk_session("s_http", ws=ws))

        hub = EventHub()
        sch = TaskScheduler(
            store=store, hub=hub, adapter=SlowAdapter(delay=0.1),
            registry=build_default_registry(),
            config=AgentConfig(max_turns=2, max_seconds=20, approval_timeout_s=2, min_evidence=0),
            max_global=2,
        )
        app = create_app(store=store, hub=hub, scheduler=sch)
        client = TestClient(app)

        # ---- health ----
        r = client.get("/api/health")
        check(r.status_code == 200, f"health 200（{r.status_code}）")
        body = r.json()
        check(body["ok"] is True and "scheduler" in body and "hub" in body, "health 含调度/缓冲信息")

        # ---- sessions ----
        r = client.get("/api/sessions")
        check(r.status_code == 200 and any(s["session_id"] == "s_http" for s in r.json()["sessions"]),
              "会话列表包含已建会话")

        r = client.post("/api/sessions", json={"title": "新会话", "workspace": ws})
        check(r.status_code == 200 and r.json()["session_id"], "新建会话")
        new_sid = r.json()["session_id"]

        # ---- 创建任务 ----
        r = client.post("/api/tasks", json={"goal": "看看目录", "session_id": "s_http", "workspace": ws})
        check(r.status_code == 200, f"创建任务 200（{r.status_code}）")
        created = r.json()
        tid = created["task_id"]
        check(created["session_id"] == "s_http", "任务带对的 session_id")
        check(created["status"] in ("running", "created"), f"初始状态 {created['status']}")

        # ---- 异常：会话不存在 ----
        r = client.post("/api/tasks", json={"goal": "x", "session_id": "nope", "workspace": ws})
        check(r.status_code == 404, f"★ 不存在的会话 → 404（{r.status_code}）")

        # ---- 异常：goal 为空 ----
        r = client.post("/api/tasks", json={"goal": "", "session_id": "s_http", "workspace": ws})
        check(r.status_code == 422, f"★ 空 goal → 422（{r.status_code}）")

        # ---- 幂等：同 key 两次 ----
        r1 = client.post("/api/tasks", json={"goal": "幂等", "session_id": new_sid, "workspace": ws},
                         headers={"Idempotency-Key": "H1"})
        r2 = client.post("/api/tasks", json={"goal": "幂等", "session_id": new_sid, "workspace": ws},
                         headers={"Idempotency-Key": "H1"})
        check(r1.json()["task_id"] == r2.json()["task_id"], "★★ HTTP 层幂等：同 key 同 task")
        check(r2.json().get("idempotent_replay") is True, "★ 标记幂等重放")

        # ---- 任务快照 ----
        r = client.get(f"/api/tasks/{tid}")
        check(r.status_code == 200, "任务快照 200")
        snap = r.json()
        check(snap["task_id"] == tid and snap["session_id"] == "s_http", "快照字段正确")
        check("events_available" in snap, "★ 快照带事件可用性")

        r = client.get("/api/tasks/no_such_task")
        check(r.status_code == 404, "★ 不存在的任务 → 404")

        # ---- 任务列表（按 session 过滤）----
        r = client.get("/api/tasks", params={"session_id": "s_http"})
        check(r.status_code == 200, "任务列表 200")
        check(all(t["session_id"] == "s_http" for t in r.json()["tasks"]), "★ 列表按 session 过滤")

        # ---- 取消 ----
        r = client.post(f"/api/tasks/{tid}/cancel")
        check(r.status_code == 200, "取消 200")
        check("accepted" in r.json() and "note" in r.json(), "★ 取消返回 accepted + note（不假装已停）")

        # ---- 审批：不匹配的 task → 409 ----
        r = client.post("/api/approvals/apr_nope/decision",
                        json={"task_id": tid, "decision": "approved", "reason": ""})
        check(r.status_code == 409, f"★★ 审批不匹配 → 409（{r.status_code}）")
        check("reason" in r.json()["detail"], "★ 409 里给出具体原因")

        # ---- SSE 帧格式 ----
        frame = sse_frame("tool.call", {"a": 1}, eid=7)
        check(frame.startswith("id: 7\nevent: tool.call\ndata: "), "SSE 帧含 id/event/data")
        check(frame.endswith("\n\n"), "SSE 帧以空行结束")
        gap_frame = sse_frame("stream.gap", {"reason": "evicted"})
        check(not gap_frame.startswith("id:"), "★★ gap 帧不写 id（不污染 Last-Event-ID）")

        # ---- 补发逻辑（直接调 hub.replay，不跨 loop）----
        from backend.events import Event as _Ev

        # ★ 用一个**干净的新 hub** 测补发，避免被前面端点测试产生的事件干扰
        iso_hub = EventHub()
        loop = _aio.new_event_loop()
        try:
            async def seed():
                for i in range(3):
                    ev = _Ev.make("thinking", {"text": f"步骤{i}", "phase": "", "done": True},
                                  task_id="task_sse")
                    ev.seq = i + 1          # Event.make 不分配 seq，这里手动给
                    await iso_hub.publish("task_sse", "s_http", ev)
            loop.run_until_complete(seed())
        finally:
            loop.close()

        replay, gap = iso_hub.replay(0)
        check(gap is None, "未淘汰时不产生 gap")
        check(len(replay) == 3, f"★ since=0 补发全部 3 条（实际 {len(replay)}）")
        check(all("id" in r for r in replay), "★ 每条补发记录都带全局 id")
        check(all("seq" in r["event"] for r in replay), "★★ data 里保留 event.seq（任务内序号）")
        check(replay[0]["id"] < replay[-1]["id"], "★ 全局 id 递增")

        # Last-Event-ID 语义：只补 after 的部分
        mid = replay[1]["id"]
        replay2, _ = iso_hub.replay(mid)
        check(len(replay2) == 1, f"★★ Last-Event-ID=mid → 只补后面 1 条（实际 {len(replay2)}）")

        # ---- 淘汰后 since 太旧 → gap ----
        small_hub = EventHub(max_events=2)
        loop2 = _aio.new_event_loop()
        try:
            async def seed_many():
                for i in range(6):
                    await small_hub.publish(
                        "t", "s_http", _Ev.make("thinking", {"text": str(i)}, task_id="t")
                    )
            loop2.run_until_complete(seed_many())
        finally:
            loop2.close()

        evs_gap, gap2 = small_hub.replay(0)
        check(evs_gap == [], "★ 已淘汰的历史不补发")
        check(gap2 is not None and gap2["type"] == "stream.gap", "★★ 淘汰后 → stream.gap")
        check(gap2["data"]["reason"] == "evicted", "★ gap 说明原因 evicted")
        check("快照" in gap2["data"]["note"], "★ gap 提示走快照恢复")
        check("oldest_available" in gap2["data"], "★ 告知最旧可用 id")

        # ---- 慢客户端：队列满了丢最旧、保留最新，并置 need_gap ----
        slow_hub = EventHub(max_events=100, sub_queue_size=3)
        slow_sub = slow_hub.subscribe()
        loop3 = _aio.new_event_loop()
        try:
            async def flood():
                for i in range(6):
                    await slow_hub.publish(
                        "t", "s", _Ev.make("thinking", {"text": f"e{i}"}, task_id="t")
                    )
            loop3.run_until_complete(flood())
        finally:
            loop3.close()

        check(slow_sub.queue.qsize() == 3, f"★ 队列达到上限（{slow_sub.queue.qsize()}/3）")
        check(slow_sub.need_gap is True, "★★ 慢客户端被标记 need_gap（下次读会收到 gap 帧）")
        check(slow_sub.dropped >= 3, f"★ 记录丢弃数（{slow_sub.dropped}）")
        # 保留的是最新的三条
        last = slow_sub.queue.get_nowait()
        texts = []
        while not slow_sub.queue.empty():
            texts.append(slow_sub.queue.get_nowait()["event"]["data"]["text"])
        check("e5" in texts, f"★★ 保留的是最新事件（{texts}）")

        # ---- 事件历史接口：不可用时明确告知 ----
        r = client.get("/api/tasks/task_sse/events")   # 这个 task 不在 store 里
        check(r.status_code == 404, f"★ 不存在的任务查事件 → 404（{r.status_code}）")

        # ★ 收尾：把两个 scheduler 里还在跑的任务停掉，否则它们会继续写
        #   已经被 cleanup 删掉的数据库 → "readonly database"
        client.close()
        loop_sd = _aio.new_event_loop()
        try:
            loop_sd.run_until_complete(sch.shutdown(timeout=3))
        finally:
            loop_sd.close()

    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
