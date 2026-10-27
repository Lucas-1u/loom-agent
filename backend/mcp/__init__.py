"""
MCP Client 集成（M4.1）。

第一版范围：**只支持本地 stdio Server**。
远程传输（streamable HTTP / SSE）、OAuth、多租户、连接池都不在本版内 ——
`McpManager` 把"发现"和"调用"收敛成两个方法，将来加传输只需要替换 `_Worker._run`。

对外只暴露三个东西：
    McpConfig / load_servers   配置与校验
    McpManager                 生命周期 + 发现 + 调用
    register_mcp_tools         把发现的工具注册进 ToolRegistry
"""

from .client import CallOutcome, DiscoveredTool, McpManager
from .config import McpConfig, McpConfigError, McpServerConfig, load_servers
from .tool import McpTool, qualify, register_mcp_tools

__all__ = [
    "CallOutcome", "DiscoveredTool", "McpManager",
    "McpConfig", "McpConfigError", "McpServerConfig", "load_servers",
    "McpTool", "qualify", "register_mcp_tools",
]
