"""
delete_file —— 在工作区内删除一个文件（删除前先备份）。

★ 补齐三个写入操作的最后一块：write_file（新建/覆盖）/ edit_file（局部改）/
  delete_file（删除）。失去它，Agent 想"删掉一个文件"只能靠 run_command 跑 rm
  —— 跳出了工具层留痕（ChangeRecord）、备份与路径校验这套机制。

────────────────────────────────────────────────────────────────
设计决策
────────────────────────────────────────────────────────────────
① 为什么先备份再删？
   删除是破坏性最强的一步：写错内容还能覆盖回来，删错文件不可逆。
   所以删除前把原文件复制到 `.loom_backup/<时间戳>/`，
   备份失败则**拒绝删除** —— 宁可不删，也不能无声删掉一个没有副本的文件。

② 为什么只删文件、不删目录？
   递归删目录（rm -rf）的破坏面量级完全不同：一次误判能删掉成百上千个文件。
   目录操作留在 run_command（Docker 沙箱 + 独立出口），本工具只处理
   "单个文件"这个明确、可留痕、可回滚的粒度；遇到目录明确报错并指引，
   而不是"顺手递归删掉"。

③ 为什么没有 confirm / allow 之类参数？
   删除的兜底是**备份**，不是用户的第二次干预。多一个开关只会让模型
   习惯性传 true，反而弱化设计。少一个参数，少一处误用。

—— 安全与 write_file 同一套：
   路径必须落在工作区内（resolve_in_workspace：realpath 解符号链接 +
   带分隔符前缀比较）；命中敏感名单（.env / id_rsa 等）时显著警示但不拒绝；
   每类失败都带可操作的 hint。
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..path_safety import (
    PathViolation,
    display_path,
    human_size,
    is_sensitive,
    resolve_in_workspace,
)
from ...agent.state import ChangeRecord, RiskLevel
from ._shared import make_backup, sha256_file

PREVIEW_LINES = 20                 # 删除预览展示的行数


def _line_count(abs_path: str) -> int:
    """宽容地数行数：任何编码都能跑，读不了就返回 0（不影响删除本身）。"""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


class DeleteFileTool(Tool):
    name = "delete_file"
    description = (
        "删除工作区内的一个**文件**（不能删目录）。"
        "删除前会把原文件备份到 .loom_backup/ 下，备份失败则拒绝删除；"
        "需要删除目录时改用 run_command（在 Docker 沙箱中执行）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区根目录的文件路径。文件必须已存在。",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.WRITE

    # ===================================================================== #
    # 执行
    # ===================================================================== #
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 0. 参数 ---------------------------------------------------
            raw_path = args.get("path", "")
            if not raw_path:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "缺少必填参数 'path'",
                    duration_ms=t.ms,
                )

            # ---- 1. 路径校验 -----------------------------------------------
            try:
                rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            except PathViolation as exc:
                outside = "越出工作区" in str(exc)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATH_OUTSIDE_WORKSPACE if outside else ErrorKind.INVALID_ARGUMENTS,
                    str(exc),
                    hint="只能删除工作区内的路径。" if outside else "请检查路径写法。",
                    duration_ms=t.ms,
                )

            # ---- 2. 存在性与类型守卫 --------------------------------------
            if not os.path.exists(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_FOUND,
                    f"文件不存在：{rp.relative}",
                    hint="如果路径写错了，可以用 list_files 先确认。",
                    duration_ms=t.ms,
                )
            if os.path.isdir(rp.absolute):
                # ★ 关键守卫：只删文件，绝不递归删目录。
                #   删目录的破坏面是"一个文件"的成百上千倍，必须留在沙箱里做。
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_FILE,
                    f"目标是目录，不是文件：{rp.relative}",
                    hint=(
                        "delete_file 只删除单个文件。"
                        "需要删除目录（含其中所有文件）时，"
                        "请改用 run_command 在 Docker 沙箱中执行目录删除。"
                    ),
                    duration_ms=t.ms,
                )

            # ---- 3. 删除前统计与备份（★ 先备份，后删除）-------------------
            before_hash = sha256_file(rp.absolute)
            removed_lines = _line_count(rp.absolute)
            size = os.path.getsize(rp.absolute)
            backup_path, err = make_backup(rp.absolute, ctx)
            if err:
                # ★ 备份失败 = 拒绝删除：宁可不删，也不能删一个没有副本的文件。
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    err,
                    hint="原文件未被删除。",
                    duration_ms=t.ms,
                )

            # ---- 4. 删除 ---------------------------------------------------
            try:
                os.remove(rp.absolute)
            except PermissionError:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    f"系统拒绝删除：{rp.relative}",
                    hint="文件可能被其他进程占用或受系统保护。",
                    duration_ms=t.ms,
                )
            except OSError as exc:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    f"删除失败：{exc}",
                    hint=(
                        f"原文件已备份到 {display_path(ctx.workspace, backup_path)}，"
                        "可手工还原。"
                    ),
                    duration_ms=t.ms,
                )

            # ---- 5. 结果与变更记录 -----------------------------------------
            change = ChangeRecord(
                path=rp.relative,
                action="delete",
                before_hash=before_hash,
                after_hash="",      # 文件已不存在
                added=0,
                removed=removed_lines,
                approval_id="",     # 步骤 5：审批移除，留痕字段保留但恒空
                backup_path=display_path(ctx.workspace, backup_path),
            )

            summary = (
                f"删除文件 {rp.relative}（{removed_lines} 行，{human_size(size)}）"
                f"；原内容已备份到 {change.backup_path}"
            )

            data: dict[str, Any] = {
                "path": rp.relative,
                "action": "delete",
                "removed_lines": removed_lines,
                "bytes": size,
                "before_hash": before_hash,
                "change": change.to_dict(),
            }

            return ToolResult.success(
                self.name,
                summary,
                data=data,
                side_effects=[
                    {
                        "kind": "file_delete",
                        "path": rp.relative,
                        "action": "delete",
                        "backup": change.backup_path,
                    }
                ],
                duration_ms=t.ms,
            )

    # ===================================================================== #
    # 影响说明（描述信息，不再经审批墙展示）
    # ===================================================================== #
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        """
        给出「要删的是哪个文件、里面大概有什么」。

        ★ 这是删除工具最有价值的补充信息：写入的预览展示"将要写什么"，
          删除的预览展示"即将失去什么"—— 用户/日志看到前 20 行，
          就能立刻发现"删错文件了"。
        """
        raw_path = str(args.get("path", ""))

        # 路径解析（失败不抛，用原文展示让用户自己判断）
        abs_path = ""
        rel = raw_path
        try:
            rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            abs_path, rel = rp.absolute, rp.relative
        except PathViolation:
            pass

        if not abs_path:
            impact = f"⚠ **路径解析失败**：{raw_path}。这次调用会失败。"
        elif os.path.isdir(abs_path):
            impact = f"⚠ **目标是目录**：{rel}。delete_file 只删文件，这次调用会失败。"
        elif not os.path.isfile(abs_path):
            impact = f"⚠ **文件不存在**：{rel}。这次调用会失败。"
        else:
            lines = _line_count(abs_path)
            size = os.path.getsize(abs_path)
            impact = (
                f"将**删除**文件 {rel}（{lines} 行 / {human_size(size)}）。"
                f"原内容会先备份到 {ctx.backup_dir_name}/，可手工还原。"
            )

        # 敏感文件警示（不拒绝，只是显著提醒）
        if abs_path and os.path.isfile(abs_path):
            sensitive, why = is_sensitive(
                abs_path,
                rel,
                sensitive_names=ctx.sensitive_names or set(),
                sensitive_dir_parts=ctx.sensitive_dir_parts or set(),
            )
            if sensitive:
                impact += f"\n⚠ **敏感文件**：{why}。请确认这确实是你要删除的内容。"

        # 内容预览：展示文件开头，供确认"要删的是不是这个"
        preview = ""
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines_shown = f.read().splitlines()[:PREVIEW_LINES]
            preview = "\n".join(lines_shown)
            if len(lines_shown) == PREVIEW_LINES:
                preview += "\n…（内容已截断）"
            if not preview.strip():
                preview = "（文件为空）"
        except OSError:
            preview = "（无法读取预览）"

        return {
            "action": "删除文件",
            "target": rel,
            "impact": impact,
            "preview": preview,
        }


def make() -> Tool:
    return DeleteFileTool()
