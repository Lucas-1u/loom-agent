"""
run_command —— 在 Docker 沙箱里执行一条命令。

★ 为什么这个工具值得单独一个目录（tools/exec/）
────────────────────────────────────────────────────────────────
  readonly/ 里的工具只读文件；mutating/ 里的工具改文件。
  run_command 既不是"读"也不是"写文件" —— 它能干任何事，
  所以它的安全边界不在文件系统，而在**容器**。单独一组，提醒读代码的人：
  这个工具的风险等级和别的工具不是一个量级。

★ 两层安全（互不替代）
────────────────────────────────────────────────────────────────
  ① 审批：每次执行都走 ApprovalManager，用户看到完整 argv 和工作目录。
  ② 沙箱：网络、挂载、用户、capabilities、资源上限全在 SandboxManager 里写死。
     **用户批准不会放宽任何一条** —— 批准的是"这条命令可以跑"，
     不是"可以突破隔离"。

★ 为什么只收结构化参数，不收 shell 字符串
────────────────────────────────────────────────────────────────
  模型输出 `${...}`、`|`、`&&`、`>` 这类东西时，一旦交给 shell，
  "审批时看到的命令"和"实际执行的命令"就可能不是一回事。
  这里用 `create_subprocess_exec(*argv)`，每个参数是一个独立字符串，
  不存在二次解析。真要跑 shell（`sh -c "..."`），那也是一条普通命令，
  会在审批界面上原样显示，并且照样跑在容器里。

★ 这个工具**没有**"在宿主机执行"的分支
    Docker 不可用 → 返回 sandbox_unavailable。不降级、不兜底。
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..path_safety import PathViolation, resolve_in_workspace
from ...agent.state import RiskLevel
from ...sandbox import SandboxManager, SandboxUnavailable

# 需要 shell 解释器的命令：不禁止，但在审批界面上明确提示
SHELL_COMMANDS = {"sh", "bash", "zsh", "dash", "ksh", "cmd", "cmd.exe", "powershell", "pwsh"}

MAX_COMMAND_CHARS = 512
MAX_ARGS = 64
MAX_ARGS_CHARS = 8192


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在一个隔离的 Docker 容器里执行命令（跑 Python 脚本、pytest、构建等）。"
        "容器只挂载当前工作区，默认无网络、非 root、根文件系统只读，"
        "并有 CPU / 内存 / 进程数 / 时长上限。"
        "命令与其参数分开传入，不经过 shell。"
        "每次执行都需要用户批准；批准只代表允许执行这条命令，不会放宽任何隔离限制。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "可执行文件名，例如 python / pytest / ls。不要传整条 shell 字符串。",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参数列表，逐个传入。例如 [\"-m\", \"pytest\", \"-q\"]。",
            },
            "cwd": {
                "type": "string",
                "description": "相对工作区根目录的工作目录，默认 '.'。不能是绝对路径或越出工作区。",
            },
            "timeout_s": {
                "type": "integer",
                "description": "超时秒数，默认 60。超时后容器会被强制终止。",
            },
        },
        "required": ["command"],
    }
    risk = RiskLevel.EXEC

    def __init__(self, sandbox: SandboxManager) -> None:
        self.sandbox = sandbox

    # ===================================================================== #
    # 参数
    # ===================================================================== #
    def validate_args(self, args: dict[str, Any]) -> str:
        base = super().validate_args(args)
        if base:
            return base

        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return "command 必须是非空字符串"
        if "\x00" in cmd or len(cmd) > MAX_COMMAND_CHARS:
            return f"command 非法（含 NUL 或超过 {MAX_COMMAND_CHARS} 字符）"

        # ---- 第六批：检测"整条命令塞进 command"--------------------------
        # 真实 E2E：模型三次把 "python -m pytest -q" 整条塞进 command，
        # 容器 exec 报 127（No such file or directory），白白烧掉 3 轮。
        # 在这里提前失败并给出**可直接照抄**的纠正写法，
        # 不自动拆分执行 —— 引号/管道拆错比多一轮更危险。
        if args.get("args") in (None, []) and " " in cmd.strip():
            parts = cmd.strip().split()
            return (
                f"command 应该只是可执行文件名，不要把整条命令塞进来（检测到 {cmd.strip()[:60]!r}）。"
                f"正确写法：command={parts[0]!r}, args={parts[1:]!r}"
            )

        raw_args = args.get("args")
        if raw_args is not None:
            if not isinstance(raw_args, list):
                return "args 必须是数组"
            if len(raw_args) > MAX_ARGS:
                return f"args 太多了（{len(raw_args)} > {MAX_ARGS}）"
            for i, a in enumerate(raw_args):
                if isinstance(a, (dict, list)) or a is None:
                    return f"args[{i}] 不是标量（收到 {type(a).__name__}）"

        cwd = args.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return "cwd 必须是字符串"

        timeout = args.get("timeout_s")
        if timeout is not None and isinstance(timeout, bool):
            return "timeout_s 应为整数"
        return ""

    def _normalize_argv(self, args: dict[str, Any]) -> tuple[str, list[str]]:
        """
        把参数整理成 `(command, [str, ...])`。

        数字/布尔会被转成字符串（模型经常写 `["-n", 5]`）；
        容器/列表已在 validate_args 里挡掉。**不做任何 shell 拼接**。
        """
        cmd = str(args.get("command", "")).strip()
        raw = args.get("args") or []
        argv: list[str] = []
        for a in raw:
            argv.append(a if isinstance(a, str) else str(a))
        return cmd, argv

    # ===================================================================== #
    # 执行
    # ===================================================================== #
    def _resolve_timeout(self, args: dict[str, Any]) -> tuple[int, int]:
        """
        返回 `(钳制后的秒数, 请求的秒数)`。

        ★ 不能用 `args.get("timeout_s") or 默认值` —— 用户明确要 0 秒时
          0 是假值，会被悄悄换成 60。这里显式区分 None 与 0。
        """
        cfg = self.sandbox.config
        raw = args.get("timeout_s")
        if raw is None:
            requested = int(cfg.default_timeout_s)
        else:
            try:
                requested = int(raw)
            except (TypeError, ValueError):
                requested = int(cfg.default_timeout_s)
        return max(1, min(requested, int(cfg.max_timeout_s))), requested

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 0. 授权闸门（纵深防御：Policy 之外再挡一次）---------------
            if not ctx.authorized:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    "命令执行未经用户批准",
                    hint="这是需要审批的操作。请说明要跑什么、为什么跑，等待用户确认。",
                    duration_ms=t.ms,
                )

            err = self.validate_args(args)
            if err:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, err, duration_ms=t.ms
                )

            command, argv = self._normalize_argv(args)
            if sum(len(a) for a in argv) > MAX_ARGS_CHARS:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"参数总长度超过 {MAX_ARGS_CHARS} 字符",
                    duration_ms=t.ms,
                )

            # ---- 1. 工作目录必须真的在工作区内 ----------------------------
            cwd_raw = args.get("cwd") or "."
            try:
                rp = resolve_in_workspace(ctx.workspace, str(cwd_raw), must_exist=True)
            except PathViolation as exc:
                outside = "越出工作区" in str(exc)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATH_OUTSIDE_WORKSPACE if outside else ErrorKind.INVALID_ARGUMENTS,
                    str(exc),
                    hint="cwd 只能是工作区内的相对路径。" if outside else "请检查 cwd 写法。",
                    duration_ms=t.ms,
                )
            if not os.path.isdir(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_DIR,
                    f"cwd 不是目录：{rp.relative}",
                    duration_ms=t.ms,
                )

            # ---- 2. 超时钳制（模型给的值不可信）---------------------------
            cfg = self.sandbox.config
            timeout_s, requested = self._resolve_timeout(args)

            # ---- 3. 交给沙箱（这里是唯一的执行路径）-----------------------
            try:
                outcome = await self.sandbox.run(
                    task_id=ctx.task_id or "task",
                    command=command,
                    args=argv,
                    workspace_root=ctx.workspace,
                    cwd_rel=rp.relative,
                    timeout_s=float(timeout_s),
                    cancel_event=ctx.cancel_event,
                )
            except SandboxUnavailable as exc:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.SANDBOX_UNAVAILABLE,
                    f"沙箱不可用：{exc}",
                    hint=(
                        "命令**没有**被执行（不会退回到宿主机运行）。"
                        "请让用户启动 Docker Desktop 并构建镜像："
                        f"docker build -f dev/Dockerfile.sandbox -t {cfg.image} ."
                    ),
                    duration_ms=t.ms,
                )
            except ValueError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, f"命令参数非法：{exc}", duration_ms=t.ms
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    f"沙箱执行异常：{type(exc).__name__}: {exc}",
                    duration_ms=t.ms,
                )

            # ---- 4. 结果 ---------------------------------------------------
            data: dict[str, Any] = {
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "cancelled": outcome.cancelled,
                # ★ stderr 放 stdout 前面：模型侧消息超过上限被截断时，
                #   先保住报错（修 bug 靠它）。
                "stderr": outcome.stderr,
                "stdout": outcome.stdout,
                "truncated": outcome.output_truncated,
                "duration_s": round(outcome.duration_s, 2),
                "cwd": rp.relative,
                "command": command,
                "sandbox_image": cfg.image,
            }

            if outcome.cancelled:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    "命令因任务取消被终止，容器已清理",
                    data=data,
                    duration_ms=t.ms,
                )

            if outcome.timed_out:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.TIMEOUT,
                    f"命令超时（{timeout_s}s），容器已被强制终止",
                    hint="可以减小测试范围、加超时参数，或把长任务拆成几步。",
                    data=data,
                    duration_ms=t.ms,
                )

            note = ""
            if requested != timeout_s:
                note = f"（请求 {requested}s，已钳制为 {timeout_s}s）"
            if outcome.output_truncated:
                note += f"（输出超过 {cfg.max_output_bytes} 字节，已截断）"

            if outcome.exit_code == 0:
                return ToolResult.success(
                    self.name,
                    f"命令执行成功（退出码 0，{outcome.duration_s:.1f}s）{note}",
                    data=data,
                    side_effects=[
                        {
                            "kind": "command_exec",
                            "container": outcome.container,
                            "exit_code": 0,
                            "cwd": rp.relative,
                        }
                    ],
                    duration_ms=t.ms,
                )

            return ToolResult.failure(
                self.name,
                ErrorKind.EXECUTION_ERROR,
                f"命令退出码 {outcome.exit_code}{note}",
                hint="读 stderr / stdout 找原因；修完代码或改参数后可以重试。",
                data=data,
                duration_ms=t.ms,
            )

    # ===================================================================== #
    # 审批展示
    # ===================================================================== #
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        """
        审批界面必须让用户看见"到底要跑什么"。

        ★ 这里**不执行任何东西**，只做字符串整理。
        """
        command, argv = self._normalize_argv(args)
        cwd = str(args.get("cwd") or ".")
        cfg = self.sandbox.config
        timeout_s, _requested = self._resolve_timeout(args)

        shown = f"{command} {' '.join(argv)}".strip()
        impact = (
            f"工作目录：{cwd}\n"
            f"容器限制：非 root（{cfg.user}）/ 无网络（--network {cfg.network}）/ "
            f"只读根文件系统 / 丢弃全部 capabilities / no-new-privileges / "
            f"{cfg.cpus} CPU / {cfg.memory} 内存 / {cfg.pids_limit} 进程 / 超时 {timeout_s}s\n"
            "挂载：仅当前工作区（读写），不挂载 docker.sock、家目录或系统目录\n"
            "★ 批准只代表允许执行这条命令，**不会**放宽上述任何限制。"
        )
        base = os.path.basename(command.lower())
        if base in SHELL_COMMANDS:
            impact += (
                f"\n⚠ 这条命令是 shell 解释器（{base}）：参数里的内容会被 shell 二次解释。"
                "请确认你真的看懂整条命令。"
            )

        preview = "实际执行的 argv（逐参数传入，不经过 shell）：\n" + repr([command, *argv])
        return {
            "action": "在隔离容器中执行命令",
            "target": shown[:300],
            "impact": impact,
            "preview": preview,
        }


def make(sandbox: SandboxManager | None = None) -> Tool:
    """构造工具。默认自建一个 SandboxManager（从环境变量读配置）。"""
    if sandbox is None:
        from ...config import SandboxConfig

        sandbox = SandboxManager(SandboxConfig.from_env())
    return RunCommandTool(sandbox)
