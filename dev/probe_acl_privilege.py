"""ACL 沙箱前置诊断：标准用户（非提权）进程能否用受限令牌创建子进程。

只读探测：只启动一个立刻退出的 python -c 子进程，不写任何文件。
用于回答「Loom 是否必须以管理员身份运行」以及该选哪个 spawn API。

运行：.venv/Scripts/python.exe dev/probe_acl_privilege.py
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

import win32api
import win32con
import win32security

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_IMPERSONATE = 0x0004
CREATE_NO_WINDOW = 0x08000000
ERROR_PRIVILEGE_NOT_HELD = 1314


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


adv = ctypes.WinDLL("advapi32", use_last_error=True)
ker = ctypes.WinDLL("kernel32", use_last_error=True)

adv.CreateProcessAsUserW.restype = wintypes.BOOL
adv.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
    ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
]
adv.CreateProcessWithTokenW.restype = wintypes.BOOL
adv.CreateProcessWithTokenW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p,
]
ker.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
ker.CloseHandle.argtypes = [wintypes.HANDLE]


def show_token(label: str) -> None:
    h = win32security.OpenProcessToken(win32api.GetCurrentProcess(), TOKEN_QUERY)
    privs = win32security.GetTokenInformation(h, win32security.TokenPrivileges)
    names = sorted(win32security.LookupPrivilegeName(None, luid) for luid, _ in privs)
    print(f"{label}: {len(names)} 项特权 -> {', '.join(names)}")
    win32api.CloseHandle(h)


def try_spawn(label: str, token, fn) -> None:
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(f'"{sys.executable}" -c "print(42)"')
    ctypes.set_last_error(0)
    ok = fn(cmdline, ctypes.byref(si), ctypes.byref(pi))
    err = ctypes.get_last_error()
    if ok:
        ker.WaitForSingleObject(pi.hProcess, 5000)
        ker.CloseHandle(pi.hThread)
        ker.CloseHandle(pi.hProcess)
        print(f"  {label}: 成功 (pid={pi.dwProcessId})")
    else:
        hint = " <== ERROR_PRIVILEGE_NOT_HELD" if err == ERROR_PRIVILEGE_NOT_HELD else ""
        print(f"  {label}: 失败 GetLastError={err}{hint}")


def main() -> None:
    show_token("当前进程")
    base = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_IMPERSONATE,
    )

    dup = win32security.DuplicateTokenEx(
        base, win32security.SecurityImpersonation,
        win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary,
    )
    restricted = win32security.CreateRestrictedToken(base, 0, [], [], [])
    print("DuplicateTokenEx 主令牌 / CreateRestrictedToken 受限令牌：均创建成功")
    print(f"STARTUPINFOW.cb = {ctypes.sizeof(STARTUPINFOW)}")

    print("\n--- CreateProcessAsUserW（DSH 用的） ---")
    try_spawn(
        "普通主令牌", dup,
        lambda cmd, si, pi: adv.CreateProcessAsUserW(
            int(dup), None, cmd, None, None, False, CREATE_NO_WINDOW, None, None, si, pi
        ),
    )
    try_spawn(
        "受限令牌(自己的)", restricted,
        lambda cmd, si, pi: adv.CreateProcessAsUserW(
            int(restricted), None, cmd, None, None, False, CREATE_NO_WINDOW, None, None, si, pi
        ),
    )

    print("\n--- CreateProcessWithTokenW（acl.py 现在用的） ---")
    try_spawn(
        "普通主令牌", dup,
        lambda cmd, si, pi: adv.CreateProcessWithTokenW(
            int(dup), 0, None, cmd, CREATE_NO_WINDOW, None, None, si, pi
        ),
    )
    try_spawn(
        "受限令牌(自己的)", restricted,
        lambda cmd, si, pi: adv.CreateProcessWithTokenW(
            int(restricted), 0, None, cmd, CREATE_NO_WINDOW, None, None, si, pi
        ),
    )

    for h in (dup, restricted, base):
        try:
            win32api.CloseHandle(int(h) if not isinstance(h, int) else h)
        except Exception:
            pass


if __name__ == "__main__":
    main()
