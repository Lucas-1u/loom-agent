"""用 CDP 给「Agent 工作树」页面截图（含真实鼠标悬停）。

为什么不直接用 `msedge --screenshot`：那个只能截静态首屏，看不到 tooltip。
这里起一个 headless Edge，通过 DevTools Protocol 发**真实鼠标移动事件**，
让 React 的 onMouseEnter 自然触发，再截图 —— 验证的是用户真会看到的画面。

跑法：python dev/shot_agent_tree.py [url]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
DEFAULT_URL = "http://localhost:5173/agent-tree.html"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")


class CDP:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0

    async def send(self, method: str, **params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=20)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})


def _wait_http(url: str, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f"等不到 {url}")


async def _shot(cdp: CDP, path: str) -> None:
    res = await cdp.send("Page.captureScreenshot", format="png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(res["data"]))
    print(f"  截图 -> {path}（{os.path.getsize(path) // 1024} KB）")


async def _nodes(cdp: CDP) -> list[dict]:
    res = await cdp.send(
        "Runtime.evaluate",
        expression=(
            "JSON.stringify([...document.querySelectorAll('.at-node')].map(n => {"
            "  const r = n.getBoundingClientRect();"
            "  return {label: (n.querySelector('.at-node__label')||n).textContent,"
            "          x: r.x + r.width/2, y: r.y + r.height/2};"
            "}))"
        ),
        returnByValue=True,
    )
    return json.loads(res["result"]["value"])


async def main() -> int:
    from websockets.asyncio.client import connect

    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    os.makedirs(OUT_DIR, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="edge-shot-")

    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1280,980",
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        _wait_http(f"http://127.0.0.1:{PORT}/json/version")
        targets = _wait_http(f"http://127.0.0.1:{PORT}/json/list")
        page = next(t for t in targets if t.get("type") == "page")
        async with connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.navigate", url=url)
            # 等首屏渲染完（React 挂载 + 字体就绪）
            for _ in range(40):
                await asyncio.sleep(0.25)
                r = await cdp.send("Runtime.evaluate",
                                   expression="document.querySelectorAll('.at-node').length",
                                   returnByValue=True)
                if (r["result"]["value"] or 0) > 0:
                    break
            await asyncio.sleep(0.6)

            print(f"页面：{url}")
            await _shot(cdp, os.path.join(OUT_DIR, "agent-tree-static.png"))

            nodes = await _nodes(cdp)
            print(f"  节点数：{len(nodes)}")

            # 悬停 1：第二层中间的节点（卡片朝右）
            mid = next((n for n in nodes if "前端分析" in n["label"]), None)
            if mid:
                await cdp.send("Input.dispatchMouseEvent", type="mouseMoved",
                               x=mid["x"], y=mid["y"], button="none")
                await asyncio.sleep(0.5)
                probe = await cdp.send(
                    "Runtime.evaluate",
                    expression=(
                        "(() => { const t = document.querySelector('.at-tip');"
                        " if (!t) return 'no-tip';"
                        " const r = t.getBoundingClientRect();"
                        " return JSON.stringify({x: Math.round(r.x), y: Math.round(r.y),"
                        " w: Math.round(r.width), h: Math.round(r.height),"
                        " vis: getComputedStyle(t).visibility}); })()"
                    ),
                    returnByValue=True,
                )
                print(f"  mid 节点 ({mid['x']:.0f},{mid['y']:.0f}) → tooltip {probe['result']['value']}")
                dump = await cdp.send(
                    "Runtime.evaluate",
                    expression=(
                        "JSON.stringify([...document.querySelectorAll('.at-node')].map(n => {"
                        "  const r = n.getBoundingClientRect();"
                        "  const l = n.querySelector('.at-node__label');"
                        "  return {label: l ? l.textContent : '?', left: Math.round(r.left),"
                        "          top: Math.round(r.top), right: Math.round(r.right),"
                        "          bottom: Math.round(r.bottom)}; }))"
                    ),
                    returnByValue=True,
                )
                print("  节点矩形：" + dump["result"]["value"])
                replay = await cdp.send(
                    "Runtime.evaluate",
                    expression=(
                        "(() => {"
                        " const anchor = [...document.querySelectorAll('.at-node')]"
                        "   .find(n => n.textContent.includes('前端分析'));"
                        " const a = anchor.getBoundingClientRect();"
                        " const t = document.querySelector('.at-tip');"
                        " const w = t.offsetWidth, h = t.offsetHeight;"
                        " const others = [...document.querySelectorAll('.at-node')]"
                        "   .filter(e => e !== anchor).map(e => e.getBoundingClientRect());"
                        " const left = a.right + 20, top = a.top + a.height/2 - h/2;"
                        " const hits = others.filter(r => !(left+w+6 <= r.left ||"
                        "   left >= r.right+6 || top+h+6 <= r.top || top >= r.bottom+6))"
                        "   .map(r => Math.round(r.left));"
                        " return JSON.stringify({left, top, w, h, n: others.length, hitLefts: hits});"
                        "})()"
                    ),
                    returnByValue=True,
                )
                print("  命中复算：" + replay["result"]["value"])
                await _shot(cdp, os.path.join(OUT_DIR, "agent-tree-hover-mid.png"))

            # 悬停 2：最右侧节点（验证卡片自动翻转，不顶出视口）
            right = max(nodes, key=lambda n: n["x"]) if nodes else None
            if right:
                await cdp.send("Input.dispatchMouseEvent", type="mouseMoved",
                               x=right["x"], y=right["y"], button="none")
                await asyncio.sleep(0.5)
                await _shot(cdp, os.path.join(OUT_DIR, "agent-tree-hover-right.png"))

            # 悬停 3：第三层叶子（竖排标签在色条下方，最容易"自己盖自己"）
            leaf = next((n for n in nodes if "依赖分析" in n["label"]), None)
            if leaf:
                await cdp.send("Input.dispatchMouseEvent", type="mouseMoved",
                               x=leaf["x"], y=leaf["y"], button="none")
                await asyncio.sleep(0.5)
                probe2 = await cdp.send(
                    "Runtime.evaluate",
                    expression=(
                        "(() => { const t = document.querySelector('.at-tip');"
                        " const r = t.getBoundingClientRect();"
                        " return JSON.stringify({x: Math.round(r.x), y: Math.round(r.y)}); })()"
                    ),
                    returnByValue=True,
                )
                print(f"  leaf 节点 ({leaf['x']:.0f},{leaf['y']:.0f}) → tooltip {probe2['result']['value']}")
                await _shot(cdp, os.path.join(OUT_DIR, "agent-tree-hover-leaf.png"))
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
