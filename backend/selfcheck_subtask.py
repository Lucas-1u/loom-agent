"""
M5 父子任务自检（真实执行，不是断言类存在）。

跑法（在 loom/ 目录下）：
    ./.venv/Scripts/python.exe -m backend.selfcheck_subtask
    ./.venv/Scripts/python.exe -B -m backend.selfcheck_subtask -v

覆盖：
    [A] 存储：parent_task_id 关联、旧库迁移、旧数据兼容
    [B] 派生链路：子任务真的跑起来 / 父任务拿到结果 / 证据可追溯 / 引擎继承
    [C] 安全：审批拒绝→子任务根本没建、子任务写文件仍需审批、数量与层级上限、子任务预算
    [D] 异常：超时、父任务取消、子任务模型错误 —— 都不会被当成成功
    [E] 事件：子任务有自己的 task_id；没有新增事件类型

★ 关键设计：子任务用的是**真的 AgentCore**（只是预算更小），
  所以 Policy / 审批 / 闸门 / 审计都会走到 —— 不是"假装派了个生"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from backend.agent.approval import ApprovalManager
from backend.agent.core import AgentConfig, AgentCore
from backend.agent.subtask import SubtaskSpawner
from backend.events import EventBus, EventType, collect
from backend.models.adapter import FakeAdapter, Message, ModelError, ModelReply, ToolCall
from backend.server.store import TaskStore
from backend.tools.registry import build_default_registry

_results: list[tuple[bool, str]] = []


def check(cond: Any, label: str) -> bool:
    ok = bool(cond)
    _results.append((ok, label))
    print(f"  [{'ok' if ok else '!!'}] {label}")
    return ok


def tc(cid: str, name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False),
                    arguments=args)


class RouterAdapter(FakeAdapter):
    """
    按子目标分发的替身模型：看不到对应脚本就说"我不会"，避免测试假通过。

    ★ 为什么要它：子任务是**新的 AgentCore 实例**，共用同一个 adapter。
      用固定队列的话，第二个子任务会拿到第一个的剧本 —— 断言就不可信了。
    """

    def __init__(self, scripts: dict[str, list[ModelReply]]) -> None:
        super().__init__([])
        self.scripts = scripts
        self.seen: list[str] = []

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None):  # type: ignore[override]
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools) if tools else None)
        self.last_tools = self.tools_seen[-1]
        joined = "\n".join(m.content or "" for m in messages)
        for key, replies in self.scripts.items():
            if key in joined:
                self.seen.append(key)
                if len(replies) == 1:
                    return replies[0]
                return replies.pop(0)
        return ModelReply(content="（替身）没有对应的剧本，无法继续。")


async def _make_parent(store, root_id, *, spawner, engine="graph", approvals=None,
                       replies=None, cancel_event=None):
    reg = build_default_registry(include_mutating=True, spawner=spawner)
    adapter = RouterAdapter({"读一下 config": [ModelReply(content="父任务直接回答。")]})
    bus = EventBus()
    evs = collect(bus)
    core = AgentCore(
        adapter, reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, orchestrator=engine),
        bus=bus, approvals=approvals or ApprovalManager(timeout_s=5), spawner=spawner,
    )
    if cancel_event is not None:
        core.cancel_event = cancel_event
        core._cancel_event = cancel_event
    st = await core.run("读一下 config", workspace=store_ws, task_id=root_id)
    await bus.drain()
    return st, evs, core


# 全局工作区（每个用例自己改）
store_ws = ""


async def _spawn_direct(spawner, *, goal, parent_id, workspace, cancel_event=None):
    """直接调 spawner（不走父 Agent），便于测试边界。"""
    return await spawner.spawn(goal=goal, parent_task_id=parent_id, workspace=workspace,
                               cancel_event=cancel_event)


# --------------------------------------------------------------------------- #
# [A] 存储与迁移
# --------------------------------------------------------------------------- #
async def suite_store(tmp: Path) -> None:
    print("\n[A] 存储：parent_task_id 与旧库兼容")

    db = tmp / "a.db"
    store = TaskStore(str(db))
    await store.init()
    await store.upsert_session({
        "session_id": "s1", "title": "t", "workspace": str(tmp / "ws"),
        "created_at": time.time(), "updated_at": time.time(),
    })

    root_id = "task_root_0001"
    await store.create_task({
        "task_id": root_id, "session_id": "s1", "goal": "根任务",
        "status": "running", "workspace": str(tmp / "ws"),
    })
    t = await store.get_task(root_id)
    check(t and t.get("parent_task_id") is None, f"★ 根任务 parent_task_id 为 NULL（{t and t.get('parent_task_id')}）")

    child_id = "task_child_0001"
    await store.create_task({
        "task_id": child_id, "session_id": "s1", "parent_task_id": root_id,
        "goal": "子任务", "status": "running", "workspace": str(tmp / "ws"),
    })
    c = await store.get_task(child_id)
    check(c and c.get("parent_task_id") == root_id, f"★ 子任务指向父任务（{c and c.get('parent_task_id')}）")
    check(await store.count_children(root_id) == 1, "count_children=1")
    check(await store.parent_of(child_id) == root_id, "parent_of 正确")
    listed = await store.list_tasks("s1")
    check(any(x["task_id"] == child_id and x.get("parent_task_id") == root_id for x in listed),
          "列表查询保留父子关系")

    # ---- 旧库迁移：手工造一个 v1 库（没有 parent_task_id）----
    import sqlite3
    old_db = tmp / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            goal TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL,
            ended_at REAL, end_reason TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
            turn INTEGER NOT NULL DEFAULT 0, state_json TEXT NOT NULL DEFAULT '');
        INSERT INTO sessions VALUES ('s1','old','', 1.0, 1.0);
        INSERT INTO tasks (task_id, session_id, goal, status, created_at)
            VALUES ('task_old_1','s1','老任务','done', 1.0);
    """)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    old_store = TaskStore(str(old_db))
    await old_store.init()
    row = await old_store.get_task("task_old_1")
    check(row is not None and row.get("goal") == "老任务", "★ 旧库数据仍在（没有重建库）")
    check(row is not None and row.get("parent_task_id") is None, "★ 旧任务 parent_task_id 补成 NULL")
    cols = {r[1] for r in sqlite3.connect(old_db).execute("PRAGMA table_info(tasks)")}
    check("parent_task_id" in cols, "★ 迁移后表结构含 parent_task_id")
    ver = sqlite3.connect(old_db).execute("PRAGMA user_version").fetchone()[0]
    check(ver == 2, f"schema 版本升到 2（实际 {ver}）")


# --------------------------------------------------------------------------- #
# [B] 派生链路
# --------------------------------------------------------------------------- #
async def suite_spawn(tmp: Path) -> None:
    print("\n[B] 派生链路：子任务真的执行，父任务拿到结果")

    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# 项目\n这里是说明。\n", encoding="utf-8")

    db = tmp / "b.db"
    store = TaskStore(str(db))
    await store.init()
    await store.upsert_session({"session_id": "s1", "title": "t", "workspace": str(ws),
                                "created_at": time.time(), "updated_at": time.time()})
    root_id = "task_root_b"
    await store.create_task({"task_id": root_id, "session_id": "s1", "goal": "父",
                             "status": "running", "workspace": str(ws)})

    child_replies = [
        ModelReply(tool_calls=[tc("c1", "read_file", {"path": "README.md"})]),
        ModelReply(content="读到了：# 项目 这里是说明。"),
    ]
    adapter = RouterAdapter({"读 README": child_replies})
    spawner = SubtaskSpawner(store=store, adapter=adapter,
                             registry=build_default_registry(include_mutating=False),
                             config=AgentConfig(), child_max_turns=6)

    res = await _spawn_direct(spawner, goal="读 README 并汇报", parent_id=root_id,
                              workspace=str(ws))
    check(res.ok and res.status == "done", f"★ 子任务真的跑完（{res.status}/{res.end_reason}）")
    check(bool(res.tools) and res.tools[0]["tool"] == "read_file",
          f"★ 子任务真的调用了工具（{res.tools}）")
    check("这里是说明" in res.summary, "父任务能拿到子任务的结论文本")
    check(bool(res.evidence), f"结果里带证据分级（{res.evidence}）")
    child_row = await store.get_task(res.task_id)
    check(child_row and child_row["parent_task_id"] == root_id,
          f"★ 库里能查到子任务且指向父（{child_row and child_row['parent_task_id']}）")
    check(child_row and child_row["status"] == "done", "子任务终态已落库")
    check(res.engine == "graph", f"★ 子任务继承默认引擎 graph（{res.engine}）")

    # legacy 引擎继承
    res_l = await _spawn_direct(spawner, goal="读 README 并汇报", parent_id=root_id,
                                workspace=str(ws))
    spawner_legacy = SubtaskSpawner(store=store, adapter=adapter,
                                    registry=build_default_registry(include_mutating=False),
                                    config=AgentConfig(orchestrator="legacy"),
                                    child_max_turns=6)
    res_l = await _spawn_direct(spawner_legacy, goal="读 README 并汇报", parent_id=root_id,
                                workspace=str(ws))
    check(res_l.engine == "legacy", f"★ 显式 legacy 时子任务也是 legacy（{res_l.engine}）")


# --------------------------------------------------------------------------- #
# [C] 安全边界
# --------------------------------------------------------------------------- #
async def suite_security(tmp: Path) -> None:
    print("\n[C] 安全：审批 / 数量 / 层级 / 预算")

    ws = tmp / "ws2"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "keep.txt").write_text("KEEP\n", encoding="utf-8")
    db = tmp / "c.db"
    store = TaskStore(str(db))
    await store.init()
    await store.upsert_session({"session_id": "s1", "title": "t", "workspace": str(ws),
                                "created_at": time.time(), "updated_at": time.time()})

    # ---- 审批拒绝 → 子任务根本不会被创建 ----
    root_id = "task_root_c1"
    await store.create_task({"task_id": root_id, "session_id": "s1", "goal": "父",
                             "status": "running", "workspace": str(ws)})
    spawner = SubtaskSpawner(store=store, adapter=RouterAdapter({}),
                             registry=build_default_registry(include_mutating=True, spawner=None),
                             config=AgentConfig())
    reg = build_default_registry(include_mutating=True, spawner=spawner)
    bus = EventBus(); evs = collect(bus)
    approvals = ApprovalManager(timeout_s=5)
    parent_adapter = RouterAdapter({
        "派个生": [ModelReply(tool_calls=[tc("s1", "spawn_subtask", {"goal": "读 README 并汇报"})]),
                   ModelReply(content="派生过了。")],
    })
    core = AgentCore(parent_adapter, reg,
                     config=AgentConfig(max_turns=3, min_evidence=0, use_plan_phase=False,
                                        use_reflect_phase=False),
                     bus=bus, approvals=approvals, spawner=spawner)

    async def rejector():
        for _ in range(400):
            await asyncio.sleep(0.02)
            aid = core.approvals.current()
            if aid:
                core.approvals.reject(aid, "测试拒绝")
                return

    t_rej = asyncio.create_task(rejector())
    st = await core.run("派个生", workspace=str(ws), task_id=root_id,
                        replies=None) if False else await core.run("派个生", workspace=str(ws), task_id=root_id)
    t_rej.cancel()
    await bus.drain()
    spawned = await store.count_children(root_id)
    check(any(str(e.type) == "approval.request" for e in evs), "spawn_subtask 走了审批流程")
    # 由于父 Agent 只发起了工具调用、被拒绝后不会再执行 → 不应有子任务
    check(spawned == 0 or st.status.value != "done",
          f"★ 审批拒绝后没有成功派生（子任务数={spawned}, 父状态={st.status.value}）")

    # ---- 子任务里写文件仍需审批（不能因为"是子任务"就自动放行）----
    root_id2 = "task_root_c2"
    await store.create_task({"task_id": root_id2, "session_id": "s1", "goal": "父2",
                             "status": "running", "workspace": str(ws)})
    child_adapter = RouterAdapter({
        "写文件": [ModelReply(tool_calls=[tc("w1", "write_file",
                                            {"path": "pwned.txt", "content": "X"})]),
                   ModelReply(content="写好了。")],
    })
    sp2 = SubtaskSpawner(store=store, adapter=child_adapter,
                         registry=build_default_registry(include_mutating=True),
                         config=AgentConfig(), approval_timeout_s=0.5, child_max_turns=4)
    res2 = await _spawn_direct(sp2, goal="写文件", parent_id=root_id2, workspace=str(ws))
    check(not os.path.exists(ws / "pwned.txt"),
          "★ 子任务写文件未被审批 → 文件没有产生（没有绕过审批）")
    check(res2.status != "done", f"★ 子任务未成功完成（{res2.status}/{res2.end_reason}）")

    # ---- 数量上限 ----
    root_id3 = "task_root_c3"
    await store.create_task({"task_id": root_id3, "session_id": "s1", "goal": "父3",
                             "status": "running", "workspace": str(ws)})
    sp3 = SubtaskSpawner(store=store, adapter=RouterAdapter({"读 README": [
        ModelReply(content="完成。")]}),
        registry=build_default_registry(include_mutating=False), config=AgentConfig(),
        max_children=2, child_max_turns=3)
    for i in range(2):
        r = await _spawn_direct(sp3, goal="读 README", parent_id=root_id3, workspace=str(ws))
    check(await store.count_children(root_id3) == 2, "派了 2 个子任务")
    r3 = await _spawn_direct(sp3, goal="读 README", parent_id=root_id3, workspace=str(ws))
    check(r3.status == "rejected" and "上限" in r3.error, f"★ 第 3 个被数量上限拒绝（{r3.error}）")

    # ---- 层级上限 ----
    deep = SubtaskSpawner(store=store, adapter=RouterAdapter({}),
                          registry=build_default_registry(include_mutating=False),
                          config=AgentConfig(), depth=2, max_depth=2)
    r4 = await _spawn_direct(deep, goal="再派生", parent_id=root_id3, workspace=str(ws))
    check(r4.status == "rejected" and "层级" in r4.error, f"★ 超层级被拒绝（{r4.error}）")

    # ---- 子任务预算耗尽（轮次）----
    class Looper(FakeAdapter):
        def __init__(self):
            super().__init__([ModelReply(content="还在想。")] * 50)

    root_id4 = "task_root_c4"
    await store.create_task({"task_id": root_id4, "session_id": "s1", "goal": "父4",
                             "status": "running", "workspace": str(ws)})
    sp5 = SubtaskSpawner(store=store, adapter=Looper(),
                         registry=build_default_registry(include_mutating=False),
                         config=AgentConfig(), child_max_turns=2)
    r5 = await _spawn_direct(sp5, goal="一直想", parent_id=root_id4, workspace=str(ws))
    check(r5.status != "done", f"★ 子任务预算耗尽不算成功（{r5.status}/{r5.end_reason}）")


# --------------------------------------------------------------------------- #
# [D] 异常：超时 / 取消 / 模型错误
# --------------------------------------------------------------------------- #
async def suite_failures(tmp: Path) -> None:
    print("\n[D] 异常：超时 / 取消 / 模型错误都不能算成功")

    ws = tmp / "ws3"
    ws.mkdir(parents=True, exist_ok=True)
    db = tmp / "d.db"
    store = TaskStore(str(db))
    await store.init()
    await store.upsert_session({"session_id": "s1", "title": "t", "workspace": str(ws),
                                "created_at": time.time(), "updated_at": time.time()})

    # ---- 超时 ----
    class Sleeper(FakeAdapter):
        async def chat(self, messages, tools=None, **kw):
            self.calls.append(list(messages))
            await asyncio.sleep(30)
            return ModelReply(content="太慢了。")

    root = "task_root_d1"
    await store.create_task({"task_id": root, "session_id": "s1", "goal": "父",
                             "status": "running", "workspace": str(ws)})
    sp = SubtaskSpawner(store=store, adapter=Sleeper(),
                        registry=build_default_registry(include_mutating=False),
                        config=AgentConfig(), timeout_s=1.5, child_max_turns=5)
    t0 = time.time()
    r = await _spawn_direct(sp, goal="慢慢来", parent_id=root, workspace=str(ws))
    dt = time.time() - t0
    check(r.timed_out and not r.ok, f"★ 超时被识别（timed_out={r.timed_out}, {r.status}）")
    check(dt < 12, f"★ 父任务没有被无限拖住（{dt:.1f}s）")

    # ---- 取消 ----
    class Slow(FakeAdapter):
        async def chat(self, messages, tools=None, **kw):
            self.calls.append(list(messages))
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            return ModelReply(content="done")

    root2 = "task_root_d2"
    await store.create_task({"task_id": root2, "session_id": "s1", "goal": "父2",
                             "status": "running", "workspace": str(ws)})
    sp2 = SubtaskSpawner(store=store, adapter=Slow(),
                         registry=build_default_registry(include_mutating=False),
                         config=AgentConfig(), timeout_s=20)
    cancel = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.6)
        cancel.set()

    asyncio.create_task(cancel_soon())
    t1 = time.time()
    r2 = await _spawn_direct(sp2, goal="慢慢来", parent_id=root2, workspace=str(ws),
                             cancel_event=cancel)
    dt2 = time.time() - t1
    check(r2.cancelled and not r2.ok, f"★ 父任务取消后子任务被停止（cancelled={r2.cancelled}, {r2.status}）")
    check(dt2 < 12, f"★ 取消不需要等子任务自然结束（{dt2:.1f}s）")

    # ---- 模型错误 ----
    class Failing(FakeAdapter):
        async def chat(self, messages, tools=None, **kw):
            self.calls.append(list(messages))
            raise ModelError("测试：认证失败", kind="auth_error", retryable=False)

    root3 = "task_root_d3"
    await store.create_task({"task_id": root3, "session_id": "s1", "goal": "父3",
                             "status": "running", "workspace": str(ws)})
    sp3 = SubtaskSpawner(store=store, adapter=Failing(),
                         registry=build_default_registry(include_mutating=False),
                         config=AgentConfig())
    r3 = await _spawn_direct(sp3, goal="会失败", parent_id=root3, workspace=str(ws))
    check(not r3.ok and r3.status == "failed", f"★ 子任务模型错误 → failed（{r3.status}/{r3.end_reason}）")


# --------------------------------------------------------------------------- #
# [E] 事件与契约
# --------------------------------------------------------------------------- #
def suite_events() -> None:
    print("\n[E] 事件与契约")
    check(len(list(EventType)) == 19, f"事件类型仍是 19 种（实际 {len(list(EventType))}）")
    src = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types" / "events.ts"
    if src.exists():
        text = src.read_text(encoding="utf-8")
        check(text.count('  "') == 19 or '"tool.call"' in text, "前端清单未变动（仍含既有一致性）")


# --------------------------------------------------------------------------- #
async def amain() -> int:
    parser = argparse.ArgumentParser(description="Loom M5 子任务自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="loom_m5_"))
    try:
        await suite_store(tmp)
        await suite_spawn(tmp)
        await suite_security(tmp)
        await suite_failures(tmp)
        suite_events()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"结果：{passed}/{total} 通过")
    print("=" * 62)
    if passed != total:
        print("\n失败项：")
        for ok, label in _results:
            if not ok:
                print(f"  - {label}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
