"""ACL 沙箱可证伪验证（步骤 3 的验收附件）。

项目惯例：删掉关键代码 → 验证应该变红。这里用 monkeypatch 做「变异测试」，
不改任何业务文件。

运行：.venv/Scripts/python.exe dev/verify_acl_falsify.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402

WS = "D:/Loom_agent/loom/sandbox/poc_ws"
OUTSIDE = os.path.join(os.path.expanduser("~"), "poc_acl_outside", "falsify_target.txt")


def run_case(label: str, mode: str, target: str) -> tuple:
    ps = (
        "try { Set-Content -Path '%s' -Value 'x' -ErrorAction Stop; "
        "Write-Output 'WRITE_OK' } catch { Write-Output 'WRITE_BLOCKED' }" % target
    )
    try:
        token, _ = acl.create_restricted_token(mode, WS)
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: 令牌创建抛异常 -> {exc!r}")
        return ("TOKEN_FAILED", "", "")
    rc, out, err = acl.spawn_with_token(
        token, "powershell", ["-NoLogo", "-NoProfile", "-Command", ps]
    )
    acl.close_token(token)
    wrote = os.path.exists(target)
    print(f"  {label}: rc={rc} out={out.strip()[:60]!r} 文件生成={wrote}")
    if os.path.exists(target):
        try:
            os.remove(target)
        except OSError:
            pass
    return rc, out.strip(), wrote


def main() -> None:
    print("A. 基线（关键代码在）：read-only 写工作区外应被拦")
    rc_a, out_a, wrote_a = run_case("基线", acl.READ_ONLY, OUTSIDE)
    baseline_ok = out_a == "WRITE_BLOCKED" and not wrote_a

    print("\nB. 变异：关掉 _grant_token_default_dacl（DSH 的默认 DACL 授权）")
    acl._grant_token_default_dacl = lambda *a, **k: None
    rc_b, out_b, _ = run_case("变异 B", acl.READ_ONLY, OUTSIDE)
    # 关键代码被拿掉后，子进程应起不来（0xC0000142），拿不到任何标记
    mutation_b_detected = rc_b == -1073741502 and out_b == ""

    # 还原
    import importlib
    importlib.reload(acl)

    print("\nC. 变异：grant_workspace_write 恒 False（应 fail-closed，不创建令牌）")
    acl.grant_dir_write = lambda *a, **k: False
    rc_c, _, _ = run_case("变异 C", acl.WORKSPACE_WRITE, os.path.join(WS, "falsify_ws.txt"))
    mutation_c_detected = rc_c == "TOKEN_FAILED"

    importlib.reload(acl)

    print("\n=== 判定 ===")
    checks = {
        "基线 read-only 真拦住（有硬证据）": baseline_ok,
        "变异 B 被检出（默认 DACL 授权是必需的）": mutation_b_detected,
        "变异 C 被检出（授权失败时 fail-closed）": mutation_c_detected,
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(checks.values())
    print(f"\n{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
