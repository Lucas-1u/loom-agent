"""
ACL 沙箱（Windows 受限令牌）—— 对齐 DSH ``dsh-sandbox-windows-acl``

2026-11-04 重写（步骤 3），后续在步骤 4 持续补齐。相对初版的关键修正，原因都写在这里：

1. **spawn API 换成 ``CreateProcessAsUserW``**
   DSH 的 ``dsh-win32-process/lib/index.js:425`` 直接调它；初版抄成了
   ``CreateProcessWithTokenW``，后者需要 SE_IMPERSONATE，标准用户必然
   ERROR_PRIVILEGE_NOT_HELD(1314)。实测（``dev/probe_acl_privilege.py``）：
   同一个普通权限进程下，前者用自己派生的受限令牌能起进程，后者两种令牌都失败。
   **所以不需要管理员运行 Loom。**

2. **令牌加 WRITE_RESTRICTED 标志（flags=13，同 DSH）**
   DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED。
   WRITE_RESTRICTED 让「restricting SID 二次检查」只作用于写访问；
   不加的话连读都被限制，子进程可能连系统 DLL 都加载不了。
   这也是「只限制写、不限制读」的实现机制。

3. **任何失败都不回退到无限制 spawn**
   DSH ``runner.js:48``：The child is NEVER spawned unrestricted。
   初版在 CreateProcess 失败时悄悄退回普通 spawn，导致「沙箱没生效」不可见。
   现在失败返回 (127, "", "ACL_SPAWN_FAILED: ...")，由调用方如实呈现。

4. **stdout/stderr 用匿名管道捕获**
   初版 ``spawn_with_token`` 恒返回 ("", "")，PoC 根本看不到 WRITE_BLOCKED。
   DSH 的 AclSandbox 也是捕获 stdio（runner 风格才用继承 stdio）。

5. **``env`` 现在生效**（21:4x 实测推翻「API 不支持」的推断）
   ``dev/probe_acl_env.py`` 证明 pywin32 的 ``CreateProcessAsUser`` 直接收 dict；
   DSH 报 ERROR_INVALID_PARAMETER 是 koffi FFI 缓冲的问题，不是 Win32 的限制。
   因此步骤 4 可以直接复用 ``shells.child_env()`` 的凭据剔除逻辑。

6. **私有 temp 目录（21:5x 补，22:1x 修正）**
   受限令牌连 TEMP 都写不了，Python 子进程会报 "No usable temporary directory"。
   ``AclSandbox`` 建私有 temp 目录、用**独立的 temp SID** 授权，并通过
   ``child_env()`` 把 TMP/TEMP/TMPDIR 指过去；``close()`` 撤销 ACE 并删目录。
   初版图省事让 workspace / temp 共用一个 SID，结果 read-only 令牌也带 workspace SID，
   命中工作区上的 standing ACE → **read-only 竟能写工作区**。
   ``dev/verify_acl_shells.py`` 实测证伪，现已对齐 DSH 拆成两个独立 SID。

三层机制（抄 DSH）：
  1. 受限令牌：CreateRestrictedToken 从当前用户令牌派生，限制 SID 列表取交集
  2. 能力 SID + DACL 授权：workspace SID = S-1-4-<路径哈希>，给工作区目录加 allow ACE
  3. spawn 时传令牌：CreateProcessAsUser 让子进程跑在受限身份下

已知边界（步骤 4 集成时要处理）：
  - 未实现 Low 完整性标签、授权目录 mandatory label、父目录拒绝删除 ACE。
  - ✅ 超时 / 取消 / 进程树 kill 已接入（23:2x）：``spawn_with_token`` 用
    ``PeekNamedPipe`` 非阻塞读 + 分段 ``WaitForSingleObject``，超时或 cancel 时
    ``taskkill /F /T`` 杀整棵进程树；返回 ``ACL_TIMEOUT`` / ``ACL_CANCELLED``
    由 shells 层转成 ``timed_out`` / ``cancelled``。
  - 目录授权会留下一条常驻 ACE；本模块用 ``--list`` / ``--revoke-all`` 登记与撤销，
    ``AclSandbox.close()`` 会主动清掉自己那条 temp 授权。
  - ``AclSandbox.spawn(env=...)`` 需要调用方传 ``shells.child_env()``：本模块**不 import
    tools 层**，否则步骤 4 的 ``shells.py -> acl`` 会形成循环依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# ---- 平台检测 ----
if sys.platform == "win32":
    import win32api
    import win32con
    import win32event
    import win32file
    import win32pipe
    import win32process
    import win32security

    HAVE_PYWIN32 = True
else:
    HAVE_PYWIN32 = False

# ---- 模式常量 ----
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
FULL_ACCESS = "full-access"
_VALID_MODES = (READ_ONLY, WORKSPACE_WRITE, FULL_ACCESS)

# ---- Win32 常量（pywin32 未全部暴露，按 SDK 定义写死） ----
# 写授权掩码：Write | Delete | FILE_DELETE_CHILD
# 不含 WRITE_DAC / WRITE_OWNER，防受限子进程改写 DACL 逃逸
GRANT_MASK = 0x112012

# SECURITY_INFORMATION：DACL 是 0x4，不是 0x8（0x8 是 SACL，初版写错了）
DACL_SECURITY_INFORMATION = 0x00000004
SE_FILE_OBJECT = 1
ACL_REVISION = 2
# ACE 继承位：授权必须传给新建的文件/子目录，否则 os.remove / tempfile 的
# 「创建 + 删除」探测会因文件没有 DELETE 权限而失败（实测 WinError 5）
ACE_OBJECT_INHERIT_ACE = 0x1
ACE_CONTAINER_INHERIT_ACE = 0x2
ACE_INHERIT_FLAGS = ACE_OBJECT_INHERIT_ACE | ACE_CONTAINER_INHERIT_ACE

# CreateRestrictedToken flags，值 13 与 DSH types-Cl_DXjhk.js:893 一致
DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
RESTRICTED_TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED

# 令牌访问权（最小集，不用 TOKEN_ALL_ACCESS）
TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_IMPERSONATE = 0x0004
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_BASIC_RIGHTS = (
    TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY
    | TOKEN_IMPERSONATE | TOKEN_ADJUST_DEFAULT
)
FILE_ALL_ACCESS = 0x1F01FF

SE_GROUP_LOGON_ID = 0xC0000000        # 从 TokenGroups 里挑 logon SID 用
CAPABILITY_SID_AUTHORITY = 4           # S-1-4-... = SECURITY_NON_UNIQUE_AUTHORITY
STARTF_USESTDHANDLES = 0x00000100
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
WAIT_TIMEOUT_MS = 30_000
WAIT_OBJECT_0 = 0
POLL_INTERVAL_MS = 50        # 分段等待的步长：够及时响应 cancel，又不忙等
KILL_WAIT_MS = 1000          # taskkill 后留给进程句柄收敛的时间
MAX_OUTPUT_CHARS = 6000
_GRANTS_FILENAME = "acl_grants.json"
_TEMP_DIRNAME = "acl_tmp"


# =========================================================================== #
# 公开 API
# =========================================================================== #

def capability_sid_string(workspace_root: str) -> str:
    """由工作区路径确定性地生成能力 SID 字符串 ``S-1-4-<4 x uint32>``。

    用 ``ConvertStringSidToSid`` 从字符串构造任意 SID，比 ctypes 调
    ``AllocateAndInitializeSid`` 简单，也和 DSH 的 ``ConvertStringSidToSidW`` 一致。
    """
    normalized = os.path.normcase(os.path.abspath(workspace_root))
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    subs = [int.from_bytes(digest[i : i + 4], "big") for i in range(0, 16, 4)]
    return f"S-1-{CAPABILITY_SID_AUTHORITY}-" + "-".join(str(s) for s in subs)


def temp_sid_string(task_tag: str) -> str:
    """私有 temp 目录专用的第二个能力 SID（与 workspace SID 完全独立）。

    DSH ``runner.js:22-24`` 也是两个 SID：workspace / temp 各一个。
    分开的原因见 ``create_restricted_token`` 里的注释 —— 共用一个会让
    read-only 意外获得工作区写权限。
    """
    digest = hashlib.sha256(("loom-temp|" + task_tag).encode("utf-8")).digest()
    subs = [int.from_bytes(digest[i : i + 4], "big") for i in range(0, 16, 4)]
    return f"S-1-{CAPABILITY_SID_AUTHORITY}-" + "-".join(str(s) for s in subs)


def create_restricted_token(
    mode: str, workspace_root: str, temp_sid=None, temp_root: str = None
) -> Tuple[Optional[object], Optional[object]]:
    """创建（受限）令牌。返回 ``(token, workspace_sid)``，失败抛异常。

    - full-access:     复制一个不受限的主令牌（仍是同一用户），统一走 spawn 路径
    - read-only:       restricting SID 只有 logon / Everyone / ``temp_sid``
    - workspace-write: 再加 workspace SID，并授权工作区目录

    ``temp_sid`` / ``temp_root`` 由 ``AclSandbox`` 传入私有 temp；不传时
    read-only 就是「哪儿都不能写」，但仍能启动进程（默认 DACL 已补 ACE）。
    三种模式都返回非 None 的令牌，调用方无需区分「没沙箱」和「创建失败」。
    """
    if not HAVE_PYWIN32:
        raise RuntimeError("ACL 沙箱只在 Windows + pywin32 下可用")
    if mode not in _VALID_MODES:
        raise ValueError(f"未知 ACL 模式: {mode!r}，可选 {_VALID_MODES}")

    base = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), TOKEN_BASIC_RIGHTS
    )
    try:
        if mode == FULL_ACCESS:
            # DuplicateTokenEx 从自己的令牌复制一个主令牌：不需要任何特权
            token = win32security.DuplicateTokenEx(
                base,
                win32security.SecurityImpersonation,
                win32con.TOKEN_ALL_ACCESS,
                win32security.TokenPrimary,
            )
            return token, None

        # TokenGroups 是「信息类常量」，不是函数；必须走 GetTokenInformation
        groups = win32security.GetTokenInformation(base, win32security.TokenGroups)
        logon_sid = None
        for sid, attrs in groups:
            if (attrs & 0xFFFFFFFF) & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
                logon_sid = sid
                break
        if logon_sid is None:
            raise RuntimeError(
                f"CreateRestrictedToken 前置条件失败：{len(groups)} 个组里找不到 logon SID"
            )

        # 由名字查 SID 用 LookupAccountName（初版把 LookupAccountSid 用反了）
        everyone_sid = win32security.LookupAccountName(None, "Everyone")[0]

        # ★ 两个独立 capability SID（对齐 DSH runner.js:22-24）：
        #   workspace SID 只进 workspace-write 令牌，temp SID 进所有非 full-access 令牌。
        #   之前图省事共用一个 SID，结果 read-only 令牌也带 workspace SID，
        #   命中了工作区上那条 standing ACE → read-only 竟能写工作区。
        #   dev/verify_acl_shells.py 实测证伪，必须分开，见 docs/25。
        write_sids = []
        cap_sid = None
        if mode == WORKSPACE_WRITE:
            cap_sid = win32security.ConvertStringSidToSid(
                capability_sid_string(workspace_root)
            )
            if not grant_dir_write(workspace_root, cap_sid):
                raise RuntimeError(
                    f"给工作区 {workspace_root!r} 添加写授权失败，"
                    "拒绝创建一个「哪里都写不了」的令牌"
                )
            write_sids.append(cap_sid)
        if temp_sid is not None:
            if not temp_root:
                raise RuntimeError("传了 temp_sid 就必须同时给 temp_root")
            if not grant_dir_write(temp_root, temp_sid):
                raise RuntimeError(
                    f"给私有 temp {temp_root!r} 添加写授权失败，"
                    "拒绝创建一个「哪里都写不了」的令牌"
                )
            write_sids.append(temp_sid)

        # pywin32 的 RestrictingSids 要 SID_AND_ATTRIBUTES：[(PySID, int), ...]
        restricting_sids = [(logon_sid, 0), (everyone_sid, 0)]
        restricting_sids.extend((sid, 0) for sid in write_sids)

        token = win32security.CreateRestrictedToken(
            base, RESTRICTED_TOKEN_FLAGS, [], [], restricting_sids
        )
        # DSH types-Cl_DXjhk.js:820 / :1292 —— 受限令牌原样继承了用户的默认 DACL，
        # 里面没有任何 restricting SID；子进程启动时创建内核对象会过不了二次写检查，
        # 实测表现为 0xC0000142 STATUS_DLL_INIT_FAILED。必须补 allow ACE。
        # 同时补 Everyone：Cygwin/MSYS 这类运行时要建命名 file mapping，
        # 只补能力 SID 仍会 Win32 error 5（见 dev/probe_acl_bash 的诊断记录）。
        _grant_token_default_dacl(token, [everyone_sid, *write_sids])
        return token, cap_sid
    finally:
        win32api.CloseHandle(base)


def grant_dir_write(directory: str, capability_sid) -> bool:
    """给目录 DACL 添加能力 SID 的写授权 ACE（幂等、自动登记）。

    工作区目录与私有 temp 目录共用这个函数：同一个能力 SID，只是授权挂在不同目录上。
    用 pywin32 的 GetFileSecurity / SetFileSecurity；初版用的
    ``GetNamedSecurityInfoW`` / ``SetNamedSecurityInfoW`` 在 pywin32 里并不存在。
    """
    try:
        path = os.path.abspath(directory)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)

        sd = win32security.GetFileSecurity(path, DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            dacl = win32security.ACL()

        # pywin32 GetAce 实测形状：((aceType, aceFlags), mask, PySID)。
        # 另注意 GetBinarySid 只接受字符串 SID，传 PySID 会报 "Textual SID invalid"；
        # PySID 本身支持 == 比较，直接用即可。
        for i in range(dacl.GetAceCount()):
            ace = dacl.GetAce(i)
            if (
                len(ace) >= 3
                and ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and ace[2] == capability_sid
            ):
                if ace[0][1] & ACE_INHERIT_FLAGS == ACE_INHERIT_FLAGS:
                    # ACE 可能早于登记文件存在（例如手工加过），命中时也补一次登记
                    _record_grant(path, capability_sid)
                    return True
                # 旧版本加的 ACE 没有 OI|CI：新建文件删不掉，自愈重建
                dacl.DeleteAce(i)
                break

        dacl.AddAccessAllowedAceEx(
            ACL_REVISION, ACE_INHERIT_FLAGS, GRANT_MASK, capability_sid
        )
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(path, DACL_SECURITY_INFORMATION, sd)
        _record_grant(path, capability_sid)
        return True
    except Exception as exc:  # noqa: BLE001 - 要如实上报，不能静默吞掉
        warnings.warn(f"grant_dir_write 失败: {exc!r}", stacklevel=2)
        return False


# 兼容早期调用点（PoC / 可证伪脚本里的旧名字）
grant_workspace_write = grant_dir_write


def _grant_token_default_dacl(token, sids) -> None:
    """给受限令牌的默认 DACL 补若干 full-access ACE。

    每个 SID **必须是 restricting SID 列表里的成员**，否则二次写检查仍然过不去
    —— 实测补一个不在列表里的 SID，子进程照样 0xC0000142。
    对齐 DSH ``setTokenDefaultDaclGrant``；补 Everyone 是兼容 Cygwin/MSYS。
    """
    items = sids if isinstance(sids, (list, tuple)) else [sids]
    dacl = win32security.GetTokenInformation(token, win32security.TokenDefaultDacl)
    if dacl is None:
        dacl = win32security.ACL()
    for sid in items:
        dacl.AddAccessAllowedAce(ACL_REVISION, FILE_ALL_ACCESS, sid)
    win32security.SetTokenInformation(token, win32security.TokenDefaultDacl, dacl)


def spawn_with_token(
    token, command: str, args: list, cwd: str = None, env: dict = None,
    timeout_ms: int = None, cancel_event=None,
) -> Tuple[int, str, str]:
    """用令牌 spawn 进程，返回 ``(exit_code, stdout, stderr)``。

    ``env`` 传 dict 即可生效（``dev/probe_acl_env.py`` 实测）；调用方应传
    ``shells.child_env()`` 以沿用凭据剔除逻辑。传 None 表示继承父进程环境。

    ``timeout_ms`` / ``cancel_event``（步骤 4 补齐）：超时或取消会用
    ``taskkill /F /T`` 杀整棵进程树，并把 ``ACL_TIMEOUT`` / ``ACL_CANCELLED``
    放在第三个返回值里，供调用方设置 timed_out / cancelled。

    **失败绝不回退到普通 spawn** —— 返回 127 + 明确错误，避免「沙箱悄悄没生效」。
    """
    if token is None or not HAVE_PYWIN32:
        raise RuntimeError("spawn_with_token 需要一个有效的令牌（没有无沙箱兜底）")

    cmd_line = _build_cmd_line(command, args)

    # 建可继承句柄的管道；父进程只保留读端，子进程拿写端
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = 1
    out_r, out_w = win32pipe.CreatePipe(sa, 0)
    in_r, in_w = win32pipe.CreatePipe(sa, 0)

    si = win32process.STARTUPINFO()
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = in_r
    si.hStdOutput = out_w
    si.hStdError = out_w

    try:
        h_process, h_thread, pid, _tid = win32process.CreateProcessAsUser(
            token,
            None,
            cmd_line,
            None,
            None,
            True,                 # bInheritHandles：让管道句柄传给孩子
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            env,                  # None = 继承；dict = 过滤后的环境表（可剔除凭据）
            cwd,
            si,
        )
    except Exception as exc:  # noqa: BLE001
        for h in (out_r, out_w, in_r, in_w):
            _close(h)
        return 127, "", f"ACL_SPAWN_FAILED: {exc!r}"

    # 父进程必须立刻关掉自己持有的「子进程端」，否则 ReadFile 等不到 EOF
    _close(in_r)
    _close(in_w)
    _close(out_w)

    # ★ 不能再用「先阻塞 ReadFile 读到 EOF、再等进程」的老写法：
    #   子进程若一直不退出，ReadFile 会永久阻塞，timeout / cancel 逻辑根本到不了。
    #   改成 PeekNamedPipe 非阻塞偷看 + 分段 WaitForSingleObject。
    limit_ms = int(timeout_ms) if timeout_ms and int(timeout_ms) > 0 else WAIT_TIMEOUT_MS
    deadline = time.monotonic() + limit_ms / 1000.0
    chunks: list[bytes] = []
    total = 0
    pipe_open = True
    stop_reason = ""

    def drain() -> None:
        """取走管道里当前可读的数据；写端关闭后不再尝试。"""
        nonlocal pipe_open, total
        if not pipe_open:
            return
        while total <= MAX_OUTPUT_CHARS * 2:
            try:
                avail = win32pipe.PeekNamedPipe(out_r, 0)[1]
            except Exception:  # noqa: BLE001 - ERROR_BROKEN_PIPE：写端已全关
                pipe_open = False
                return
            if not avail:
                return
            try:
                _hr, data = win32file.ReadFile(out_r, min(avail, 65536))
            except Exception:  # noqa: BLE001
                pipe_open = False
                return
            if not data:
                return
            chunks.append(data)
            total += len(data)

    try:
        while True:
            drain()
            if cancel_event is not None and getattr(
                cancel_event, "is_set", lambda: False
            )():
                stop_reason = "ACL_CANCELLED"
                break
            if win32event.WaitForSingleObject(h_process, 0) == WAIT_OBJECT_0:
                drain()  # 进程刚退出，把尾巴读干净
                break
            if time.monotonic() >= deadline:
                stop_reason = "ACL_TIMEOUT"
                break
            win32event.WaitForSingleObject(h_process, POLL_INTERVAL_MS)
    finally:
        _close(out_r)

    if stop_reason:
        _kill_process_tree(pid)
        win32event.WaitForSingleObject(h_process, KILL_WAIT_MS)

    try:
        exit_code = win32process.GetExitCodeProcess(h_process)
    except Exception:  # noqa: BLE001
        exit_code = -1
    if stop_reason:
        exit_code = -1
    win32api.CloseHandle(h_thread)
    win32api.CloseHandle(h_process)

    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    return exit_code, text[:MAX_OUTPUT_CHARS], stop_reason


def close_token(token) -> None:
    """关闭令牌句柄。"""
    if token is not None and HAVE_PYWIN32:
        _close(token)


# =========================================================================== #
# 授权登记与撤销
# 方案 B：承认 ACE 会常驻（同 DSH 的 "workspace ACEs stay standing"），
# 但把每次授权登记在 Loom 自己的运行时目录，并提供一键撤销入口。
# =========================================================================== #

def _runtime_dir() -> Path:
    """Loom 运行时目录（``sandbox/``）：私有 temp 与授权登记都放这里。"""
    return Path(__file__).resolve().parents[2] / "sandbox"


def _grants_file() -> Path:
    """登记文件放 Loom 运行时目录，不写进用户工作区。"""
    return _runtime_dir() / _GRANTS_FILENAME


def _load_grants() -> dict:
    path = _grants_file()
    if not path.exists():
        return {"version": 1, "grants": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("grants"), list):
            return data
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"授权登记文件损坏，按空登记处理: {exc!r}", stacklevel=2)
    return {"version": 1, "grants": []}


def _save_grants(data: dict) -> None:
    path = _grants_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # 原子替换，避免写一半留下坏文件


def _record_grant(path: str, capability_sid) -> None:
    key = os.path.normcase(os.path.abspath(path))
    sid_str = win32security.ConvertSidToStringSid(capability_sid)
    data = _load_grants()
    if any(g.get("path") == key and g.get("sid") == sid_str for g in data["grants"]):
        return  # 幂等：同一路径 + 同一 SID 只登记一次
    data["grants"].append({
        "path": key,
        "sid": sid_str,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_grants(data)


def list_grants() -> list:
    """列出所有仍登记在案的目录授权。"""
    return list(_load_grants()["grants"])


def _revoke_one(path: str, sid_str: str) -> tuple[bool, str]:
    if not os.path.isdir(path):
        return False, "目录不存在"
    try:
        sd = win32security.GetFileSecurity(path, DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            return True, "目录无 DACL（无需撤销）"
        sid = win32security.ConvertStringSidToSid(sid_str)
        hits = []
        for i in range(dacl.GetAceCount()):
            ace = dacl.GetAce(i)
            if (
                len(ace) >= 3
                and ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and ace[2] == sid
            ):
                hits.append(i)
        if not hits:
            return True, "ACE 已不存在（可能已被手工清理）"
        for i in reversed(hits):  # 从后往前删，避免索引前移
            dacl.DeleteAce(i)
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(path, DACL_SECURITY_INFORMATION, sd)
        return True, f"删除 {len(hits)} 条 ACE"
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def revoke_grants(paths=None) -> dict:
    """撤销已登记的 ACE。``paths=None`` 表示全部。

    撤销失败的记录会**保留**在登记里，便于下次重试（不谎称已清理）。
    """
    data = _load_grants()
    wanted = (
        {os.path.normcase(os.path.abspath(p)) for p in paths}
        if paths else None
    )
    removed, failed, remaining = [], [], []
    for g in data["grants"]:
        if wanted is not None and g.get("path") not in wanted:
            remaining.append(g)
            continue
        ok, msg = _revoke_one(g.get("path", ""), g.get("sid", ""))
        (removed if ok else failed).append({**g, "result": msg})
        if not ok:
            remaining.append(g)
    data["grants"] = remaining
    _save_grants(data)
    return {"removed": removed, "failed": failed, "remaining": len(remaining)}


# =========================================================================== #
# 内部辅助
# =========================================================================== #

def _kill_process_tree(pid: int) -> None:
    """超时 / 取消时杀整棵进程树（对齐 ``shells._kill_tree`` 的 Windows 做法）。

    由**父进程**（正常权限）执行 taskkill，不受受限令牌影响。
    只杀主进程会留下孙进程继续持有资源，所以必须用 /T。
    """
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, timeout=5,
        )
    except Exception:  # noqa: BLE001 - 杀不掉也不能把父任务卡住
        pass


def _close(handle) -> None:
    try:
        if hasattr(handle, "Close"):
            handle.Close()
        else:
            win32api.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def _build_cmd_line(command: str, args: list) -> str:
    """构建命令行字符串；按 CommandLineToArgvW 规则转义引号与反斜杠。"""
    def quote(part: str) -> str:
        s = str(part)
        if s and not any(c in s for c in ' \t"\n'):
            return s
        out = []
        bs = 0
        for ch in s:
            if ch == "\\":
                bs += 1
            elif ch == '"':
                out.append("\\" * (bs * 2 + 1) + '"')
                bs = 0
            else:
                out.append("\\" * bs + ch)
                bs = 0
        return '"' + "".join(out) + "\\" * (bs * 2) + '"'

    return " ".join(quote(p) for p in [command, *args])


# =========================================================================== #
# 便捷类
# =========================================================================== #

@dataclass
class AclSandbox:
    """ACL 沙箱会话：持有受限令牌 + 私有 temp 目录，负责完整生命周期。

    **步骤 4 请用这个类**，不要直接用底层 ``create_restricted_token`` —— 只有它会：
      - 建私有 temp 目录并授权（否则 Python 子进程报 "No usable temporary directory"）
      - 把 ``TMP/TEMP/TMPDIR`` 指过去
      - ``close()`` 撤销 temp 授权、删目录、关令牌

    ``spawn(env=...)`` 在生产里应传 ``shells.child_env()``（已剔除凭据）。
    本模块不 import tools 层，避免步骤 4 的 ``shells.py -> acl`` 形成循环依赖。
    """

    mode: str = WORKSPACE_WRITE
    workspace_root: str = ""
    token: object = None
    capability_sid: object = None
    temp_root: str = ""
    temp_sid: object = None
    _created: bool = False

    def create(self) -> bool:
        try:
            self.temp_root = ""
            self.temp_sid = None
            if self.mode != FULL_ACCESS:
                # 父进程（正常权限）建私有目录；受限子进程只能写这里
                tag = uuid.uuid4().hex[:12]
                self.temp_root = str(_runtime_dir() / _TEMP_DIRNAME / tag)
                os.makedirs(self.temp_root, exist_ok=True)
                # 第二个独立 SID：与 workspace SID 分开，保证 read-only 不误开工作区
                self.temp_sid = win32security.ConvertStringSidToSid(temp_sid_string(tag))
            token, capability_sid = create_restricted_token(
                self.mode,
                self.workspace_root,
                temp_sid=self.temp_sid,
                temp_root=self.temp_root or None,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"AclSandbox 创建失败: {exc!r}", stacklevel=2)
            self._cleanup_temp()
            return False
        self.token = token
        self.capability_sid = capability_sid
        self._created = True
        return True

    def child_env(self, base_env: dict = None) -> dict:
        """在调用方给的环境表上覆盖 ``TMP`` / ``TEMP`` / ``TMPDIR``。

        ``base_env`` 生产环境应传 ``shells.child_env()``；传 None 会从
        ``os.environ`` 派生（**含凭据**，仅适合离线验证）。
        """
        env = dict(base_env if base_env is not None else os.environ)
        if self.temp_root:
            env["TMP"] = self.temp_root
            env["TEMP"] = self.temp_root
            env["TMPDIR"] = self.temp_root
        return env

    def spawn(self, command: str, args: list, cwd: str = None, env: dict = None,
              timeout_ms: int = None, cancel_event=None):
        return spawn_with_token(
            self.token, command, args,
            cwd=cwd, env=self.child_env(env),
            timeout_ms=timeout_ms, cancel_event=cancel_event,
        )

    def _cleanup_temp(self) -> None:
        if not self.temp_root:
            return
        try:
            # 撤销该目录上登记的 ACE，并从登记文件里移除
            revoke_grants([self.temp_root])
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"撤销 temp 授权失败: {exc!r}", stacklevel=2)
        try:
            # 只删运行时目录里的私有 temp，绝不误删别处
            root = str((_runtime_dir() / _TEMP_DIRNAME).resolve())
            target = os.path.abspath(self.temp_root)
            if os.path.commonpath([root, target]) == root:
                shutil.rmtree(target, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"清理私有 temp 失败: {exc!r}", stacklevel=2)
        self.temp_root = ""

    def close(self):
        if self._created:
            close_token(self.token)
            self.token = None
            self._created = False
        self._cleanup_temp()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - 解释器退出时 globals 可能已卸载
            pass


# =========================================================================== #
# 命令行入口（授权登记管理）
#   .venv/Scripts/python.exe -m backend.sandbox.acl --list
#   .venv/Scripts/python.exe -m backend.sandbox.acl --revoke-all
#   .venv/Scripts/python.exe -m backend.sandbox.acl --revoke <路径>
# =========================================================================== #

def _cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Loom ACL 沙箱：目录授权登记管理")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出所有已登记的目录授权")
    group.add_argument("--revoke-all", action="store_true", help="撤销全部授权")
    group.add_argument("--revoke", metavar="PATH", help="只撤销指定目录的授权")
    ns = parser.parse_args(argv)

    if ns.list:
        grants = list_grants()
        if not grants:
            print("当前没有登记的目录授权。")
        else:
            print(f"已登记 {len(grants)} 条目录授权：")
            for i, item in enumerate(grants, 1):
                print(f"  {i}. {item.get('path')}")
                print(f"     SID={item.get('sid')}  添加于 {item.get('added_at')}")
        return 0

    result = revoke_grants(None if ns.revoke_all else [ns.revoke])
    for item in result["removed"]:
        print(f"[已撤销] {item['path']} -> {item['result']}")
    for item in result["failed"]:
        print(f"[失败]   {item['path']} -> {item['result']}")
    print(
        f"撤销 {len(result['removed'])} 条，失败 {len(result['failed'])} 条，"
        f"登记剩余 {result['remaining']} 条。"
    )
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
