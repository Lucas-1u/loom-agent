"""
Loom Agent v0.1 · 工具注册表

★ 老项目经验（tool_registry.py）的升级版
────────────────────────────────────────────────────────────────
老项目：`REGISTRY = {name: {"schema": ..., "handler": ...}}`，加工具 3 步。
这个思路是对的（集中注册、单一来源），本文件保留并强化三点：

  ① **注册即校验**：注册时检查名字非空、description 非空、parameters 是合法 object schema。
     老项目没校验，工具写错 schema 要到运行时才炸。

  ② **分组**：工具按 readonly / mutating 分组注入，便于「实验性地只开放只读工具」
     （比如给不可信任务降权），也便于文档生成。

  ③ **可枚举**：`to_specs(only=...)` 能给出一份「本次任务允许的工具清单」。
     这是 Policy 层做工具白名单的基础。

依赖方向：registry 只 import tools/base，不 import agent/。
（Policy 在 agent/ 里调 registry，方向是 agent → tools，正确。）
"""

from __future__ import annotations

import logging
from typing import Iterable

from .base import Tool, ToolContext, ToolResult

log = logging.getLogger("loom.tools")


class ToolRegistryError(Exception):
    pass


class ToolRegistry:
    """
    工具注册表。

    用法：
        reg = ToolRegistry()
        reg.register(ListFilesTool(), group="readonly")
        reg.register(ReadFileTool(), group="readonly")
        tools = reg.to_specs()          # 给模型看的声明列表
        tool = reg.get("read_file")
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._groups: dict[str, list[str]] = {}

    # ---- 注册 --------------------------------------------------------------
    def register(self, tool: Tool, *, group: str = "default") -> None:
        if not tool.name:
            raise ToolRegistryError(f"工具缺少 name：{tool!r}")
        if not tool.description:
            raise ToolRegistryError(f"工具 '{tool.name}' 缺少 description（模型需要它来选择工具）")
        if tool.name in self._tools:
            raise ToolRegistryError(f"工具名重复注册：{tool.name}")

        schema = tool.parameters
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ToolRegistryError(
                f"工具 '{tool.name}' 的 parameters 必须是 type=object 的 JSON Schema"
            )

        self._tools[tool.name] = tool
        self._groups.setdefault(group, []).append(tool.name)
        log.debug("注册工具 %s（group=%s, risk=%s）", tool.name, group, tool.risk.value)

    def register_many(self, tools: Iterable[Tool], *, group: str = "default") -> None:
        for t in tools:
            self.register(t, group=group)

    def unregister(self, name: str) -> bool:
        tool = self._tools.pop(name, None)
        if tool is None:
            return False
        for names in self._groups.values():
            if name in names:
                names.remove(name)
        return True

    # ---- 查询 --------------------------------------------------------------
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolRegistryError(f"未注册的工具：{name}")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    def group(self, name: str) -> list[str]:
        return list(self._groups.get(name, []))

    def filter(self, *, groups: Iterable[str] | None = None, names: Iterable[str] | None = None) -> "ToolRegistry":
        """
        派生一个子注册表。用途：
          · 「只读子集」给低信任任务
          · 测试里只装 1 个工具，隔离验证
        """
        sub = ToolRegistry()
        allowed: set[str] = set()
        if groups is not None:
            for g in groups:
                allowed.update(self._groups.get(g, []))
        if names is not None:
            allowed.update(names)
        for n in allowed:
            if n in self._tools:
                sub.register(self._tools[n], group="filtered")
        return sub

    # ---- 给模型 ------------------------------------------------------------
    def to_specs(self, *, only: Iterable[str] | None = None) -> list:
        """
        生成给模型看的工具声明列表。

        排序：按名字 —— 保证同一任务多次调用时顺序稳定，
        这样 prompt 前缀缓存（如果供应商支持）能命中，也便于比对不同模型的行为。
        """
        keys = list(self._tools.keys()) if only is None else [n for n in only if n in self._tools]
        keys.sort()
        return [self._tools[n].to_spec() for n in keys]

    def describe(self) -> list[dict[str, str]]:
        """给人看的工具清单（CLI 启动时打印）。"""
        out = []
        for group, names in sorted(self._groups.items()):
            for n in sorted(names):
                t = self._tools[n]
                out.append(
                    {
                        "name": t.name,
                        "group": group,
                        "risk": t.risk.value,
                        "approval": "需要审批" if t.needs_approval else "自动执行",
                        "description": t.description.split("。")[0],
                    }
                )
        return out


# --------------------------------------------------------------------------- #
# 默认注册表（v0.1a 工具集）
# --------------------------------------------------------------------------- #
def build_default_registry(
    *,
    include_mutating: bool = True,
    sandbox: "SandboxManager | None" = None,
    knowledge: "KnowledgeBase | None" = None,
    mcp: "McpManager | None" = None,
    spawner: Any = None,
    terminal: bool | None = None,
) -> ToolRegistry:
    """
    构建默认工具集。

        readonly : list_files, read_file (+ search_docs 仅在注入知识库时可用)
        mutating : write_file, edit_file       （需审批）
        exec     : run_command                 （需审批 + Docker 沙箱）

    参数 include_mutating 允许「只读模式」：实验时可一键关掉所有写/执行能力。
    sandbox 允许外部注入 SandboxManager（服务端统一管理容器生命周期；
    不传则工具自己建一个）。
    knowledge 允许注入项目知识库（M3）：**不传就不注册 search_docs** ——
    这样离线演示、单测、没开 RAG 的部署里，工具清单与以前完全一致。
    mcp 允许注入 MCP 管理器（M4.1）：**不传就不注册任何 MCP 工具**。
    spawner 允许注入子任务派生器（M5）：**不传就不注册 spawn_subtask**。
    """
    from .readonly.list_files import make as make_list
    from .readonly.read_file import make as make_read

    reg = ToolRegistry()
    reg.register(make_list(), group="readonly")
    reg.register(make_read(), group="readonly")
    if knowledge is not None and getattr(knowledge, "usable", False):
        from .readonly.search_docs import make as make_search
        reg.register(make_search(knowledge), group="readonly")

    if spawner is not None:
        # M5：父子任务（不传 spawner 就没有这个工具，行为与 M4 完全一致）
        from .mutating.spawn_subtask import make as make_spawn
        reg.register(make_spawn(spawner), group="subagent")

    if mcp is not None:
        # MCP 工具走同一套注册表；风险等级由配置决定（默认 WRITE=需审批）。
        # 注册失败/重名不会抛出来拖垮启动，而是记在报告里（启动日志可见）。
        from ..mcp.tool import register_mcp_tools
        report = register_mcp_tools(reg, mcp)
        if report["skipped"]:
            import logging
            logging.getLogger("loom.mcp").warning("跳过 %d 个 MCP 工具：%s",
                                                  len(report["skipped"]), report["skipped"])

    if include_mutating:
        from .exec.run_command import make as make_run
        from .mutating.edit_file import make as make_edit
        from .mutating.write_file import make as make_write

        # ★ 注册顺序不影响模型看到的顺序（to_specs 会按名字排序），
        #   这里 write 在前只是因为它是先写的。
        reg.register(make_write(), group="mutating")
        reg.register(make_edit(), group="mutating")
        # ★ run_command 单独分组：它的风险不在"改文件"，而在"能执行任意程序"，
        #   安全边界是 Docker 容器（见 backend/sandbox/manager.py）。
        reg.register(make_run(sandbox), group="exec")

        # ★ M6：宿主机终端（PowerShell / CMD / Git Bash）。默认开启，
        #   LOOM_TERMINAL=0 可关闭。它不走 Docker：安全边界是审批 + 工作目录 + 超时。
        import os as _os
        _terminal_on = (terminal if terminal is not None
                        else _os.environ.get("LOOM_TERMINAL", "1").lower()
                        not in ("0", "false", "off", "no"))
        if _terminal_on:
            from .exec.terminal import make as make_terminal
            reg.register(make_terminal(), group="exec")

    return reg


# --------------------------------------------------------------------------- #
# 统一执行入口（带计时与异常兜底）
# --------------------------------------------------------------------------- #
async def execute(reg: ToolRegistry, name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """
    执行工具的唯一入口。

    为什么要有这个函数，而不是让 Agent 直接 `tool.run(args, ctx)`？
      ① 统一 catch 所有异常 → 转成 ToolResult（工具内部 bug 不该炸掉整个 Agent）
      ② 统一处理「工具不存在」
      ③ 将来加钩子（如审计日志、限流）只需改这里一处

    ★ 注意：这个函数**不做安全校验**。校验是 Policy 的职责，
      必须在校验通过之后才调用这里。顺序不能颠倒。
    """
    from .base import ErrorKind, timed

    tool = reg.get(name)
    if tool is None:
        return ToolResult.failure(
            name,
            ErrorKind.NOT_IMPLEMENTED,
            f"没有名为 '{name}' 的工具",
            hint=f"可用工具：{', '.join(sorted(reg.names))}",
        )

    with timed() as t:
        try:
            result = await tool.run(args, ctx)
        except Exception as exc:  # noqa: BLE001 —— 工具内部 bug 必须被隔离
            log.exception("工具 %s 抛出未捕获异常", name)
            return ToolResult.failure(
                name,
                ErrorKind.INTERNAL_ERROR,
                f"工具内部异常：{type(exc).__name__}: {exc}",
                duration_ms=t.ms,
            )

    if result.duration_ms == 0:
        result.duration_ms = t.ms
    return result
