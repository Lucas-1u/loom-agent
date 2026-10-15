"""
list_files —— 列出工作区内某个目录的内容。

定位：Coding Agent 的「第一只眼睛」。任务开始时几乎总是先看目录结构。

设计要点
--------
1. **必须限制在工作区内**。所有路径走 resolve_in_workspace 校验。
2. **跳过噪音目录**（.git / node_modules / __pycache__ ...）。
   不是技术上做不到，而是**上下文预算**问题：node_modules 动辄 3 万个文件，
   列出来会把模型的上下文窗口塞满，且毫无信息量。
3. **输出是结构化的**（entries 列表），不是一段文本。
   这样程序可以统计、可以排序、可以被前端渲染成树，而模型看到的是 JSON。
4. **有上限**（max_entries）。目录太大时返回前 N 项 + 提示模型缩小范围。
   这就是「让模型知道该换策略」的机制，而不是硬失败。
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..path_safety import PathViolation, human_size, resolve_in_workspace
from ...agent.state import RiskLevel


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "列出工作区内某个目录的文件与子目录。返回每一项的名字、类型、大小。"
        "适合在任务开始时了解项目结构。不会递归进入子目录（只列一层）。"
        "若目录项过多，会只返回前若干项并提示缩小范围。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区根目录的路径。省略或传 '.' 表示工作区根目录。",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "是否显示以点开头的隐藏文件/目录，默认 false。",
            },
        },
        "required": [],
    }
    risk = RiskLevel.READ

    MAX_ENTRIES = 300
    MAX_DEPTH_GUARD = 0  # 只列一层

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 1. 参数 ---------------------------------------------------
            raw_path = args.get("path", ".") or "."
            show_hidden = bool(args.get("show_hidden", False))

            # ---- 2. 路径校验（安全边界）------------------------------------
            try:
                rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=True)
            except PathViolation as exc:
                kind = (
                    ErrorKind.PATH_OUTSIDE_WORKSPACE
                    if "越出工作区" in str(exc)
                    else ErrorKind.NOT_FOUND
                )
                hint = (
                    "只能访问工作区内的路径。请换一个工作区内的路径。"
                    if kind is ErrorKind.PATH_OUTSIDE_WORKSPACE
                    else "请先用 list_files('.') 确认实际存在的目录。"
                )
                return ToolResult.failure(self.name, kind, str(exc), hint=hint, duration_ms=t.ms)

            # ---- 3. 类型检查 -----------------------------------------------
            if not os.path.isdir(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_DIR,
                    f"不是一个目录：{rp.relative or '.'}",
                    hint="该路径是文件。若要读内容请用 read_file。",
                    duration_ms=t.ms,
                )

            # ---- 4. 遍历一层 -----------------------------------------------
            ignored = ctx.ignored_dir_names or frozenset()
            entries: list[dict[str, Any]] = []
            skipped_dirs: list[str] = []
            total_visible = 0

            try:
                with os.scandir(rp.absolute) as it:
                    for entry in it:
                        name = entry.name
                        if not show_hidden and name.startswith("."):
                            if name in ignored:
                                skipped_dirs.append(name)
                            continue

                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_link = entry.is_symlink()
                        except OSError:
                            continue

                        if is_dir and name in ignored:
                            skipped_dirs.append(name)
                            continue

                        total_visible += 1
                        if len(entries) >= self.MAX_ENTRIES:
                            continue

                        item: dict[str, Any] = {"name": name, "type": "dir" if is_dir else "file"}
                        if is_link:
                            item["symlink"] = True

                        if not is_dir:
                            try:
                                st = entry.stat(follow_symlinks=False)
                                item["size"] = st.st_size
                                item["size_h"] = human_size(st.st_size)
                            except OSError:
                                pass
                        entries.append(item)

            except PermissionError:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    f"系统拒绝访问：{rp.relative or '.'}",
                    hint="换个目录试试。",
                    duration_ms=t.ms,
                )
            except OSError as exc:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    f"读取目录失败：{exc}",
                    duration_ms=t.ms,
                )

            # ---- 5. 排序：目录在前，然后按名字（给模型稳定的输出）-----------
            entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))

            dirs = sum(1 for e in entries if e["type"] == "dir")
            files = len(entries) - dirs

            summary = f"{rp.relative or '.'} 下有 {dirs} 个目录、{files} 个文件"
            truncated = total_visible > self.MAX_ENTRIES
            if truncated:
                summary += f"（共 {total_visible} 项，已截断到 {self.MAX_ENTRIES} 项）"
            if skipped_dirs:
                summary += f"；已跳过 {len(skipped_dirs)} 个噪音目录"

            data: dict[str, Any] = {
                "path": rp.relative or ".",
                "dirs": dirs,
                "files": files,
                "entries": entries,
            }
            if truncated:
                data["truncated"] = True
                data["total_visible"] = total_visible

            return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)


def make() -> Tool:
    return ListFilesTool()
