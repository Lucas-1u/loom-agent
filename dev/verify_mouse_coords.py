"""验证 agent-desktop 的 mouse-move/mouse-click 吃的是物理坐标还是逻辑坐标。

方法：读光标（DPI-aware 的真实物理位置）→ mouse-move 到 (150,1290) → 再读 → 移回原位。
判据：
  · 读回 (150,1290) → 它吃物理坐标（我们的换算正确）
  · 读回 (225,1935) 或接近屏幕底边 → 它吃逻辑坐标（我们多乘了一次 1.5，点到任务栏去了）
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

# 本进程声明 Per-Monitor DPI aware，GetCursorPos 才会返回物理像素
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    ctypes.windll.user32.SetProcessDPIAware()

_user32 = ctypes.windll.user32


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def screen_size() -> tuple[int, int]:
    return int(_user32.GetSystemMetrics(0)), int(_user32.GetSystemMetrics(1))


async def main() -> int:
    from backend.mcp.client import McpManager
    from backend.mcp.config import McpConfig

    print(f"物理屏幕（DPI-aware）= {screen_size()}")
    x0, y0 = cursor_pos()
    print(f"移动前光标 = ({x0},{y0})")

    mgr = McpManager(McpConfig.from_env())
    await mgr.start()
    try:
        target = (150, 1290)   # desktop_click_at 换算出来的物理坐标
        out = await mgr.call("agent_desktop", "desktop_mouse_move",
                             {"xy": f"{target[0]},{target[1]}"}, timeout=20.0)
        print(f"mouse-move xy={target[0]},{target[1]} → ok={out.ok} err={out.error}")
        await asyncio.sleep(0.4)
        x1, y1 = cursor_pos()
        print(f"移动后光标 = ({x1},{y1})")

        print("\n判定：")
        if (x1, y1) == target:
            print("  → 吃的是**物理坐标**。desktop_click_at 的 ×scale 换算正确。")
        elif abs(x1 - int(target[0] * 1.5)) <= 3 and abs(y1 - int(target[1] * 1.5)) <= 3:
            print("  → 吃的是**逻辑坐标**！它内部又乘了一次 1.5 —— desktop_click_at 多乘了。")
        elif y1 >= screen_size()[1] - 5:
            print("  → 被夹到屏幕底边：说明它按逻辑坐标理解后越界（1290 > 逻辑高度）。")
        else:
            print("  → 其他映射，需要进一步查。")

        # 移回原位
        await mgr.call("agent_desktop", "desktop_mouse_move", {"xy": f"{x0},{y0}"}, timeout=20.0)
        await asyncio.sleep(0.3)
        print(f"已移回 ({cursor_pos()})")
        return 0
    finally:
        await mgr.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
