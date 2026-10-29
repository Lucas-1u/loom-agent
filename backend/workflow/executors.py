"""
执行适配层（V1）：把不同类型的"一步"统一成 Job 执行接口。

★ 统一接口不等于统一能力
--------------------
每个执行器必须**声明**自己的能力：
    resumable      进程重启后能否安全重跑（幂等）
    cancellable    能否中途取消
    side_effect    是否有副作用（决定失败后能不能自动重试）

  · http(GET)   resumable=True   cancellable=True   side_effect=False
  · tool(只读)  resumable=True   cancellable=True   side_effect=False
  · shell       resumable=False  cancellable=True   side_effect=True

★ 网络限制（防 SSRF）
------------------
只允许 http/https；解析目标主机，命中私有网段/回环/链路本地一律拒绝
（测试用 `allow_local=True` 打开本机）。响应有字节上限，默认不跟随重定向
（跟随会把"审批看到的 URL"和"实际访问的 URL"变成两件事）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

try:  # ★ 顶层导入 + 预热一个共享客户端：
      #   httpx 首次 import 与首次构造 client 合计要 0.3s+，如果发生在协程里，
      #   会把事件循环卡住，表现为"第二个并行请求晚半秒才开始"（实测过）。
    import httpx as _httpx

    _SHARED_CLIENT = _httpx.AsyncClient(follow_redirects=False, timeout=30.0)
except Exception:  # noqa: BLE001 —— 没装也能加载模块，运行时报可诊断错误
    _httpx = None
    _SHARED_CLIENT = None

MAX_BYTES = 256 * 1024
DEFAULT_HTTP_TIMEOUT = 30.0


@dataclass
class ExecResult:
    ok: bool = False
    value: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    timed_out: bool = False
    cancelled: bool = False
    unknown: bool = False
    resumable: bool = False


def _is_private_host(host: str) -> bool:
    """解析主机名并判断是否落在内网/回环/链路本地。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved):
            return True
    return False


class HttpExecutor:
    """GET 一个 URL。只读、幂等 → 可安全重跑。"""

    kind = "http"
    resumable = True
    cancellable = True
    side_effect = False

    def __init__(self, *, allow_local: bool = False, max_bytes: int = MAX_BYTES) -> None:
        self.allow_local = allow_local
        self.max_bytes = max_bytes

    async def run(self, spec: dict[str, Any], *, timeout_s: float,
                  cancel_event: Any = None) -> ExecResult:
        url = str(spec.get("url") or "").strip()
        method = str(spec.get("method") or "GET").upper()
        if method != "GET":
            return ExecResult(error=f"只支持 GET（收到 {method}）", retryable=False)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ExecResult(error=f"不支持的协议：{parsed.scheme or '(空)'}", retryable=False)
        if not parsed.hostname:
            return ExecResult(error="URL 缺少主机名", retryable=False)
        if not self.allow_local and _is_private_host(parsed.hostname):
            return ExecResult(
                error=f"目标主机 {parsed.hostname} 属于内网/回环地址，已拒绝（防 SSRF）",
                retryable=False,
            )

        httpx = _httpx
        if httpx is None:
            return ExecResult(error="httpx 不可用（依赖缺失）", retryable=False)

        headers = {"User-Agent": "Loom-Workflow/1.0"}
        headers.update({str(k): str(v) for k, v in (spec.get("headers") or {}).items()})
        try:
            client = _SHARED_CLIENT
            if client is None:
                return ExecResult(error="httpx 客户端不可用", retryable=False)
            resp = await client.get(url, headers=headers, timeout=timeout_s)
            raw = resp.content or b""
            truncated = len(raw) > self.max_bytes
            if truncated:
                raw = raw[: self.max_bytes]
            text = raw.decode("utf-8", errors="replace")
            status = resp.status_code
            ok = status < 400
            return ExecResult(ok=ok, value={
                "url": url,
                "status": status,
                "content_type": resp.headers.get("content-type", ""),
                "bytes": len(raw),
                "truncated": truncated,
                "location": resp.headers.get("location", ""),
                "text_head": text[:2000],
            }, error=("" if ok else f"HTTP {status}"),
               retryable=(status >= 500))
        except (asyncio.CancelledError,):
            raise
        except Exception as exc:  # noqa: BLE001 —— 超时与网络错误分开处理
            name = type(exc).__name__
            if "Timeout" in name:
                return ExecResult(timed_out=True, error=f"请求超时（{timeout_s:.1f}s）")
            retryable = any(k in name for k in ("Connect", "Read", "Network", "RemoteProtocol"))
            return ExecResult(error=f"{name}: {exc}", retryable=retryable)


class ToolExecutor:
    """
    跑一个**只读**工具（read_file / list_files / search_docs）。

    ★ 只能只读：写工具会产生副作用、要审批，不适合放进后台并行——那会把
      "批准一次"变成"后台偷偷改多个文件"。要写就回主链路同步做。
    """

    kind = "tool"
    resumable = True
    cancellable = True
    side_effect = False
    ALLOWED = {"read_file", "list_files", "search_docs"}

    def __init__(self, runner: Callable[[str, dict[str, Any], str], Any] | None,
                 workspace: str = "") -> None:
        self.runner = runner
        self.workspace = workspace

    async def run(self, spec: dict[str, Any], *, timeout_s: float,
                  cancel_event: Any = None) -> ExecResult:
        name = str(spec.get("tool") or "").strip()
        args = spec.get("args") or {}
        if name not in self.ALLOWED:
            return ExecResult(
                error=f"后台只允许只读工具 {sorted(self.ALLOWED)}，拒绝：{name}",
                retryable=False,
            )
        if self.runner is None:
            return ExecResult(error="没有注入工具执行器", retryable=False)
        try:
            res = await self.runner(name, dict(args), self.workspace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return ExecResult(error=f"{type(exc).__name__}: {exc}", retryable=True)
        value = {"tool": name, "ok": bool(res.get("ok")), "summary": res.get("summary", ""),
                 "data": res.get("data")}
        return ExecResult(ok=bool(res.get("ok")), value=value,
                          error=("" if res.get("ok") else str(res.get("error") or "工具返回失败")),
                          retryable=False)


class ShellExecutor:
    """
    跑一条**宿主机**命令（与 terminal 工具同一套实现）。

    ★ resumable=False：进程重启后无法确认命令是否已经产生副作用，
      只能标 unknown 等人处理——绝不自动重跑。
    """

    kind = "shell"
    resumable = False
    cancellable = True
    side_effect = True

    def __init__(self, *, workspace: str = "") -> None:
        self.workspace = workspace

    async def run(self, spec: dict[str, Any], *, timeout_s: float,
                  cancel_event: Any = None) -> ExecResult:
        from ..tools.exec.shells import ShellUnavailable, run_command

        command = str(spec.get("command") or "").strip()
        if not command:
            return ExecResult(error="shell 步骤缺少 command", retryable=False)
        cwd = str(spec.get("cwd") or "") or self.workspace
        if not cwd:
            return ExecResult(error="shell 步骤没有工作目录", retryable=False)
        try:
            result = await run_command(command, cwd=cwd, shell=str(spec.get("shell") or "auto"),
                                       timeout=timeout_s, cancel_event=cancel_event)
        except ShellUnavailable as exc:
            return ExecResult(error=str(exc), retryable=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return ExecResult(error=f"{type(exc).__name__}: {exc}", retryable=False)

        value = {
            "command": command, "exit_code": result.exit_code,
            "stdout": result.stdout, "stderr": result.stderr,
            "timed_out": result.timed_out, "cancelled": result.cancelled,
            "duration_ms": result.duration_ms,
        }
        if result.cancelled:
            return ExecResult(value=value, cancelled=True, error="命令被取消", resumable=False)
        if result.timed_out:
            return ExecResult(value=value, timed_out=True, error="命令超时", resumable=False)
        if result.exit_code != 0:
            return ExecResult(value=value, error=f"退出码 {result.exit_code}", retryable=False)
        return ExecResult(ok=True, value=value)


def get_executor(kind: str, *, allow_local: bool = False,
                 tool_runner: Callable[[str, dict[str, Any], str], Any] | None = None,
                 workspace: str = "") -> Any:
    kind = (kind or "").strip().lower()
    if kind == "http":
        return HttpExecutor(allow_local=allow_local)
    if kind == "tool":
        return ToolExecutor(tool_runner, workspace=workspace)
    if kind == "shell":
        return ShellExecutor(workspace=workspace)
    raise ValueError(f"未知的步骤类型：{kind}（可选：http / tool / shell）")
