"""
M4.1 MCP Client 自检（真实 stdio 子进程，不是 mock transport）。

跑法（在 loom/ 目录下）：
    ./.venv/Scripts/python.exe -m backend.selfcheck_mcp
    ./.venv/Scripts/python.exe -B -m backend.selfcheck_mcp -v

★ 为什么用真实子进程
------------------
mock 掉 transport 只能证明"我们的代码调了自己的桩"。这一套会真的启动
`backend/mcp/testing/server.py`（官方 SDK 写的 stdio Server），走完整
JSON-RPC 链路：连接 → 发现 → 调用 → 关闭。代价是启动慢 1~2 秒，值得。

★ 可证伪断言
----------
审批相关的用例不看 Loom 自己的状态，而是数**Server 侧**的调用计数文件：
用户拒绝 → 计数文件里零行；批准 → 恰好一行。这样"没执行"不是猜的。

覆盖：
    [A] 配置解析与安全边界（shell 控制符 / args 类型 / 重复 id / 凭据 env）
    [B] 连接与工具发现
    [C] 统一注册（命名 / 来源元数据 / 默认要审批 / read_tools 授权 / 重名跳过）
    [D] 调用成功与结果归一化（文本 + 结构化 + 来源）
    [E] Server 返回业务错误 → 不当作成功
    [F] 超大结果有界截断
    [G] 调用超时 → 明确失败并标记不可用
    [H] 审批：拒绝=零执行，批准=一次执行
    [I] 事件：tool.call 带 source，tool.result 成功
    [J] 关闭后不可调用；重复关闭幂等
    [K] Graph 与 Legacy 行为一致
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.agent.approval import ApprovalManager
from backend.agent.core import AgentConfig, AgentCore
from backend.events import EventBus, collect
from backend.mcp.client import McpManager
from backend.mcp.config import (McpConfig, McpConfigError, build_child_env, load_servers)
from backend.mcp.tool import McpTool, qualify, register_mcp_tools
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.tools.base import ToolContext
from backend.tools.registry import ToolRegistry

_results: list[tuple[bool, str]] = []


def check(cond: Any, label: str) -> bool:
    ok = bool(cond)
    _results.append((ok, label))
    print(f"  [{'ok' if ok else '!!'}] {label}")
    return ok


def tc(cid: str, name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=cid, name=name,
                    arguments_raw=json.dumps(args, ensure_ascii=False), arguments=args)


SERVER_SCRIPT = Path(__file__).resolve().parent / "mcp" / "testing" / "server.py"


def _write_cfg(tmp: Path, *, extra: dict | None = None, trust: str = "prompt",
               read_tools: list[str] | None = None) -> tuple[str, str, str]:
    """写一份可用的配置，返回 (配置路径, 计数器路径, 笔记目录)。"""
    tmp.mkdir(parents=True, exist_ok=True)
    counter = str(tmp / "calls.txt")
    notes = tmp / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "hello.txt").write_text("你好，这是笔记。\n", encoding="utf-8")

    server = {
        "id": "demo",
        "command": sys.executable,
        "args": ["-B", str(SERVER_SCRIPT)],
        "env": {"MCP_COUNTER_FILE": counter, "MCP_TEST_DIR": str(notes)},
        "enabled": True,
        "trust": trust,
    }
    if read_tools is not None:
        server["read_tools"] = read_tools
    if extra:
        server.update(extra)
    path = tmp / "mcp_servers.json"
    path.write_text(json.dumps({"servers": [server]}, ensure_ascii=False), encoding="utf-8")
    return str(path), counter, str(notes)


def _lines(path: str) -> list[str]:
    try:
        return [x for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
# [A] 配置与安全边界
# --------------------------------------------------------------------------- #
def suite_config(tmp: Path) -> None:
    print("\n[A] 配置解析与安全边界")

    path, counter, notes = _write_cfg(tmp / "cfg_ok")

    servers = load_servers(path)
    check(len(servers) == 1 and servers[0].server_id == "demo", "合法配置能解析出 1 个 Server")
    check(servers[0].trust == "prompt", "缺省 trust=prompt（默认要审批）")
    check(servers[0].command == sys.executable, "command 与 args 分开保存")

    env = build_child_env(servers[0])
    check(env.get("MCP_COUNTER_FILE") == counter, "配置里的 env 会传给子进程")
    check("PYTHONIOENCODING" in env, "默认注入 PYTHONIOENCODING（避免中文乱码）")

    os.environ["LOOM_TEST_SECRET_TOKEN"] = "should-not-leak"
    try:
        env2 = build_child_env(servers[0])
        check("LOOM_TEST_SECRET_TOKEN" not in env2, "★ 父进程的敏感 env 不会自动继承")
    finally:
        os.environ.pop("LOOM_TEST_SECRET_TOKEN", None)

    def bad(payload: dict, label: str) -> None:
        p = tmp / f"bad_{label}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            load_servers(str(p))
            check(False, f"非法配置被拒绝：{label}")
        except McpConfigError:
            check(True, f"非法配置被拒绝：{label}")

    bad({"servers": [{"id": "x", "command": "python a.py && rm -rf /"}]}, "shell 控制符")
    bad({"servers": [{"id": "x", "command": "python", "args": "not-a-list"}]}, "args 非数组")
    bad({"servers": [{"id": "X-BAD", "command": "python"}]}, "id 非法")
    bad({"servers": [{"id": "a", "command": "python"}, {"id": "a", "command": "python"}]}, "id 重复")
    bad({"servers": [{"id": "a", "command": "python",
                      "env": {"OPENAI_API_KEY": "sk-x"}}]}, "凭据 env")
    bad({"servers": "nope"}, "servers 非数组")
    check(not (tmp / "cfg_ok" / "mcp_servers.json").read_text(encoding="utf-8").count("sk-"),
          "配置文件里没有密钥字面量")


# --------------------------------------------------------------------------- #
# [B]-[F] 连接 / 发现 / 注册 / 调用
# --------------------------------------------------------------------------- #
async def suite_connect(tmp: Path) -> tuple[McpManager, str, str]:
    print("\n[B] 连接与工具发现")
    path, counter, notes = _write_cfg(tmp / "run", read_tools=["read_note"])
    cfg = McpConfig(enabled=True, config_path=path, servers=load_servers(path),
                    call_timeout_s=8.0, startup_timeout_s=25.0)
    mgr = McpManager(cfg)
    await mgr.start()

    status = mgr.status()
    check(status["servers"] and status["servers"][0]["state"] == "ready",
          f"Server 连上了（{status['servers'][0]['state'] if status['servers'] else '无'}）")
    names = {t.name for t in mgr.tools()}
    check({"echo", "fail", "big", "sleep_for", "read_note"} <= names,
          f"发现了全部工具（{sorted(names)}）")
    check(not status["servers"][0]["error"], "没有连接错误")

    print("\n[C] 统一注册")
    reg = ToolRegistry()
    report = register_mcp_tools(reg, mgr)
    check(len(report["registered"]) == 5, f"注册了 5 个工具（{len(report['registered'])}）")
    check("mcp__demo__echo" in reg.names, "★ 注册名带 Server 前缀（mcp__demo__echo）")
    tool = reg.get("mcp__demo__echo")
    check(isinstance(tool, McpTool), "注册进的是 Loom 的 Tool 子类")
    check(tool.source == {"kind": "mcp", "server": "demo", "tool": "echo"},
          f"★ 工具自带来源元数据（{tool.source}）")
    check(tool.risk.value == "write" and tool.needs_approval,
          "★ 默认风险 = write（未知风险工具必须审批）")
    read_tool = reg.get("mcp__demo__read_note")
    check(read_tool.risk.value == "read" and not read_tool.needs_approval,
          "配置里 read_tools 指定的工具按只读处理")
    check(tool.parameters.get("type") == "object" and "text" in tool.parameters.get("properties", {}),
          "inputSchema 原样保留（含 properties）")

    # 重复注册：同名必须被跳过，而不是覆盖内置/其它 Server
    report2 = register_mcp_tools(reg, mgr)
    check(not report2["registered"] and len(report2["skipped"]) == 5,
          f"重复注册被跳过（skipped={len(report2['skipped'])}）")
    check(reg.get("mcp__demo__echo") is tool, "已注册的实例没有被替换")

    # 与内置工具共存：MCP 名字带前缀，不会挤掉内置工具
    from backend.tools.registry import build_default_registry
    reg2 = build_default_registry(include_mutating=False, mcp=mgr)
    check("read_file" in reg2.names and "mcp__demo__echo" in reg2.names,
          f"内置 + MCP 共存（{sorted(reg2.names)}）")
    check(reg2.get("read_file").risk.value == "read", "内置工具的风险等级没被改动")

    print("\n[D] 调用成功与结果归一化")
    ctx = ToolContext(workspace=str(tmp))
    res = await tool.run({"text": "hello"}, ctx)
    check(res.ok, f"调用成功（summary={res.summary!r}）")
    check(res.data.get("source", {}).get("server") == "demo", "结果里带来源")
    check(res.data.get("structured") is not None, "结构化内容被保留（structuredContent）")
    check(_lines(counter).count("echo") == 1, "★ Server 侧确实执行了一次（计数文件）")

    read_res = await read_tool.run({"path": "hello.txt"}, ctx)
    check(read_res.ok and "你好" in read_res.summary, "只读工具能读到测试文件")

    print("\n[D2] 任务取消")
    import asyncio as _aio
    cancel = _aio.Event(); cancel.set()
    ctx2 = ToolContext(workspace=str(tmp), cancel_event=cancel)
    before_cancel = _lines(counter).count("echo")
    res_cancel = await tool.run({"text": "nope"}, ctx2)
    check(not res_cancel.ok, "已取消的任务 → 调用失败")
    check(_lines(counter).count("echo") == before_cancel,
          f"★ 取消后 Server 侧零调用（{before_cancel}）")

    print("\n[E] Server 返回业务错误")
    fail_tool = reg.get("mcp__demo__fail")
    res_fail = await fail_tool.run({"text": "boom"}, ctx)
    check(not res_fail.ok, "★ 业务错误 → ok=False（不当作成功）")
    check(res_fail.data.get("is_error") is True, "结果里标了 is_error")
    check(res_fail.error and res_fail.error.get("kind") == "execution_error",
          f"错误类型=execution_error（{res_fail.error})")

    print("\n[F] 超大结果截断")
    big_tool = reg.get("mcp__demo__big")
    res_big = await big_tool.run({"n": 50_000}, ctx)
    check(res_big.ok, "大结果本身仍是成功")
    check(res_big.data.get("truncated") is True, "★ 标记了 truncated")
    check(len(res_big.summary) <= 420, f"summary 有界（{len(res_big.summary)} 字符）")

    return mgr, counter, notes


# --------------------------------------------------------------------------- #
# [H] 审批（可证伪：数 Server 侧调用次数）
# --------------------------------------------------------------------------- #
async def _run_agent(reg: ToolRegistry, script: list, *, engine: str = "legacy",
                     decision: str = "") -> tuple[Any, list, ApprovalManager]:
    bus = EventBus()
    evs = collect(bus)
    approvals = ApprovalManager(timeout_s=5)
    core = AgentCore(
        FakeAdapter(script), reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, orchestrator=engine,
                           approval_timeout_s=5),
        bus=bus, approvals=approvals,
    )

    async def decide() -> None:
        for _ in range(500):
            await asyncio.sleep(0.02)
            aid = core.approvals.current()
            if aid:
                if decision == "approve":
                    core.approvals.approve(aid, "测试批准")
                else:
                    core.approvals.reject(aid, "测试拒绝")
                return

    task = asyncio.create_task(decide())
    st = await core.run("用 MCP 工具回显一句话", workspace=tempfile.mkdtemp())
    task.cancel()
    await bus.drain()
    return st, evs, approvals


async def suite_approval(mgr: McpManager, counter: str, tmp: Path) -> None:
    print("\n[H] 审批：拒绝=零执行，批准=一次执行")

    # ---- 拒绝 ----
    reg = ToolRegistry()
    register_mcp_tools(reg, mgr)
    before = _lines(counter).count("echo")
    st, evs, _ = await _run_agent(reg, [
        ModelReply(tool_calls=[tc("m1", "mcp__demo__echo", {"text": "x"})]),
        ModelReply(content="工具没有执行。"),
    ], decision="reject")
    after = _lines(counter).count("echo")
    check(after == before, f"★ 拒绝后 Server 侧零调用（{before}→{after}）")
    check(any(str(e.type) == "approval.request" for e in evs), "发出了 approval.request")
    check(any(str(e.type) == "approval.decision" for e in evs), "发出了审批决策")
    req = next(e for e in evs if str(e.type) == "approval.request")
    check(req.data.get("tool") == "mcp__demo__echo", "审批关联到正确的工具")
    check("demo" in json.dumps(req.data, ensure_ascii=False), "★ 审批载荷里能看到 Server 来源")

    # ---- 批准 ----
    reg2 = ToolRegistry()
    register_mcp_tools(reg2, mgr)
    before2 = _lines(counter).count("echo")
    st2, evs2, _ = await _run_agent(reg2, [
        ModelReply(tool_calls=[tc("m2", "mcp__demo__echo", {"text": "y"})]),
        ModelReply(content="已经用 MCP 回显了。"),
    ], decision="approve")
    after2 = _lines(counter).count("echo")
    check(after2 == before2 + 1, f"★ 批准后 Server 恰好执行 1 次（{before2}→{after2}）")
    check(st2.tool_events and st2.tool_events[0].ok, "工具结果 ok=True 回传 Agent")
    check(any(str(e.type) == "tool.result" and e.data.get("ok") for e in evs2),
          "发出 tool.result(ok=True)")

    print("\n[I] 事件来源元数据")
    call_ev = next((e for e in evs2 if str(e.type) == "tool.call"), None)
    check(call_ev is not None, "发出 tool.call")
    if call_ev:
        src = call_ev.data.get("source") or {}
        check(src.get("kind") == "mcp" and src.get("server") == "demo",
              f"★ tool.call 带 source（{src}）")

    print("\n[K] Graph 与 Legacy 一致")
    reg3 = ToolRegistry()
    register_mcp_tools(reg3, mgr)
    st3, evs3, _ = await _run_agent(reg3, [
        ModelReply(tool_calls=[tc("g1", "mcp__demo__echo", {"text": "g"})]),
        ModelReply(content="graph 也调用了 MCP。"),
    ], engine="graph", decision="approve")
    check(st3.engine == "graph", "引擎标注 graph")
    check(st3.tool_events and st3.tool_events[0].ok, f"graph 下 MCP 调用成功（{st3.status.value}）")
    check(any(str(e.type) == "tool.call" and (e.data.get("source") or {}).get("server") == "demo"
              for e in evs3), "graph 下 tool.call 同样带来源")


# --------------------------------------------------------------------------- #
# [G] 超时 / [J] 关闭
# --------------------------------------------------------------------------- #
async def suite_timeout_and_close(tmp: Path, mgr: McpManager) -> None:
    print("\n[G] 调用超时")
    path, counter, _ = _write_cfg(tmp / "timeout", read_tools=["sleep_for"])
    cfg = McpConfig(enabled=True, config_path=path, servers=load_servers(path),
                    call_timeout_s=1.0, startup_timeout_s=25.0)
    mgr2 = McpManager(cfg)
    await mgr2.start()
    try:
        reg = ToolRegistry()
        register_mcp_tools(reg, mgr2)
        tool = reg.get("mcp__demo__sleep_for")
        ctx = ToolContext(workspace=str(tmp))
        res = await tool.run({"seconds": 5}, ctx)
        check(not res.ok, "超时 → ok=False")
        check(res.error and res.error.get("kind") == "timeout",
              f"错误类型=timeout（{res.error and res.error.get('kind')}）")
        # 超时后不再复用卡住的 session
        res2 = await tool.run({"seconds": 0}, ctx)
        check(not res2.ok, "★ 超时后该 Server 标记不可用，后续调用快速失败")
        check("不可用" in (res2.error or {}).get("message", "") or "unavailable" in json.dumps(res2.data, ensure_ascii=False),
              "失败原因写明不可用")
    finally:
        await mgr2.close()

    print("\n[J] 关闭与资源释放")
    await mgr.close()
    check(mgr.status()["servers"][0]["state"] == "closed", "关闭后状态=closed")
    check(mgr.tools() == [], "关闭后不再暴露工具")
    from backend.agent.state import RiskLevel
    from backend.mcp.client import DiscoveredTool
    ctx = ToolContext(workspace=str(tmp))
    spec = DiscoveredTool(server_id="demo", name="echo", description="",
                          input_schema={"type": "object", "properties": {}})
    tool = McpTool(mgr, spec, risk=RiskLevel.WRITE)
    res = await tool.run({"text": "z"}, ctx)
    check(not res.ok and "不可用" in (res.error or {}).get("message", ""),
          "★ 关闭后调用返回失败（不会继续调用已失效的工具）")
    await mgr.close()
    check(True, "重复 close 不报错（幂等）")


# --------------------------------------------------------------------------- #
async def amain() -> int:
    parser = argparse.ArgumentParser(description="Loom M4.1 MCP 自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.parse_args()

    if not SERVER_SCRIPT.is_file():
        print(f"找不到测试 Server：{SERVER_SCRIPT}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="loom_mcp_test_"))
    try:
        suite_config(tmp)
        mgr, counter, notes = await suite_connect(tmp)
        try:
            check(os.path.isfile(notes + "/hello.txt"), "测试环境就绪（笔记文件存在）")
            await suite_approval(mgr, counter, tmp)
        finally:
            pass
        await suite_timeout_and_close(tmp, mgr)
        print("\n[L] 内置工具清单未受影响")
        reg = ToolRegistry()
        from backend.tools.registry import build_default_registry
        base = build_default_registry(include_mutating=False)
        check(set(base.names) == {"list_files", "read_file"},
              f"不注入 MCP 时工具清单不变（{sorted(base.names)}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 62)
    print(f"结果：{passed}/{total} 通过")
    print("=" * 62)
    if passed != total:
        print("\n失败项：")
        for ok, label in _results:
            if not ok:
                print(f"  - {label}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
