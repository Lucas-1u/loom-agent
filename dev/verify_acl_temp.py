"""私有 temp 目录 + 凭据隔离验收（步骤 4 前置）。

验证链路：``shells.child_env()`` → ``AclSandbox.child_env()`` 覆盖 TMP/TEMP
→ 受限子进程能写私有 temp、看不到凭据、仍写不了工作区外。
收尾 ``close()`` 会撤销 temp 授权并删目录。

运行：.venv/Scripts/python.exe dev/verify_acl_temp.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl  # noqa: E402
from backend.tools.exec.shells import child_env  # noqa: E402

WS = "D:/Loom_agent/loom/sandbox/poc_ws"
SECRET_KEY = "LOOM_TEST_API_KEY"
SECRET_VALUE = "SHOULD_NOT_LEAK"
OUTSIDE = os.path.join(os.path.expanduser("~"), "poc_acl_outside", "temp_probe.txt")

# 单行 Python 子进程：检查 temp 可写、把看到的凭据 / TEMP 打印回来
CODE_TEMP = (
    "import os,tempfile; print('SECRET=',os.environ.get('%s','ABSENT')); "
    "print('TEMPDIR=',tempfile.gettempdir()); "
    "f=tempfile.NamedTemporaryFile(delete=False); f.write(b'ok'); f.close(); "
    "os.remove(f.name); print('TEMP_WRITE_OK')" % SECRET_KEY
)
# 单行 Python 子进程：尝试写工作区外（被拒会抛异常，rc != 0）
CODE_OUTSIDE = (
    "p=r'%s'; f=open(p,'w'); f.write('x'); f.close(); print('WRITE_OK')" % OUTSIDE
)


def run_python(sandbox, code: str, env: dict) -> tuple:
    rc, out, err = sandbox.spawn(sys.executable, ["-c", code], env=env)
    return rc, out.strip(), err.strip()


def main() -> None:
    checks = []
    os.environ[SECRET_KEY] = SECRET_VALUE

    # 清场，保证 workspace 授权从零开始
    acl.revoke_grants()

    filtered = child_env()
    checks.append(("shells.child_env() 剔除凭据", SECRET_KEY not in filtered))
    print(f"1. shells.child_env() 含 {SECRET_KEY}? {SECRET_KEY in filtered}")

    print("\n--- workspace-write 会话 ---")
    sb = acl.AclSandbox(mode=acl.WORKSPACE_WRITE, workspace_root=WS)
    created = sb.create()
    checks.append(("会话创建成功", created))
    if not created:
        print("会话创建失败，中止")
        sys.exit(1)

    temp_root = sb.temp_root
    merged = sb.child_env(filtered)
    checks.append(("TEMP 指向私有目录", merged.get("TEMP") == temp_root))
    checks.append(("合并后凭据仍不在", SECRET_KEY not in merged))
    print(f"2. 私有 temp = {temp_root}")

    rc, out, err = run_python(sb, CODE_TEMP, filtered)
    print(f"3. Python 子进程 rc={rc} out={out[:160]!r}")
    checks.append(("Python 正常启动（无 sitecustomize 报错）", "sitecustomize" not in out))
    checks.append(("私有 temp 可写", rc == 0 and "TEMP_WRITE_OK" in out))
    checks.append(("凭据未泄漏到子进程", "SECRET= ABSENT" in out and SECRET_VALUE not in out))

    rc2, out2, _ = run_python(sb, CODE_OUTSIDE, filtered)
    wrote = os.path.exists(OUTSIDE)
    if wrote:
        os.remove(OUTSIDE)
    print(f"4. 写工作区外 rc={rc2} out={out2[:80]!r} 文件生成={wrote}")
    # 不能看输出里有没有 WRITE_OK：traceback 会把源码行（含该字样）打印出来。
    # 硬证据 = 子进程非零退出 + 父进程确认文件真的没生成。
    checks.append(("写工作区外仍被拦", rc2 != 0 and not wrote))

    sb.close()
    checks.append(("close 后私有 temp 目录被删", not os.path.exists(temp_root)))
    still = [g["path"] for g in acl.list_grants() if g["path"] == os.path.normcase(os.path.abspath(temp_root))]
    checks.append(("close 后 temp 授权登记被移除", not still))
    print(f"5. close 后目录存在={os.path.exists(temp_root)} 残留登记={len(still)}")

    print("\n--- read-only 会话（也应能启动 Python）---")
    ro = acl.AclSandbox(mode=acl.READ_ONLY, workspace_root=WS)
    checks.append(("read-only 会话创建成功", ro.create()))
    rc3, out3, _ = run_python(ro, CODE_TEMP, filtered)
    print(f"6. read-only Python rc={rc3} out={out3[:160]!r}")
    checks.append(("read-only 下也能写私有 temp", rc3 == 0 and "TEMP_WRITE_OK" in out3))
    rc4, out4, _ = run_python(ro, CODE_OUTSIDE, filtered)
    wrote4 = os.path.exists(OUTSIDE)
    if wrote4:
        os.remove(OUTSIDE)
    checks.append(("read-only 写工作区外被拦", rc4 != 0 and not wrote4))
    ro.close()

    acl.revoke_grants()  # 收尾清场
    os.environ.pop(SECRET_KEY, None)

    print("\n=== 判定 ===")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(ok for _, ok in checks)
    print(f"\n{passed}/{len(checks)} 项通过")
    if passed != len(checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
