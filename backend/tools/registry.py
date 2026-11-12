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

    def without(self, name: str) -> "ToolRegistry":
        """
        派生一份「剔除单个工具」的注册表，**保留原分组与工具实例**。

        ★ 用途：任务级能力开关（如联网搜索关闭时剔除 web_search）。
          与 `filter()` 的区别：filter 会把幸存工具全塞进 group="filtered"，
          丢掉 readonly/mutating/exec 这些语义；这里原样保留。
        ★ 不修改自身：返回新实例，主注册表不受影响（多任务并发共用一份注册表，
          就地删工具会让开关失效）。
        """
        sub = ToolRegistry()
        for group, names in self._groups.items():
            for n in names:
                if n == name:
                    continue
                tool = self._tools.get(n)
                if tool is not None:
                    sub.register(tool, group=group)
        return sub

    def readonly_view(self) -> "ToolRegistry":
        """
        派生一份「只读视图」：只保留 risk=read 的工具（保留原分组）。

        ★ 步骤 7：read-only 会话模式用它做**服务端真强制**。
          之前 read-only 只在事件里声明（`enforced: false`），工具注册表里
          仍有写/执行工具，模型照样能写 —— 等于假安全。

        ★ 为什么复用同一批工具实例，而不是重新 build 一份：
          `build_default_registry` 会重建 terminal / MCP 工具。MCP 工具里
          持有 Server 会话引用，重建虽然能用，但会多一份注册开销且容易和
          主注册表的状态漂移。这里只做**筛选**，工具对象完全一致。

        ★ 为什么按 risk 而不是按 group：
          group 只能区分 readonly/mutating/exec/subagent/workflow，
          而 MCP 工具与 spawn 工具的风险是**实例属性**，可能配置成 write。
          按 risk 筛才能兜住这些「分组正确但风险高」的工具。
        """
        sub = ToolRegistry()
        for group, names in self._groups.items():
            for n in names:
                tool = self._tools.get(n)
                # 用 .value 比较，避免 registry 反向 import agent.state
                if tool is not None and getattr(tool.risk, "value", "") == "read":
                    sub.register(tool, group=group)
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
                        "approval": "无审批",  # 步骤 5：审批已移除
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
    workflow: Any = None,
    workflow_allow_local: bool = False,
    workflow_parallel: int = 3,
    vision: Any = None,
    search: Any = None,
    skills: bool = True,
) -> ToolRegistry:
    """
    构建默认工具集。

        readonly : list_files, read_file, capture_screen, load_skill
                   (+ vision_look 仅配置了 LOOM_VISION_MODEL 时)
                   (+ desktop_locate 仅配置视觉模型时注册)
                   (+ search_docs 仅在注入知识库时可用)
                   (+ web_search 仅在注入 search 且已配置 key 时可用)
        mutating : write_file, edit_file, delete_file       （无审批，安全边界由沙箱承担）
        exec     : run_command, terminal                     （Docker 沙箱 / 宿主机 shell）
        computer : desktop_click（agent-desktop 在线时注册）

    参数 include_mutating 允许「只读模式」：实验时可一键关掉所有写/执行能力。
    sandbox 允许外部注入 SandboxManager（服务端统一管理容器生命周期；
    不传则工具自己建一个）。
    knowledge 允许注入项目知识库（M3）：**不传就不注册 search_docs** ——
    这样离线演示、单测、没开 RAG 的部署里，工具清单与以前完全一致。
    mcp 允许注入 MCP 管理器（M4.1）：**不传就不注册任何 MCP 工具**。
    spawner 允许注入子任务派生器（M5）：**不传就不注册 spawn_subtask**。
    vision 允许注入视觉模型配置：**不配置就不注册 vision_look / desktop_locate**。
    search 允许注入联网搜索配置（对比表 #21）：**不注入或未配 key 就不注册 web_search**。
    skills 控制是否注册 load_skill（对比表 #24）：LOOM_SKILLS=0 时传 False。
      ★ 这里只决定"有没有这个入口"，具体能看到哪些技能是**按任务工作区**扫描的
        （library 挂在 ToolContext 上），所以注册表不必为每个任务重建。
    """
    from .readonly.list_files import make as make_list
    from .readonly.read_file import make as make_read

    reg = ToolRegistry()
    reg.register(make_list(), group="readonly")
    reg.register(make_read(), group="readonly")
    # ★ Skill：按名载入技能正文（只读；read-only 任务也该能用）
    if skills:
        from .readonly.skill import make as make_skill
        reg.register(make_skill(), group="readonly")
    # ★ Computer-Use 方案2：本地截屏（纯观察，默认常开）
    from .readonly.capture_screen import make as make_capture
    reg.register(make_capture(), group="readonly")
    # ★ ask_user：Agent 主动向用户提问（通用能力，read-only 也能用）
    from .readonly.ask_user import make as make_ask
    reg.register(make_ask(), group="readonly")
    # ★ Computer-Use 方案2：视觉问答读图（不配置视觉模型就不注册，
    #   工具清单与之前完全一致）
    if vision is not None and getattr(vision, "usable", False):
        from .readonly.vision_query import make as make_vision
        make_v = make_vision(vision)
        if make_v is not None:
            reg.register(make_v, group="readonly")
    # ★ Computer-Use 方案2：桌面定位（视觉模型找坐标，纯只读；
    #   视觉未配置时不注册。
    if vision is not None and getattr(vision, "usable", False):
        from .computer.locate import make as make_locate
        make_l = make_locate(vision)
        if make_l is not None:
            reg.register(make_l, group="computer")

    if knowledge is not None and getattr(knowledge, "usable", False):
        from .readonly.search_docs import make as make_search
        reg.register(make_search(knowledge), group="readonly")

    # ★ 对比表 #21：联网搜索（DeepSeek Search API）。
    #   不注入 / 未开总开关 / 未配 key → 不注册，工具清单与历史完全一致。
    if search is not None and getattr(search, "enabled", False) and getattr(search, "api_key", ""):
        from .readonly.web_search import make as make_web_search
        make_ws = make_web_search(
            base_url=search.base_url, api_key=search.api_key, timeout=search.timeout,
        )
        if make_ws is not None:
            reg.register(make_ws, group="readonly")

    if spawner is not None:
        # M5：父子任务（不传 spawner 就没有这个工具，行为与 M4 完全一致）
        from .mutating.spawn_subtask import make as make_spawn
        reg.register(make_spawn(spawner), group="subagent")
        # P1-#42：并行派生多个子任务
        from .mutating.spawn_parallel import make as make_spawn_parallel
        reg.register(make_spawn_parallel(spawner), group="subagent")

    if mcp is not None:
        # MCP 工具走同一套注册表；风险等级由配置决定（默认 WRITE=需审批）。
        # 注册失败/重名不会抛出来拖垮启动，而是记在报告里（启动日志可见）。
        from ..mcp.tool import register_mcp_tools
        report = register_mcp_tools(reg, mcp)
        if report["skipped"]:
            import logging
            logging.getLogger("loom.mcp").warning("跳过 %d 个 MCP 工具：%s",
                                                  len(report["skipped"]), report["skipped"])
        # ★ Computer-Use 方案2：像素点击工具（agent-desktop 在线时注册）
        from .computer.click_pixel import make as make_click
        make_c = make_click(mcp)
        if make_c is not None:
            reg.register(make_c, group="computer")

    if include_mutating:
        from .exec.run_command import make as make_run
        from .mutating.edit_file import make as make_edit
        from .mutating.delete_file import make as make_delete
        from .mutating.write_file import make as make_write

        # ★ 注册顺序不影响模型看到的顺序（to_specs 会按名字排序），
        #   这里 write 在前只是因为它是先写的。
        reg.register(make_write(), group="mutating")
        reg.register(make_edit(), group="mutating")
        # delete_file：删除前自动备份，只删文件不删目录。
        reg.register(make_delete(), group="mutating")
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

        # ★ M6：异步工作流（不传 store 就不注册；离线测试与旧部署行为不变）
        if workflow is not None:
            from .exec.run_workflow import make as make_workflow
            reg.register(make_workflow(workflow, reg, allow_local=workflow_allow_local,
                                       max_parallel=workflow_parallel),
                         group="workflow")

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
