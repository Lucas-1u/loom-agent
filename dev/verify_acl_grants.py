"""方案 B 验收：目录授权「登记 → 幂等 → 撤销 → 重新授权」闭环。

运行：.venv/Scripts/python.exe dev/verify_acl_grants.py
结束时会自动清场，不给工作区留下常驻 ACE。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import win32security

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402

WS = "D:/Loom_agent/loom/sandbox/poc_ws"


def count_cap_aces() -> int:
    """数一下工作区目录 DACL 里有多少条能力 SID 的 ACE。"""
    path = os.path.abspath(WS)
    sd = win32security.GetFileSecurity(path, acl.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return 0
    sid = win32security.ConvertStringSidToSid(acl.capability_sid_string(WS))
    return sum(
        1
        for i in range(dacl.GetAceCount())
        if len(dacl.GetAce(i)) >= 3 and dacl.GetAce(i)[2] == sid
    )


def token_roundtrip() -> None:
    token, _ = acl.create_restricted_token(acl.WORKSPACE_WRITE, WS)
    acl.close_token(token)


def main() -> None:
    checks = []

    acl.revoke_grants()  # 清场，保证起点干净
    a = (count_cap_aces(), len(acl.list_grants()))
    print(f"A 清场后            : ACE={a[0]} 登记={a[1]}")
    checks.append(("清场后无 ACE、无登记", a == (0, 0)))

    token_roundtrip()
    b = (count_cap_aces(), len(acl.list_grants()))
    print(f"B 授权后            : ACE={b[0]} 登记={b[1]}")
    checks.append(("授权会写 ACE 且登记", b == (1, 1)))

    token_roundtrip()
    c = (count_cap_aces(), len(acl.list_grants()))
    print(f"C 重复授权（幂等）  : ACE={c[0]} 登记={c[1]}")
    checks.append(("重复授权不叠加", c == (1, 1)))

    result = acl.revoke_grants()
    d = (count_cap_aces(), len(acl.list_grants()))
    print(f"D 撤销全部          : ACE={d[0]} 登记={d[1]} 撤销数={len(result['removed'])}")
    checks.append(("撤销后 ACE 与登记都被清掉", d == (0, 0)))
    checks.append(("撤销报告如实写了 1 条", len(result["removed"]) == 1))

    token_roundtrip()
    e = (count_cap_aces(), len(acl.list_grants()))
    print(f"E 撤销后可重新授权  : ACE={e[0]} 登记={e[1]}")
    checks.append(("撤销后仍能重新授权", e == (1, 1)))

    acl.revoke_grants()  # 收尾：不给工作区留常驻 ACE
    f = (count_cap_aces(), len(acl.list_grants()))
    print(f"F 收尾清场          : ACE={f[0]} 登记={f[1]}")

    print("\n=== 判定 ===")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(ok for _, ok in checks)
    print(f"\n{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
