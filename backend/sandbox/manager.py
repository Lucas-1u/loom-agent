"""
Docker 沙箱（命令执行环境）

★ 一句话：这个模块是**唯一的**命令执行出口。
  它只会拼 `docker ...` 命令；项目里没有任何"直接在宿主机跑命令"的代码路径。
  Docker 不可用 → 明确报错，**不降级**。

★ 两层安全（互不替代）
────────────────────────────────────────────────────────────────
  第一层：用户审批（Policy + ApprovalManager）
      —— 决定"这条命令该不该跑"。用户看得到完整 argv 和工作目录。
  第二层：容器隔离（本模块）
      —— 决定"跑起来最多能伤到哪儿"。**审批通过也不会放宽这里的任何限制**：
         网络、挂载、capabilities、资源上限全部由代码写死，不接受模型参数。

★ 为什么不引 docker-py
    docker SDK 是个新依赖，而 `docker` CLI 已经随 Docker Desktop 安装。
    用 `create_subprocess_exec` 传参数列表（不经过 shell），
    命令构造完全可测、可审计 —— 每个 flag 都在 `build_run_argv` 里看得见。

★ 这个沙箱**不是**绝对安全的
   它与 Docker 守护进程同权限级别：容器内的 root（本实现已禁用）或内核漏洞
   仍可能影响宿主机。它降低的是"Agent 写错命令把系统搞坏"的概率，
   不是"对抗恶意租户"的强隔离。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from ..config import SandboxConfig

log = logging.getLogger("loom.sandbox")

CONTAINER_PREFIX = "loom-sbx-"
READ_CHUNK = 64 * 1024


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class SandboxUnavailable(RuntimeError):
    """Docker / 镜像不可用。★ 绝不能 catch 之后改在宿主机执行。"""


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DockerStatus:
    available: bool = False
    version: str = ""
    detail: str = ""
    image: str = ""
    image_present: bool = False

    @property
    def ready(self) -> bool:
        return self.available and self.image_present

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "version": self.version,
            "image": self.image,
            "image_present": self.image_present,
            "ready": self.ready,
            "detail": self.detail,
        }


@dataclass
class RunOutcome:
    """一次容器执行的结果。exit_code 为 None 表示进程没能正常给出退出码。"""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    duration_s: float = 0.0
    container: str = ""
    argv: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "output_truncated": self.output_truncated,
            "duration_s": round(self.duration_s, 2),
            "container": self.container,
        }


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class SandboxManager:
    """
    负责：探测 Docker、拼容器参数、跑命令、超时/取消时杀容器。

    并发：一个任务同一时刻只跑一条命令（Agent 是串行的），
    但不同任务可能并发，所以 `_active` 按 task_id 分组，杀的时候不会误伤。
    """

    def __init__(self, config: SandboxConfig, *, spawn: Callable[..., Awaitable[Any]] | None = None) -> None:
        self.config = config
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._status: DockerStatus | None = None
        self._active: dict[str, set[str]] = {}
        # 记录杀容器调用，便于测试断言（生产环境只是几十字节）
        self.kill_calls: list[tuple[str, ...]] = []

    # ===================================================================== #
    # 探测
    # ===================================================================== #
    async def _docker_quick(self, args: Sequence[str], timeout: float = 10.0) -> tuple[int, str, str]:
        """跑一条短命令（version / inspect / kill / rm），不经过 shell。"""
        argv = [self.config.docker_bin, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return 127, "", f"无法启动 docker CLI：{exc}"

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return 124, "", f"docker {' '.join(args)} 超时（{timeout:.0f}s）"

        return (
            proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def probe(self, *, force: bool = False) -> DockerStatus:
        """检查 docker CLI + 守护进程 + 镜像是否存在。结果缓存，force=True 重新探测。"""
        if self._status is not None and not force:
            return self._status

        # ★ 显式禁用时不必去探 Docker —— 也保证 run() 一定拿不到 available。
        if not self.config.enabled:
            self._status = DockerStatus(
                available=False,
                image=self.config.image,
                detail="沙箱已禁用（LOOM_SANDBOX_ENABLED=0）",
            )
            return self._status

        code, out, err = await self._docker_quick(["version", "--format", "{{.Server.Version}}"], 8)
        if code != 0 or not out.strip():
            detail = (err or out).strip()[:400] or "docker version 失败（守护进程未启动？）"
            self._status = DockerStatus(available=False, image=self.config.image, detail=detail)
            return self._status

        version = out.strip().splitlines()[0]
        code2, _, _err2 = await self._docker_quick(["image", "inspect", self.config.image], 8)
        present = code2 == 0
        detail = "" if present else (
            f"镜像 {self.config.image} 不存在。请先构建：\n"
            f"    docker build -f dev/Dockerfile.sandbox -t {self.config.image} ."
        )
        self._status = DockerStatus(
            available=True,
            version=version,
            detail=detail,
            image=self.config.image,
            image_present=present,
        )
        return self._status

    def cached_status(self) -> DockerStatus | None:
        return self._status

    # ===================================================================== #
    # 容器参数（★ 安全边界全在这里，且不接受模型输入）
    # ===================================================================== #
    def build_run_argv(
        self,
        *,
        container_name: str,
        command: str,
        args: Sequence[str],
        cwd_rel: str,
        workspace_root: str,
    ) -> list[str]:
        """
        构造 `docker run` 的参数列表。

        ★ 这里没有任何一个参数来自模型。模型只能决定「命令 + 参数 + 相对工作目录 + 超时」，
          网络、挂载、用户、capabilities、资源上限全部写死。
        ★ cwd_rel 必须是工作区内的相对路径；这里再做一次防御性校验。
        """
        cfg = self.config
        raw = str(cwd_rel or ".").replace("\\", "/").strip()
        # ★ 先判绝对路径，再 strip('/')。顺序反了会把 "/etc" 变成 "etc" 混过去。
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise ValueError(f"cwd 非法（不允许绝对路径）：{cwd_rel!r}")
        rel = raw.strip("/")
        if rel in ("", "."):
            workdir = cfg.workspace_mount
        else:
            parts = [p for p in rel.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise ValueError(f"cwd 非法（不允许 ..）：{cwd_rel!r}")
            workdir = f"{cfg.workspace_mount}/{'/'.join(parts)}"

        workspace_abs = str(Path(workspace_root).resolve())

        return [
            cfg.docker_bin,
            "run",
            "--rm",                                  # 退出即删
            "--init",                                # 正确的 PID 1（回收僵尸进程）
            "--name", container_name,
            "--pull", "never",                       # ★ 绝不隐式联网拉镜像
            "--network", cfg.network,                # ★ none
            "--read-only",                           # ★ 根文件系统只读
            "--tmpfs", f"/tmp:rw,noexec,nosuid,size={cfg.tmpfs_size}",
            "--user", cfg.user,                      # ★ 非 root
            "--cap-drop", "ALL",                     # ★ 去掉全部 capabilities
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(cfg.pids_limit),
            "--memory", cfg.memory,
            "--memory-swap", cfg.memory,             # 禁止用 swap 绕过内存上限
            "--cpus", cfg.cpus,
            "--stop-timeout", "1",
            # ---- 只挂载工作区，绝不挂 docker.sock / 家目录 / 系统目录 ----
            "-v", f"{workspace_abs}:{cfg.workspace_mount}:rw",
            "-w", workdir,
            # ---- 环境变量：只给白名单，绝不透传宿主机 env ----
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PYTHONUNBUFFERED=1",
            "-e", "PYTHONIOENCODING=utf-8",
            "-e", "TMPDIR=/tmp",
            cfg.image,
            command,
            *[str(a) for a in args],
        ]

    # ===================================================================== #
    # 执行
    # ===================================================================== #
    async def run(
        self,
        *,
        task_id: str,
        command: str,
        args: Sequence[str],
        workspace_root: str,
        cwd_rel: str,
        timeout_s: float,
        cancel_event: asyncio.Event | None = None,
    ) -> RunOutcome:
        """
        在隔离容器里跑一条命令。

        ★ 任何异常路径（超时 / 取消 / 进程异常）都会先杀容器再返回。
        ★ Docker 不可用直接抛 SandboxUnavailable —— 调用方必须把它变成失败结果，
          绝不允许"改在宿主机跑一下试试"。
        """
        status = await self.probe()
        if not status.available:
            raise SandboxUnavailable(status.detail or "Docker 不可用")
        if not status.image_present:
            raise SandboxUnavailable(status.detail or f"镜像不存在：{status.image}")

        name = f"{CONTAINER_PREFIX}{task_id[-8:] or 'task'}-{uuid.uuid4().hex[:6]}"
        argv = self.build_run_argv(
            container_name=name,
            command=command,
            args=args,
            cwd_rel=cwd_rel,
            workspace_root=workspace_root,
        )

        cap = max(1024, int(self.config.max_output_bytes))
        out_buf = bytearray()
        err_buf = bytearray()
        truncated = False
        t0 = time.time()

        timed_out = cancelled = False
        killed = False
        exit_code: int | None = None
        proc: Any = None

        self._active.setdefault(task_id, set()).add(name)
        try:
            proc = await self._spawn(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._active.get(task_id, set()).discard(name)
            raise SandboxUnavailable(f"无法启动 docker CLI：{exc}") from exc

        async def _read(stream: Any, sink: bytearray) -> None:
            nonlocal truncated
            while True:
                chunk = await stream.read(READ_CHUNK)
                if not chunk:
                    return
                room = cap - len(sink)
                if room > 0:
                    sink.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True   # 继续读、丢弃，避免子进程写满管道卡死

        readers = [
            asyncio.create_task(_read(proc.stdout, out_buf)),
            asyncio.create_task(_read(proc.stderr, err_buf)),
        ]
        wait_task = asyncio.create_task(proc.wait())
        cancel_task: asyncio.Task[Any] | None = None
        waiters: set[asyncio.Task[Any]] = {wait_task}
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.add(cancel_task)

        try:
            try:
                done, _pending = await asyncio.wait(
                    waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
                )
                if wait_task in done:
                    exit_code = proc.returncode
                else:
                    if cancel_task is not None and cancel_task in done:
                        cancelled = True
                    else:
                        timed_out = True
                    await self._kill_container(name, task_id=task_id)
                    killed = True
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    exit_code = proc.returncode
            finally:
                # ★ 先给 reader 一点时间读到 EOF，再取消。
                #   直接 cancel 会把"进程已退出但管道里还有数据"的那段丢掉 ——
                #   pytest 的报错恰好都在最后几行。
                if readers:
                    _done, pending = await asyncio.wait(readers, timeout=2.0)
                    for t in pending:
                        t.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(*readers, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(wait_task, return_exceptions=True), timeout=5
                    )
                if cancel_task is not None:
                    cancel_task.cancel()
                    # ★ 不能用 `suppress(Exception)` 包 `await cancel_task`：
                    #   await 一个被取消的 Task 抛的是 CancelledError，
                    #   而它继承自 BaseException —— suppress(Exception) 抓不住。
                    #   这条路径在「命令**正常**结束、但带了 cancel_event」时必定走到，
                    #   漏掉会把整个任务误判成"用户取消"（真实 E2E 才暴露的 bug）。
                    #   gather(return_exceptions=True) 会把 CancelledError 当结果收下。
                    await asyncio.gather(cancel_task, return_exceptions=True)
        except asyncio.CancelledError:
            # 外层任务被硬取消：也要把容器带走
            await self._kill_container(name, task_id=task_id)
            killed = True
            raise
        finally:
            if not killed:
                # 进程正常退出时 --rm 已经清理；这里只处理"没走到正常退出"的兜底
                with contextlib.suppress(Exception):
                    if proc.returncode is None:
                        await self._kill_container(name, task_id=task_id)
            self._active.get(task_id, set()).discard(name)
            if not self._active.get(task_id):
                self._active.pop(task_id, None)

        log.info(
            "沙箱执行结束 container=%s exit=%s timed_out=%s cancelled=%s 用时=%.1fs",
            name, exit_code, timed_out, cancelled, time.time() - t0,
        )
        return RunOutcome(
            exit_code=exit_code,
            stdout=out_buf.decode("utf-8", "replace"),
            stderr=err_buf.decode("utf-8", "replace"),
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=truncated,
            duration_s=time.time() - t0,
            container=name,
            argv=argv,
        )

    async def _kill_container(self, name: str, *, task_id: str = "") -> None:
        """
        `docker kill` + `docker rm -f`，幂等、吞掉"容器不存在"这类错误。

        为什么两个都要：--rm 只在容器正常退出时清理；被 kill 时有时会残留。
        """
        self.kill_calls.append(("kill", name))
        await self._docker_quick(["kill", name], timeout=10)
        self.kill_calls.append(("rm", name))
        await self._docker_quick(["rm", "-f", name], timeout=10)
        if task_id:
            self._active.get(task_id, set()).discard(name)
        log.info("已终止沙箱容器 %s", name)

    # ===================================================================== #
    # 生命周期
    # ===================================================================== #
    async def kill_task(self, task_id: str) -> int:
        """杀掉某个任务的所有容器（任务取消时由上层调用）。"""
        names = list(self._active.get(task_id, set()))
        for n in names:
            await self._kill_container(n, task_id=task_id)
        return len(names)

    async def close(self) -> int:
        """进程退出前清理所有仍然活着的容器。"""
        total = 0
        for tid in list(self._active.keys()):
            total += await self.kill_task(tid)
        return total

    def active_containers(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._active.items() if v}
