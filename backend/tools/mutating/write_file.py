"""
write_file —— 在工作区内新建或覆盖一个文本文件。

★ v0.1a 里唯一需要审批的工具。审批机制的完整价值在这里体现。

────────────────────────────────────────────────────────────────
为什么 v0.1a 只做 write_file，不做 edit_file？
────────────────────────────────────────────────────────────────
小陆的 C4 决定：v0.1a 先验证「审批流」本身是否正确，v0.1b 再补高级文件操作。

这个切分很对，因为：
  · write_file 的语义最简单 ——「把这段内容写进这个文件」。审批要展示什么、
    挂起怎么恢复、拒绝怎么回灌给模型，这些**流程问题**在这里就能全部验完。
  · edit_file 引入的是**内容匹配问题**（old_string 唯一性、patch_conflict、
    stale_content），那是另一类复杂度。两个混在一起调，出问题分不清是谁的锅。

────────────────────────────────────────────────────────────────
安全设计（四条）
────────────────────────────────────────────────────────────────
① **必须已授权**。ctx.authorized 由 Policy 在执行前置 True（Policy 内部走完审批）。
   工具自己再检查一次 —— 这是「纵深防御」：万一 Policy 有 bug，
   写工具这道闸仍然拦得住。**不许把安全校验只放在一层。**

② **覆盖前备份**。原文件先复制到 `.loom_backup/<时间戳>/<相对路径>`。
   给用户后悔的机会。备份失败则**拒绝执行**（宁可不写，也不能无声覆盖）。

③ **内容上限**。防止模型一次吐 500KB 把磁盘塞满 / 把审批界面卡死。

④ **敏感文件额外警示**（命中 .env 等时在影响说明里高亮）。

────────────────────────────────────────────────────────────────
审批要展示什么？
────────────────────────────────────────────────────────────────
C4 要求展示三要素。这里逐个落实：
    action  "写入文件"（或 "新建文件" / "覆盖文件"）
    target  工作区相对路径（不用绝对路径，避免泄露用户目录结构）
    impact  新建 vs 覆盖、会影响什么（行数变化、原文件大小）、是否敏感文件
    preview 新内容前若干行（让用户**看得见要写什么**，而不是盲签）
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
from ._shared import make_backup, sha256_file, sha256_text


MAX_CONTENT_CHARS = 200_000       # 单次写入上限（字符）
PREVIEW_LINES = 40                # 审批时预览的行数


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "在工作区内新建文件，或覆盖已有文件的全部内容。"
        "此操作会修改磁盘上的文件，需要用户批准后才会真正执行。"
        "若只是修改文件中一小段，请优先考虑使用 edit_file（本版本暂未提供）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区根目录的文件路径。父目录会自动创建。",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整文件内容（覆盖模式会替换原有全部内容）。",
            },
            "overwrite": {
                "type": "boolean",
                "description": "文件已存在时是否允许覆盖，默认 true。设为 false 则文件存在时直接失败，更安全。",
            },
        },
        "required": ["path", "content"],
    }
    risk = RiskLevel.WRITE

    # ===================================================================== #
    # 执行
    # ===================================================================== #
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 0. 授权闸门（纵深防御第一层）------------------------------
            if not ctx.authorized:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    "写入操作未经用户批准",
                    hint="这是需要审批的操作。请说明修改目的，等待用户确认后重试。",
                    duration_ms=t.ms,
                )

            # ---- 1. 参数 ---------------------------------------------------
            raw_path = args.get("path", "")
            content = args.get("content")
            overwrite = args.get("overwrite", True)

            if not raw_path:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, "缺少必填参数 'path'", duration_ms=t.ms
                )
            if content is None:
                content = ""
            if not isinstance(content, str):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"content 必须是字符串，收到 {type(content).__name__}",
                    duration_ms=t.ms,
                )
            if len(content) > MAX_CONTENT_CHARS:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.TOO_LARGE,
                    f"内容过长（{len(content)} 字符 > 上限 {MAX_CONTENT_CHARS}）",
                    hint="请分多次写入，或只写必要部分。",
                    duration_ms=t.ms,
                )

            # ---- 2. 路径校验 -----------------------------------------------
            try:
                rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            except PathViolation as exc:
                outside = "越出工作区" in str(exc)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATH_OUTSIDE_WORKSPACE if outside else ErrorKind.INVALID_ARGUMENTS,
                    str(exc),
                    hint="只能写入工作区内的路径。" if outside else "请检查路径写法。",
                    duration_ms=t.ms,
                )

            if os.path.isdir(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_FILE,
                    f"目标是目录，不是文件：{rp.relative}",
                    hint="请给出具体文件名。",
                    duration_ms=t.ms,
                )

            existed = os.path.isfile(rp.absolute)

            # ---- 3. 覆盖保护 -----------------------------------------------
            if existed and not overwrite:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"文件已存在且 overwrite=false：{rp.relative}",
                    hint="如果需要覆盖，请显式设置 overwrite=true，或先读取现有内容再决定。",
                    duration_ms=t.ms,
                )

            # ---- 4. 备份（覆盖时）------------------------------------------
            backup_path = ""
            before_hash = ""
            if existed:
                before_hash = sha256_file(rp.absolute)
                backup_path, err = make_backup(rp.absolute, ctx)
                if err:
                    return ToolResult.failure(
                        self.name, ErrorKind.EXECUTION_ERROR, err, duration_ms=t.ms
                    )

            # ---- 5. 写入 ---------------------------------------------------
            try:
                parent = os.path.dirname(rp.absolute)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(rp.absolute, "w", encoding="utf-8", newline="\n") as f:
                    f.write(content)
            except PermissionError:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    f"系统拒绝写入：{rp.relative}",
                    duration_ms=t.ms,
                )
            except OSError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, f"写入失败：{exc}", duration_ms=t.ms
                )

            # ---- 6. 结果与变更记录 -----------------------------------------
            new_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            action = "overwrite" if existed else "create"

            change = ChangeRecord(
                path=rp.relative,
                action=action,
                before_hash=before_hash,
                after_hash=sha256_text(content),
                added=new_lines,
                approval_id=ctx.approval_id,
                backup_path=display_path(ctx.workspace, backup_path) if backup_path else "",
            )

            verb = "覆盖" if existed else "新建"
            summary = f"{verb}文件 {rp.relative}（{new_lines} 行，{human_size(len(content.encode('utf-8')))}）"
            if backup_path:
                summary += f"；原内容已备份到 {change.backup_path}"

            data: dict[str, Any] = {
                "path": rp.relative,
                "action": action,
                "lines": new_lines,
                "bytes": len(content.encode("utf-8")),
                "change": change.to_dict(),
            }

            return ToolResult.success(
                self.name,
                summary,
                data=data,
                side_effects=[
                    {
                        "kind": "file_write",
                        "path": rp.relative,
                        "action": action,
                        "backup": change.backup_path,
                    }
                ],
                duration_ms=t.ms,
            )

    # ===================================================================== #
    # 审批展示（C4 三要素）
    # ===================================================================== #
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        """
        生成审批界面要展示的信息。

        ★ 注意这里**会读磁盘**（判断文件是否存在、多大），但**绝不写**。
          读盘是为了给出准确的影响说明（"将覆盖一个 200 行的文件"比
          "将覆盖文件"有用得多）。
        """
        raw_path = str(args.get("path", ""))
        content = args.get("content", "")
        if not isinstance(content, str):
            content = str(content)

        # ---- 路径解析（失败也不抛，用原文展示，让用户自己判断）----------
        abs_path = ""
        rel = raw_path
        try:
            rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            abs_path, rel = rp.absolute, rp.relative
        except PathViolation:
            pass

        existed = bool(abs_path) and os.path.isfile(abs_path)

        # ---- 影响说明 --------------------------------------------------
        new_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        new_bytes = len(content.encode("utf-8"))

        if existed:
            try:
                old_size = os.path.getsize(abs_path)
                old_lines = 0
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        old_lines = sum(1 for _ in f)
                except OSError:
                    pass
                impact = (
                    f"将**覆盖**已有文件（原 {old_lines} 行 / {human_size(old_size)}"
                    f" → 新 {new_lines} 行 / {human_size(new_bytes)}）。"
                    f"原内容会先备份到 {ctx.backup_dir_name}/，可手工还原。"
                )
            except OSError:
                impact = f"将**覆盖**已有文件（新 {new_lines} 行 / {human_size(new_bytes)}）"
        else:
            impact = f"将**新建**文件（{new_lines} 行 / {human_size(new_bytes)}）"

        # ---- 敏感文件警示 ----------------------------------------------
        if abs_path:
            sensitive, why = is_sensitive(
                abs_path,
                rel,
                sensitive_names=ctx.sensitive_names or set(),
                sensitive_dir_parts=ctx.sensitive_dir_parts or set(),
            )
            if sensitive:
                impact += f"\n⚠ **敏感文件**：{why}。请确认这确实是你要 Agent 修改的内容。"

        # ---- 预览 ------------------------------------------------------
        lines = content.splitlines()
        preview = "\n".join(lines[:PREVIEW_LINES])
        if len(lines) > PREVIEW_LINES:
            preview += f"\n…（还有 {len(lines) - PREVIEW_LINES} 行未显示）"
        if not preview.strip():
            preview = "（内容为空）"

        return {
            "action": "覆盖文件" if existed else "新建文件",
            "target": rel,
            "impact": impact,
            "preview": preview,
        }


def make() -> Tool:
    return WriteFileTool()
