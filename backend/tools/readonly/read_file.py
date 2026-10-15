"""
read_file —— 读取工作区内某个文本文件的内容。

设计要点
--------
1. **必须带行号？** —— 带。
   理由：Coding Agent 后续要基于内容做修改（v0.1b 的 edit_file 需要精确匹配），
   行号能让模型在分析时引用具体位置（"第 12 行有个 bug"），
   也让用户看日志时能快速定位。这是 v0.1b 的地基，v0.1a 就铺好。

2. **编码探测**。老项目踩过坑：国内环境下很多文件是 GBK/GB18030。
   顺序：UTF-8 → UTF-8-BOM → GB18030 → latin-1（兜底不报错）。
   注意 latin-1 永远不会失败，所以要放最后。

3. **截断而非失败**。大文件返回前 N 字符 + 明确告知被截断 + 给了 offset/limit
   让模型可以分段读。这比直接报错「文件太大」有用得多。

4. **二进制检测**。遇到含空字节的文件直接拒绝，不要往上下文里塞乱码。

5. **敏感文件**。命中 .env / id_rsa 之类时，**仍然允许读**（因为 Agent 可能要
   检查配置是否缺项），但在结果里显著标注，让用户从事件流里看得见。
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..path_safety import (
    PathViolation,
    human_size,
    is_sensitive,
    resolve_in_workspace,
)
from ...agent.state import RiskLevel


# 编码探测顺序（老项目经验：GB18030 能覆盖 GBK/GB2312，放它而不是 GBK）
ENCODING_CANDIDATES = ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1")


def _read_text(abs_path: str) -> tuple[str, str, str]:
    """
    读文本，返回 (内容, 命中编码, 警告信息)。

    latin-1 兜底：它能把任意字节序列映射成字符，永不失败 ——
    保证工具不会因为「编码不认识」而彻底失败。
    但此时内容可能是乱码，所以要带警告。
    """
    with open(abs_path, "rb") as f:
        raw = f.read()

    # ---- 二进制检测（含空字节基本可断定是二进制）----------------------------
    if b"\x00" in raw[:8192]:
        raise ValueError("binary")

    for enc in ENCODING_CANDIDATES:
        try:
            text = raw.decode(enc)
            if enc == "latin-1":
                return text, enc, "无法确定编码，按 latin-1 兜底解码，可能出现乱码"
            return text, enc, ""
        except UnicodeDecodeError:
            continue

    # 理论上到不了这里（latin-1 不会失败）
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)", "解码有损，部分字符被替换"


def _with_line_numbers(text: str, start_line: int = 1) -> str:
    """
    加行号。格式：`   12 | 内容`

    行号宽度按总行数自适应（4 位数不会挤压 3 位数的对齐）。
    """
    lines = text.splitlines()
    if not lines:
        return ""
    width = max(3, len(str(start_line + len(lines) - 1)))
    out = []
    for i, line in enumerate(lines, start=start_line):
        out.append(f"{str(i).rjust(width)} | {line}")
    return "\n".join(out)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取工作区内某个文本文件的完整内容（带行号）。"
        "适合查看源码、配置、文档。文件过大时会截断，可用 offset/limit 分段读取。"
        "二进制文件会被拒绝。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区根目录的文件路径。",
            },
            "offset": {
                "type": "integer",
                "description": "从第几行开始读（1 起）。省略则从第 1 行开始。",
            },
            "limit": {
                "type": "integer",
                "description": "最多读多少行。省略则读全部（受总字符上限约束）。",
            },
        },
        "required": ["path"],
    }
    risk = RiskLevel.READ

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 1. 参数 ---------------------------------------------------
            raw_path = args.get("path", "")
            if not raw_path:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "缺少必填参数 'path'",
                    hint="请提供要读取的文件路径，例如 'backend/main.py'。",
                    duration_ms=t.ms,
                )

            offset = args.get("offset")
            limit = args.get("limit")
            try:
                offset = int(offset) if offset is not None else None
                limit = int(limit) if limit is not None else None
            except (TypeError, ValueError):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "offset/limit 必须是整数",
                    duration_ms=t.ms,
                )
            if offset is not None and offset < 1:
                offset = 1

            # ---- 2. 路径校验 -----------------------------------------------
            try:
                rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=True)
            except PathViolation as exc:
                outside = "越出工作区" in str(exc)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATH_OUTSIDE_WORKSPACE if outside else ErrorKind.NOT_FOUND,
                    str(exc),
                    hint=(
                        "只能读取工作区内的文件。"
                        if outside
                        else "文件不存在。请先用 list_files 确认路径，注意大小写。"
                    ),
                    duration_ms=t.ms,
                )

            if os.path.isdir(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_FILE,
                    f"这是一个目录，不是文件：{rp.relative or '.'}",
                    hint="请用 list_files 查看该目录内容，再指定具体文件。",
                    duration_ms=t.ms,
                )

            # ---- 3. 大小闸门 -----------------------------------------------
            try:
                size = os.path.getsize(rp.absolute)
            except OSError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, f"无法获取文件大小：{exc}", duration_ms=t.ms
                )

            # ---- 4. 敏感文件标注（不拒绝，但显著提示）----------------------
            sensitive, why = is_sensitive(
                rp.absolute,
                rp.relative,
                sensitive_names=ctx.sensitive_names or set(),
                sensitive_dir_parts=ctx.sensitive_dir_parts or set(),
            )

            # ---- 5. 读取 ---------------------------------------------------
            try:
                text, encoding, warning = _read_text(rp.absolute)
            except ValueError as exc:
                if str(exc) == "binary":
                    return ToolResult.failure(
                        self.name,
                        ErrorKind.INVALID_ARGUMENTS,
                        f"这是二进制文件，无法作为文本读取：{rp.relative}",
                        hint="read_file 只支持文本文件。",
                        duration_ms=t.ms,
                    )
                raise
            except PermissionError:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    f"系统拒绝读取：{rp.relative}",
                    duration_ms=t.ms,
                )
            except OSError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, f"读取失败：{exc}", duration_ms=t.ms
                )

            # ---- 6. 分段 / 截断 ---------------------------------------------
            all_lines = text.splitlines()
            total_lines = len(all_lines)
            start_idx = (offset - 1) if offset else 0
            end_idx = (start_idx + limit) if limit else total_lines
            end_idx = min(end_idx, total_lines)

            selected = all_lines[start_idx:end_idx]
            body = "\n".join(selected)

            truncated_by_chars = False
            max_chars = ctx.max_read_chars or 40_000
            if len(body) > max_chars:
                body = body[:max_chars]
                truncated_by_chars = True
                truncated_at_line = len(body.splitlines())
                end_idx = start_idx + truncated_at_line
                selected = all_lines[start_idx:end_idx]

            numbered = _with_line_numbers(body, start_line=start_idx + 1)

            # ---- 7. 摘要 ---------------------------------------------------
            parts = [f"读取 {rp.relative}（{total_lines} 行，{human_size(size)}，{encoding}）"]
            if offset or limit:
                parts.append(f"本次取第 {start_idx + 1}-{end_idx} 行")
            if truncated_by_chars:
                parts.append(f"内容超过 {max_chars} 字符已截断，可用 offset 继续读")
            if sensitive:
                parts.append(f"⚠ 敏感文件：{why}")
            if warning:
                parts.append(warning)
            summary = "；".join(parts)

            data: dict[str, Any] = {
                "path": rp.relative,
                "content": numbered,
                "lines": total_lines,
                "range": [start_idx + 1, end_idx],
                "encoding": encoding,
                "size": size,
            }
            if truncated_by_chars:
                data["truncated"] = True
                data["next_offset"] = end_idx + 1
            if sensitive:
                data["sensitive"] = True
                data["sensitive_reason"] = why

            return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)

    # ---- 审批展示（只读工具其实不会进审批，但接口保持一致）------------------
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        return {
            "action": "读取文件",
            "target": str(args.get("path", "")),
            "impact": "只读操作，不修改任何文件",
            "preview": "",
        }


def make() -> Tool:
    return ReadFileTool()
