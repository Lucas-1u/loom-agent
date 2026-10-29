"""
统一 Shell 执行器（M6 · Terminal Tool）。

★ 它是什么 / 不是什么
------------------
「在**用户机器上**跑一条命令」的唯一实现：探测可用 shell → 组 argv → 起子进程 →
收 stdout/stderr/exit code → 超时或取消时**杀掉整棵进程树**。

不是沙箱。`run_command`（Docker 容器）仍然是"隔离执行"的那条路；
这里的 terminal 是"在宿主机动手"，所以它的安全边界靠**审批 + 工作目录 + 超时 + 输出上限**，
而不是容器。这一点必须在文档与界面里说清楚，不能含糊成"也有沙箱"。

★ 为什么命令以字符串传入、而不是 argv 列表
------------------------------------
用户在终端里想跑的就是一条完整命令（带管道、重定向、变量展开）。
硬拆 argv 会让 `git log --oneline | head -5` 这种正常需求无法表达。
代价是"命令字符串里可能藏东西"，所以：
  · 执行前必须过审批（风险等级默认 EXEC）；
  · 只读白名单命令要求**不含任何 shell 元字符**才允许免审批；
  · 工作目录单独校验（不能靠命令里的 `cd ..` 绕出去）。

★ Shell 探测
----------
· powershell：优先 `pwsh.exe`（PowerShell 7），再退回系统自带的 `powershell.exe`；
· cmd：`%ComSpec%` 或 `cmd.exe`；
· bash：只认 Git Bash（`bash.exe` 且**不在 System32 下**）—— System32 里那个 bash.exe
  是 WSL 启动器，跑起来是 Linux，不是用户以为的 Git Bash，必须排除。
找不到就抛 `ShellUnavailable`，错误信息里写清"装什么/怎么装"，不静默降级。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

SHELL_CHOICES = ("auto", "powershell", "cmd", "bash")

# 名字像凭据的环境变量：不传给子进程，也不允许出现在回给模型的输出里
SENSITIVE_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

# 判断"只读命令"用的元字符：出现任何一个就不许免审批
_SHELL_META = (";", "&&", "||", "|", ">", "<", "`", "$(", "${", "\n", "\r", "&")

DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 300.0
MAX_OUTPUT_CHARS = 6000


class ShellUnavailable(RuntimeError):
    """请求的 shell 在当前机器上不可用。"""


@dataclass
class ShellInfo:
    kind: str           # powershell | cmd | bash
    path: str
    label: str          # 给界面看：PowerShell 7 / Windows PowerShell / CMD / Git Bash

    def argv(self, command: str) -> list[str]:
        if self.kind == "powershell":
            return [self.path, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", command]
        if self.kind == "cmd":
            # /d 跳过 AutoRun，/s 保持引号原样，/c 执行后退出
            return [self.path, "/d", "/s", "/c", command]
        return [self.path, "--noprofile", "--norc", "-c", command]


@dataclass
class ShellResult:
    shell: ShellInfo
    command: str
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    spawn_error: str = ""
    redacted: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.spawn_error and not self.timed_out and not self.cancelled and self.exit_code == 0


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def _which(name: str) -> str:
    p = shutil.which(name)
    return p or ""


def _is_system32(path: str) -> bool:
    low = os.path.normcase(path)
    return os.path.normcase(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")) in low


def detect_shell(choice: str = "auto") -> ShellInfo:
    """按 `choice` 找 shell；找不到抛 ShellUnavailable（带可操作提示）。"""
    choice = (choice or "auto").strip().lower()
    if choice not in SHELL_CHOICES:
        raise ShellUnavailable(f"不支持的 shell：{choice}（可选：{' / '.join(SHELL_CHOICES)}）")

    if choice in ("auto", "powershell"):
        for exe, label in (("pwsh.exe", "PowerShell 7"), ("powershell.exe", "Windows PowerShell")):
            path = _which(exe)
            if not path and exe == "powershell.exe":
                cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
                path = cand if os.path.isfile(cand) else ""
            if path:
                return ShellInfo("powershell", path, label)
        if choice == "powershell":
            raise ShellUnavailable(
                "找不到 PowerShell（pwsh.exe / powershell.exe）。"
                "Windows 一般自带 Windows PowerShell；精简系统需要先安装。"
            )

    if choice in ("auto", "cmd"):
        path = os.environ.get("ComSpec") or _which("cmd.exe")
        if path and os.path.isfile(path):
            return ShellInfo("cmd", path, "CMD")
        if choice == "cmd":
            raise ShellUnavailable("找不到 cmd.exe（%ComSpec% 为空且 PATH 里没有）。")

    if choice in ("auto", "bash"):
        for cand in (_which("bash.exe"), _which("bash")):
            if cand and not _is_system32(cand):
                return ShellInfo("bash", cand, "Git Bash")
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
        ):
            if cand and os.path.isfile(cand):
                return ShellInfo("bash", cand, "Git Bash")
        if choice == "bash":
            raise ShellUnavailable(
                "找不到 Git Bash（只找到 System32 下的 WSL 启动器，已排除）。"
                "请安装 Git for Windows（安装时勾选 Git Bash）。"
            )

    raise ShellUnavailable("没有探测到任何可用 shell（PowerShell / CMD / Git Bash 都不可用）。")


# --------------------------------------------------------------------------- #
# 环境与输出处理
# --------------------------------------------------------------------------- #
def sensitive_env_values(env: dict[str, str] | None = None) -> list[str]:
    """收集"像凭据"的环境变量值（只用于输出打码，不会返回给模型）。"""
    env = env if env is not None else os.environ
    out: list[str] = []
    for k, v in env.items():
        if any(h in k.upper() for h in SENSITIVE_ENV_HINTS) and isinstance(v, str) and len(v) >= 6:
            out.append(v)
    # 长的先替换，避免短串先命中把长串切碎
    return sorted(set(out), key=len, reverse=True)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """
    子进程环境：继承宿主环境，但**剔除**名字像凭据的变量。

    ★ 为什么这么做：terminal 的输出会进模型上下文。若把进程环境原样带下去，
      一条 `set` / `env` 就能把 API Key 打出来。剔除是阻断，不是过滤。
    """
    env = {k: v for k, v in os.environ.items()
           if not any(h in k.upper() for h in SENSITIVE_ENV_HINTS)}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # ★ PATHEXT 必须存在且非空：PS 靠它给裸命令名补 .EXE。
    #   本机实测过父进程 PATHEXT 为空的情况：cmd 仍能找到 python，
    #   而 PS 报"无法将 python 项识别为..." —— 同一台机两个 shell 行为不一致。
    if not env.get("PATHEXT"):
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC"
    if extra:
        env.update({k: v for k, v in extra.items()
                    if not any(h in k.upper() for h in SENSITIVE_ENV_HINTS)})
    return env


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _redact(text: str, secrets: list[str]) -> tuple[str, int]:
    hits = 0
    for s in secrets:
        if s and s in text:
            hits += text.count(s)
            text = text.replace(s, "***")
    return text, hits


def clip_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """超长输出保留头 60% + 尾 40%，中间明确标注被裁掉多少。"""
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.6)
    tail = limit - head
    return (
        text[:head]
        + f"\n…（输出被截断：原 {len(text)} 字符，保留首尾共 {limit} 字符）…\n"
        + text[-tail:],
        True,
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _spawn_flags() -> int:
    if sys.platform == "win32":
        # 新进程组：超时/取消时 taskkill /T 才能带走子进程
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return 0


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """尽力杀整棵进程树（Windows 用 taskkill /T；POSIX 用 killpg）。"""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:  # noqa: BLE001 —— 杀不掉也不能把父任务卡住
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def run_command(
    command: str,
    *,
    cwd: str,
    shell: str = "auto",
    timeout: float = DEFAULT_TIMEOUT_S,
    cancel_event: Any = None,
    extra_env: dict[str, str] | None = None,
    max_output: int = MAX_OUTPUT_CHARS,
) -> ShellResult:
    """
    执行一条命令并返回结构化结果（**不抛业务异常**：失败也是结果）。

    参数超限在这里再夹一次 —— 调用方（工具层）已经校验过，双保险成本很低。
    """
    info = detect_shell(shell)          # 探测失败由调用方转成可诊断错误
    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    argv = info.argv(command)
    env = child_env(extra_env)
    started = time.time()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_spawn_flags(),
    )

    out = b""
    err = b""
    timed_out = False
    cancelled = False

    async def watch_cancel() -> None:
        while True:
            if proc.returncode is not None:
                return
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                await _kill_tree(proc)
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_cancel()) if cancel_event is not None else None
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out, err = out_b or b"", err_b or b""
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_tree(proc)
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5)
            out, err = out_b or b"", err_b or b""
        except Exception:  # noqa: BLE001
            pass
    finally:
        if watcher is not None:
            watcher.cancel()
            if not watcher.done():
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    if watcher is not None and getattr(cancel_event, "is_set", lambda: False)():
        cancelled = True

    text_out = _decode(out)
    text_err = _decode(err)
    secrets = sensitive_env_values()
    text_out, hits1 = _redact(text_out, secrets)
    text_err, hits2 = _redact(text_err, secrets)
    text_out, cut1 = clip_output(text_out, max_output)
    text_err, cut2 = clip_output(text_err, max_output)

    return ShellResult(
        shell=info,
        command=command,
        cwd=cwd,
        exit_code=int(proc.returncode if proc.returncode is not None else -1),
        stdout=text_out,
        stderr=text_err,
        duration_ms=int((time.time() - started) * 1000),
        timed_out=timed_out,
        cancelled=cancelled,
        truncated=cut1 or cut2,
        redacted=hits1 + hits2,
    )


# --------------------------------------------------------------------------- #
# 只读命令判定（用于"免审批"的白名单，不是安全边界）
# --------------------------------------------------------------------------- #
_READONLY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "status"), ("git", "diff"), ("git", "log"), ("git", "branch"),
    ("git", "show"), ("git", "rev-parse"), ("git", "remote"),
    ("dir",), ("ls",), ("pwd",), ("cd",), ("where",), ("which",), ("type",),
    ("python", "--version"), ("python3", "--version"), ("py", "--version"),
    ("node", "--version"), ("npm", "--version"), ("pip", "--version"),
    ("pip", "list"), ("pytest", "--collect-only"), ("echo",),
)


def is_readonly_command(command: str) -> bool:
    """
    判断这条命令是否可以"免审批直接跑"。

    ★ 这不是安全边界，只是**减少打扰**的启发式：一旦含 shell 元字符
      （管道、重定向、命令链、变量展开、换行）就一律返回 False ——
      因为 "git status" 与 "git status; rm -rf /" 在前缀上无法区分。
    ★ 真正的边界是审批 + 工作目录 + 超时；模型不能靠这句话给自己发权限。
    """
    text = (command or "").strip()
    if not text or len(text) > 300:
        return False
    if any(m in text for m in _SHELL_META):
        return False
    parts = text.split()
    if not parts:
        return False
    low = [p.lower() for p in parts]
    for prefix in _READONLY_PREFIXES:
        n = len(prefix)
        if tuple(low[:n]) == prefix:
            return True
    return False
