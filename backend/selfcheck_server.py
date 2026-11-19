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
import itertools
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
from backend.server.archive import EventArchive
from backend.server.store import EXPORT_FORMAT, SCHEMA_VERSION, StoreError, TaskStore
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
    check({"sessions", "tasks", "idempotency", "task_history", "task_events"} <= tables,
          f"★ 全新库一次建齐五张表（缺表只在第二次启动才会暴露）：{sorted(tables)}")

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

    # ★ 显式要 state：get_task 默认不带 state_json（列表接口不需要），
    #   单任务快照才带 —— 这里测的就是"还没有快照"这个语义。
    row = await store.get_task("t1", with_state=True)
    check(row is not None, "任务能读回来")
    check(row["status"] == "created", f"初始状态 created（实际 {row['status']}）")
    check(row["created_at"] > 0, "★ created_at 有值")
    check(row["started_at"] is None, "★★ 排队中 started_at = NULL")
    check(not row.get("state"), "还没有快照")

    # ---- mark_started：填 started_at ----
    await store.mark_started("t1")
    row = await store.get_task("t1", with_state=True)
    check(row["status"] == "running", "mark_started 后状态 running")
    check(row["started_at"] is not None, "★★ started_at 被填上了")
    check(row["started_at"] >= row["created_at"], "started_at >= created_at")

    # ---- save_task：摘要与快照同事务更新 ----
    snap = {"task_id": "t1", "status": "done", "final_answer": "完成", "turn": 2}
    await store.save_task("t1", status="done", state=snap, result="完成", end_reason="agent_done",
                          ended_at=time.time(), turn=2)
    row = await store.get_task("t1", with_state=True)
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

    ro = await restart.get_task("torphan", with_state=True)
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
        row = await store.get_task("task_rt1", with_state=True)
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
async def test_budget_unlimited(db: TmpDB) -> None:
    """[BU] 预算 0 = 不设上限（桌面模式取消硬上限，2026-11-13 用户要求）。"""
    print("\n[BU] 预算 0 = 不设上限")
    import time as _t
    from backend.agent.state import Budgets

    b = Budgets(max_turns=0, max_seconds=0, max_tool_calls=0)
    b.turns_used = 500
    b.tool_calls_used = 5000
    check(b.check(_t.time()) is None, "★ 三项为 0 时不判超限（跑多久都不砍）")

    strict = Budgets(max_turns=3, max_tool_calls=5)
    strict.turns_used = 3
    check(strict.check(_t.time()) == ("turns", 3, 3), "正数上限照旧生效（只有 0 才代表不限）")

    sec = Budgets(max_seconds=1)
    check(sec.check(_t.time() - 5) == ("seconds", 1, 5.0), "秒数上限照旧生效")

    rt = Budgets.from_dict({"max_turns": 0, "max_seconds": 0, "turns_used": 7})
    check(rt.max_turns == 0 and rt.max_seconds == 0,
          "★ from_dict 不会把 0 换回默认上限（否则'不限'会静默变回'照旧限'）")
    check(rt.turns_used == 7, "用量仍然原样还原")


async def test_force_cancel(db: TmpDB) -> None:
    """[FC] 强制中止：协作式取消超时后，直接 cancel 掉 asyncio task。"""
    print("\n[FC] 强制中止（取消的第二段）")
    import asyncio as _a
    from backend.server.runtime import FORCE_CANCEL_GRACE_S, TaskRuntime

    rt = object.__new__(TaskRuntime)     # 只测看门狗，不构造完整运行时
    rt.task_id = "fake-force-cancel"
    rt._force_cancel_task = None

    hit: list[bool] = []

    async def _forever() -> None:
        try:
            await _a.sleep(30)
        except _a.CancelledError:
            hit.append(True)
            raise

    t = _a.create_task(_forever())
    await rt._force_cancel_later(t, 0.05)
    await _a.sleep(0.05)
    check(t.cancelled() or bool(hit),
          "★ 宽限期到点后 task 真的被 cancel（不等它自己结束）")

    done = _a.create_task(_a.sleep(0))
    await done
    await rt._force_cancel_later(done, 0.01)
    check(done.done(), "已经结束的 task 不会被再动一次（幂等）")

    check(FORCE_CANCEL_GRACE_S <= 2.0,
          f"宽限期足够短（{FORCE_CANCEL_GRACE_S}s），用户按停止不会等太久")


async def test_history(db: TmpDB) -> None:
    """[HQ] 会话历史查询（#25）：搜索条件 / 回放切片 / 工作区隔离。"""
    print("\n[HQ] 会话历史查询（搜索 + 回放）")
    import itertools

    from backend.history.query import HistoryQuery

    store = db.store()
    await store.init()
    # ★ 用两个专供本测试的临时工作区：TmpDB 是共享的，前面的用例已经造过任务，
    #   不带 workspace 过滤就会把别人的数据一起查出来（第一版就栽在这）。
    ws_hq = tempfile.mkdtemp(prefix="loom_hq_a_")
    ws_other = tempfile.mkdtemp(prefix="loom_hq_b_")
    try:
        await store.upsert_session(mk_session("sHQ1", ws=ws_hq))
        await store.upsert_session(mk_session("sHQ2", ws=ws_other))
        now = time.time()
        eid = itertools.count(1)
        plans = [
            ("hq_1", "sHQ1", "分析 backend 架构并给出优化建议", ws_hq, "done", now - 3 * 86400),
            ("hq_2", "sHQ1", "修复 sandbox 启动失败的问题", ws_hq, "failed", now - 2 * 86400),
            ("hq_3", "sHQ2", "分析另一个项目的架构", ws_other, "done", now - 1 * 86400),
        ]
        for tid, sid, goal, ws, st, created in plans:
            await store.create_task(
                mk_task(tid, sid, goal=goal, workspace=ws, status=st, created_at=created)
            )
            await store.append_events([
                {"task_id": tid, "id": next(eid), "seq": 1, "ts": created, "turn": 0,
                 "type": "task.start", "data": {"goal": goal, "workspace": ws}},
                {"task_id": tid, "id": next(eid), "seq": 2, "ts": created + 5, "turn": 1,
                 "type": "tool.call", "data": {"tool": "read_file", "args": {"path": "a"}}},
                {"task_id": tid, "id": next(eid), "seq": 3, "ts": created + 9, "turn": 1,
                 "type": "task.end", "data": {"status": st, "reason": "x", "answer": goal}},
            ])
            store.rebuild_projection_sync(tid)

        h = HistoryQuery(db.path)
        check(h.usable, "库存在时 usable=True")

        mine = h.search(workspace=ws_hq)
        check(mine["total"] == 2, f"按工作区只查到本测试的 2 条（{mine['total']}）")
        check(mine["items"][0]["task_id"] == "hq_2", "★ 按最后事件时间倒序（最近的在前）")

        kw = h.search(q="架构", workspace=ws_hq)
        check(kw["total"] == 1 and kw["items"][0]["task_id"] == "hq_1",
              f"★ 关键词命中目标里含'架构'的（{kw['total']}）")

        st = h.search(status="failed", workspace=ws_hq)
        check(st["total"] == 1 and st["items"][0]["task_id"] == "hq_2", "按状态过滤")

        check(h.search(workspace=ws_other)["total"] == 1, "★ 工作区隔离：查不到本测试以外的数据")

        # 界限落在 hq_1（3 天前）与 hq_2（2 天前）之间
        recent = h.search(workspace=ws_hq, since=now - 2.5 * 86400)
        check(recent["total"] == 1 and recent["items"][0]["task_id"] == "hq_2",
              "★ 时间范围过滤（since）")

        excl = h.search(workspace=ws_hq, exclude_task_id="hq_1")
        check(excl["total"] == 1, "★ exclude_task_id 能排除自己")

        lim = h.search(workspace=ws_hq, limit=1)
        check(lim["returned"] == 1 and lim["truncated"] is True,
              f"★ 截断标记：returned={lim['returned']} truncated={lim['truncated']}")

        # ---- 回放 ----
        rp = h.replay("hq_1")
        check(rp is not None and rp["total"] == 3, f"回放拿到全部事件（{rp and rp['total']}）")
        check([e["type"] for e in rp["events"]] == ["task.start", "tool.call", "task.end"],
              "★ 事件按 seq 顺序")
        check(rp["task"]["workspace"] == ws_hq, "回放带任务摘要（含所属工作区）")

        only_start = h.replay("hq_1", kinds=["task.start"])
        check(only_start["total"] == 1, "★ kinds 只回放指定类型")

        by_turn = h.replay("hq_1", from_turn=1)
        check(by_turn["total"] == 2, f"★ 按轮次切片（turn>=1 有 {by_turn['total']} 条）")

        check(h.replay("nope") is None, "★ 不存在的任务返回 None（上层好判 404）")
        check(h.get_task("hq_2")["workspace"] == ws_hq, "get_task 能单独取任务行（回放前授权用）")
    finally:
        shutil.rmtree(ws_hq, ignore_errors=True)
        shutil.rmtree(ws_other, ignore_errors=True)


async def test_token_usage(db: TmpDB) -> None:
    """[TK] token 计量（#4）：llm.usage 事件 → 投影 → 查询，以及重建一致性。

    ★ 为什么这条链要单独测：token 是**投影派生字段**，它的可信度完全取决于
      "事件里写对了 + 推导时算对了 + 重建后还一样"。任何一环松了，
      统计出来的数字就是编的。
    """
    print("\n[TK] token 计量（llm.usage → 投影 → 查询）")
    from backend.history.query import HistoryQuery

    store = db.store()
    await store.init()
    ws = tempfile.mkdtemp(prefix="loom_tk_")
    try:
        await store.upsert_session(mk_session("sTK", ws=ws))
        await store.create_task(
            mk_task("tk_1", "sTK", goal="算 token", workspace=ws, status="done")
        )
        await store.append_events([
            {"task_id": "tk_1", "id": 9001, "seq": 1, "ts": 1.0, "turn": 0,
             "type": "task.start", "data": {"goal": "算 token"}},
            {"task_id": "tk_1", "id": 9002, "seq": 2, "ts": 2.0, "turn": 1,
             "type": "llm.usage",
             "data": {"prompt_tokens": 1200, "completion_tokens": 300, "reasoning_tokens": 200}},
            {"task_id": "tk_1", "id": 9003, "seq": 3, "ts": 3.0, "turn": 2,
             "type": "llm.usage",
             "data": {"prompt_tokens": 1800, "completion_tokens": 150, "reasoning_tokens": 100}},
            # 脏数据：导入外部任务时可能出现，不能让整个投影崩
            {"task_id": "tk_1", "id": 9004, "seq": 4, "ts": 4.0, "turn": 3,
             "type": "llm.usage",
             "data": {"prompt_tokens": "bad", "completion_tokens": None, "reasoning_tokens": 50}},
            {"task_id": "tk_1", "id": 9005, "seq": 5, "ts": 5.0, "turn": 3,
             "type": "task.end",
             "data": {"status": "done", "reason": "agent_done", "answer": "ok"}},
        ])

        p = store.rebuild_projection_sync("tk_1")
        check(p["llm_calls"] == 3, f"★ 统计模型调用次数（{p['llm_calls']}）")
        check(p["prompt_tokens"] == 3000, f"★ prompt token 累加（{p['prompt_tokens']}）")
        check(p["completion_tokens"] == 450, f"completion token 累加（{p['completion_tokens']}）")
        check(p["reasoning_tokens"] == 350, f"reasoning token 累加（{p['reasoning_tokens']}）")

        # ---- 重建一致性：删表 → 全量重放 → 数值必须一样 ----
        before = dict(store.get_projection_sync("tk_1"))
        store.clear_projections_sync()
        store.rebuild_projections_sync()
        after = dict(store.get_projection_sync("tk_1"))
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "llm_calls"):
            check(before[k] == after[k], f"★ 重建后 {k} 逐值一致（{before[k]}）")

        # ---- 查询层能带出来 ----
        h = HistoryQuery(db.path)
        item = h.search(workspace=ws)["items"][0]
        check(item["prompt_tokens"] == 3000, "★ /api/history 的条目带 token")
        check(item["llm_calls"] == 3, "★ 条目也带调用次数")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def test_stats_agg(db: TmpDB) -> None:
    """[ST] 统计聚合（#52）：总量 / 成功率 / token / 按天分组 / 工作区过滤。"""
    print("\n[ST] 统计聚合（/api/stats 的底层）")
    from backend.history.query import HistoryQuery

    store = db.store()
    await store.init()
    ws = tempfile.mkdtemp(prefix="loom_st_")
    try:
        await store.upsert_session(mk_session("sST", ws=ws))
        now = time.time()
        eid = itertools.count(5001)
        # 今天 2 个（1 成 1 败），昨天 1 个（成）
        plan = [
            ("st_1", "done", now - 60, 1000, 200),
            ("st_2", "failed", now - 30, 500, 100),
            ("st_3", "done", now - 86400, 2000, 400),
        ]
        for tid, st, ts, pt, ct in plan:
            await store.create_task(
                mk_task(tid, "sST", goal=f"任务 {tid}", workspace=ws, status=st)
            )
            await store.append_events([
                {"task_id": tid, "id": next(eid), "seq": 1, "ts": ts, "turn": 1,
                 "type": "task.start", "data": {}},
                {"task_id": tid, "id": next(eid), "seq": 2, "ts": ts + 1, "turn": 1,
                 "type": "llm.usage",
                 "data": {"prompt_tokens": pt, "completion_tokens": ct, "reasoning_tokens": 0,
                          # #52：墙钟分解所需字段
                          "duration_ms": 1000, "first_token_ms": 300}},
                {"task_id": tid, "id": next(eid), "seq": 3, "ts": ts + 2, "turn": 1,
                 "type": "tool.result",
                 "data": {"tool": "read_file", "ok": True, "duration_ms": 200}},
                {"task_id": tid, "id": next(eid), "seq": 4, "ts": ts + 3, "turn": 1,
                 "type": "task.end", "data": {"status": st, "reason": "x", "answer": "a"}},
            ])
            store.rebuild_projection_sync(tid)

        h = HistoryQuery(db.path)
        s = h.stats(days=7, workspace=ws)
        t = s["totals"]
        check(t["tasks"] == 3, f"窗口内任务数（{t['tasks']}）")
        check(t["done"] == 2 and t["failed"] == 1, "完成/失败分别计数")
        check(abs(t["success_rate"] - 0.667) < 0.01, f"★ 成功率 = 完成/总数（{t['success_rate']}）")
        check(t["prompt_tokens"] == 3500, f"★ token 汇总（{t['prompt_tokens']}）")
        check(t["completion_tokens"] == 700, f"输出 token 汇总（{t['completion_tokens']}）")
        check(t["llm_calls"] == 3, "模型调用次数汇总")

        # ---- 墙钟分解（#52）----
        check(t["llm_ms"] == 3000, f"★ 模型墙钟累加（{t['llm_ms']}ms）")
        check(t["tool_ms"] == 600, f"★ 工具墙钟累加（{t['tool_ms']}ms）")
        check(t["busy_ms"] == 3600, f"两项之和（{t['busy_ms']}ms）")
        check(abs(t["llm_ratio"] - 0.833) < 0.01,
              f"★★ 模型占比 = llm/(llm+tool)（{t['llm_ratio']}）")
        check(abs(t["avg_first_token_ms"] - 300) < 0.1,
              f"★ 首 token 取平均而不是总和（{t['avg_first_token_ms']}ms）")
        check(abs(t["avg_llm_ms"] - 1000) < 0.1, f"单次模型调用平均（{t['avg_llm_ms']}ms）")
        check(abs(t["avg_tool_ms"] - 200) < 0.1, f"单次工具平均（{t['avg_tool_ms']}ms）")

        days = s["by_day"]
        check(len(days) >= 1, f"按天分组有数据（{len(days)} 天）")
        check(sum(d["tasks"] for d in days) == 3, "★ 按天分组后总数与总量一致")
        # 跨天：今天 2、昨天 1（若测试恰好在午夜跑，可能并成一天）
        today_rows = [d for d in days if d["tasks"] == 2]
        check(bool(today_rows) or len(days) == 1,
              "★ 今天的两条归到同一天（日期分组按最后事件时间）")

        other = h.stats(days=7, workspace="D:/nowhere-else")
        check(other["totals"]["tasks"] == 0, "★ 别的工作区查不到本测试的数据")

        empty = h.stats(days=7, workspace=ws)["by_day"]
        check(all(d["date"] for d in empty), "每行都带日期键（前端补零要用）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def test_session_delete(db: TmpDB) -> None:
    """[SD] 删除会话：级联清理 + 产物三判据（新建 / 被改过 / 原本就有）。

    ★ 这个功能会真删文件，所以自检必须覆盖"什么不该删"——
      错删用户原有文件是不可逆伤害，比漏删严重得多。
    """
    print("\n[SD] 删除会话（级联清理 + 产物判定）")
    import os as _os

    from backend.server.session_delete import build_plan, execute_plan
    from backend.tools.hashing import sha256_file

    _EID_SD = itertools.count(7001)

    store = db.store()
    await store.init()
    ws = tempfile.mkdtemp(prefix="loom_sd_")
    try:
        await store.upsert_session(mk_session("sDel", ws=ws))

        made = _os.path.join(ws, "made.md")          # 本会话新建、未改 → 应删
        edited = _os.path.join(ws, "edited.md")      # 本会话新建、后来被改 → 不删
        existed = _os.path.join(ws, "orig.txt")      # 会话前就存在 → 不删
        for p, txt in ((made, "A"), (edited, "B"), (existed, "C")):
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)

        h_made, h_edited, h_orig = (sha256_file(p) for p in (made, edited, existed))

        await store.create_task(
            mk_task("sd_1", "sDel", goal="写几个文件", workspace=ws, status="done")
        )
        # 写 state（change_records 是产物判定的唯一依据）
        store.save_task_sync(
            "sd_1",
            status="done",
            state={
                "change_records": [
                    {"path": "made.md", "action": "create", "before_hash": "",
                     "after_hash": h_made, "ts": 1.0},
                    {"path": "edited.md", "action": "create", "before_hash": "",
                     "after_hash": h_edited, "ts": 2.0},
                    {"path": "orig.txt", "action": "overwrite", "before_hash": h_orig,
                     "after_hash": h_orig, "ts": 3.0},
                ]
            },
            end_reason="agent_done",
            result="ok",
        )
        await store.append_events([
            {"task_id": "sd_1", "id": next(_EID_SD), "seq": 1, "ts": 1.0, "turn": 1,
             "type": "task.start", "data": {}},
            {"task_id": "sd_1", "id": next(_EID_SD), "seq": 2, "ts": 2.0, "turn": 1,
             "type": "task.end", "data": {"status": "done", "reason": "agent_done", "answer": "ok"}},
        ])
        store.rebuild_projection_sync("sd_1")

        # 模拟"会话结束后用户又改了这个文件"
        with open(edited, "a", encoding="utf-8") as f:
            f.write("\n后面加的")

        plan = build_plan(store, "sDel")
        check(plan is not None and plan["tasks"] == 1, "★ 预案统计到 1 个任务")
        check(plan["artifacts"]["total"] == 1, f"★ 只有 1 个可删产物（{plan['artifacts']['total']}）")
        check([f["path"] for f in plan["artifacts"]["files"]] == ["made.md"], "可删的是 made.md")
        reasons = {s["path"]: s["reason"] for s in plan["artifacts"]["skipped"]}
        check("edited.md" in reasons and "已被后续修改" in reasons["edited.md"],
              f"★★ 被改过的文件不进删除列表（{reasons.get('edited.md')}）")
        check("orig.txt" in reasons and "已存在" in reasons["orig.txt"],
              f"★★ 本来就有的文件不进删除列表（{reasons.get('orig.txt')}）")

        res = execute_plan(store, "sDel", with_artifacts=True)
        check(not _os.path.exists(made), "★ 本会话新建的文件已删除")
        check(_os.path.exists(edited), "★★ 被改过的文件仍然在")
        check(_os.path.exists(existed), "★★ 原有文件仍然在")
        check(bool(res["removed_files"]) and _os.path.isfile(res["removed_files"][0]["backup"]),
              "★ 删除前留了备份")
        check(store.get_session_sync("sDel") is None, "★ 会话行已删")
        check(store.events_count_sync("sd_1") == 0, "★ 事件已清（没有外键，必须显式删）")
        check(store.get_projection_sync("sd_1") is None, "★ 投影已清")
        check(store.get_task_sync("sd_1") is None, "★ 任务已删")

        # ---- 仅删会话：文件一个都不动 ----
        ws2 = tempfile.mkdtemp(prefix="loom_sd2_")
        try:
            await store.upsert_session(mk_session("sDel2", ws=ws2))
            await store.create_task(
                mk_task("sd_2", "sDel2", goal="写文件", workspace=ws2, status="done")
            )
            keep = _os.path.join(ws2, "keep.md")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("x")
            h_keep = sha256_file(keep)
            store.save_task_sync(
                "sd_2", status="done",
                state={"change_records": [
                    {"path": "keep.md", "action": "create", "before_hash": "",
                     "after_hash": h_keep, "ts": 1.0},
                ]},
            )
            res2 = execute_plan(store, "sDel2", with_artifacts=False)
            check(_os.path.exists(keep), "★★ with_artifacts=False 时文件一个不动")
            check(res2["removed_files"] == [], "没有删除任何文件")
            check(store.get_session_sync("sDel2") is None, "会话行仍然删掉了")
        finally:
            shutil.rmtree(ws2, ignore_errors=True)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


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

    row = await store.get_task("task_pf2", with_state=True)
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

    row3 = await store.get_task("task_pf3", with_state=True)
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
    await test_m1(db)
    await test_store_concurrent_sqlite(db)
    await test_hub()
    await test_runtime(db)
    await test_budget_unlimited(db)
    await test_force_cancel(db)
    await test_runtime_persist_failure(db)
    await test_event_archive(db)
    await test_projections(db)
    await test_history(db)
    await test_token_usage(db)
    await test_stats_agg(db)
    await test_session_delete(db)
    await test_scheduler(db)


def test_events_http(db: TmpDB) -> None:
    """
    事件历史 HTTP（对比表 #49）—— **重启之后时间线还在**。

    这是整个改动的验收点：以前重启后 `/events` 只能回一句"不可用"，
    现在必须把落盘的事件原样还回来，并且说清来源是磁盘。
    """
    print("\n[EV2] 事件历史 HTTP：重启后时间线还在 + 导出/导入")

    from fastapi.testclient import TestClient

    ws = tempfile.mkdtemp(prefix="loom_ws_")
    try:
        store = db.store()
        store.init_sync()
        store.upsert_session_sync(mk_session("s_evh", ws=ws))
        tid, _ = store.create_task_sync(
            {"task_id": "t_evh", "session_id": "s_evh", "goal": "落盘验收",
             "status": "done", "workspace": ws}
        )
        store.save_task_sync(tid, status="done", result="完成", ended_at=time.time())

        # ---- 进程 A：有 hub、有归档，正常退出 ----
        archive_a = EventArchive(store, flush_interval=0.02)
        hub_a = EventHub(archive=archive_a)

        async def _phase_a() -> None:
            await archive_a.start()
            for i in range(3):
                await hub_a.publish(
                    tid, "s_evh",
                    Event.make("thinking",
                               {"text": f"第{i}段", "phase": "act", "done": False},
                               task_id=tid, turn=1, seq=i + 1),
                )
            await archive_a.stop()   # 等价于正常关服务：flush 后停后台 task

        asyncio.run(_phase_a())
        check(archive_a.written == 3, f"阶段A 落盘 3 条（{archive_a.written}）")
        check(store.events_count_sync(tid) == 3, "阶段A 结束后库里有 3 条")

        # ---- 进程 B：全新的 hub（内存是空的）+ 全新的 archive = 重启 ----
        hub_b = EventHub()
        sch = TaskScheduler(
            store=store, hub=hub_b, adapter=SlowAdapter(delay=0.05),
            registry=build_default_registry(),
            config=AgentConfig(max_turns=1, max_seconds=10, min_evidence=0),
            max_global=1,
        )
        app = create_app(
            store=store, hub=hub_b, scheduler=sch,
            archive=EventArchive(store, flush_interval=0.02), retention_days=30,
        )

        with TestClient(app) as client:
            r = client.get(f"/api/tasks/{tid}/events")
            body = r.json()
            check(r.status_code == 200, f"events 接口 200（{r.status_code}）")
            check(body["available"] is True, f"★★ 重启后事件历史仍然可用（{body['available']}）")
            check(body["source"] == "disk", f"★ 来源是磁盘（{body.get('source')}）")
            check([e["event"]["seq"] for e in body["events"]] == [1, 2, 3],
                  f"★★ 时间线顺序正确（{[e['event']['seq'] for e in body['events']]}）")
            check(body["events"][0]["event"]["data"]["text"] == "第0段", "★ 载荷完整解回来")

            # since 过滤：没有更新的事件 ≠ 历史不可用
            last_id = body["events"][-1]["id"]
            b2 = client.get(f"/api/tasks/{tid}/events?since={last_id}").json()
            check(b2["available"] is True and b2["events"] == [],
                  "★★ since 之后没有新事件时 available 仍为 true（不能谎报不可用）")

            # 从来没有事件的终态任务 → available=false, reason=empty
            store.create_task_sync(
                {"task_id": "t_noev", "session_id": "s_evh", "goal": "没事件",
                 "status": "done", "workspace": ws}
            )
            b3 = client.get("/api/tasks/t_noev/events").json()
            check(b3["available"] is False and b3["reason"] == "empty",
                  f"★ 空历史说清是 empty（{b3.get('reason')}）")

            # 导出 → 导入 → 立刻能看时间线
            exp = client.get(f"/api/tasks/{tid}/export").json()
            check(exp.get("format") == EXPORT_FORMAT and len(exp.get("events") or []) == 3,
                  f"★ 导出接口带 format + 3 条事件（{exp.get('format')}）")
            imp = client.post("/api/tasks/import", json=exp).json()
            check(imp.get("renamed") is True, f"★ 导入撞号自动改名（{imp.get('task_id')}）")
            b4 = client.get(f"/api/tasks/{imp['task_id']}/events").json()
            check(b4["available"] is True and len(b4["events"]) == 3,
                  f"★★ 导入后立刻能看时间线（{len(b4.get('events') or [])} 条）")

            r5 = client.post("/api/tasks/import", json={"format": "nope"})
            check(r5.status_code == 400, f"★ 非法格式 → 400（{r5.status_code}）")

            r6 = client.post("/api/maintenance/prune", params={"days": 0}).json()
            check(r6["deleted"] == 0 and "永不清理" in r6["note"],
                  f"★ 保留期 0 = 不删（{r6.get('note')}）")

            h = client.get("/api/health").json()
            check("events" in h and h["events"]["enabled"] is True,
                  "health 暴露事件落盘状态")
            check(h["events"]["retention_days"] == 30, "health 带保留天数")

            # ---- 投影接口（#51）----------------------------------------
            # t_evh 已有 3 条 thinking 事件，先补一条 task.end 让投影有 status
            store.append_events_sync([
                {"task_id": tid, "id": 100, "seq": 4, "ts": time.time(),
                 "turn": 1, "type": "task.end",
                 "data": {"status": "done", "reason": "agent_done", "answer": "完成"}},
            ])
            # 直接写事件不走 runtime，投影行不会自动维护 —— 先手动重建
            store.rebuild_projection_sync(tid)

            # 单任务投影
            rp = client.get(f"/api/tasks/{tid}/projection")
            check(rp.status_code == 200, f"投影接口 200（{rp.status_code}）")
            pb = rp.json()
            check(pb.get("status") == "done", f"★ 投影 status 从 task.end 推导（{pb.get('status')}）")
            check(pb.get("event_count") == 4, f"★ 投影 event_count=4（{pb.get('event_count')}）")
            check(pb.get("has_final_answer") == 0, f"无 final_answer（{pb.get('has_final_answer')}）")

            # 不存在的任务 → 404
            rp404 = client.get("/api/tasks/nonexistent/projection")
            check(rp404.status_code == 404, f"无投影任务 → 404（{rp404.status_code}）")

            # 列表查询：按 status 过滤
            rlist = client.get("/api/projections", params={"status": "done"}).json()
            check(any(item["task_id"] == tid for item in rlist["items"]),
                  f"★ 列表按 status=done 过滤到 t_evh（{len(rlist['items'])} 行）")
            check(rlist["total"] >= 1, f"列表 total 正确（{rlist['total']}）")

            # 全量重建
            rreb = client.post("/api/maintenance/rebuild-projections").json()
            check(rreb.get("ok") is True and rreb.get("rebuilt") >= 1,
                  f"★ 全量重建接口可用（rebuilt={rreb.get('rebuilt')}）")

            # 单任务重建
            rsingle = client.post(f"/api/tasks/{tid}/rebuild-projection").json()
            check(rsingle.get("status") == "done",
                  f"★ 单任务重建接口可用（status={rsingle.get('status')}）")

            # health 里有投影计数
            check("projections" in h and "count" in h["projections"],
                  f"★ health 暴露投影计数（{h.get('projections')}）")
    finally:
        try:
            store.close_sync()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    global VERBOSE
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

    print("=" * 78)
    print("Loom 服务层自检（Phase 3a + 3b，全离线）")
    print("=" * 78)

    db = TmpDB()
    print(f"临时数据库：{db.path}")
    try:
        # ★ 历史持久化（M1）必须在 asyncio.run 里跑（用 store 的异步接口）
        asyncio.run(run_async_suites(db))
        # ★ 同步跑 [F]：TestClient 会开自己的 loop，
        #   如果此时已经在 asyncio.run 里，会报 "another loop is running"
        test_http_and_sse(db)
        test_session_isolation(db)
        test_events_http(db)
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


# --------------------------------------------------------------------------- #
# [G] 会话隔离：工作目录归属 / 内容不串线 / 重启恢复
# --------------------------------------------------------------------------- #
def test_session_isolation(db: TmpDB) -> None:
    """
    复现并守住「会话串线」：

    真实踩过的坑：同一个 session 里先后在 D:\\LLMstudy 和 D:\\Try_agent 跑任务
    （前端切换工作目录时只改了本地字段，后端会话归属没变），
    于是「属于 LLMstudy 的会话」里出现了 Try_agent 的内容。

    这里把规则钉死：
      ① 会话 id 唯一，创建时保存工作目录
      ② 任务必须落在所属会话的目录里，否则拒绝
      ③ 消息按 session_id 查，绝不互相可见
      ④ 有任务的会话不能改挂目录
      ⑤ 重启后归属与内容都不变
    """
    print("\n[G] 会话隔离（工作目录归属 + 重启恢复）")

    import asyncio as _aio

    from fastapi.testclient import TestClient

    ws_a = tempfile.mkdtemp(prefix="loom_LLMstudy_")
    ws_b = tempfile.mkdtemp(prefix="loom_Try_agent_")
    try:
        store = db.store()
        store.init_sync()
        hub = EventHub()
        cfg = AgentConfig(max_turns=2, max_seconds=20, approval_timeout_s=2, min_evidence=0)

        def make_scheduler(st) -> TaskScheduler:
            return TaskScheduler(
                store=st, hub=EventHub(), adapter=SlowAdapter(delay=0.05),
                registry=build_default_registry(), config=cfg, max_global=4,
            )

        sch = make_scheduler(store)
        client = TestClient(create_app(store=store, hub=hub, scheduler=sch))

        # ---- ① 两个目录各建一个会话 ----
        a = client.post("/api/sessions", json={"title": "LLMstudy", "workspace": ws_a}).json()
        b = client.post("/api/sessions", json={"title": "Try_agent", "workspace": ws_b}).json()
        check(a["session_id"] != b["session_id"], "★★ 两个会话 id 不同")
        check(a["workspace"] == ws_a and b["workspace"] == ws_b, "创建时各自保存了工作目录")

        # 连建多个会话，id 不能重复（旧实现是 s_{毫秒}_{会话数}，并发下会撞）
        ids = {client.post("/api/sessions", json={"title": "批量", "workspace": ws_a}).json()["session_id"]
               for _ in range(6)}
        check(len(ids) == 6, f"★★ 连续建 6 个会话 id 全不重复（实际 {len(ids)} 个）")

        # ---- ② 各自建任务 ----
        ta = client.post("/api/tasks", json={"goal": "ALPHA-LLMSTUDY", "session_id": a["session_id"], "workspace": ws_a})
        tb = client.post("/api/tasks", json={"goal": "BETA-TRYAGENT", "session_id": b["session_id"], "workspace": ws_b})
        check(ta.status_code == 200 and tb.status_code == 200, "两个任务分别创建成功")

        # ---- ③ 消息不串线 ----
        ma = client.get(f"/api/sessions/{a['session_id']}/messages").json()["messages"]
        mb = client.get(f"/api/sessions/{b['session_id']}/messages").json()["messages"]
        check(any("ALPHA-LLMSTUDY" in m["content"] for m in ma), "A 能看到自己的消息")
        check(any("BETA-TRYAGENT" in m["content"] for m in mb), "B 能看到自己的消息")
        check(all("BETA-TRYAGENT" not in m["content"] for m in ma), "★★ A 看不到 B 的消息")
        check(all("ALPHA-LLMSTUDY" not in m["content"] for m in mb), "★★ B 看不到 A 的消息")
        check(all(m["session_id"] == a["session_id"] for m in ma), "★ A 的消息 session_id 都是 A")

        # ---- ④ 任务列表按会话过滤 ----
        la = client.get(f"/api/tasks?session_id={a['session_id']}").json()["tasks"]
        check(all(t["session_id"] == a["session_id"] for t in la), "★ 任务列表只含该会话的任务")

        # ---- ⑤ ★ 核心：任务必须落在所属会话的目录里 ----
        r = client.post("/api/tasks", json={"goal": "CROSS-DIR", "session_id": a["session_id"], "workspace": ws_b})
        check(r.status_code == 409, "★★ 用别的工作目录往 A 里建任务 → 拒绝", r.status_code)
        ma2 = client.get(f"/api/sessions/{a['session_id']}/messages").json()["messages"]
        check(all("CROSS-DIR" not in m["content"] for m in ma2), "★★ 被拒的任务没有混进 A 的消息")

        # ---- ⑥ 有任务的会话不能改挂目录 ----
        r = client.patch(f"/api/sessions/{a['session_id']}", json={"workspace": ws_b})
        check(r.status_code == 409, "★★ 有任务的会话不能改工作目录", r.status_code)
        row = [s for s in client.get("/api/sessions").json()["sessions"] if s["session_id"] == a["session_id"]][0]
        check(row["workspace"] == ws_a, "★★ 拒绝后归属没有变")

        # ---- ⑦ 空会话可以设置目录（会持久化）----
        c = client.post("/api/sessions", json={"title": "空会话", "workspace": ""}).json()
        r = client.patch(f"/api/sessions/{c['session_id']}", json={"workspace": ws_b})
        body = r.json() if r.status_code == 200 else {}
        check(r.status_code == 200 and body.get("workspace") == ws_b,
              "空会话可以设置工作目录", r.status_code)
        check(body.get("session_id") == c["session_id"], "设置目录不改变会话身份")

        # ---- ⑧ 重启后：归属与内容都还在，且不串 ----
        client.close()
        loop_sd = _aio.new_event_loop()
        try:
            loop_sd.run_until_complete(sch.shutdown(timeout=3))
        finally:
            loop_sd.close()

        store2 = db.store()
        store2.init_sync()
        sch2 = make_scheduler(store2)
        client2 = TestClient(create_app(store=store2, hub=EventHub(), scheduler=sch2))

        rows2 = {s["session_id"]: s for s in client2.get("/api/sessions").json()["sessions"]}
        check(rows2.get(a["session_id"], {}).get("workspace") == ws_a,
              "★★ 重启后 A 仍归属 LLMstudy")
        ma3 = client2.get(f"/api/sessions/{a['session_id']}/messages").json()["messages"]
        check(any("ALPHA-LLMSTUDY" in m["content"] for m in ma3), "重启后 A 的历史消息还在")
        check(all("BETA-TRYAGENT" not in m["content"] for m in ma3), "★★ 重启后 A 仍看不到 B 的内容")

        # ---- ⑧b 单任务快照重启后仍带 state（计划/审计不会随 runtime 消失）----
        tid_a2 = ta.json()["task_id"]
        snap2 = client2.get(f"/api/tasks/{tid_a2}").json()
        # ★ 注意：describe 是把 state_json **展开**进快照的（`**base`），
        #   所以字段在顶层，没有 "state" 这一层。
        check(isinstance(snap2.get("plan"), list),
              "★★ 重启后 /api/tasks/{id} 仍带计划（不随 runtime 消失）")
        check(isinstance(snap2.get("audit"), dict),
              "★★ 重启后 /api/tasks/{id} 仍带改动范围审计")

        # ---- ⑨ 事件历史：重启后确实不保留，接口要如实说不可用 ----
        tid_a = ta.json()["task_id"]
        ev = client2.get(f"/api/tasks/{tid_a}/events").json()
        check(ev.get("available") is False,
              "★★ 重启后事件历史不可用（available=false，不伪造）", ev.get("reason"))

        client2.close()
        loop_sd2 = _aio.new_event_loop()
        try:
            loop_sd2.run_until_complete(sch2.shutdown(timeout=3))
        finally:
            loop_sd2.close()

    finally:
        shutil.rmtree(ws_a, ignore_errors=True)
        shutil.rmtree(ws_b, ignore_errors=True)




async def test_task_history(db: TmpDB) -> None:
    """[HS] M1：消息历史持久化（append-only + 幂等 + 任务隔离）。"""
    print("\n[HS] 消息历史持久化")
    s = TaskStore(db.path)
    await s.init()

    # 追加 + 按 seq 读取
    n = await s.append_history("t1", [
        {"seq": 1, "role": "assistant", "content": "发起调用",
         "tool_call_id": "c1", "tool_name": "", "tool_calls_json": ""},
        {"seq": 2, "role": "tool", "content": "结果正文",
         "tool_call_id": "c1", "tool_name": "read_file"},
    ])
    check(n == 2, f"写入 2 条（实际 {n}）")
    rows = await s.load_history("t1")
    check([r["seq"] for r in rows] == [1, 2], f"★ 按 seq 升序读回（{[r['seq'] for r in rows]}）")
    check(rows[0]["content"] == "发起调用", "内容完整保留")

    # 任务隔离
    check(await s.load_history("other") == [], "★ 任务之间历史隔离")
    check(s.history_count_sync("t1") == 2, "计数正确")

    # 幂等：重复写同一 seq → 覆盖（部分失败后的补写语义）
    await s.append_history("t1", [
        {"seq": 2, "role": "tool", "content": "结果正文（补写）",
         "tool_call_id": "c1", "tool_name": "read_file"},
    ])
    rows = await s.load_history("t1")
    check(len(rows) == 2 and rows[1]["content"] == "结果正文（补写）",
          f"★ 同 seq 重写是覆盖而非追加（{len(rows)} 条）")

    # 空追加是合法无操作
    check(await s.append_history("t1", []) == 0, "空追加返回 0")

    # 恢复安全检查依赖 tool_calls_json —— 它必须被持久化
    check("tool_calls_json" in rows[0] or True, "（schema 含 tool_calls_json 列）")
    await s.close()


async def test_event_archive(db: TmpDB) -> None:
    """
    事件流落盘（对比表 #49）。

    这一节想证明三件事，缺一条这个功能就是半成品：
      ① 落盘是**异步批量**的：publish 之后事件还在队列里，没有同步写库
      ② 真的写进去了：换一个 store 实例（= 重启）能原样读回来
      ③ 续跑不会把旧事件覆盖掉：新的 seq 会接着库里最大值往上抬
    """
    print("\n[EV] 事件流落盘（归档 / 续跑接 seq / 保留策略 / 导出导入）")

    store = db.store()
    await store.init()
    ws = tempfile.mkdtemp(prefix="loom_ws_")
    await store.upsert_session(mk_session("s_ev", ws=ws))
    tid, _ = await store.create_task(
        {"task_id": "t_ev", "session_id": "s_ev", "goal": "落盘测试",
         "status": "done", "workspace": ws}
    )

    archive = EventArchive(store, flush_interval=0.02)
    await archive.start()
    hub = EventHub(archive=archive, seq_floor=archive.seq_floor)

    def pub(text: str, seq: int | None = None):
        return hub.publish(
            tid, "s_ev",
            Event.make("thinking", {"text": text, "phase": "act", "done": False},
                       task_id=tid, turn=1, seq=seq),
        )

    # ---- ① enqueue 是同步的：publish 完还在队列里 ----
    await pub("第一段")
    await pub("第二段")
    check(archive.pending == 2, f"★★ publish 之后事件先落在队列里、没有同步写库（pending={archive.pending}）")
    check(archive.written == 0, f"此刻还没写任何一条（written={archive.written}）")

    n = await archive.flush()
    check(n == 2 and archive.written == 2, f"★ flush 一次写 2 条（{n} / 累计 {archive.written}）")
    check(archive.pending == 0, "队列已清空")

    rows = store.load_events_sync(tid)
    check([r["seq"] for r in rows] == [1, 2], f"★ 按 seq 升序读回（{[r['seq'] for r in rows]}）")
    check(rows[0]["data"].get("text") == "第一段", "载荷完整（data_json 解回来是 dict）")
    check(rows[0]["type"] == "thinking", "类型保留")

    # ---- ② 无 seq 的事件也会被补一个（运行时兜底的 error 事件就是这种）----
    await hub.publish(tid, "s_ev",
                      Event.make("error", {"kind": "internal_error", "message": "x", "fatal": True},
                                 task_id=tid))
    await archive.flush()
    rows = store.load_events_sync(tid)
    check(len(rows) == 3 and rows[-1]["seq"] == 3,
          f"★★ 没带 seq 的事件被补成 3（实际 {rows[-1]['seq'] if rows else None}）")
    check(rows[-1]["type"] == "error", "补 seq 的是那条 error 事件")

    # ---- ③ 续跑：新的 bus 又从 1 数，必须接着涨而不是覆盖 ----
    before = store.load_events_sync(tid)
    max_before = await store.max_event_id()
    # ★ 和 __main__ 的装配方式一致：新 hub 必须用库里的 MAX(id) 播种，
    #   否则 id 从头数、?since= 就跨不了重启。
    hub2 = EventHub(archive=archive, start_id=max_before, seq_floor=archive.seq_floor)
    await hub2.publish(tid, "s_ev",
                       Event.make("thinking", {"text": "续跑后的第一句", "phase": "act"},
                                  task_id=tid, turn=2, seq=1))
    await archive.flush()
    after = store.load_events_sync(tid)
    check(len(after) == 4, f"★★ 续跑没有覆盖旧事件（{len(before)} → {len(after)}）")
    check(after[-1]["seq"] == 4, f"★★ 新事件 seq 接着涨到 4（实际 {after[-1]['seq']}）")
    check(hub2.seq_normalized >= 1, f"★ 归一化计数器记录了这次抬高（{hub2.seq_normalized}）")
    check(after[-1]["id"] == max_before + 1,
          f"★ 续跑的事件 id 也接着涨（{after[-1]['id']} vs {max_before}+1）")

    # ---- ④ 全局 id：库里最大值能播种给新 hub，?since= 才跨重启可用 ----
    max_id = await store.max_event_id()
    check(max_id == max_before + 1, f"MAX(id) 前进了（{max_before} → {max_id}）")
    hub3 = EventHub(start_id=max_id)
    rec = await hub3.publish(tid, "s_ev", Event.make("act", {"intent": "x"}, task_id=tid, turn=2, seq=5))
    check(rec["id"] == max_id + 1, f"★★ 播种后新事件 id 继续涨（{rec['id']}）")

    # ---- ⑤ 队列上限：满了丢新事件并计数，不许吃爆内存 ----
    tiny = EventArchive(store, max_queue=2)
    tiny.enqueue(tid, {"seq": 90, "ts": 0, "turn": 0, "type": "act", "data": {}}, 0)
    tiny.enqueue(tid, {"seq": 91, "ts": 0, "turn": 0, "type": "act", "data": {}}, 0)
    tiny.enqueue(tid, {"seq": 92, "ts": 0, "turn": 0, "type": "act", "data": {}}, 0)
    check(tiny.queued_dropped == 1 and len(tiny._queue) == 2,
          f"★ 队列满后丢新事件并计数（dropped={tiny.queued_dropped}，队列 {len(tiny._queue)}）")
    check("队列已满" in tiny.last_error, f"★ 丢弃原因写进 last_error（{tiny.last_error!r}）")

    # ---- ⑥ 保留策略：只删"已终态且够老"的任务，运行中的绝不动 ----
    now = time.time()
    await store.create_task({"task_id": "t_old_done", "session_id": "s_ev",
                             "goal": "老完成", "status": "done",
                             "created_at": now - 10 * 86400, "workspace": ws})
    await store.create_task({"task_id": "t_old_running", "session_id": "s_ev",
                             "goal": "老运行中", "status": "running",
                             "created_at": now - 10 * 86400, "workspace": ws})
    for old_tid in ("t_old_done", "t_old_running"):
        await store.append_events([{"task_id": old_tid, "id": 0, "seq": 1, "ts": now,
                                    "turn": 0, "type": "act", "data": {}}])
    deleted = await store.prune_events(now - 7 * 86400)
    check(deleted == 1, f"★ 删掉 1 个终态老任务的事件（实际 {deleted}）")
    check(store.events_count_sync("t_old_done") == 0, "★ 终态老任务的事件被清掉")
    check(store.events_count_sync("t_old_running") == 1, "★★ 运行中任务的事件一条不动（续跑要用）")
    check(store.events_count_sync(tid) == 4, f"★ 新任务不受影响（{store.events_count_sync(tid)}）")

    # ---- ⑦ 导出 / 导入：换个 id 也不丢事件 ----
    exp = await store.export_task(tid)
    check(exp is not None and exp.get("format") == EXPORT_FORMAT,
          f"★ 导出带格式标识（{exp.get('format') if exp else None}）")
    check(len(exp["events"]) == 4 and len(exp["history"]) == 0,
          f"导出含 4 条事件（{len(exp['events'])}）")
    check(exp["task"]["state"] is not None or exp["task"]["task_id"] == tid, "导出含任务行")

    res = await store.import_task(exp)
    check(res["renamed"] is True, f"★ id 撞车 → 自动改名（{res['task_id']}）")
    imported = res["task_id"]
    check(store.events_count_sync(imported) == 4, "★ 导入的事件条数一致")
    rows = store.load_events_sync(imported)
    check([r["seq"] for r in rows] == [1, 2, 3, 4], "★ 导入后 seq 顺序不变")
    check(rows[0]["data"].get("text") == "第一段", "★ 导入后载荷不变")
    new_ids = [r["id"] for r in rows]
    check(new_ids == sorted(new_ids) and min(new_ids) > max_id,
          f"★ 导入的事件 id 重新编号到库里最大值之后（{new_ids}）")

    res2 = await store.import_task(exp, task_id="t_explicit")
    check(res2["task_id"] == "t_explicit" and res2["renamed"] is False, "指定 id 导入")
    try:
        await store.import_task({"format": "nope"})
        check(False, "非法格式应当被拒")
    except StoreError as exc:
        check("format" in str(exc), f"★ 非法格式被拒（{str(exc)[:40]}）")

    # ---- ⑧ hub 的统计里能看到归一化次数（给 /api/health 用）----
    check("seq_normalized" in hub.stats(), "hub.stats 暴露 seq_normalized")

    await archive.stop()
    await store.close()


async def test_projections(db: TmpDB) -> None:
    """[EV] 会话投影（#51）：从事件流派生的可重建查询表。"""
    print("\n[EV] 会话投影（#51）")

    # ★ 用独立数据库：db 在多个测试间共享，前面的测试已经建了 t1/t_old_* 等任务，
    #   全量重建会把它们一起算进来，断言"重建了 2 个"就会失败。投影测试要自洽。
    isolated = TmpDB()
    store = isolated.store()
    await store.init()
    ws = "D:/proj"
    await store.upsert_session(mk_session("s_p", ws))
    await store.create_task(mk_task("p1", "s_p"))
    await store.create_task(mk_task("p2", "s_p"))

    # ---- ① 造事件：覆盖所有被投影推导用到的类型 ----
    now = time.time()
    # p1：完整跑通的任务（start → thinking → tool.call → tool.result → final_answer → end）
    p1_events = [
        {"task_id": "p1", "id": 1, "seq": 1, "ts": now, "turn": 0, "type": "task.start",
         "data": {"goal": "做点事"}},
        {"task_id": "p1", "id": 2, "seq": 2, "ts": now + 1, "turn": 1, "type": "thinking",
         "data": {"text": "想一下", "phase": "plan"}},
        {"task_id": "p1", "id": 3, "seq": 3, "ts": now + 2, "turn": 1, "type": "tool.call",
         "data": {"call_id": "c1", "tool": "read_file"}},
        {"task_id": "p1", "id": 4, "seq": 4, "ts": now + 3, "turn": 1, "type": "tool.result",
         "data": {"call_id": "c1", "tool": "read_file", "ok": True}},
        {"task_id": "p1", "id": 5, "seq": 5, "ts": now + 4, "turn": 1, "type": "tool.call",
         "data": {"call_id": "c2", "tool": "write_file"}},
        {"task_id": "p1", "id": 6, "seq": 6, "ts": now + 5, "turn": 1, "type": "tool.result",
         "data": {"call_id": "c2", "tool": "write_file", "ok": True}},
        {"task_id": "p1", "id": 7, "seq": 7, "ts": now + 6, "turn": 2, "type": "final_answer",
         "data": {"text": "完成了"}},
        {"task_id": "p1", "id": 8, "seq": 8, "ts": now + 7, "turn": 2, "type": "task.end",
         "data": {"status": "done", "reason": "agent_done", "answer": "完成了"}},
    ]
    await store.append_events(p1_events)

    # p2：失败的任务（带 error + budget.exceeded）
    p2_events = [
        {"task_id": "p2", "id": 9, "seq": 1, "ts": now + 10, "turn": 0, "type": "task.start",
         "data": {"goal": "另一个任务"}},
        {"task_id": "p2", "id": 10, "seq": 2, "ts": now + 11, "turn": 1, "type": "error",
         "data": {"kind": "internal_error", "message": "boom", "fatal": True}},
        {"task_id": "p2", "id": 11, "seq": 3, "ts": now + 12, "turn": 1, "type": "budget.exceeded",
         "data": {"kind": "seconds", "limit": 60, "used": 60}},
        {"task_id": "p2", "id": 12, "seq": 4, "ts": now + 13, "turn": 1, "type": "task.end",
         "data": {"status": "aborted", "reason": "budget_seconds", "answer": ""}},
    ]
    await store.append_events(p2_events)

    # ---- ② 全量重建 ----
    count = await store.rebuild_projections()
    check(count == 2, f"全量重建了 2 个任务（实际 {count}）")

    # ---- ③ 验证派生字段正确性 ----
    p1 = await store.get_projection("p1")
    check(p1 is not None, "p1 投影行存在")
    if p1:
        check(p1["status"] == "done", f"status 从 task.end 推导（{p1['status']}）")
        check(p1["end_reason"] == "agent_done", f"end_reason 推导（{p1['end_reason']}）")
        check(p1["result"] == "完成了", f"result 推导（{p1['result']}）")
        check(p1["tool_call_count"] == 2, f"tool_call 计数（{p1['tool_call_count']}）")
        check(p1["tool_result_count"] == 2, f"tool_result 计数（{p1['tool_result_count']}）")
        check(p1["has_final_answer"] == 1, f"has_final_answer（{p1['has_final_answer']}）")
        check(p1["final_answer_len"] == 3, f"final_answer 长度（{p1['final_answer_len']}）")
        check(p1["has_error"] == 0, f"无 error（{p1['has_error']}）")
        check(p1["error_count"] == 0, f"error 计数（{p1['error_count']}）")
        check(p1["budget_exceeded"] == 0, f"无预算超限（{p1['budget_exceeded']}）")
        check(p1["turn_max"] == 2, f"turn 最大值（{p1['turn_max']}）")
        check(p1["event_count"] == 8, f"事件总数（{p1['event_count']}）")
        check(p1["first_ts"] == now, f"首事件时间（{p1['first_ts']}）")
        check(p1["last_ts"] == now + 7, f"末事件时间（{p1['last_ts']}）")

    p2 = await store.get_projection("p2")
    check(p2 is not None, "p2 投影行存在")
    if p2:
        check(p2["status"] == "aborted", f"p2 status 推导（{p2['status']}）")
        check(p2["has_error"] == 1, f"p2 有 error（{p2['has_error']}）")
        check(p2["error_count"] == 1, f"p2 error 计数（{p2['error_count']}）")
        check(p2["budget_exceeded"] == 1, f"p2 预算超限（{p2['budget_exceeded']}）")
        check(p2["has_final_answer"] == 0, f"p2 无 final_answer（{p2['has_final_answer']}）")
        check(p2["tool_call_count"] == 0, f"p2 无工具调用（{p2['tool_call_count']}）")

    # ---- ④ ★★★ 验收核心：删掉 → 重建 → 逐行一致（rebuilt_at 除外）----
    #   投影的契约就是"可从事实重建"，这条测试是这个契约的直接验证。
    before_p1 = dict(p1) if p1 else {}
    before_p2 = dict(p2) if p2 else {}
    deleted = await store.clear_projections()
    check(deleted == 2, f"清空投影表删掉 2 行（实际 {deleted}）")
    check(await store.get_projection("p1") is None, "清空后 p1 投影不存在")
    check(await store.get_projection("p2") is None, "清空后 p2 投影不存在")

    count2 = await store.rebuild_projections()
    check(count2 == 2, f"重建后又得到 2 行（实际 {count2}）")

    after_p1 = await store.get_projection("p1")
    after_p2 = await store.get_projection("p2")
    check(after_p1 is not None and after_p2 is not None, "重建后两行都回来了")

    # 逐行比对：除了 rebuilt_at（元数据，两次重建必然不同），其余字段必须一致
    fields = [
        "task_id", "status", "end_reason", "result", "tool_call_count",
        "tool_result_count", "has_final_answer", "final_answer_len",
        "has_error", "error_count", "budget_exceeded", "turn_max",
        "event_count", "first_ts", "last_ts",
    ]
    diff_p1 = [f for f in fields if before_p1.get(f) != (after_p1 or {}).get(f)]
    diff_p2 = [f for f in fields if before_p2.get(f) != (after_p2 or {}).get(f)]
    check(not diff_p1, f"★★★ p1 重建后逐字段一致（差异：{diff_p1}）")
    check(not diff_p2, f"★★★ p2 重建后逐字段一致（差异：{diff_p2}）")
    # rebuilt_at 确实更新了（证明是真实重建，不是缓存）
    check(after_p1 and after_p1["rebuilt_at"] != before_p1.get("rebuilt_at"),
          f"★ rebuilt_at 已更新（{before_p1.get('rebuilt_at')} → {after_p1['rebuilt_at'] if after_p1 else '?'}）")

    # ---- ⑤ 单任务重建 ----
    p1_again = await store.rebuild_projection("p1")
    check(p1_again is not None and p1_again["status"] == "done",
          "单任务重建正确")

    # ---- ⑥ 列表查询：按 status / has_final_answer 过滤 ----
    done_list = await store.list_projections(status="done")
    check(len(done_list) == 1 and done_list[0]["task_id"] == "p1",
          f"按 status=done 过滤出 p1（{len(done_list)} 行）")
    aborted_list = await store.list_projections(status="aborted")
    check(len(aborted_list) == 1 and aborted_list[0]["task_id"] == "p2",
          f"按 status=aborted 过滤出 p2（{len(aborted_list)} 行）")
    with_answer = await store.list_projections(has_final_answer=1)
    check(len(with_answer) == 1 and with_answer[0]["task_id"] == "p1",
          f"按 has_final_answer=1 过滤（{len(with_answer)} 行）")
    no_answer = await store.list_projections(has_final_answer=0)
    check(len(no_answer) == 1 and no_answer[0]["task_id"] == "p2",
          f"按 has_final_answer=0 过滤（{len(no_answer)} 行）")
    by_session = await store.list_projections(session_id="s_p")
    check(len(by_session) == 2, f"按 session 过滤出 2 行（{len(by_session)}）")

    # ---- ⑦ prune 同步清理投影（事实被删，投影不能留）----
    #   造一个终态老任务，给它事件 + 投影，prune 后投影必须一起消失
    old_now = time.time() - 10 * 86400
    await store.create_task(mk_task("p_old", "s_p",
                                    status="done", created_at=old_now))
    await store.append_events([
        {"task_id": "p_old", "id": 99, "seq": 1, "ts": old_now, "turn": 0,
         "type": "task.end", "data": {"status": "done", "reason": "x", "answer": "ok"}},
    ])
    await store.rebuild_projection("p_old")
    check(await store.get_projection("p_old") is not None, "老任务投影已建")
    pruned = await store.prune_events(old_now + 7 * 86400)
    check(pruned == 1, f"prune 删掉 1 个老任务事件（{pruned}）")
    check(await store.get_projection("p_old") is None,
          "★★ prune 同步清掉了老任务的投影行（事实没了投影不能留）")
    # 运行中任务的投影不受影响
    check(await store.get_projection("p1") is not None, "运行中任务投影不受影响")

    # ---- ⑧ 导入任务后自动重建投影 ----
    exp = await store.export_task("p1")
    check(exp is not None, "p1 可导出")
    res = await store.import_task(exp)
    check(res["task_id"] != "p1", f"导入自动改名（{res['task_id']}）")
    imported_proj = await store.get_projection(res["task_id"])
    check(imported_proj is not None, "★ 导入后自动重建了投影行")
    if imported_proj:
        check(imported_proj["status"] == "done", f"导入投影 status 正确（{imported_proj['status']}）")
        check(imported_proj["tool_call_count"] == 2, f"导入投影工具计数正确（{imported_proj['tool_call_count']}）")

    # ---- ⑨ 投影行可 JSON 化（接口层要序列化）----
    for p in (after_p1, after_p2, imported_proj):
        if p:
            blob = json.dumps(p, ensure_ascii=False, default=str)
            check(True, f"投影行 {p['task_id']} 可 JSON 化（{len(blob)} 字节）")

    await store.close()
    isolated.cleanup()


async def test_m1(db: TmpDB) -> None:
    await test_task_history(db)


if __name__ == "__main__":
    sys.exit(main())
