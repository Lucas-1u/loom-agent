"""
测试用 MCP Server（M4.1 自检夹具）。

★ 它是什么
----------
一个**只读、无危险副作用**的本地 stdio Server，用官方 SDK 的 `MCPServer` 写成。
自检通过 `McpManager` 真实启动它（子进程 + JSON-RPC over stdio），
覆盖"连接 → 发现 → 调用 → 关闭"整条链路，而不是 mock 掉 transport。

★ 为什么要把每次调用写进计数文件
--------------------------
审批测试需要一条**可证伪**的断言："用户拒绝后工具一次都没执行"。
如果只看 Loom 的状态，无法区分"没执行"和"执行了但被上层丢弃"。
所以 Server 每次被调用都往 `$MCP_COUNTER_FILE` 追加一行，测试直接数行数。

★ 工具清单
----------
    echo(text)          回显，用于成功路径
    fail(text)          抛异常（Server 返回 isError），用于业务错误路径
    big(n)              返回 n 个字符，用于超大结果截断
    sleep_for(seconds)  睡眠，用于超时
    read_note(path)     读取 $MCP_TEST_DIR 下的 .txt，用于"真实只读工具"验收
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("loom-test-server")

COUNTER = os.environ.get("MCP_COUNTER_FILE", "")
BASE = Path(os.environ.get("MCP_TEST_DIR", ".")).resolve()


def _count(tool: str) -> None:
    if not COUNTER:
        return
    try:
        with open(COUNTER, "a", encoding="utf-8") as f:
            f.write(tool + "\n")
    except OSError:
        pass


@mcp.tool()
def echo(text: str) -> str:
    """原样回显输入文本（测试用）。"""
    _count("echo")
    return f"echo:{text}"


@mcp.tool()
def fail(text: str) -> str:
    """总是抛错（测试 Server 返回 isError 的路径）。"""
    _count("fail")
    raise RuntimeError(f"故意失败：{text}")


@mcp.tool()
def big(n: int) -> str:
    """返回 n 个字符（测试超大结果的处理）。"""
    _count("big")
    return "x" * max(0, min(int(n), 200_000))


@mcp.tool()
async def sleep_for(seconds: float) -> str:
    """睡眠若干秒（测试调用超时）。"""
    _count("sleep_for")
    await asyncio.sleep(max(0.0, min(float(seconds), 10.0)))
    return f"slept:{seconds}"


@mcp.tool()
def read_note(path: str) -> str:
    """读取测试目录下的 .txt 文件（只读）。"""
    _count("read_note")
    target = (BASE / path).resolve()
    if not str(target).startswith(str(BASE)):
        raise ValueError("路径越界")
    if target.suffix.lower() != ".txt":
        raise ValueError("只允许 .txt")
    return target.read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run(transport="stdio")
