"""诊断：受限令牌下私有 temp 目录到底哪一步写不了。

运行：.venv/Scripts/python.exe dev/probe_acl_temp_write.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402
from backend.tools.exec.shells import child_env  # noqa: E402

WS = "D:/Loom_agent/loom/sandbox/poc_ws"

# 单行，不用 try：哪一步失败，异常会直接打印出来
CODE = (
    "import os,tempfile; "
    "t=os.environ['TEMP']; "
    "print('TEMP=',t,'isdir=',os.path.isdir(t)); "
    "p=os.path.join(t,'probe.txt'); "
    "f=open(p,'w'); f.write('ok'); f.close(); "
    "print('DIRECT_WRITE_OK',os.path.exists(p)); "
    "os.remove(p); print('DIRECT_DELETE_OK'); "
    "print('gettempdir=',tempfile.gettempdir()); "
    "g=tempfile.NamedTemporaryFile(delete=False); g.write(b'ok'); g.close(); "
    "print('TEMPFILE_OK',g.name)"
)


def main() -> None:
    acl.revoke_grants()
    sb = acl.AclSandbox(acl.WORKSPACE_WRITE, WS)
    print("create:", sb.create(), "temp:", sb.temp_root)
    env = sb.child_env(child_env())

    rc, out, err = sb.spawn(sys.executable, ["-c", CODE], env=env)
    print("\nrc:", rc)
    print("--- stdout+stderr 完整 ---")
    print(out[:4000])
    if err:
        print("--- err ---")
        print(err[:1000])

    print("\n--- 授权 ACE 掩码 ---")
    import win32security
    sd = win32security.GetFileSecurity(sb.temp_root, acl.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    sid = win32security.ConvertStringSidToSid(acl.capability_sid_string(WS))
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        if len(ace) >= 3 and ace[2] == sid:
            print(f"  ACE[{i}] flags={ace[0]} mask=0x{ace[1]:08X}")

    sb.close()
    acl.revoke_grants()


if __name__ == "__main__":
    main()
