"""用 CDP 给任意页面截一张图（可等某个选择器出现）。

用法：python dev/shot_page.py <url> <输出png> [等待选择器] [等待毫秒]

和 shot_agent_tree.py 的区别：那个要为"悬停"制造鼠标事件，这个是纯静态截图，
用来验统计页 / 主应用头部这种不需要交互的画面。
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
PORT = 9334


class CDP:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0

    async def send(self, method: str, **params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        # ★ 总时长上限，而不是每次 recv 给 25s：
        #   主应用（有 SSE + React）会持续推 console 之类的 CDP 事件，
        #   每来一条都重置 recv 的超时 —— 循环可以无限转下去，永远等不到自己的响应。
        deadline = time.time() + 20
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"{method} 等待响应超时（被其它事件淹没？）")
            raw = await asyncio.wait_for(self.ws.recv(), timeout=remain)
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


async def main() -> int:
    from websockets.asyncio.client import connect

    if len(sys.argv) < 3:
        print("用法: shot_page.py <url> <out.png> [selector] [wait_ms]")
        return 2
    url, out = sys.argv[1], sys.argv[2]
    selector = sys.argv[3] if len(sys.argv) > 3 else ""
    wait_ms = int(sys.argv[4]) if len(sys.argv) > 4 else 1200
    click_sel = sys.argv[5] if len(sys.argv) > 5 else ""

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    profile = tempfile.mkdtemp(prefix="edge-shot-")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--window-size=1280,900",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        _wait_http(f"http://127.0.0.1:{PORT}/json/version")
        page = next(t for t in _wait_http(f"http://127.0.0.1:{PORT}/json/list") if t.get("type") == "page")
        async with connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.navigate", url=url)

            if selector:
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    r = await cdp.send("Runtime.evaluate",
                                       expression=f"document.querySelectorAll({selector!r}).length",
                                       returnByValue=True)
                    if (r["result"]["value"] or 0) > 0:
                        break
            await asyncio.sleep(wait_ms / 1000)

            if click_sel:
                # 支持 "||" 分隔的连续点击：比如「打开弹窗 || 选中第二个选项」
                for sel in [x.strip() for x in click_sel.split("||") if x.strip()]:
                    expr = (
                        "(() => { const el = document.querySelector(" + repr(sel) + ");"
                        " if (!el) return 'not-found';"
                        " try { el.click(); return 'clicked'; }"
                        " catch (e) { return 'error: ' + e.message; } })()"
                    )
                    try:
                        r = await cdp.send("Runtime.evaluate", expression=expr, returnByValue=True)
                        print(f"点击 {sel} → {r['result']['value']}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"点击失败（继续）：{type(exc).__name__}: {exc}")
                    await asyncio.sleep(1.4)      # 秒！等 UI 响应 / 拉 preview

            res = await cdp.send("Page.captureScreenshot", format="png")
            with open(out, "wb") as f:
                f.write(base64.b64decode(res["data"]))
            print(f"截图 -> {out}（{os.path.getsize(out) // 1024} KB）")

            # 顺便把页面上的文字摘要打出来，方便确认"渲染了但不是白屏"
            r = await cdp.send("Runtime.evaluate",
                               expression="(document.body.innerText||'').replace(/\\s+/g,' ').slice(0,300)",
                               returnByValue=True)
            print("页面文字：", r["result"]["value"])
            return 0
    finally:
        # ★ Windows 上 proc.terminate() 对 msedge 基本无效（它自己有进程树），
        #   异常路径里会一直挂住，最后被外层超时 SIGTERM 杀掉、什么都看不到。
        #   直接 taskkill /T 杀整棵树。
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
