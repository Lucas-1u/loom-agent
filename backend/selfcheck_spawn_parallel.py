"""
spawn_parallel 自检（P1-#42）

验证并行派生的聚合逻辑与数量上限，不跑真实子任务
（monkey-patch spawn() 返回假结果，只测 spawn_parallel 的编排）。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent.subtask import (
    SubtaskSpawner,
    SubtaskResult,
    ParallelSpawnResult,
)

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} {detail}")


class FakeStore:
    def __init__(self, children=0):
        self._children = children
        self._tasks = {}

    async def count_children(self, parent_task_id):
        return self._children

    async def get_task(self, task_id):
        return {"session_id": "sess_test", "model": "fake"} if task_id else None

    async def create_task(self, payload):
        self._tasks[payload["task_id"]] = payload

    async def mark_started(self, task_id):
        pass

    async def save_task(self, task_id, **kw):
        pass


def make_spawner(children=0, max_children=3):
    return SubtaskSpawner(
        store=FakeStore(children),
        adapter=None,
        registry=None,
        config=None,
        max_children=max_children,
    )


async def test_empty():
    print("\n== A. 空 goals ==")
    sp = make_spawner()
    r = await sp.spawn_parallel(goals=[], parent_task_id="p", workspace=".")
    check("A1 空列表 total=0", r.total == 0)
    check("A2 ok=False（无子任务）", r.ok is False)


async def test_parallel_aggregate():
    print("\n== B. 并行聚合 ==")
    sp = make_spawner()

    # monkey-patch spawn 返回假结果
    async def fake_spawn(*, goal, parent_task_id, workspace, **kw):
        # 用 goal 文本区分结果
        if "fail" in goal:
            return SubtaskResult(task_id="c_fail", status="aborted", end_reason="err")
        if "reject" in goal:
            return SubtaskResult(task_id="c_rej", status="rejected", error="no")
        return SubtaskResult(task_id=f"c_{goal[:3]}", status="done")

    sp.spawn = fake_spawn

    goals = ["task_a done", "task_b fail", "task_c reject", "task_d done"]
    r = await sp.spawn_parallel(goals=goals, parent_task_id="p", workspace=".")

    # max_children=3 → 只接受前 3 个，第 4 个被拒
    check("B1 total=4（含被拒）", r.total == 4)
    check("B2 done_count=1（task_a done）", r.done_count == 1, f"got {r.done_count}")
    check("B3 aborted_count=1（task_b fail）", r.aborted_count == 1, f"got {r.aborted_count}")
    check("B4 rejected_count=2（task_c reject + task_d 超限）", r.rejected_count == 2, f"got {r.rejected_count}")
    check("B5 ok=False（有非 done）", r.ok is False)
    check("B6 results 数量=4", len(r.results) == 4)


async def test_all_done():
    print("\n== C. 全部完成 ==")
    sp = make_spawner()
    async def fake_spawn(*, goal, **kw):
        return SubtaskResult(task_id=f"c_{goal[:3]}", status="done")
    sp.spawn = fake_spawn

    r = await sp.spawn_parallel(goals=["a", "b", "c"], parent_task_id="p", workspace=".")
    check("C1 done_count=3", r.done_count == 3)
    check("C2 ok=True", r.ok is True)


async def test_max_children():
    print("\n== D. 数量上限 ==")
    # 已派生 2 个，max_children=3 → 本次只能 1 个
    sp = make_spawner(children=2, max_children=3)
    async def fake_spawn(*, goal, **kw):
        return SubtaskResult(task_id="c", status="done")
    sp.spawn = fake_spawn

    r = await sp.spawn_parallel(goals=["x", "y", "z"], parent_task_id="p", workspace=".")
    check("D1 total=3（含被拒）", r.total == 3)
    check("D2 done_count=1（只跑 1 个）", r.done_count == 1, f"got {r.done_count}")
    check("D3 rejected_count=2", r.rejected_count == 2, f"got {r.rejected_count}")


async def test_zero_available():
    print("\n== E. 无可用额度 ==")
    sp = make_spawner(children=3, max_children=3)
    async def fake_spawn(*, goal, **kw):
        return SubtaskResult(task_id="c", status="done")
    sp.spawn = fake_spawn

    r = await sp.spawn_parallel(goals=["a", "b"], parent_task_id="p", workspace=".")
    check("E1 done_count=0", r.done_count == 0)
    check("E2 rejected_count=2", r.rejected_count == 2)
    check("E3 spawn 未被调用", fake_spawn_called() is False)


_called = False
async def fake_spawn(*, goal, **kw):
    global _called
    _called = True
    return SubtaskResult(task_id="c", status="done")


def fake_spawn_called():
    return _called


async def main():
    print("=" * 70)
    print("spawn_parallel 自检（P1-#42 · 并行派生聚合）")
    print("=" * 70)
    await test_empty()
    await test_parallel_aggregate()
    await test_all_done()
    await test_max_children()
    await test_zero_available()

    print("\n" + "=" * 70)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
