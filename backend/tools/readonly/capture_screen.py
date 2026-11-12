"""
capture_screen —— Windows 全屏截图（方案2 的第一块砖）。

★ 它做什么：抓取**主显示器**整屏，存为 PNG（写到工作区 `capture/` 目录）。
   依赖 Windows PowerShell 5.1 自带的 .NET（System.Windows.Forms/
   System.Drawing），不引入第三方包。

★ 安全边界：
   · 是"看"不是"动"——不移动鼠标、不按键、不改任何程序状态；
   · 唯一的副作用是工作区内的一张 PNG（风险 READ，和 search_docs 同级）；
   · 输出目录固定在 `<workspace>/capture/`，不写到别处。

★ 已知限制：
   · 只截主显示器（多屏全截要走 user32 EnumDisplayMonitors，留作 TODO）；
   · 截不到最小化/后台窗口（那是论 UIA 树的活，交给 agent-desktop）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import shutil
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel

MAX_BYTES = 60 * 1024 * 1024  # 60MB 上限（正常全屏 PNG 远小于此）

_PS_HEAD = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($b.Width,$b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size)
$bmp.Save('__PATH__',[System.Drawing.Imaging.ImageFormat]::Png)
$w = $bmp.Width; $h = $bmp.Height
$g.Dispose(); $bmp.Dispose()
Write-Output "OK $w $h"
"""


def _decode_ps(data: bytes) -> str:
    """解码 Windows PowerShell 输出（可能是 UTF-8 也可能是 GBK）。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "ascii"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


class CaptureScreenTool(Tool):
    name = "capture_screen"
    description = (
        "抓取 Windows **主显示器**整屏截图，保存为 PNG（写到工作区 capture/ 目录）。"
        "返回文件路径、体积、尺寸。适合视觉模型工作流第一步：先截图，"
        "再用「视觉问答」看屏幕内容、找控件坐标。注意：只截主屏、截不到后台/最小化窗口。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "subdir": {
                "type": "string",
                "description": "输出目录名（相对工作区），默认 capture。",
            },
        },
        "required": [],
    }
    risk = RiskLevel.READ

    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        sub = str((args or {}).get("subdir") or "capture")
        if os.path.basename(sub) != str(sub) or ".." in sub.replace("\\", "/"):
            return "subdir 只能是单层目录名（不含路径分隔符）"
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            workspace = getattr(ctx, "workspace", "") or os.getcwd()
            sub = str((args or {}).get("subdir") or "capture")
            out_dir = os.path.join(workspace, sub)
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.PERMISSION_DENIED,
                    f"无法创建截图目录：{sub}（{type(exc).__name__}）",
                    hint="检查工作区权限，或换一个 subdir。",
                )

            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(out_dir, f"screen_{ts}.png")

            ws = shutil.which("powershell.exe")
            if not ws:
                return ToolResult.failure(
                    self.name, ErrorKind.SANDBOX_UNAVAILABLE,
                    "找不到 powershell.exe —— 本工具依赖 Windows PowerShell 5.1",
                    hint="Windows 自带；若被精简请改用 PowerShell 7 安装或联系环境管理员。",
                )

            # ★ 路径必须内嵌进脚本串（而不是 -Command <脚本> -Path <路径>）：
            #   PowerShell 会把 -Path 追加到**脚本尾部**，param() 根本收不到它。
            script = _PS_HEAD.replace("__PATH__", path.replace("'", "''"))

            proc = await asyncio.create_subprocess_exec(
                ws, "-NoProfile", "-NonInteractive", "-Command", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult.failure(
                    self.name, ErrorKind.TIMEOUT, "截图超时（>30s）",
                    hint="桌面太卡时可能超时；重试一次或先关掉大窗口。",
                )

            tail = _decode_ps(stderr_b).strip()[-300:]
            if proc.returncode != 0 or not os.path.isfile(path):
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"截屏失败（exit {proc.returncode}）：{tail}",
                    hint="确认显示驱动/无障碍权限正常；输出已留空，无半截文件。",
                )

            size = os.path.getsize(path)
            if size > MAX_BYTES:
                os.remove(path)
                return ToolResult.failure(
                    self.name, ErrorKind.TOO_LARGE, f"截图过大（{size} 字节），已清理",
                    hint="正常全屏 PNG 不会这么大——检查是否截到了奇怪内容。",
                )

            size_kb = size // 1024
            return ToolResult.success(
                self.name, 
                f"已截图：{sub}/screen_{ts}.png（{size_kb} KB），绝对路径 {path}",
                data={"path": path, "bytes": size},
                duration_ms=t.ms,
            )


def make() -> Tool:
    return CaptureScreenTool()
