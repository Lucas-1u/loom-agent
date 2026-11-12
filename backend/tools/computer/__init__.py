"""computer-use 方案2 闭环工具组（Loom 工具层）。

★ 工具集合：
    locate     （desktop_locate）: 截图 + 视觉模型 → 结构化坐标 JSON（纯只读）
    click      （desktop_click）  : 执行像素坐标点击（WRITE 风险，走 agent-desktop MCP 代理）

★ 依赖注入约定（对齐 build_default_registry 的 mcp/vision 参数）：
    · desktop_locate: 需要在 config 中注入 vision
    · desktop_click : 需要注入 MCP manager（agent-desktop 在线时才注册，否则不存在）
    → 工厂返回 None 表示"本次不注册"，工具清单維持一致，符合"不注入就不注册"。

★ 闭环结构：
    capture_screen → desktop_locate（VLM 坐标 JSON）→ desktop_click（执行点击）
    → 再截一次 → 再 locate 一次 —— 验证闭环（状态变化可见）
"""

from .locate import DesktopLocateTool, make as make_locate
from .click_pixel import DesktopClickTool, make as make_click

__all__ = ["DesktopLocateTool", "make_locate", "DesktopClickTool", "make_click"]
