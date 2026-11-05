"""ACL 路径 timeout / cancel / 进程树 kill 验收（步骤 4 补强）。

硬判据：让命令在 10 秒后写一个文件；若超时/取消真的把进程杀了，
再等几秒文件也不会出现。不看子进程自述。

运行：.venv/Scripts/python.exe dev/verify_acl_timeout.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402
from backend.tools.exec.shells import run_command  # noqa: E402

WS = "D:/Loom_agent/loom/sandbox/poc_ws"
LATE = os.path.join(WS, "late_write.txt")
SLOW = (
    "Start-Sleep -Seconds 10; Set-Content -Path '%s' -Value ok"
    % LATE.replace("/", "\\")
)
FAST = "Write-Output ACL_FAST_OK"
TWO_LINES = "Write-Output LINE1; Write-Output LINE2"

checks: list[tuple[str, bool, str]] = []


def cleanup() -> None:
    if os.path.exists(LATE):
        try:
            os.remove(LATE)
        except OSError:
            pass


async def run(cmd: str, timeout: float, cancel_event=None):
    return await run_command(
        cmd, cwd=WS, shell="powershell", timeout=timeout,
        cancel_event=cancel_event,
        acl_mode=acl.WORKSPACE_WRITE, workspace_root=WS,
    )


async def main() -> None:
    acl.revoke_grants()
    os.makedirs(WS, exist_ok=True)
    cleanup()

    print("=== 1 正常命令 + 输出不丢 ===")
    r1 = await run(FAST, 10.0)
    ok1 = r1.ok and "ACL_FAST_OK" in r1.stdout and not r1.timed_out and not r1.cancelled
    checks.append(("正常命令不受影响", ok1, f"rc={r1.exit_code} out={r1.stdout.strip()[:60]!r}"))
    print(f"  正常命令 -> {'PASS' if ok1 else 'FAIL'} | rc={r1.exit_code} out={r1.stdout.strip()[:60]!r} spawn_error={r1.spawn_error!r}")

    r_lines = await run(TWO_LINES, 10.0)
    ok_lines = "LINE1" in r_lines.stdout and "LINE2" in r_lines.stdout
    checks.append(("非阻塞读管道后输出未丢失", ok_lines, repr(r_lines.stdout.strip()[:80])))
    print(f"  两行输出 -> {'PASS' if ok_lines else 'FAIL'} | {r_lines.stdout.strip()[:80]!r}")

    print("\n=== 2 超时 ===")
    cleanup()
    t0 = time.monotonic()
    r2 = await run(SLOW, 2.0)
    elapsed = time.monotonic() - t0
    print(f"  timeout=2s 实耗 {elapsed:.2f}s timed_out={r2.timed_out} extra={r2.extra}")
    checks.append(("超时被标为 timed_out", r2.timed_out, f"timed_out={r2.timed_out}"))
    checks.append(("超时及时返回（< 6s）", elapsed < 6.0, f"elapsed={elapsed:.2f}s"))
    checks.append(("extra 如实标注", r2.extra.get("acl_timeout_enforced") is True, str(r2.extra)))
    checks.append(("超时后延迟写未发生", not os.path.exists(LATE), f"exists={os.path.exists(LATE)}"))
    # 等更久：若只杀了主进程、孙进程还活着，会在这段时间补写
    await asyncio.sleep(3)
    checks.append(("再等 3s 仍无补写（进程树被杀）", not os.path.exists(LATE), ""))
    cleanup()

    print("\n=== 3 取消 ===")
    cleanup()
    evt = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(1.0)
        evt.set()

    task = asyncio.create_task(cancel_soon())
    t1 = time.monotonic()
    r3 = await run(SLOW, 20.0, cancel_event=evt)
    cancel_elapsed = time.monotonic() - t1
    await task
    print(f"  cancel 实耗 {cancel_elapsed:.2f}s cancelled={r3.cancelled} timed_out={r3.timed_out}")
    checks.append(("取消被标为 cancelled", r3.cancelled and not r3.timed_out,
                   f"cancelled={r3.cancelled} timed_out={r3.timed_out}"))
    checks.append(("取消及时返回（< 6s）", cancel_elapsed < 6.0, f"elapsed={cancel_elapsed:.2f}s"))
    checks.append(("取消后延迟写未发生", not os.path.exists(LATE), ""))
    cleanup()

    acl.revoke_grants()
    cleanup()

    print("\n=== 判定 ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" | {detail}"))
    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
