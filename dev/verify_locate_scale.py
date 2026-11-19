"""A/B：同一张截图，「原图直发」vs「先降到 1MP 再发」的定位误差对比。

背景：主流 VLM 的图片入口有像素上限，超限会被服务端悄悄降采样；模型按缩小图
给坐标、harness 按原图执行 → 系统性点偏。这个脚本用真实截图 + 真实视觉模型量化它。

跑法：./.venv/Scripts/python.exe dev/verify_locate_scale.py [截图路径]
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

DEFAULT_IMG = r"D:/nothin/capture/screen_20261113_162921.png"
JOB = "桌面左下角的网易云音乐图标（红色圆形，抖音图标右边）"
TRUTH = (165, 920)   # 人工从截图上量的图标中心（逻辑像素）


def _dist(p: tuple[int, int], q: tuple[int, int]) -> float:
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


async def _ask(cfg, data: bytes, w: int, h: int) -> dict | None:
    from backend.models.vision import describe_image
    from backend.tools.computer.locate import _parse_coords

    prompt = (
        f"这张截图的像素尺寸是 {w}×{h}（宽×高）。"
        f"你输出的坐标必须**以这张 {w}×{h} 图片的左上角为原点**，"
        f"取值范围 x∈[0,{w - 1}]、y∈[0,{h - 1}]。\n"
        f"请在这张截图中找到：「{JOB}」。\n"
        "如果它存在，输出 JSON 格式："
        "{\"action\":\"click\",\"x\":整数,\"y\":整数,\"reason\":\"简短中文理由\"}\n"
        "坐标请给出**目标元素的中心点**；如果找不到它，输出 {\"action\":\"none\"}。"
        "只输出 JSON，不要任何其他文字。"
    )
    reply = await describe_image(cfg, data, prompt)
    print(f"    模型原文：{reply.text.strip()[:160]}")
    return _parse_coords(reply.text)


async def main() -> int:
    from backend.models.vision import VisionConfig
    from backend.tools.computer.locate import _fit_size, _png_size, _shrink_png

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMG
    raw = open(path, "rb").read()
    W, H = _png_size(raw) or (0, 0)
    print(f"截图 = {path}\n尺寸 = {W}×{H}（{W * H / 1e6:.2f}MP）  真值 = {TRUTH}")
    if not (W and H):
        print("[FAIL] 读不出尺寸")
        return 1

    cfg = VisionConfig.from_env()
    if not cfg.usable:
        print("[FAIL] 视觉模型未配置")
        return 1

    nw, nh, sc = _fit_size(W, H)
    small = _shrink_png(raw, (nw, nh))
    print(f"降采样目标 = {nw}×{nh}（scale {sc:.4f}，{nw * nh / 1e6:.2f}MP）"
          f"，重编码{'成功' if small else '失败'}")

    print("\n[A] 原图直发（模拟修复前的行为）")
    a = await _ask(cfg, raw, W, H)
    print("\n[B] 降到 1MP 再发（修复后）")
    b = await _ask(cfg, small, nw, nh) if small else None

    print("\n" + "=" * 58)
    ok = True
    if a and a.get("action") != "none":
        pa = (a["x"], a["y"])
        print(f"A 原图   → {pa}   误差 {_dist(pa, TRUTH):.0f}px")
    else:
        print("A 原图   → 没解析出坐标")
        ok = False
    if b and b.get("action") != "none":
        pb = (round(b["x"] * W / nw), round(b["y"] * H / nh))
        print(f"B 缩放   → 模型给 ({b['x']},{b['y']})，还原 {pb}   误差 {_dist(pb, TRUTH):.0f}px")
    else:
        print("B 缩放   → 没解析出坐标")
        ok = False
    if ok and a and b:
        ea = _dist((a["x"], a["y"]), TRUTH)
        eb = _dist(pb, TRUTH)
        print(f"\n结论：{'缩放版更准' if eb < ea else '缩放版没有改善'}（{ea:.0f}px → {eb:.0f}px）")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
