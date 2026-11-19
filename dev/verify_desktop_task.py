"""手动验证：起一个 desktop 模式任务并跟踪事件流，直到结束。

用途：验证 Computer-Use 链路（MCP 桌面工具 + 视觉定位）在真实模型下能否跑通。
跑法：python dev/_run_desktop_task.py ["任务描述"]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

GOAL = ("打开网易云音乐（如果它已经在运行就直接用那个窗口），搜索歌曲《我不难过》，"
        "并确认搜索结果里出现了这首歌。禁止用 terminal 模拟鼠标键盘，用桌面工具完成。")


def _req(path: str, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=30) as r:
        return json.loads(r.read())


def _pick_session() -> str:
    sessions = _req("/api/sessions")
    items = sessions.get("sessions") or sessions.get("items") or []
    for s in items:
        if (s.get("workspace") or "").lower().startswith("d:\\nothin"):
            return s["session_id"]
    if items:
        return items[0]["session_id"]
    # 一个都没有就新建
    snap = _req("/api/sessions", {"name": "desktop-verify", "workspace": "D:\\nothin"}, "POST")
    return snap["session_id"]


def main() -> int:
    goal = sys.argv[1] if len(sys.argv) > 1 else GOAL
    sid = _pick_session()
    print(f"session = {sid}")
    snap = _req("/api/tasks", {"session_id": sid, "goal": goal, "mode": "desktop"}, "POST")
    tid = snap.get("task_id") or snap.get("id")
    print(f"task_id = {tid}\n" + "=" * 70)

    seen: set = set()
    calls: dict = {}
    deadline = time.time() + 420
    final = None
    while time.time() < deadline:
        d = _req(f"/api/tasks/{tid}/events")
        for ev in d.get("events") or []:
            eid = ev.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            t = ev.get("type")
            data = ev.get("data") or {}
            if t == "tool.call":
                name = data.get("tool") or data.get("name")
                args = json.dumps(data.get("args"), ensure_ascii=False)
                calls[eid] = name
                print(f"  → {name}  {args[:180]}")
            elif t == "tool.result":
                ok = data.get("ok")
                summ = str(data.get("summary"))[:200].replace("\n", " ")
                print(f"    {'OK ' if ok else 'ERR'} {summ}")
            elif t == "task.end":
                final = data
                print(f"\n[task.end] status={data.get('status')} reason={data.get('end_reason')}")
            elif t == "final_answer":
                print(f"\n[final] {str(data.get('text'))[:300]}")
            elif t == "error":
                print(f"  [error] {str(data.get('message'))[:160]}")
        st = _req(f"/api/tasks/{tid}").get("status")
        if st in ("done", "failed", "aborted", "cancelled"):
            # 终态后再拉一次，把收尾事件（task.end / final_answer）读全
            if final is None:
                time.sleep(1)
                continue
            break
        time.sleep(3)

    print("=" * 70)
    print(f"最终状态 = {_req(f'/api/tasks/{tid}').get('status')}")
    used = [c for c in calls.values() if c]
    desk = [c for c in used if "desktop" in (c or "") or "mcp__" in (c or "")]
    print(f"工具调用 {len(used)} 次；桌面/MCP 类 {len(desk)} 次；terminal {used.count('terminal')} 次")
    print(f"task_id = {tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
