"""步骤 4 验收：shells.py 的 ACL 模式分发，三种 shell 各跑一遍。

判定一律用硬证据（父进程复查目标文件是否真的生成 / 输出里有没有密钥），
不看子进程自述的 WRITE_OK —— traceback 会把源码片段打印出来造成假阳性。

运行：.venv/Scripts/python.exe dev/verify_acl_shells.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402
from backend.tools.exec.shells import (  # noqa: E402
    ShellUnavailable, detect_shell, run_command,
)

WS = "D:/Loom_agent/loom/sandbox/poc_ws"
WS_FILE = os.path.join(WS, "shell_probe.txt")
OUTSIDE = os.path.join(os.path.expanduser("~"), "poc_acl_outside", "shell_probe.txt")
SECRET_KEY = "LOOM_TEST_API_KEY"
SECRET_VALUE = "SHOULD_NOT_LEAK"

# 每种 shell 用原生语法；路径不含空格，避免 CMD / PowerShell 的嵌套引号坑
COMMANDS = {
    "powershell": {
        "write": "Set-Content -Path '{t}' -Value ok",
        "secret": "Write-Output $env:LOOM_TEST_API_KEY",
    },
    "cmd": {
        "write": "echo ok> {t}",
        "secret": "echo %LOOM_TEST_API_KEY%",
    },
    "bash": {
        "write": "echo ok > {t}",
        "secret": "echo $LOOM_TEST_API_KEY",
    },
}

checks: list[tuple[str, bool, str]] = []


def cleanup(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def write_case(shell: str, label: str, target: str, mode: str, expect_write: bool) -> None:
    cmd = COMMANDS[shell]["write"].format(t=target.replace("/", "\\"))
    cleanup(target)
    result = await run_command(
        cmd, cwd=WS, shell=shell, timeout=40.0,
        acl_mode=mode, workspace_root=WS,
    )
    wrote = os.path.exists(target)
    cleanup(target)
    ok = (wrote == expect_write) and not result.spawn_error
    detail = (
        f"exit={result.exit_code} 文件生成={wrote} (期望 {expect_write}) "
        f"spawn_error={result.spawn_error!r}"
    )
    checks.append((f"{shell} / {label}", ok, detail))
    print(f"  [{shell:10s}] {label:22s} -> {'PASS' if ok else 'FAIL'} | {detail}")


async def main() -> None:
    os.environ[SECRET_KEY] = SECRET_VALUE
    acl.revoke_grants()
    os.makedirs(WS, exist_ok=True)
    os.makedirs(os.path.dirname(OUTSIDE), exist_ok=True)

    print("=== 三种 shell × workspace-write ===")
    available = []
    for shell in ("powershell", "cmd", "bash"):
        try:
            info = detect_shell(shell)
            available.append(shell)
            print(f"  [探测] {shell} -> {info.label} ({info.path})")
        except ShellUnavailable as exc:
            print(f"  [SKIP] {shell}: {exc}")

    for shell in available:
        if shell == "bash":
            # 已知机制限制：MSYS 运行时与 WRITE_RESTRICTED 令牌不兼容。
            # 这里验证的是「明确拒绝」，不是「能跑」。若将来修好，改回正常三项用例。
            cmd = COMMANDS[shell]["write"].format(t=WS_FILE.replace("/", "\\"))
            cleanup(WS_FILE)
            r = await run_command(
                cmd, cwd=WS, shell=shell, timeout=40.0,
                acl_mode=acl.WORKSPACE_WRITE, workspace_root=WS,
            )
            ok = (
                bool(r.spawn_error)
                and "Git Bash" in r.spawn_error
                and not os.path.exists(WS_FILE)
            )
            checks.append((f"{shell} / 已知不兼容时明确拒绝", ok, r.spawn_error))
            print(f"  [{shell:10s}] {'已知不兼容时明确拒绝':22s} -> {'PASS' if ok else 'FAIL'} | {r.spawn_error!r}")
            cleanup(WS_FILE)
            continue

        await write_case(shell, "工作区内写", WS_FILE, acl.WORKSPACE_WRITE, True)
        await write_case(shell, "工作区外写", OUTSIDE, acl.WORKSPACE_WRITE, False)
        cmd = COMMANDS[shell]["secret"]
        r = await run_command(
            cmd, cwd=WS, shell=shell, timeout=40.0,
            acl_mode=acl.WORKSPACE_WRITE, workspace_root=WS,
        )
        ok = SECRET_VALUE not in r.stdout and SECRET_VALUE not in r.stderr
        checks.append((f"{shell} / 凭据不泄漏", ok, f"stdout={r.stdout.strip()[:60]!r}"))
        print(f"  [{shell:10s}] {'凭据不泄漏':22s} -> {'PASS' if ok else 'FAIL'} | stdout={r.stdout.strip()[:60]!r}")

    if "powershell" in available:
        print("\n=== 模式分发 (PowerShell) ===")
        await write_case("powershell", "read-only 写工作区内", WS_FILE, acl.READ_ONLY, False)
        await write_case("powershell", "full-access 写工作区外", OUTSIDE, acl.FULL_ACCESS, True)

    print("\n=== 默认路径不受影响 / 非法模式 fail-closed ===")
    plain = await run_command("echo LOOM_PLAIN_OK", cwd=WS, shell="auto", timeout=20.0)
    ok_plain = plain.ok and "LOOM_PLAIN_OK" in plain.stdout and not plain.extra.get("acl")
    checks.append(("默认路径行为不变", ok_plain, f"ok={plain.ok} extra={plain.extra}"))
    print(f"  默认路径 -> {'PASS' if ok_plain else 'FAIL'} | ok={plain.ok} extra={plain.extra}")

    bad = await run_command(
        "echo x", cwd=WS, shell="auto", timeout=20.0,
        acl_mode="not-a-mode", workspace_root=WS,
    )
    ok_bad = bool(bad.spawn_error) and "未知 ACL 模式" in bad.spawn_error
    checks.append(("非法模式 fail-closed", ok_bad, f"spawn_error={bad.spawn_error!r}"))
    print(f"  非法模式 -> {'PASS' if ok_bad else 'FAIL'} | {bad.spawn_error!r}")

    acl.revoke_grants()
    os.environ.pop(SECRET_KEY, None)
    cleanup(WS_FILE)
    cleanup(OUTSIDE)

    print("\n=== 判定 ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f" | {detail}"))
    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
