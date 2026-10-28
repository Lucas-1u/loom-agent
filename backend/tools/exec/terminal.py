"""
terminal —— 在**用户机器上**执行命令（M6）。

★ 与 run_command 的分工
--------------------
    run_command  → Docker 容器（隔离；断网、只读根、非 root）——"安全地跑"
    terminal     → 宿主机 shell（PowerShell / CMD / Git Bash）——"真的跑"

两者都保留：需要装依赖、访问本机 Windows 环境、看真实路径时用 terminal；
只想要一个隔离执行环境时用 run_command。**terminal 没有沙箱**，
它的安全边界是"审批 + 工作目录 + 超时 + 输出限制"，这一点在审批卡片里会写明。

★ 风险分级
--------
· 默认 `risk=EXEC`：每条命令都要用户批准；
· 只有命中**只读白名单**（`git status` / `dir` / `echo` …）且**不含任何 shell 元字符**
  时才降为 READ、免审批。白名单只是"少打扰"，不是安全边界；
  模型不能靠命令参数给自己发权限（判定在 Policy 里用同一个函数做）。
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel
from .shells import (DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, ShellUnavailable, ShellInfo,
                     detect_shell, is_readonly_command, run_command)

MAX_COMMAND_CHARS = 2000


class TerminalTool(Tool):
    name = "terminal"
    description = (
        "在本机终端里执行一条命令（Windows：PowerShell / CMD / Git Bash），返回 stdout、stderr 与退出码。"
        "适合跑测试、构建、git、查看本机环境；命令在**宿主机**上执行，不是沙箱。"
        "默认每条命令都需要用户批准；只读白名单里的命令（如 git status、dir）可直接执行。"
        "工作目录限定在当前任务的工作区；超时（默认 60s，最多 300s）后会终止整棵进程树。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令（一整条，允许管道与重定向）。",
            },
            "cwd": {
                "type": "string",
                "description": "工作目录（相对工作区）。默认 = 任务工作区根目录；不允许跳出工作区。",
            },
            "shell": {
                "type": "string",
                "enum": ["auto", "powershell", "cmd", "bash"],
                "description": "用哪种 shell；auto = 优先 PowerShell 7 / Windows PowerShell，其次 CMD。",
            },
            "timeout": {
                "type": "number",
                "description": f"超时秒数（默认 {int(DEFAULT_TIMEOUT_S)}，上限 {int(MAX_TIMEOUT_S)}）。",
            },
        },
        "required": ["command"],
    }
    risk = RiskLevel.EXEC          # 默认要审批；只读白名单会在 risk_for 里降级

    # ---- 动态风险：只读命令免审批（判定与现实一致，不依赖模型自述）----
    def risk_for(self, args: dict[str, Any]) -> RiskLevel:
        return RiskLevel.READ if is_readonly_command(str(args.get("command") or "")) else RiskLevel.EXEC

    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        command = str(args.get("command") or "")
        cwd = str(args.get("cwd") or ".") or "."
        try:
            shell = detect_shell(str(args.get("shell") or "auto")).label
        except ShellUnavailable as exc:
            shell = f"（不可用：{exc}）"
        return {
            "action": f"在本机 {shell} 中执行命令（工作目录：{cwd}）",
            "target": command[:160],
            "impact": (
                "命令在**宿主机**上执行，不受 Docker 沙箱隔离：可以访问你的文件、网络与已安装程序，"
                "拥有与你当前用户相同的权限。批准只代表同意执行这一条命令。"
            ),
            "preview": f"shell={args.get('shell') or 'auto'}\ncwd={cwd}",
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..path_safety import PathViolation, display_path, resolve_in_workspace

        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS, "command 不能为空",
                                      hint="给出要执行的命令，例如 git status --short。")
        if len(command) > MAX_COMMAND_CHARS:
            return ToolResult.failure(
                self.name, ErrorKind.INVALID_ARGUMENTS,
                f"命令太长（{len(command)} > {MAX_COMMAND_CHARS} 字符）",
                hint="把命令复杂度降下来，或写成脚本文件再执行。",
            )

        # ---- 工作目录：必须在工作区内（不能靠 cwd 参数跳出去）----
        rel = str(args.get("cwd") or "").strip() or "."
        try:
            resolved = resolve_in_workspace(ctx.workspace, rel, must_exist=True)
        except PathViolation as exc:
            return ToolResult.failure(
                self.name, ErrorKind.PERMISSION_DENIED,
                f"工作目录不在允许范围内：{exc}",
                hint="terminal 的工作目录限定在任务工作区内；需要别的目录请在答复里说明。",
            )
        if not os.path.isdir(resolved.absolute):
            return ToolResult.failure(self.name, ErrorKind.INVALID_ARGUMENTS,
                                      f"cwd 不是目录：{rel}")

        shell_choice = str(args.get("shell") or "auto").strip().lower()
        timeout = args.get("timeout")
        timeout = float(timeout) if isinstance(timeout, (int, float)) else DEFAULT_TIMEOUT_S

        with timed() as t:
            try:
                result = await run_command(
                    command, cwd=resolved.absolute, shell=shell_choice,
                    timeout=timeout, cancel_event=getattr(ctx, "cancel_event", None),
                )
            except ShellUnavailable as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, str(exc),
                    hint="换一个 shell（cmd / powershell）或先安装对应终端；不要反复重试同一个不存在的 shell。",
                    duration_ms=t.ms,
                )
            except (FileNotFoundError, OSError) as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"启动 shell 失败：{type(exc).__name__}: {exc}",
                    hint="检查 shell 路径是否存在、是否有执行权限。",
                    duration_ms=t.ms,
                )

            data = {
                "kind": "terminal",
                "shell": result.shell.kind,
                "shell_label": result.shell.label,
                "command": result.command,
                "cwd": display_path(ctx.workspace, result.cwd),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "duration_ms": result.duration_ms,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "truncated": result.truncated,
                "redacted": result.redacted,
            }
            head = (result.stdout.strip().splitlines() or [""])[0][:160]

            if result.cancelled:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, "命令已随任务取消被终止",
                    data=data, duration_ms=result.duration_ms,
                    hint="任务已取消，不要继续执行命令。",
                )
            if result.timed_out:
                return ToolResult.failure(
                    self.name, ErrorKind.TIMEOUT,
                    f"命令超时（>{int(timeout)}s），进程树已被终止",
                    data=data, duration_ms=result.duration_ms,
                    hint="缩小命令范围、加超时参数，或拆成几步；不要原样重试。",
                )
            if result.exit_code != 0:
                tail = (result.stderr.strip().splitlines() or [""])[-1][:200]
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"退出码 {result.exit_code}" + (f"：{tail}" if tail else ""),
                    data=data, duration_ms=result.duration_ms,
                    hint="读 stderr 定位原因后再执行下一条命令；不要假装成功。",
                )
            return ToolResult.success(
                self.name,
                f"[{result.shell.label}] exit 0" + (f"：{head}" if head else ""),
                data=data, duration_ms=result.duration_ms,
            )


def make() -> TerminalTool:
    return TerminalTool()
