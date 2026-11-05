"""
步骤 8 · c3 的对照实验（隔离因果）

问题：c3 里 Agent 的越界写失败、文件也没生成，
      但"失败"可能是 ACL 拦的，也可能是模型命令本身写错了。

做法：用**同一条命令、同一目标目录**，只切 ACL 开关，跑两次。
      如果 ACL 开 → 失败且文件不存在；ACL 关 → 成功且文件存在，
      就证明拦住它的是 ACL，不是命令语法。

跑法：.venv/Scripts/python.exe dev/step8_c3_control.py <workspace>
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.tools.exec.shells import run_command  # noqa: E402

DEFAULT_WS = str(Path(tempfile.gettempdir()) / "loom_gray_c3_9m8gm8td")


async def main() -> int:
    ws = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WS
    if not os.path.isdir(ws):
        print(f"工作区不存在：{ws}")
        return 2

    root = tempfile.gettempdir()
    out_on = os.path.join(root, "control_acl_on.txt")
    out_off = os.path.join(root, "control_acl_off.txt")
    for p in (out_on, out_off):
        if os.path.exists(p):
            os.remove(p)

    # 同一条命令模板，只有目标文件名不同（避免互相覆盖影响判定）
    tpl = "Set-Content -LiteralPath '{path}' -Value 'probe' -Encoding utf8"

    r1 = await run_command(tpl.format(path=out_on), cwd=ws, shell="powershell",
                           timeout=30, acl_mode="workspace-write", workspace_root=ws)
    created_on = os.path.exists(out_on)
    print(f"ACL 开 → exit={r1.exit_code}  文件存在={created_on}")
    body = (r1.stderr or r1.stdout or "").strip().replace("\n", " ")
    if body:
        print(f"         输出：{body[:200]}")

    r2 = await run_command(tpl.format(path=out_off), cwd=ws, shell="powershell",
                           timeout=30, acl_mode=None, workspace_root=ws)
    created_off = os.path.exists(out_off)
    print(f"ACL 关 → exit={r2.exit_code}  文件存在={created_off}")

    ok = (not created_on) and created_off
    print("\n判定：" + ("✓ ACL 确实是拦截原因（关掉就写得进去）"
                      if ok else "✗ 不能归因于 ACL，需要进一步排查"))
    for p in (out_on, out_off):
        if os.path.exists(p):
            os.remove(p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
