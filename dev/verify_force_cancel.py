"""实测强制中止：起一个任务 → 等它在模型思考中 → 按停止 → 计时到 status=aborted。

对比背景：改之前 cancel 只置一个 event，要等模型这一轮返回后、循环走到
下一个检查点才生效；模型一次请求几十秒，体感就是"按了没反应"。
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

GOAL = ("请从 1 数到 60，每个数字都单独用一句话解释它为什么重要，"
        "不要用工具，纯文字回答。")   # 故意做成"会长时间思考/输出"的任务


def req(path: str, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with _OP.open(r, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    sessions = req("/api/sessions")
    items = sessions.get("sessions") or sessions.get("items") or []
    sid = next((s["session_id"] for s in items
                if (s.get("workspace") or "").lower().startswith("d:\\nothin")), None)
    if sid is None and items:
        sid = items[0]["session_id"]
    if sid is None:
        print("[FAIL] 没有可用会话")
        return 1

    snap = req("/api/tasks", {"session_id": sid, "goal": GOAL, "mode": "workspace-write"}, "POST")
    tid = snap.get("task_id")
    print(f"task = {tid}  session = {sid}")

    # 等它真的跑起来并进入模型调用（不是排队）
    deadline = time.time() + 30
    while time.time() < deadline:
        st = req(f"/api/tasks/{tid}").get("status")
        if st == "running":
            break
        time.sleep(0.5)
    time.sleep(6)          # 让它进入模型调用（这几十秒里按停止，正是用户遇到的场景）

    t0 = time.time()
    res = req(f"/api/tasks/{tid}/cancel", {}, "POST")
    print(f"cancel → {res}")

    # 轮询到终态，计时
    final = None
    while time.time() - t0 < 30:
        st = req(f"/api/tasks/{tid}")
        if st.get("status") in ("aborted", "failed", "done", "cancelled"):
            final = st
            break
        time.sleep(0.1)
    dt = time.time() - t0

    print("=" * 56)
    if final is None:
        print("[FAIL] 30s 内没有停下来")
        return 1
    print(f"停下来用了 {dt:.2f}s  →  status={final.get('status')}  reason={final.get('end_reason')}")
    if dt <= 2.5:
        print("[PASS] 强制中止生效（不再等模型这一轮返回）")
    else:
        print("[WARN] 超过 2.5s，可能没走到强制路径（协作式取消先停了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
