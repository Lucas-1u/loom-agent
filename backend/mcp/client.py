"""
MCP Client 与 Server 生命周期（M4.1，第一版只支持本地 stdio）。

★ 为什么是"每个 Server 一个常驻任务"
--------------------------------
`mcp` SDK 的 `stdio_client` / `ClientSession` 都是**异步上下文管理器**，
而 anyio 的 cancel scope 要求 `__aenter__` 与 `__aexit__` 在**同一个 task** 里。
如果"启动时进入、关闭时退出"分属两个 task，退出时会炸（Windows 上尤其明显）。

所以每个 Server 由一个后台任务全程持有连接：

    _Worker._run:  async with stdio_client(...) -> async with ClientSession(...)
                   initialize → list_tools → ready.set() → 等 stop
    调用方:        直接用 session.call_tool(...)（不是上下文管理器，可以跨 task 调用）

这样：
  · 只启动一次进程，不是每轮推理启动一次；
  · 关闭时由**同一个 task** 退出上下文，干净释放子进程；
  · 调用与连接解耦，Agent 侧只看到普通的异步函数。

★ 失败与重连策略（保守）
--------------------
· 启动失败 / 连接断开 / 调用超时 → 该 Server 标记为 unhealthy，**不自动重启**
  （避免"无限重启"和"盲目重试有副作用的调用"）；后续调用直接快速失败并说明原因。
· 需要恢复时由上层显式 `restart(server_id)`（会重新发现工具，schema 不会用旧的）。
· 超时后标记 unhealthy 而不是继续复用 session：卡住的 session 会把后续调用也拖死。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .config import McpConfig, McpServerConfig, build_child_env

log = logging.getLogger("loom.mcp")


def describe_exc(exc: BaseException, *, depth: int = 0) -> str:
    """
    把（可能嵌套的）异常压成一行可诊断文本。

    ★ 为什么要递归：anyio 的 TaskGroup 会把真实错误包在 ExceptionGroup 里，
      直接 str() 只能看到 "unhandled errors in a TaskGroup" —— 对排障毫无用处。
    """
    text = f"{type(exc).__name__}: {exc}"
    subs = getattr(exc, "exceptions", None) or []
    if subs and depth < 2:
        inner = " | ".join(describe_exc(e, depth=depth + 1) for e in subs[:3])
        text = f"{text} → {inner}"
    return text[:400]


@dataclass
class DiscoveredTool:
    """Server 暴露的一个工具（原始元数据，不做任何"翻译"）。"""

    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    title: str = ""

    @property
    def source(self) -> dict[str, str]:
        return {"kind": "mcp", "server": self.server_id, "tool": self.name}


@dataclass
class CallOutcome:
    """一次 MCP 调用的归一化结果（还没变成 Loom 的 ToolResult）。"""

    ok: bool
    text: str = ""
    structured: Any = None
    is_error: bool = False          # Server 明确说"业务错误"
    kind: str = ""                  # timeout / disconnected / protocol / unavailable
    error: str = ""
    raw_types: list[str] = field(default_factory=list)
    truncated: bool = False


class _Worker:
    """一个 Server 的连接持有者。"""

    def __init__(self, cfg: McpServerConfig, *, default_timeout: float,
                 startup_timeout: float = 15.0) -> None:
        self.cfg = cfg
        self.server_id = cfg.server_id
        self.timeout_s = cfg.timeout_s or default_timeout
        self.startup_timeout = startup_timeout
        self.tools: list[DiscoveredTool] = []
        self.session: Any = None
        self.error: str = ""
        self.state: str = "idle"       # idle | connecting | ready | failed | closed
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._errlog = None

    # ---- 启动 --------------------------------------------------------------
    async def start(self, startup_timeout: float) -> None:
        if self._task is not None:
            return
        self.state = "connecting"
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.server_id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=startup_timeout)
        except asyncio.TimeoutError:
            self.error = f"启动超时（>{startup_timeout:.0f}s）"
            self.state = "failed"
            await self.stop()

    async def _run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.cfg.command,
            args=list(self.cfg.args),
            env=build_child_env(self.cfg),
            cwd=self.cfg.cwd or None,
        )
        # Server 的 stderr 不直接倒进 Loom 的日志：写进 devnull，
        # 需要排障时把 LOOM_MCP_DEBUG=1 打开即可（见下）。
        self._errlog = None
        try:
            if os.environ.get("LOOM_MCP_DEBUG") == "1":
                errlog = None            # 交给 SDK 默认 = sys.stderr
            else:
                self._errlog = open(os.devnull, "w", encoding="utf-8")
                errlog = self._errlog

            async with stdio_client(params, errlog=errlog) as (read, write):
                # ★ 会话级 read timeout 必须是 None：SDK 会把它同时用在 initialize /
                #   list_tools 上，如果设成"单次调用超时"（比如 1s），启动握手就会超时。
                #   单次调用的超时由 call_tool(read_timeout_seconds=) + wait_for 控制。
                async with ClientSession(read, write, read_timeout_seconds=None) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.startup_timeout)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=self.startup_timeout)
                    self.tools = [
                        DiscoveredTool(
                            server_id=self.server_id,
                            name=t.name,
                            title=getattr(t, "title", "") or "",
                            description=t.description or "",
                            input_schema=dict(t.input_schema or {}),
                            output_schema=getattr(t, "output_schema", None),
                        )
                        for t in listed.tools
                    ]
                    self.session = session
                    self.state = "ready"
                    self._ready.set()
                    await self._stop.wait()
                    self.session = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 任何失败都要落成"可诊断状态"
            self.error = describe_exc(exc)
            self.state = "failed"
            log.warning("MCP Server %s 连接失败：%s", self.server_id, self.error)
        finally:
            self._ready.set()          # 启动失败时也要唤醒等待者
            self.state = "closed" if self.state != "failed" else "failed"
            if self.session is not None:
                self.session = None
            if self._errlog is not None:
                try:
                    self._errlog.close()
                except OSError:
                    pass

    # ---- 状态 --------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.state == "ready" and self.session is not None

    def status(self) -> dict[str, Any]:
        return {
            "id": self.server_id,
            "state": self.state,
            "tools": [t.name for t in self.tools],
            "error": self.error,
            "trust": self.cfg.trust,
            "command": self.cfg.command,
            "args_count": len(self.cfg.args),
        }

    # ---- 调用 --------------------------------------------------------------
    async def call(self, tool: str, args: dict[str, Any], *, timeout: float | None = None) -> CallOutcome:
        if not self.alive:
            return CallOutcome(
                ok=False, kind="unavailable",
                error=f"MCP Server 「{self.server_id}」当前不可用（{self.state}"
                      f"{'：' + self.error if self.error else ''}）",
            )
        wait = timeout or self.timeout_s
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool, args or {}, read_timeout_seconds=wait),
                timeout=wait + 1.0,
            )
        except asyncio.TimeoutError:
            # ★ 超时后不再复用这个 session：卡住的连接会把后续调用一起拖死。
            self.error = f"工具调用超时（>{wait:.0f}s）"
            self.state = "failed"
            return CallOutcome(ok=False, kind="timeout", error=self.error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.error = describe_exc(exc)
            low = str(exc).lower()
            # ★ SDK 在 read_timeout 到点时会抛 McpError("Request 'x' timed out")，
            #   它不是 asyncio.TimeoutError —— 不识别的话会被误分类成"协议错误"。
            if (isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    or "timed out" in low or "timeout" in low):
                self.state = "failed"       # 卡住的 session 不再复用
                return CallOutcome(ok=False, kind="timeout",
                                   error=f"工具调用超时（>{wait:.0f}s）：{self.error}")
            if "closed" in low or "broken" in low or "connection" in low:
                self.state = "failed"
                return CallOutcome(ok=False, kind="disconnected", error=self.error)
            return CallOutcome(ok=False, kind="protocol", error=self.error)

        return _normalize(result)

    # ---- 关闭 --------------------------------------------------------------
    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            self.state = "closed"
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        self.state = "closed"


def _normalize(result: Any, *, max_chars: int = 12_000) -> CallOutcome:
    """
    把 MCP 的 CallToolResult 归一化。

    ★ 三种内容要分开：文本（给模型看）、结构化（给程序用）、其它类型（图片/音频等）。
      第一版只把文本拼进 summary，其它类型只报类型与大小 —— 不假装理解它们。
    """
    texts: list[str] = []
    kinds: list[str] = []
    for part in getattr(result, "content", None) or []:
        kind = getattr(part, "type", "") or type(part).__name__
        kinds.append(str(kind))
        if kind == "text":
            texts.append(getattr(part, "text", "") or "")
        else:
            # 非文本内容：保留类型与粗略体积，避免把二进制塞进上下文
            size = len(str(getattr(part, "data", "") or ""))
            texts.append(f"[{kind} 内容，约 {size} 字节，未内联]")

    body = "\n".join(t for t in texts if t).strip()
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…（已截断，原始 {len(body)} 字符）"
        truncated = True

    is_error = bool(getattr(result, "is_error", False))
    structured = getattr(result, "structured_content", None)
    return CallOutcome(
        ok=not is_error,
        text=body,
        structured=structured,
        is_error=is_error,
        raw_types=kinds,
        truncated=truncated,
    )


class McpManager:
    """所有 Server 的门面：启动 / 发现 / 调用 / 关闭 / 状态。"""

    def __init__(self, config: McpConfig) -> None:
        self.config = config
        self._workers: dict[str, _Worker] = {}
        self._started = False

    # ---- 生命周期 ----------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for cfg in self.config.servers:
            if not cfg.enabled:
                continue
            worker = _Worker(cfg, default_timeout=self.config.call_timeout_s,
                             startup_timeout=self.config.startup_timeout_s)
            self._workers[cfg.server_id] = worker
        # 并发连接：一个 Server 卡住不该拖住其它 Server 的发现
        await asyncio.gather(
            *(w.start(self.config.startup_timeout_s) for w in self._workers.values()),
            return_exceptions=True,
        )
        for w in self._workers.values():
            log.info("MCP Server %s：state=%s tools=%d%s",
                     w.server_id, w.state, len(w.tools),
                     f"（{w.error}）" if w.error else "")

    async def close(self) -> None:
        """幂等：重复调用不会报错，也不会漏掉子进程。"""
        if not self._workers:
            return
        await asyncio.gather(*(w.stop() for w in self._workers.values()),
                             return_exceptions=True)
        # ★ 不 clear：保留 worker 对象，健康检查/测试才能看到 "closed" 状态。
        #   tools() 只返回 state=ready 的，所以关闭后自然为空。
        self._started = False

    async def restart(self, server_id: str) -> list[DiscoveredTool]:
        """显式重启一个 Server（重新发现工具，schema 不会用旧的）。"""
        cfg = next((c for c in self.config.servers if c.server_id == server_id), None)
        if cfg is None:
            return []
        old = self._workers.get(server_id)
        if old is not None:
            await old.stop()
        worker = _Worker(cfg, default_timeout=self.config.call_timeout_s,
                         startup_timeout=self.config.startup_timeout_s)
        self._workers[server_id] = worker
        await worker.start(self.config.startup_timeout_s)
        return worker.tools

    # ---- 查询 --------------------------------------------------------------
    def tools(self) -> list[DiscoveredTool]:
        out: list[DiscoveredTool] = []
        for w in self._workers.values():
            if w.state == "ready":
                out.extend(w.tools)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "servers": [w.status() for w in self._workers.values()],
            "tools": len(self.tools()),
        }

    def risk_for(self, server_id: str, tool: str):
        """按配置决定某个工具的风险等级（默认 WRITE = 需要审批）。"""
        cfg = next((c for c in self.config.servers if c.server_id == server_id), None)
        return cfg.risk_for(tool) if cfg else None

    # ---- 调用 --------------------------------------------------------------
    async def call(self, server_id: str, tool: str, args: dict[str, Any],
                   *, timeout: float | None = None) -> CallOutcome:
        worker = self._workers.get(server_id)
        if worker is None:
            return CallOutcome(ok=False, kind="unavailable",
                               error=f"未知的 MCP Server：{server_id}")
        # 只允许调用"当前清单里仍有"的工具：Server 重连后 schema/工具可能变了
        if not any(t.name == tool for t in worker.tools):
            return CallOutcome(ok=False, kind="unavailable",
                               error=f"Server 「{server_id}」当前没有工具「{tool}」（可能已下线或改名）")
        outcome = await worker.call(tool, args, timeout=timeout)
        if outcome.truncated and not outcome.error:
            outcome.error = ""
        return outcome
