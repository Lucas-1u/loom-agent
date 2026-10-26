"""
Loom Agent v0.1 · CLI 入口

跑法：
    # 单次任务
    ./backend/.venv/Scripts/python.exe -m backend.cli.main "分析当前项目结构" --workspace D:/some/project

    # 交互模式（连续对话式跑多个任务，状态不保留 —— C6 决定 v0.1 纯内存）
    ./backend/.venv/Scripts/python.exe -m backend.cli.main --workspace D:/some/project

    # 自动批准（批跑/测试用，跳过人工确认）
    ./backend/.venv/Scripts/python.exe -m backend.cli.main "创建 hello.py" -w D:/tmp --yes

    # 只读模式（禁用所有写入工具，绝对安全）
    ./backend/.venv/Scripts/python.exe -m backend.cli.main "分析项目" -w D:/proj --readonly

    # 关闭规划/自评阶段（对比实验）
    ./backend/.venv/Scripts/python.exe -m backend.cli.main "分析项目" -w D:/proj --no-plan --no-reflect

★ 这是 v0.1a 的交付形态：**先让 Agent Loop + 审批在 CLI 上跑正确**，
  再接 Tauri GUI。CLI 的好处是——所有交互都看得见、可脚本化、可回归。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# 允许 `python -m backend.cli.main` 直接跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agent.approval import ApprovalManager
from backend.agent.core import AgentConfig, AgentCore
from backend.cli.approve import CLIApprover
from backend.cli.console import read_line
from backend.cli.render import C, TerminalRenderer, _enable_ansi
from backend.config import Config, setup_logging
from backend.events import EventBus, EventType
from backend.models.openai_compat import OpenAICompatAdapter
from backend.tools.registry import build_default_registry


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loom",
        description="Loom Agent v0.1a —— 本地编码智能体（CLI 版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  loom "分析这个项目的技术栈" -w D:/myproject
  loom -w D:/myproject                      # 交互模式
  loom "创建 hello.py" -w D:/tmp --yes      # 自动批准
  loom "看看代码" -w D:/proj --readonly      # 只读模式（零副作用）
""",
    )
    p.add_argument("goal", nargs="?", default="", help="任务描述。省略则进入交互模式。")
    p.add_argument("-w", "--workspace", default=".", help="工作区根目录（Agent 只能操作这里面的文件）")
    p.add_argument("-m", "--model", default="", choices=["", "bailian", "deepseek", "xfyun"],
                   help="模型供应商（留空则用 .env 的 LOOM_DEFAULT_MODEL）")
    p.add_argument("--model-name", default="", help="覆盖具体模型名，如 qwen3.7-plus / deepseek-chat")
    p.add_argument("--host", default="", help="服务监听地址（v0.1a 未起 HTTP，仅预留）")
    p.add_argument("--max-turns", type=int, default=0, help="最大轮次（默认 20）")
    p.add_argument("--timeout", type=float, default=0.0, help="任务总时长上限（秒，默认 300）")
    p.add_argument("--approval-timeout", type=float, default=0.0, help="审批等待上限（秒，默认 180）")

    # 审批策略
    g = p.add_mutually_exclusive_group()
    g.add_argument("--yes", "-y", action="store_true", help="自动批准所有审批请求（批跑用）")
    g.add_argument("--no", action="store_true", help="自动拒绝所有审批请求（测试拒绝路径用）")
    p.add_argument("--readonly", action="store_true", help="只读模式：卸载所有写入工具")

    # 阶段开关（对比实验）
    p.add_argument("--no-plan", action="store_true", help="关闭 PLAN 阶段")
    p.add_argument("--no-reflect", action="store_true", help="关闭 REFLECT 阶段")
    p.add_argument("--no-stream", action="store_true", help="关闭流式（不显示思维链）")

    # 展示控制
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    p.add_argument("--no-think", action="store_true", help="不显示思维链")
    p.add_argument("--show-data", action="store_true", help="显示工具返回的完整数据")
    p.add_argument("--compact", action="store_true", help="精简输出（只看骨架）")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    p.add_argument("--dry-run", action="store_true", help="只检查环境与工具，不真的跑任务")
    return p


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run_one(
    goal: str,
    *,
    workspace: str,
    adapter,
    registry,
    cfg,
    args,
    index: int = 0,
    knowledge=None,
) -> int:
    """
    跑一个任务。返回退出码。

    ★ 每个任务都是**全新的 State 与 Bus** —— C6 决定 v0.1 纯内存、
      不做跨任务记忆。这样每次运行都是干净可复现的，
      调试 Agent 时这一点至关重要（否则你分不清行为差异是来自代码还是历史残留）。
    """
    bus = EventBus()
    renderer = TerminalRenderer(
        color=not args.no_color,
        show_thinking=not args.no_think,
        show_tool_data=args.show_data,
        compact=args.compact,
    )
    bus.subscribe(renderer.on_event)

    # ★ 审批超时必须以命令行覆盖值为准。
    #   坑：ApprovalManager 才是真正执行 `asyncio.wait_for(fut, timeout)` 的那个对象，
    #   而 AgentConfig.approval_timeout_s 只是配置快照、不参与实际等待。
    #   最初这里直接用了 cfg.budgets.approval_timeout_s，导致 `--approval-timeout 8`
    #   对实际等待毫无影响（实测现象：CLI 提示仍是「179s 后超时」）。
    approval_timeout = args.approval_timeout or cfg.budgets.approval_timeout_s
    approvals = ApprovalManager(timeout_s=approval_timeout)
    auto = "approve" if args.yes else ("reject" if args.no else "")
    approver = CLIApprover(approvals, renderer=renderer, auto=auto, color=not args.no_color)
    bus.subscribe(approver.on_event)

    agent_cfg = AgentConfig.from_budget_config(
        cfg.budgets,
        max_turns=args.max_turns or cfg.budgets.max_turns,
        max_seconds=args.timeout or cfg.budgets.max_seconds,
        approval_timeout_s=approval_timeout,
        use_plan_phase=not args.no_plan,
        use_reflect_phase=not args.no_reflect,
        stream=not args.no_stream,
    )

    agent_cfg.rag_auto = bool(getattr(cfg.rag, "auto_prefetch", True)) and knowledge is not None
    core = AgentCore(adapter, registry, config=agent_cfg, bus=bus, approvals=approvals,
                     knowledge=knowledge)

    # 审批输入与 Agent 并行运行
    attend_task = asyncio.create_task(approver.attend())
    t0 = time.time()
    try:
        state = await core.run(goal, workspace=workspace)
    except KeyboardInterrupt:
        print()
        renderer._w(renderer._c("  ⏹ 已中止（Ctrl+C）", C.B_YELLOW))
        return 130
    finally:
        attend_task.cancel()
        try:
            await attend_task
        except asyncio.CancelledError:
            pass
        await bus.drain()

    elapsed = time.time() - t0

    # ---- 运行摘要（这是 CLI 的「调试面板」------------------------------
    if not args.compact:
        print()
        print(renderer._c("─" * 76, C.DIM))
        print(renderer._c("  运行摘要", C.BOLD))
        rows = [
            f"  状态      {state.status.value}  （{state.end_reason}）",
            f"  耗时      {elapsed:.1f}s    轮次 {state.turn}/{state.budgets.max_turns}",
            f"  工具调用  {state.budgets.tool_calls_used} 次    证据 {len([e for e in state.evidence_summary if e.ok])} 条",
            f"  审批      {len(state.approval_history)} 次  "
            f"（批准 {approver.count_approved} / 拒绝 {approver.count_rejected}）",
            f"  文件变更  {len(state.change_records)} 处",
        ]
        for r in rows:
            print(renderer._c(r, C.DIM))
        if state.change_records:
            for cr in state.change_records:
                print(renderer._c(f"    · {cr.action:<10} {cr.path}", C.DIM))
        print(renderer._c("─" * 76, C.DIM))

    return 0 if state.status.value == "done" else 1


async def amain(args) -> int:
    setup_logging("DEBUG" if args.verbose else "WARNING")

    # ---- 配置 ----------------------------------------------------------
    cfg = Config.load(args.model)
    if args.model_name:
        cfg.model.model = args.model_name

    # ---- 工作区 --------------------------------------------------------
    from backend.tools.path_safety import PathViolation, normalize_root

    try:
        root = normalize_root(args.workspace)
    except PathViolation as exc:
        print(f"{C.B_RED}工作区无效：{exc}{C.RESET}")
        return 2

    # ---- 模型 ----------------------------------------------------------
    if not cfg.model.api_key:
        key_name = "BAILIAN_API_KEY" if cfg.model.provider == "bailian" else "DEEPSEEK_API_KEY"
        print(f"{C.B_RED}未配置 {key_name}{C.RESET}")
        print(f"  请在 {Path(__file__).resolve().parents[2] / '.env'} 中填入：")
        print(f"    {key_name}=sk-xxxxxxxx")
        print(f"  或参考 .env.example（已有完整注释）。")
        return 3

    try:
        adapter = OpenAICompatAdapter.from_config(cfg.model)
    except Exception as exc:  # noqa: BLE001
        print(f"{C.B_RED}模型适配器初始化失败：{exc}{C.RESET}")
        return 3

    # ---- 工具 ----------------------------------------------------------
    # M3：CLI 也走同一套知识库（不可用则 registry 里没有 search_docs）
    from backend.agent.rag import KnowledgeBase
    from backend.config import Config as _Cfg
    _rag_cfg = _Cfg.load(args.model if getattr(args, "model", "") else "").rag
    knowledge = KnowledgeBase(_rag_cfg) if _rag_cfg.usable else None
    registry = build_default_registry(include_mutating=not args.readonly, knowledge=knowledge)
    if args.readonly:
        # 保险：即使 build 里有 bug，也强制剔除写工具
        for name in list(registry.names):
            if registry.require(name).needs_approval:
                registry.unregister(name)

    # ---- 环境概览 ------------------------------------------------------
    # ★ 这里必须显示**生效值**（命令行覆盖之后），不是配置文件的原始值。
    #   否则 `--approval-timeout 8` 跑起来横幅还写「审批超时180s」，
    #   让人以为参数没生效 —— 实测就踩过这个坑。
    eff_turns = args.max_turns or cfg.budgets.max_turns
    eff_seconds = args.timeout or cfg.budgets.max_seconds
    eff_approval = args.approval_timeout or cfg.budgets.approval_timeout_s
    if not args.compact:
        bar = C.CYAN if not args.no_color else ""
        rst = C.RESET if not args.no_color else ""
        print(f"{bar}{'═' * 76}{rst}")
        print(f"  {C.B_CYAN if not args.no_color else ''}Loom Agent v0.1a{rst}   CLI  ·  {cfg.model.provider} / {cfg.model.model}")
        print(f"{bar}{'─' * 76}{rst}")
        print(f"  工作区      {root}")
        print(f"  预算        轮次≤{eff_turns}  时长≤{eff_seconds:.0f}s  "
              f"工具≤{cfg.budgets.max_tool_calls}  审批超时{eff_approval:.0f}s  最小证据{cfg.budgets.min_evidence}")
        mode = "只读（无副作用）" if args.readonly else ("自动批准" if args.yes else ("自动拒绝" if args.no else "人工审批"))
        print(f"  审批模式    {mode}")
        if args.no_plan or args.no_reflect:
            print(f"  阶段开关    PLAN={'关' if args.no_plan else '开'}  REFLECT={'关' if args.no_reflect else '开'}")
        print(f"  可用工具    {len(registry.names)} 个")
        for d in sorted(registry.describe(), key=lambda x: x["name"]):
            mark = "●" if d["approval"] == "需要审批" else "○"
            print(f"    {mark} {d['name']:<12} {d['approval']:<8} {d['description'][:44]}")
        if args.readonly:
            print(f"    {C.DIM if not args.no_color else ''}（只读模式：写入能力已卸载）{rst}")
        print(f"{bar}{'═' * 76}{rst}")
        print()

    if args.dry_run:
        print(f"{C.B_GREEN}环境检查通过（--dry-run，未执行任务）{C.RESET}")
        return 0

    # ---- 单次任务 ------------------------------------------------------
    if args.goal.strip():
        return await run_one(
            args.goal.strip(), workspace=root, adapter=adapter, registry=registry, cfg=cfg, args=args,
            knowledge=knowledge,
        )

    # ---- 交互模式 ------------------------------------------------------
    print(f"{C.DIM if not args.no_color else ''}进入交互模式。输入任务后回车执行；输入 :q 退出，:h 查看帮助。{C.RESET}")
    print()
    idx = 0
    last_code = 0
    while True:
        try:
            sys.stdout.write(f"{C.B_CYAN if not args.no_color else ''}loom>{C.RESET} ")
            sys.stdout.flush()
            line = await asyncio.get_running_loop().run_in_executor(None, read_line)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        except UnicodeDecodeError as exc:
            # ★ 二道防线：正常路径已被 align_stdin_encoding() 修好，
            #   这里是万一还有别的编码组合没覆盖到时的兜底 ——
            #   宁可给一句人话，也不要甩一屏 Python 堆栈给用户。
            print()
            print(f"{C.B_YELLOW if not args.no_color else ''}"
                  f"  [!] 读到的输入无法按 {exc.encoding} 解码 —— "
                  f"终端编码与 Python 不一致。{C.RESET}")
            print(f"      建议用 loom.bat 启动，或把终端代码页设为 936 / 65001。")
            break
        if line == "":
            break
        goal = line.strip()
        if not goal:
            continue
        if goal in (":q", ":quit", ":exit", "exit", "quit"):
            break
        if goal in (":h", ":help", "help"):
            print("  :q 退出   :h 帮助   :t 显示工具   :s 显示当前预算")
            continue
        if goal == ":t":
            for d in registry.describe():
                print(f"    {d['name']:<12} {d['approval']:<8} {d['description']}")
            continue
        if goal == ":s":
            print(f"    轮次≤{cfg.budgets.max_turns} 时长≤{cfg.budgets.max_seconds:.0f}s "
                  f"工具≤{cfg.budgets.max_tool_calls} 最小证据{cfg.budgets.min_evidence}")
            continue

        idx += 1
        print()
        last_code = await run_one(
            goal, workspace=root, adapter=adapter, registry=registry, cfg=cfg, args=args, index=idx,
            knowledge=knowledge,
        )
        print()

    print(f"{C.DIM if not args.no_color else ''}已退出。{C.RESET}")
    return last_code


def main() -> int:
    args = build_parser().parse_args()
    if not args.no_color:
        _enable_ansi()
    # ★ 这里不再需要预先「对齐 stdin 编码」——
    #   所有输入都走 console.read_line()，它拿原始字节做宽容解码，
    #   无论终端送 UTF-8 还是 GBK 都能读对。见 cli/console.py。
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
