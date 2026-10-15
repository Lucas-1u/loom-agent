"""
Loom Agent v0.1 · 离线演示（不需要 API Key）

★ 用途：让「Agent 内部到底长什么样」这件事，在你配好 Key 之前就能看见。

它用 FakeAdapter 演一段剧本，把完整事件流走一遍：
    PLAN → ACT(list_files) → OBSERVE → REFLECT
         → ACT(write_file) → 审批询问（自动批准）→ OBSERVE → REFLECT
         → FINAL ANSWER

跑法：
    ./.venv/Scripts/python.exe -m backend.demo

    # 看审批被拒绝的那条路径
    ./.venv/Scripts/python.exe -m backend.demo --reject

    # 看预算耗尽（轮次掐断）时会发生什么
    ./.venv/Scripts/python.exe -m backend.demo --budget
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile

from backend.agent.core import AgentConfig, AgentCore
from backend.cli.approve import CLIApprover
from backend.cli.render import C, TerminalRenderer
from backend.events import EventBus
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.tools.registry import build_default_registry


def tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False), arguments=args)


def make_workspace() -> str:
    d = tempfile.mkdtemp(prefix="loom_demo_")
    for name, content in {
        "main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'status': 'ok'}\n",
        "requirements.txt": "fastapi>=0.110.0\nuvicorn>=0.29.0\n",
        "README.md": "# 演示项目\n\n一个最小的 FastAPI 服务，用于 Loom Agent 演示。\n",
    }.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content)
    os.makedirs(os.path.join(d, "node_modules"), exist_ok=True)
    # ★ 显式指定编码：不写的话用的是 locale 默认编码，
    #   而在「清掉了 PYTHONUTF8」的环境里那可能是 cp936 / cp1252 等，
    #   写入中文就会在不同机器上行为不一致。工具层早已全部显式 utf-8，
    #   演示夹具也保持一致。
    with open(os.path.join(d, "node_modules", "junk.js"), "w", encoding="utf-8") as f:
        f.write("// 噪音")
    return d


async def demo(kind: str) -> None:
    ws = make_workspace()
    print(f"{C.DIM if kind else ''}演示工作区：{ws}\n")

    # ---------------- 剧本 ---------------- #
    if kind == "budget":
        # 反复做同一件事，看熔断 + 预算掐断
        same = ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})])
        script = [
            ModelReply(content='{"steps":[{"title":"反复列目录","detail":"演示熔断"}],"note":""}'),
            same, same, same, same, same, same, same, same,
        ]
        cfg = AgentConfig(max_turns=6, max_same_call_repeat=2, use_reflect_phase=False, min_evidence=0)
        approve_mode = "approve"
    elif kind == "reject":
        script = [
            ModelReply(content='{"steps":[{"title":"列出目录","detail":"确认项目结构"},{"title":"创建配置文件","detail":"生成 config.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(content='{"progress":"已看到 main.py / requirements.txt / README.md","next_action":"创建 config.py 存放配置","done":false}'),
            ModelReply(tool_calls=[tc("c2", "write_file", {"path": "config.py", "content": "DEBUG = True\nPORT = 8000\n"})]),
            ModelReply(content='{"progress":"用户拒绝了写入，我不再重试","next_action":"基于只读观测给出结论","done":true}'),
            ModelReply(content="这是一个 FastAPI 演示项目，包含 main.py、requirements.txt、README.md 三个文件。用户拒绝了创建 config.py 的请求，所以我只做了分析，没有改动任何文件。"),
        ]
        cfg = AgentConfig(max_turns=10, min_evidence=1)
        approve_mode = "reject"
    else:
        script = [
            # ① PLAN
            ModelReply(content='{"steps":[{"title":"列出工作区根目录","detail":"确认项目类型与结构"},{"title":"读取入口文件","detail":"看 main.py 用了什么框架"},{"title":"查看依赖清单","detail":"确认版本约束"}],"note":"三步足够了，不需要更细"}'),
            # ② ACT: list_files
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            # ③ REFLECT
            ModelReply(content='{"progress":"看到 main.py、requirements.txt、README.md，是个 Python 项目","next_action":"读取 main.py 确认框架","done":false}'),
            # ④ ACT: read_file
            ModelReply(tool_calls=[tc("c2", "read_file", {"path": "main.py"})]),
            # ⑤ REFLECT
            ModelReply(content='{"progress":"确认是 FastAPI 应用，有一个 /health 接口","next_action":"创建 config.py 把配置抽出来（需要用户批准）","done":false}'),
            # ⑥ ACT: write_file（会触发审批）
            ModelReply(tool_calls=[tc("c3", "write_file", {"path": "config.py", "content": "DEBUG = True\nPORT = 8000\n"})]),
            # ⑦ REFLECT（若被批准）
            ModelReply(content='{"progress":"已创建 config.py","next_action":"finish","done":true}'),
            # ⑧ 兜底正文
            ModelReply(content="这是一个 FastAPI 演示项目：main.py 提供 /health 接口，依赖见 requirements.txt。"),
        ]
        cfg = AgentConfig(max_turns=12, min_evidence=1, use_reflect_phase=True)
        approve_mode = "approve"

    adapter = FakeAdapter(script)
    registry = build_default_registry()
    bus = EventBus()

    renderer = TerminalRenderer(color=True, show_thinking=False)
    bus.subscribe(renderer.on_event)

    # 旁路收集事件类型，用于收尾统计（不影响渲染）
    collector_events: list[str] = []

    async def _collect(ev) -> None:
        collector_events.append(ev.type)

    bus.subscribe(_collect)

    core = AgentCore(adapter, registry, config=cfg, bus=bus)
    approver = CLIApprover(core.approvals, renderer=renderer, auto=approve_mode)
    bus.subscribe(approver.on_event)

    attend = asyncio.create_task(approver.attend())
    try:
        state = await core.run("分析这个项目并创建一份配置文件", workspace=ws)
    finally:
        attend.cancel()
        try:
            await attend
        except asyncio.CancelledError:
            pass
        await bus.drain()

    # ---------------- 收尾面板 ---------------- #
    print(f"\n{C.B_CYAN}{'═' * 76}{C.RESET}")
    print(f"{C.B_CYAN}  调试面板{C.RESET}  （这些数字在真实 GUI 里会实时刷新）")
    print(f"{C.DIM}{'─' * 76}{C.RESET}")
    print(f"  task_id      {state.task_id}")
    print(f"  状态         {state.status.value}  ({state.end_reason})")
    print(f"  轮次         {state.turn} / {state.budgets.max_turns}")
    print(f"  工具调用     {state.budgets.tool_calls_used} / {state.budgets.max_tool_calls}")
    print(f"  证据         {len([e for e in state.evidence_summary if e.ok])} 条（门槛 {state.budgets.min_evidence}）")
    print(f"  审批         {len(state.approval_history)} 次")
    print(f"  文件变更     {len(state.change_records)} 处")
    if state.plan:
        print(f"  计划         ", end="")
        print("  ".join(
            f"{'[x]' if s.status.value == 'done' else '[ ]'}{s.title}" for s in state.plan
        ))
    if state.change_records:
        print(f"\n{C.DIM}  变更明细：{C.RESET}")
        for cr in state.change_records:
            print(f"    · {cr.action:<10} {cr.path}  (备份: {cr.backup_path or '无'})")
    if state.reflections:
        print(f"\n{C.DIM}  自评轨迹：{C.RESET}")
        for rf in state.reflections:
            print(f"    第{rf['turn']}轮  done={str(rf['done']):<5}  {rf['progress'][:70]}")
    print(f"{C.DIM}{'─' * 76}{C.RESET}")

    # 事件统计
    print(f"\n{C.DIM}  事件统计（按类型）：{C.RESET}")
    counts: dict[str, int] = {}
    for ev in collector_events:
        counts[ev] = counts.get(ev, 0) + 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"    {k:<22} ×{v}")

    print(f"\n{C.DIM}  磁盘实际状态：{C.RESET}")
    for f in sorted(os.listdir(ws)):
        p = os.path.join(ws, f)
        if os.path.isfile(p):
            print(f"    {f}")
        elif f == ".loom_backup":
            for root, _dirs, files in os.walk(p):
                for bf in files:
                    rel = os.path.relpath(os.path.join(root, bf), ws)
                    print(f"    {C.CYAN}{rel}{C.RESET}  ← 覆盖前的自动备份，可手工还原")

    shutil.rmtree(ws, ignore_errors=True)
    print(f"\n{C.DIM}（演示工作区已清理）{C.RESET}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Loom Agent 离线演示")
    ap.add_argument("--reject", action="store_true", help="演示「审批被拒绝」的路径")
    ap.add_argument("--budget", action="store_true", help="演示「重复熔断 + 预算掐断」")
    args = ap.parse_args()

    kind = "reject" if args.reject else ("budget" if args.budget else "normal")

    await demo(kind)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
