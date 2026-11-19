"""验证 agent-desktop MCP 能否被 Loom 真实连接并发现桌面工具。

用 Loom 自己的 McpConfig.from_env() + McpManager 走完整启动链路，
不手搓协议，确保验证的就是线上真实路径。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()


async def main() -> int:
    from backend.mcp.config import McpConfig
    from backend.mcp.client import McpManager

    cfg = McpConfig.from_env()
    print("== McpConfig ==")
    print(f"enabled       = {cfg.enabled}")
    print(f"config_path   = {cfg.config_path}")
    print(f"startup_timeout_s = {cfg.startup_timeout_s}")
    print(f"call_timeout_s    = {cfg.call_timeout_s}")
    print(f"servers       = {[(s.server_id, s.command, s.args, s.enabled) for s in cfg.servers]}")

    if not cfg.enabled:
        print("\n[FAIL] LOOM_MCP 未开启")
        return 1
    if not cfg.servers:
        print("\n[FAIL] 没有可用 server（配置路径或 servers 数组有问题）")
        return 1

    mgr = McpManager(cfg)
    try:
        await mgr.start()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] McpManager.start 抛异常：{exc}")
        return 1

    print("\n== 连接后状态 ==")
    status = mgr.status()
    print(json.dumps(status, ensure_ascii=False, indent=2))

    ok = True
    for w in mgr._workers.values():  # noqa: SLF001 —— 验证脚本直接看 worker 详情
        print(f"\n-- worker {w.server_id}: state={w.state} tools={len(w.tools)}")
        if w.error:
            print(f"   error = {w.error}")
        if w.state != "ready":
            ok = False

    names = [t.name for t in mgr.tools()]
    print("\n== 发现的工具 ==")
    print(f"共 {len(names)} 个")
    for n in sorted(names):
        print(f"  - {n}")

    target = "desktop_mouse_click"
    if target in names:
        print(f"\n[PASS] 发现 {target} → desktop_click 会正常注册，模型能真实点击")
    else:
        print(f"\n[FAIL] 没发现 {target} → desktop_click 工厂返回 None，不会注册（这就是上次失败的根因）")
        ok = False

    # 顺带确认点击/输入类工具齐不齐
    for need in ("desktop_mouse_click", "desktop_type", "desktop_press", "desktop_drag"):
        if need not in names:
            print(f"[WARN] 缺 {need}（对应原生 desktop_* 可能不注册）")

    # ★ desktop_click 的 DPI 换算前提：它拿 list-displays 的 bounds/scale 把
    #   截图坐标换成物理坐标。这里把这两个值打出来，和截图尺寸对一下就知道会不会点偏。
    #   （2026-11-13 实测：bounds 2560×1600 scale 1.5，截图 1707×1067 → 1.5 倍吻合）
    if target in names:
        out = await mgr.call("agent_desktop", "desktop_list_displays", {}, timeout=20.0)
        print("\n== desktop_click 的换算前提（list-displays）==")
        if not out.ok:
            print(f"[WARN] 读不到显示器信息：{out.error}（desktop_click 会回退 2560×1600 / scale=1.0）")
        else:
            for d in (out.structured or {}).get("data") or []:
                b = d.get("bounds") or {}
                print(f"  {d.get('id')} primary={d.get('is_primary')} "
                      f"bounds={b.get('width')}x{b.get('height')} scale={d.get('scale')}")
            print("  核对：截图逻辑尺寸 × scale 应 ≈ 上面的物理尺寸；"
                  "不等就会点偏（先看 capture_screen 存的 PNG 尺寸）。")

    return 0 if ok else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    print(f"\nexit = {rc}")
    raise SystemExit(rc)
