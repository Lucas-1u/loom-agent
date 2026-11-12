"""真实断点续跑验证：跑到一半硬杀进程 → 重启 → 看它自己接上。

隔离：独立 DB（/tmp）+ 独立工作区 + 工作区内 .loom_checkpoints，不碰 sandbox/loom.db。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
WS = r"D:\Loom_agent\_resume_probe"
DB = r"D:\Loom_agent\_resume_probe_db.sqlite3"
PY = r".\.venv\Scripts\python.exe"
LOG1 = r"D:\Loom_agent\_resume_probe_run1.log"
LOG2 = r"D:\Loom_agent\_resume_probe_run2.log"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, path: str, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    with _opener.open(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(n=60):
    for _ in range(n):
        try:
            req("GET", "/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def start(logp):
    f = open(logp, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(
        [PY, "-m", "backend.server", "--db", DB],
        stdout=f, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return p, f


def step(msg):
    print("\n==== %s ====" % msg, flush=True)


def main():
    for p in (WS, DB, LOG1, LOG2):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    os.makedirs(WS, exist_ok=True)
    with open(os.path.join(WS, "a.txt"), "w", encoding="utf-8") as f:
        f.write("LOOM-RESUME-PROBE-42\n")

    step("第一次启动")
    p, f = start(LOG1)
    if not wait_health():
        print("启动失败"); f.close(); p.kill(); return 1
    print("server pid =", p.pid)

    sess = req("POST", "/api/sessions", {"title": "断点续跑探针", "workspace": WS})
    sid = sess["session_id"]
    task = req("POST", "/api/tasks", {
        "goal": (
            "按顺序做四件事，每件都必须用一个工具调用完成，不要合并、不要提前总结：\n"
            "1) 用 read_file 读 a.txt；\n"
            "2) 用 write_file 把读到的内容原样写进 b.txt；\n"
            "3) 用 write_file 写 c.txt，内容是 'step3 done'；\n"
            "4) 用 read_file 读 c.txt 确认。"
        ),
        "session_id": sid,
        "workspace": WS,
    })
    tid = task["task_id"]
    print("task_id =", tid)

    step("等它跑起来（最多 75s，等到 turn>=2 或已出 3 个 checkpoint）")
    ck_dir = os.path.join(WS, ".loom_checkpoints")
    killed_at = None
    for i in range(75):
        time.sleep(1)
        try:
            d = req("GET", "/api/tasks/" + tid)
        except Exception:
            d = {}
        st = d.get("status", "?")
        snap = d.get("state") or {}
        turn = snap.get("turn", 0)
        nck = len(os.listdir(ck_dir)) if os.path.isdir(ck_dir) else 0
        print("  t=%2ds status=%-9s turn=%-2s ckpt_files=%d" % (i + 1, st, turn, nck), flush=True)
        if st in ("done", "failed", "aborted"):
            print("!! 任务在杀死前就结束了，无法验证续跑")
            f.close(); p.kill(); return 2
        if turn >= 2 and nck >= 2:
            killed_at = (turn, nck)
            break

    step("★ 硬杀进程（TerminateProcess，不给收尾机会）")
    p.kill()
    p.wait(timeout=10)
    f.close()
    time.sleep(1)
    nck = len(os.listdir(ck_dir)) if os.path.isdir(ck_dir) else 0
    print("杀死点: turn=%s, checkpoint 文件数=%d" % (killed_at, nck))
    print("checkpoint 目录:", sorted(os.listdir(ck_dir))[:8], "...")
    print("工作区文件:", sorted(os.listdir(WS)))

    step("第二次启动（不带任何提示，看它自己接）")
    p2, f2 = start(LOG2)
    if not wait_health():
        print("重启失败"); f2.close(); p2.kill(); return 3

    print("\n--- 启动日志里的恢复摘要 ---")
    with open(LOG2, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "恢复" in line or "↩" in line or "×" in line:
                print("   " + line.rstrip())

    step("等续跑完成（最多 150s）")
    final = None
    for i in range(150):
        time.sleep(1)
        try:
            d = req("GET", "/api/tasks/" + tid)
        except Exception:
            continue
        st = d.get("status", "?")
        snap = d.get("state") or {}
        turn = snap.get("turn", 0)
        if i % 5 == 0 or st != "running":
            print("  t=%3ds status=%-9s turn=%s" % (i + 1, st, turn), flush=True)
        if st in ("done", "failed", "aborted"):
            final = d
            break

    step("结果")
    if final is None:
        print("!! 150s 内未结束")
    else:
        snap = final.get("state") or {}
        print("status      =", final.get("status"))
        print("turn        =", snap.get("turn"), "(杀死时 %s)" % (killed_at[0] if killed_at else "?"))
        print("end_reason  =", snap.get("end_reason") or final.get("end_reason"))
        print("工作区文件  =", sorted(os.listdir(WS)))
        for name in ("b.txt", "c.txt"):
            fp = os.path.join(WS, name)
            print("  %-6s exists=%s content=%r" % (
                name, os.path.exists(fp),
                open(fp, encoding="utf-8").read()[:40] if os.path.exists(fp) else None))
    f2.close()
    p2.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
