"""诊断：CreateProcessAsUser 能否接收自定义环境表（决定凭据隔离走 A 还是 B）。

只读探测：子进程只打印一个测试变量的值，不写文件、不联网。
运行：.venv/Scripts/python.exe dev/probe_acl_env.py
"""

from __future__ import annotations

import os
import sys

import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32process
import win32security

SECRET_KEY = "LOOM_TEST_SECRET"
SECRET_VALUE = "SHOULD_NOT_APPEAR"
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100


def spawn(token, env, extra_flags, label):
    """用给定 env / flags 启动 python 子进程并读回它看到的变量值。"""
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = 1
    out_r, out_w = win32pipe.CreatePipe(sa, 0)
    in_r, in_w = win32pipe.CreatePipe(sa, 0)

    si = win32process.STARTUPINFO()
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = in_r
    si.hStdOutput = out_w
    si.hStdError = out_w

    child_code = "import os; print(os.environ.get(%r, %r))" % (SECRET_KEY, "ABSENT")
    cmdline = '"%s" -c "%s"' % (sys.executable, child_code)

    try:
        hp, ht, _pid, _tid = win32process.CreateProcessAsUser(
            token, None, cmdline, None, None, True,
            CREATE_NO_WINDOW | extra_flags, env, None, si,
        )
    except Exception as exc:  # noqa: BLE001
        for h in (out_r, out_w, in_r, in_w):
            try:
                h.Close()
            except Exception:
                pass
        print(f"  {label}: spawn 失败 -> {exc!r}")
        return None, "SPAWN_FAILED"

    for h in (in_r, in_w, out_w):
        h.Close()

    chunks = []
    while True:
        try:
            _hr, data = win32file.ReadFile(out_r, 65536)
        except Exception:  # noqa: BLE001
            break
        if not data:
            break
        chunks.append(data)
    out_r.Close()

    win32event.WaitForSingleObject(hp, 10000)
    rc = win32process.GetExitCodeProcess(hp)
    win32api.CloseHandle(ht)
    win32api.CloseHandle(hp)
    out = b"".join(chunks).decode("utf-8", errors="replace").strip()
    print(f"  {label}: rc={rc} 子进程看到={out!r}")
    return rc, out


def main() -> None:
    os.environ[SECRET_KEY] = SECRET_VALUE
    print(f"父进程 {SECRET_KEY}={os.environ[SECRET_KEY]!r}")

    base = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        0x0008 | 0x0002 | 0x0001 | 0x0004 | 0x0080,
    )
    full = win32security.DuplicateTokenEx(
        base, win32security.SecurityImpersonation,
        win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary,
    )

    groups = win32security.GetTokenInformation(base, win32security.TokenGroups)
    logon = next(
        (s for s, a in groups if (a & 0xFFFFFFFF) & 0xC0000000 == 0xC0000000), None
    )
    everyone = win32security.LookupAccountName(None, "Everyone")[0]
    restricted = win32security.CreateRestrictedToken(
        base, 13, [], [], [(logon, 0), (everyone, 0)]
    )
    dacl = win32security.GetTokenInformation(
        restricted, win32security.TokenDefaultDacl
    )
    dacl.AddAccessAllowedAce(2, 0x1F01FF, everyone)
    win32security.SetTokenInformation(
        restricted, win32security.TokenDefaultDacl, dacl
    )

    filtered = dict(os.environ)
    filtered.pop(SECRET_KEY, None)

    print("\n--- 完整令牌 ---")
    verdicts = {}
    r, o = spawn(full, None, 0, "env=None（继承）")
    verdicts["inherit"] = o
    r, o = spawn(full, filtered, 0, "env=dict（无 unicode flag）")
    verdicts["dict_plain"] = o
    r, o = spawn(full, filtered, CREATE_UNICODE_ENVIRONMENT, "env=dict（unicode flag）")
    verdicts["dict_unicode"] = o

    print("\n--- 受限令牌 ---")
    r, o = spawn(restricted, filtered, CREATE_UNICODE_ENVIRONMENT, "env=dict（unicode flag）")
    verdicts["restricted"] = o

    print("\n=== 判定 ===")
    ok_plain = verdicts["dict_plain"] == "ABSENT"
    ok_unicode = verdicts["dict_unicode"] == "ABSENT"
    ok_restricted = verdicts["restricted"] == "ABSENT"
    print(f"  env=None 继承到密钥（对照组）: {verdicts['inherit'] == SECRET_VALUE}")
    print(f"  [{'PASS' if ok_plain else 'FAIL'}] 传 dict 不带 flag: {verdicts['dict_plain']}")
    print(f"  [{'PASS' if ok_unicode else 'FAIL'}] 传 dict 带 unicode flag: {verdicts['dict_unicode']}")
    print(f"  [{'PASS' if ok_restricted else 'FAIL'}] 受限令牌下传 dict: {verdicts['restricted']}")
    if ok_plain or ok_unicode:
        print("\n=> 方案 B 成立：pywin32 可以直接传过滤后的环境表，不需要改配置加载（方案 A）。")
    else:
        print("\n=> 方案 B 不成立：需要回退方案 A（把凭据从 os.environ 源头挪走）。")

    for h in (full, restricted, base):
        win32api.CloseHandle(h)
    os.environ.pop(SECRET_KEY, None)


if __name__ == "__main__":
    main()
