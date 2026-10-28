"""
M6 Terminal Tool 自检（真实子进程，不是 mock）。

跑法（loom/ 目录）：
    ./.venv/Scripts/python.exe -m backend.selfcheck_terminal
    ./.venv/Scripts/python.exe -B -m backend.selfcheck_terminal -v

覆盖：
    [A] Shell 探测：auto / cmd / bash / 非法值 / 未安装
    [B] 真实执行：退出码、stdout 与 stderr 分离、cwd、中文、截断、超时、取消
    [C] 安全：越界 cwd、只读分级、元字符、敏感 env 不传入、输出打码
    [D] Agent 集成：graph / legacy 调 terminal；失败后重试；审批拒绝=不执行；只读免审批
    [E] 注册与事件：工具在清单里；tool.result 带终端信封

★ 为什么必须真跑：本工具的价值就是"真的执行"。mock 掉子进程等于什么都没测。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from backend.agent.approval import ApprovalManager
from backend.agent.core import AgentConfig, AgentCore
from backend.events import EventBus, collect
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.tools.base import ToolContext
from backend.tools.exec import shells
from backend.tools.exec.terminal import make as make_terminal
from backend.tools.registry import build_default_registry

_results: list[tuple[bool, str]] = []


def check(cond, label: str) -> bool:
    ok = bool(cond)
    _results.append((ok, label))
    print(f"  [{'ok' if ok else '!!'}] {label}")
    return ok


def tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False),
                    arguments=args)


TOOL = make_terminal()


async def run_tool(ws: str, args: dict, cancel=None):
    ctx = ToolContext(workspace=ws, cancel_event=cancel, task_id="task_term_test")
    return await TOOL.run(args, ctx)


# --------------------------------------------------------------------------- #
# [A] Shell 探测
# --------------------------------------------------------------------------- #
def suite_detect() -> None:
    print("\n[A] Shell 探测")
    auto = shells.detect_shell("auto")
    check(auto.kind in ("powershell", "cmd", "bash"), f"auto 探测到 {auto.label}")
    check(os.path.isfile(auto.path), "探测到的 shell 路径真实存在")

    cmd = shells.detect_shell("cmd")
    check(cmd.kind == "cmd" and os.path.isfile(cmd.path), f"显式 cmd 可用（{cmd.path}）")

    try:
        shells.detect_shell("nonsense")
        check(False, "非法 shell 应被拒绝")
    except shells.ShellUnavailable as exc:
        check("不支持" in str(exc), f"非法 shell 被拒绝：{exc}")

    # 模拟"没装"：把 which 与固定候选都掐掉
    with mock.patch.object(shells, "_which", lambda name: ""), \
            mock.patch("os.path.isfile", lambda p: False):
        try:
            shells.detect_shell("bash")
            check(False, "未安装时应抛 ShellUnavailable")
        except shells.ShellUnavailable as exc:
            check("Git Bash" in str(exc) and "安装" in str(exc),
                  f"未安装时给出可操作提示：{str(exc)[:60]}")


# --------------------------------------------------------------------------- #
# [B] 真实执行
# --------------------------------------------------------------------------- #
async def suite_exec(tmp: Path) -> str:
    print("\n[B] 真实执行")
    ws = str(tmp / "ws")
    os.makedirs(os.path.join(ws, "sub"), exist_ok=True)
    (tmp / "ws" / "sub" / "marker.txt").write_text("m\n", encoding="utf-8")

    r = await run_tool(ws, {"command": "echo hello-loom", "shell": "cmd"})
    d = r.data or {}
    check(r.ok and d.get("exit_code") == 0, f"echo 成功且退出码 0（{d.get('exit_code')}）")
    check("hello-loom" in d.get("stdout", ""), f"stdout 有真实输出（{d.get('stdout','').strip()[:40]!r}）")
    check(d.get("shell") == "cmd", "结果里标明真实 shell")

    # ★ 用脚本文件而不是 -c "..."：cmd 对嵌套引号的处理会把 -c 参数拆坏
    #   （Python 收到的是 '"import' 这种），这是 shell 引号现实，不是工具缺陷。
    (tmp / "ws" / "hello.py").write_text("print('py-ok')\n", encoding="utf-8")
    r2 = await run_tool(ws, {"command": "python hello.py", "shell": "cmd"})
    check(r2.ok and "py-ok" in (r2.data or {}).get("stdout", ""),
          f"能跑 Python 并拿到输出（{(r2.data or {}).get('stdout','').strip()[:30]!r}）")

    r3 = await run_tool(ws, {"command": "exit 7", "shell": "cmd"})
    d3 = r3.data or {}
    check(not r3.ok and d3.get("exit_code") == 7, f"★ 非零退出码如实返回（{d3.get('exit_code')}）")
    check("7" in r3.summary, f"摘要写明退出码（{r3.summary}）")

    r4 = await run_tool(ws, {"command": "echo to-stderr 1>&2", "shell": "cmd"})
    d4 = r4.data or {}
    check(r4.ok and "to-stderr" in d4.get("stderr", ""), "stderr 被单独捕获")
    check("to-stderr" not in d4.get("stdout", ""), "★ stderr 没有混进 stdout")

    r5 = await run_tool(ws, {"command": "cd", "shell": "cmd", "cwd": "sub"})
    out5 = (r5.data or {}).get("stdout", "").strip().lower()
    check(out5.endswith("sub") and ws.lower().split("\\")[-1] in out5,
          f"★ cwd 生效（{out5[-40:]}）")
    check(str((r5.data or {}).get("cwd", "")).replace("\\", "/").strip("./") in ("sub", "./sub"),
          f"结果里 cwd 是相对路径（{(r5.data or {}).get('cwd')}）")

    # 中文（走 python，避免控制台代码页干扰）
    r6 = await run_tool(ws, {"command": "python -c \"print('中文输出测试')\""})
    check("中文输出测试" in (r6.data or {}).get("stdout", ""), "中文输出解码正确")

    # 超长输出截断
    r7 = await run_tool(ws, {"command": "python -c \"print('x'*20000)\""})
    d7 = r7.data or {}
    check(d7.get("truncated") is True, "★ 超长输出标记 truncated")
    check(len(d7.get("stdout", "")) < 9000, f"输出被限制（{len(d7.get('stdout',''))} 字符）")
    check("输出被截断" in d7.get("stdout", ""), "截断处有明确标记")

    # 超时
    t0 = time.time()
    r8 = await run_tool(ws, {"command": "ping -n 10 127.0.0.1 > nul", "shell": "cmd", "timeout": 2})
    dt = time.time() - t0
    check(not r8.ok and (r8.data or {}).get("timed_out") is True, "★ 超时被识别（timed_out=True）")
    check(dt < 15, f"超时后快速返回（{dt:.1f}s）")
    check("超时" in r8.summary, f"超时摘要明确（{r8.summary}）")

    # 取消
    ev = asyncio.Event()

    async def setter():
        await asyncio.sleep(0.6)
        ev.set()

    asyncio.create_task(setter())
    t1 = time.time()
    r9 = await run_tool(ws, {"command": "ping -n 10 127.0.0.1 > nul", "shell": "cmd"}, cancel=ev)
    dt2 = time.time() - t1
    check(not r9.ok and (r9.data or {}).get("cancelled") is True, "★ 取消被识别（cancelled=True）")
    check(dt2 < 15, f"取消后快速返回（{dt2:.1f}s）")

    return ws


# --------------------------------------------------------------------------- #
# [C] 安全
# --------------------------------------------------------------------------- #
async def suite_security(tmp: Path, ws: str) -> None:
    print("\n[C] 安全边界")

    r = await run_tool(ws, {"command": "cd", "cwd": "../.."})
    check(not r.ok and (r.error or {}).get("kind") == "permission_denied",
          f"★ cwd 越界被拒（{(r.error or {}).get('kind')}）")

    abs_out = str(tmp / "outside")
    os.makedirs(abs_out, exist_ok=True)
    r2 = await run_tool(ws, {"command": "cd", "cwd": abs_out})
    check(not r2.ok, "★ 绝对路径（工作区外）也被拒")

    check(TOOL.risk_for({"command": "git status --short"}).value == "read", "只读命令 → read（免审批）")
    check(TOOL.risk_for({"command": "dir"}).value == "read", "dir → read")
    for bad in ("git status && del x", "dir | more", "echo $(whoami)", "dir > out.txt", "dir; del x"):
        check(TOOL.risk_for({"command": bad}).value == "exec",
              f"★ 含 shell 元字符不降级：{bad}")

    # 敏感环境变量不传给子进程
    os.environ["LOOM_TEST_API_TOKEN"] = "sk-test-should-not-leak-123"
    try:
        r3 = await run_tool(ws, {"command": "echo %LOOM_TEST_API_TOKEN%", "shell": "cmd"})
        out = (r3.data or {}).get("stdout", "")
        check("sk-test-should-not-leak-123" not in out and "%LOOM_TEST" in out,
              f"★ 凭据类环境变量没有传给子进程（{out.strip()[:40]!r}）")

        # 如果命令自己把密钥写出来（例如用户粘贴），输出也要打码
        r4 = await run_tool(ws, {"command": "echo sk-test-should-not-leak-123", "shell": "cmd"})
        out4 = (r4.data or {}).get("stdout", "")
        check("sk-test-should-not-leak-123" not in out4 and "***" in out4,
              f"★ 输出里的密钥被打码（{out4.strip()[:40]!r}）")
        check((r4.data or {}).get("redacted", 0) >= 1, "结果里记录了打码次数")
    finally:
        os.environ.pop("LOOM_TEST_API_TOKEN", None)

    # 参数校验
    r5 = await run_tool(ws, {"command": "   "})
    check(not r5.ok and (r5.error or {}).get("kind") == "invalid_arguments", "空命令被拒")
    r6 = await run_tool(ws, {"command": "echo " + "a" * 2100})
    check(not r6.ok and "too" not in str(r6.summary), f"超长命令被拒（{r6.summary[:40]}）")


# --------------------------------------------------------------------------- #
# [D] Agent 集成
# --------------------------------------------------------------------------- #
async def _run_agent(ws: str, script: list, *, engine: str, decision: str = "") -> tuple:
    reg = build_default_registry(terminal=True)
    bus = EventBus()
    evs = collect(bus)
    core = AgentCore(
        FakeAdapter(script), reg,
        config=AgentConfig(max_turns=8, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, orchestrator=engine,
                           approval_timeout_s=5),
        bus=bus, approvals=ApprovalManager(timeout_s=5),
    )

    async def decide():
        for _ in range(400):
            await asyncio.sleep(0.02)
            aid = core.approvals.current()
            if aid:
                if decision == "approve":
                    core.approvals.approve(aid, "测试批准")
                else:
                    core.approvals.reject(aid, "测试拒绝")
                return

    task = asyncio.create_task(decide()) if decision else None
    st = await core.run("检查一下环境", workspace=ws, task_id=f"task_term_{engine}")
    if task:
        task.cancel()
    await bus.drain()
    return st, evs


async def suite_agent(tmp: Path) -> None:
    print("\n[D] Agent 集成")
    ws = str(tmp / "agent_ws")
    os.makedirs(ws, exist_ok=True)

    for engine in ("graph", "legacy"):
        st, evs = await _run_agent(ws, [
            ModelReply(tool_calls=[tc("t1", "terminal", {"command": "echo agent-ok", "shell": "cmd"})]),
            ModelReply(content="命令执行成功，输出是 agent-ok。"),
        ], engine=engine, decision="approve")
        te = st.tool_events[0] if st.tool_events else None
        check(te is not None and te.ok, f"{engine}：模型能调用 terminal 并成功执行")
        check(st.status.value == "done", f"{engine}：任务正常 done（{st.status.value}）")
        check(any(str(e.type) == "tool.call" and (e.data.get("args") or {}).get("command") == "echo agent-ok"
                  for e in evs), f"{engine}：tool.call 带了真实命令")
        check(any(str(e.type) == "tool.result" and (e.data.get("data") or {}).get("kind") == "terminal"
                  for e in evs), f"{engine}：tool.result 带终端信封")

    # 失败后重试：第一次 exit 1 → 模型看 stderr 再跑一次
    st2, evs2 = await _run_agent(ws, [
        ModelReply(tool_calls=[tc("r1", "terminal", {"command": "exit 5", "shell": "cmd"})]),
        ModelReply(tool_calls=[tc("r2", "terminal", {"command": "echo fixed", "shell": "cmd"})]),
        ModelReply(content="第一次失败（exit 5），第二次通过。"),
    ], engine="graph", decision="approve")
    names = [e.tool for e in st2.tool_events]
    check(names.count("terminal") == 2, f"★ 失败后再次调用终端（{names}）")
    check(st2.tool_events[0].ok is False and st2.tool_events[1].ok is True,
          "★ 第一次失败、第二次成功（状态没被合并）")

    # 审批拒绝 → 命令没有执行（用一个会留痕的命令）
    target = os.path.join(ws, "should_not_exist.txt")
    st3, evs3 = await _run_agent(ws, [
        ModelReply(tool_calls=[tc("d1", "terminal",
                                 {"command": "echo x > should_not_exist.txt", "shell": "cmd"})]),
        ModelReply(content="没有执行。"),
    ], engine="graph", decision="reject")
    check(any(str(e.type) == "approval.request" for e in evs3), "★ 高风险命令进入审批")
    check(not os.path.exists(target), "★ 审批拒绝后命令没有执行（文件不存在）")

    # 只读命令免审批
    st4, evs4 = await _run_agent(ws, [
        ModelReply(tool_calls=[tc("ro1", "terminal", {"command": "git status --short", "shell": "cmd"})]),
        ModelReply(content="看过了。"),
    ], engine="graph", decision="")
    check(not any(str(e.type) == "approval.request" for e in evs4),
          "★ 只读命令没有触发审批（白名单生效）")
    check(st4.tool_events and st4.tool_events[0].tool == "terminal", "只读命令确实执行了")


# --------------------------------------------------------------------------- #
# [E] 注册与清单
# --------------------------------------------------------------------------- #
def suite_registry() -> None:
    print("\n[E] 注册与清单")
    reg = build_default_registry(terminal=True)
    check("terminal" in reg.names, "terminal 在默认注册表里")
    check("run_command" in reg.names, "run_command（Docker 沙箱）仍在，两者并存")
    specs = {s.name: s for s in reg.to_specs()}
    check("terminal" in specs, "模型能看到的工具 Schema 里有 terminal")
    params = specs["terminal"].parameters
    check(set(params.get("required", [])) == {"command"}, "必填参数只有 command")
    check(set(params["properties"]) >= {"command", "cwd", "shell", "timeout"}, "参数齐全")
    check(specs["terminal"].parameters["properties"]["shell"].get("enum") ==
          ["auto", "powershell", "cmd", "bash"], "shell 是受控枚举（不是任意路径）")

    reg2 = build_default_registry(terminal=False)
    check("terminal" not in reg2.names, "terminal=False 时不注册（可关闭）")


# --------------------------------------------------------------------------- #
async def amain() -> int:
    parser = argparse.ArgumentParser(description="Loom Terminal Tool 自检")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="loom_term_test_"))
    try:
        suite_detect()
        ws = await suite_exec(tmp)
        await suite_security(tmp, ws)
        await suite_agent(tmp)
        suite_registry()
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
