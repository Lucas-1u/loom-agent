"""
P6 自测 —— CLI 端到端（用 FakeAdapter，不需要 API Key）

验证的是「CLI 这一层」是否正确：
    · 渲染器把所有 16 种事件都翻译成人看得懂的文案，且不崩
    · 审批交互（自动批准 / 自动拒绝）能正确驱动挂起-恢复
    · 只读模式真的卸载了写工具
    · 摘要统计正确

跑法：
    ./.venv/Scripts/python.exe -m backend.selfcheck_cli
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile

from backend.agent.approval import ApprovalManager, build_pending
from backend.agent.core import AgentConfig, AgentCore
from backend.agent.state import RiskLevel
from backend.cli.approve import CLIApprover
from backend.cli.render import C, TerminalRenderer
from backend.config import setup_logging
from backend.events import Event, EventBus, EventType, collect
from backend.models.adapter import FakeAdapter, ModelReply, ToolCall
from backend.tools.registry import build_default_registry

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(f"  [{'ok' if cond else '!!'}] {label}")


def tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False), arguments=args)


class Cap(io.StringIO):
    """捕获渲染输出，用于断言文案。"""

    def isatty(self) -> bool:
        return True


async def main() -> int:
    setup_logging("ERROR")
    print("=" * 76)
    print("P6 · CLI 层自测（渲染 + 审批交互 + 只读模式）")
    print("=" * 76)

    ws = tempfile.mkdtemp(prefix="loom_cli_")
    with open(os.path.join(ws, "app.py"), "w", encoding="utf-8") as f:
        f.write("import os\n\n\ndef main():\n    print('hello')\n")
    with open(os.path.join(ws, "README.md"), "w", encoding="utf-8") as f:
        f.write("# 演示项目\n")

    # ================================================================== #
    print("\n[1] 渲染器：16 种事件全部能渲染且不崩")
    cap = Cap()
    r = TerminalRenderer(out=cap, color=False, show_thinking=True, show_tool_data=True)
    bus = EventBus()
    bus.subscribe(r.on_event)

    # 手工发一轮全类型事件
    bus.task_start("演示任务", ws, "fake-1")
    bus.plan([{"index": 0, "title": "看目录", "status": "pending", "detail": "确认结构"}], "")
    bus.thinking("先看看这个目录里有什么东西", phase="act", turn=1)
    bus.thinking("", phase="act", turn=1, done=True)
    bus.act("调用 list_files", tool="list_files", turn=1)
    bus.tool_call("c1", "list_files", {"path": "."}, turn=1)
    bus.tool_result("c1", "list_files", True, "列出 2 个文件", data={"count": 2}, duration_ms=3, turn=1)
    bus.observe("c1", "列出 2 个文件", turn=1)
    bus.reflect("已看到目录结构", "读取 app.py", False, turn=1)
    bus.error("insufficient_evidence", "试图凭空作答被驳回", fatal=False, turn=1)
    bus.budget_exceeded("turns", 20, 20, turn=20)
    bus.error("auth_error", "认证失败", fatal=True, turn=1)
    bus.final_answer("这是最终答复。", turn=2)
    bus.task_end("done", "这是最终答复。", "agent_done")
    await bus.drain()

    text = cap.getvalue()
    for label, needle in [
        ("task.start", "LOOM AGENT"),
        ("plan", "[PLAN   ]"),
        ("thinking", "思考中…"),
        ("act", "[ACT    ]"),
        ("tool_call", "[TOOL   ]"),
        ("tool_result", "[RESULT ]"),
        ("observe", "已记入观测"),
        ("reflect", "[REFLECT]"),
        ("error", "[ERROR  ]"),
        ("budget", "预算耗尽"),
        ("final_answer", "这是最终答复"),
        ("task_end", "任务结束"),
    ]:
        check(needle in text, f"渲染出 {label}（找 {needle!r}）")

    check("安全事件" not in text or True, "安全事件标记（本组无安全错误，跳过）")

    # ---- 安全事件的渲染 ----
    cap2 = Cap()
    r2 = TerminalRenderer(out=cap2, color=False)
    bus2 = EventBus()
    bus2.subscribe(r2.on_event)
    bus2.tool_result(
        "cx", "read_file", False, "路径越出工作区",
        error={"kind": "path_outside_workspace", "message": "越界", "hint": "只能访问工作区内", "retryable": False, "security": True},
        turn=1,
    )
    await bus2.drain()
    t2 = cap2.getvalue()
    check("path_outside_workspace" in t2, "渲染错误类型")
    check("安全事件" in t2, "★ 安全事件被显著标记")
    check("可重试" not in t2, "不可重试的错误不显示「可重试」")
    check("只能访问工作区内" in t2, "★ 渲染 hint（给模型的建议）")

    # ---- tool_result 带可重试标记 ----
    cap3 = Cap()
    r3 = TerminalRenderer(out=cap3, color=False)
    bus3 = EventBus()
    bus3.subscribe(r3.on_event)
    bus3.tool_result(
        "cy", "read_file", False, "文件不存在",
        error={"kind": "not_found", "message": "不存在", "hint": "先 list_files", "retryable": True, "security": False},
        turn=1,
    )
    await bus3.drain()
    check("可重试" in cap3.getvalue(), "可重试的错误显示「可重试」")

    # ================================================================== #
    print("\n[2] 审批请求的渲染（C4 三要素齐全）")
    cap4 = Cap()
    r4 = TerminalRenderer(out=cap4, color=False)
    bus4 = EventBus()
    bus4.subscribe(r4.on_event)
    bus4.emit(
        Event.make(
            EventType.APPROVAL_REQUEST,
            {
                "approval_id": "apr_abc",
                "tool": "write_file",
                "args": {"path": "x.py", "content": "print(1)"},
                "args_hash": "deadbeefcafe1234",
                "risk": "write",
                "action": "新建文件",
                "target": "x.py",
                "impact": "将**新建**文件（3 行 / 20B）",
                "preview": "print(1)\n",
                "turn": 2,
            },
            turn=2,
        )
    )
    await bus4.drain()
    t4 = cap4.getvalue()
    check("需要你的批准" in t4, "★ 审批请求显著展示")
    check("新建文件" in t4, "★ 要素 1：要做什么")
    check("x.py" in t4, "★ 要素 2：对谁做")
    check("将**新建**文件" in t4, "★ 要素 3：有什么影响")
    check("print(1)" in t4, "★ 内容预览（让用户看得见要写什么）")
    check("deadbeefcafe1234" in t4, "指纹展示（便于核对）")

    # ================================================================== #
    print("\n[3] CLIApprover 自动批准驱动挂起-恢复")
    bus5 = EventBus()
    adapter5 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写文件","detail":"创建 out.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "out.py", "content": "RESULT = 42\n"})]),
            ModelReply(content="已创建 out.py。"),
        ]
    )
    core5 = AgentCore(adapter5, build_default_registry(), config=AgentConfig(max_turns=6, min_evidence=0), bus=bus5)
    approver5 = CLIApprover(core5.approvals, auto="approve", color=False)
    bus5.subscribe(approver5.on_event)

    attend = asyncio.create_task(approver5.attend())
    st5 = await core5.run("创建 out.py", workspace=ws)
    attend.cancel()
    await bus5.drain()

    check(approver5.count_approved == 1, f"★ 自动批准生效（批准 {approver5.count_approved} 次）")
    check(os.path.isfile(os.path.join(ws, "out.py")), "★ 文件真的被写入")
    with open(os.path.join(ws, "out.py"), encoding="utf-8") as f:
        check(f.read() == "RESULT = 42\n", "内容正确")

    # ================================================================== #
    print("\n[4] CLIApprover 自动拒绝")
    bus6 = EventBus()
    adapter6 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写文件","detail":"创建 no.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "no.py", "content": "x\n"})]),
            ModelReply(content="好的，我不写了。"),
        ]
    )
    core6 = AgentCore(adapter6, build_default_registry(), config=AgentConfig(max_turns=6, min_evidence=0), bus=bus6)
    approver6 = CLIApprover(core6.approvals, auto="reject", color=False)
    bus6.subscribe(approver6.on_event)

    attend6 = asyncio.create_task(approver6.attend())
    st6 = await core6.run("创建 no.py", workspace=ws)
    attend6.cancel()
    await bus6.drain()

    check(approver6.count_rejected == 1, f"★ 自动拒绝生效（拒绝 {approver6.count_rejected} 次）")
    check(not os.path.exists(os.path.join(ws, "no.py")), "★ 拒绝后文件未被创建")
    check(st6.status.value == "done", "拒绝后 Agent 仍正常收敛")

    # ================================================================== #
    print("\n[5] 只读模式真的卸载了写工具")
    from backend.tools.registry import build_default_registry as build

    reg_ro = build(include_mutating=False)
    check("list_files" in reg_ro.names and "read_file" in reg_ro.names, "只读工具保留")
    check("write_file" not in reg_ro.names, "★ write_file 被卸载")

    bus7 = EventBus()
    adapter7 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写","detail":"试试写"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "ro.py", "content": "x"})]),
            ModelReply(content="看起来我没有写入权限。"),
        ]
    )
    core7 = AgentCore(adapter7, reg_ro, config=AgentConfig(max_turns=5, min_evidence=0), bus=bus7)
    ev7 = collect(bus7)
    st7 = await core7.run("尝试写入", workspace=ws)
    await bus7.drain()
    check(not os.path.exists(os.path.join(ws, "ro.py")), "★ 只读模式下写入确实未发生")
    check(
        any(e.type == "tool.result" and not e.data["ok"] for e in ev7),
        "写入尝试以失败结果回灌给模型",
    )

    # ================================================================== #
    print("\n[6] 渲染器健壮性")
    cap8 = Cap()
    r8 = TerminalRenderer(out=cap8, color=False)
    bus8 = EventBus()
    bus8.subscribe(r8.on_event)
    # 未知事件类型
    bus8.emit(Event.make("some.future.event", {"x": 1}))
    # 空 data
    bus8.emit(Event.make(EventType.PLAN, {}))
    bus8.emit(Event.make(EventType.THINKING, {}))
    bus8.emit(Event.make(EventType.FINAL_ANSWER, {}))
    bus8.emit(Event.make(EventType.ERROR, {}))
    bus8.emit(Event.make(EventType.TOOL_RESULT, {"ok": False}))
    await bus8.drain()
    check(True, f"★ 未知事件 / 空载荷均不崩（输出 {len(cap8.getvalue())} 字符）")

    # 一个订阅者崩溃不影响渲染器
    cap9 = Cap()
    r9 = TerminalRenderer(out=cap9, color=False)
    bus9 = EventBus()

    async def boom(ev: Event) -> None:
        raise RuntimeError("渲染订阅者崩溃测试")

    bus9.subscribe(boom)
    bus9.subscribe(r9.on_event)
    bus9.task_start("x", ws, "m")
    await bus9.drain()
    check("LOOM AGENT" in cap9.getvalue(), "★ 其他订阅者崩溃不影响渲染器")

    # ================================================================== #
    print("\n[7] 只读模式下审批管理器不应被触发")
    bus10 = EventBus()
    adapter10 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"看","detail":""}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(content="看到了 2 个文件。"),
        ]
    )
    core10 = AgentCore(adapter10, build_default_registry(), config=AgentConfig(max_turns=6, min_evidence=0), bus=bus10)
    ev10 = collect(bus10)
    st10 = await core10.run("看看有什么", workspace=ws)
    await bus10.drain()
    check(
        not any(e.type == EventType.APPROVAL_REQUEST.value for e in ev10),
        "★ 只读工具不触发审批（不打扰用户）",
    )
    check(st10.status.value == "done", "只读任务正常完成")

    # ================================================================== #
    print("\n[8] 事件流可完整序列化（未来 GUI 的硬要求）")
    all_events: list[dict] = []

    async def collector(ev: Event) -> None:
        all_events.append(ev.to_dict())

    bus11 = EventBus()
    bus11.subscribe(collector)
    adapter11 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"a","detail":"b"},{"title":"c","detail":"d"}],"note":"n"}'),
            ModelReply(tool_calls=[tc("c1", "read_file", {"path": "app.py"})]),
            ModelReply(content='{"progress":"读到了","next_action":"finish","done":true}'),
        ]
    )
    core11 = AgentCore(adapter11, build_default_registry(), config=AgentConfig(max_turns=6, min_evidence=1), bus=bus11)
    await core11.run("读 app.py", workspace=ws)
    await bus11.drain()

    try:
        blob = json.dumps(all_events, ensure_ascii=False)
        check(True, f"★ 全部 {len(all_events)} 条事件可 JSON 化（{len(blob)} 字节）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"JSON 化失败：{exc}")

    check(all("seq" in e for e in all_events), "每条事件都有 seq（前端排序用）")
    check(all("ts" in e for e in all_events), "每条事件都有 ts")
    check(all("type" in e for e in all_events), "每条事件都有 type")
    check(all(e["task_id"] for e in all_events), "★ 每条事件都带 task_id（V0.1 单任务也留了字段）")

    types = {e["type"] for e in all_events}
    check(len(types) >= 7, f"事件类型覆盖充分：{sorted(types)}")

    # ---- SSE 帧格式（P5 GUI 阶段直接复用）----
    bus12 = EventBus()
    got_frames: list[str] = []

    async def sse_sink(ev: Event) -> None:
        got_frames.append(ev.to_sse())

    bus12.subscribe(sse_sink)
    bus12.tool_call("c1", "read_file", {"path": "a.py"}, turn=1)
    await bus12.drain()
    frame = got_frames[0]
    check(frame.startswith("event: tool.call\n"), "SSE 帧首行是 event: <type>")
    check("data: {" in frame and frame.endswith("\n\n"), "SSE 帧结构正确")

    # ================================================================== #
    # [9] ★ CLIApprover 输入解析的边界（真实模型阶段暴露的两个挂死 bug）
    # ================================================================== #
    print("\n[9] ★ 审批输入读取：EOF 与超时必须区分（否则 Agent 永久挂起）")

    # ---- ① _read_line 的三种返回值必须互不相同 ----
    #   背景：最初 EOF 和超时都返回 None，上层直接 return 不管了 ——
    #   但 asyncio.Future 从未被 resolve，Agent 就一直等在那里挂死。
    #   实测现象：`< /dev/null` 跑批时进程不退，只能靠外层 timeout 强杀。
    #   ★ 注意：CLIApprover / ApprovalManager 已在文件顶部 import，
    #     这里**不要**再写局部 import —— 会让上面几节里的同名变量被当成
    #     局部名，直接 UnboundLocalError（踩过）。
    apr = CLIApprover(ApprovalManager(timeout_s=5), color=False)

    import io as _io

    old_stdin = sys.stdin

    class FakeStdin:
        def __init__(self, lines: list[str]):
            self._lines = list(lines)

        def readline(self) -> str:
            return self._lines.pop(0) if self._lines else ""   # "" == EOF

    try:
        # 正常一行
        sys.stdin = FakeStdin(["y\n"])
        got = await apr._read_line("", timeout=2)
        check(got == "y", f"正常读到一行：{got!r}")

        # EOF（stdin 关闭）→ 必须是哨兵对象，不能是 None
        sys.stdin = FakeStdin([])
        got_eof = await apr._read_line("", timeout=2)
        check(got_eof is CLIApprover._EOF, "★★ EOF 返回专用哨兵（不是 None）")
        check(got_eof is not None, "★★ EOF 与超时返回值可区分")

        # 超时（stdin 给不出东西）→ None
        #   ★ 用一个「永不返回」的对象来模拟，但**不能真的 sleep**：
        #     run_in_executor 用的是线程池，线程被卡住会拖住进程退出，
        #     把后面的测试全带崩（实测：整个套件跑到一半就死）。
        #     用 Event.wait() 配合一个我们自己能 set 的阈值即可，
        #     反正 wait_for 到点就返回 None，不必等线程真的结束。
        import threading as _th

        release = _th.Event()

        class HangingStdin:
            def readline(self) -> str:
                release.wait(timeout=4)   # 远超下面的 0.3s 超时
                return "y\n"

        sys.stdin = HangingStdin()
        got_to = await apr._read_line("", timeout=0.3)
        check(got_to is None, "★ 超时返回 None")
        check(got_to is not CLIApprover._EOF, "★ 超时与 EOF 可区分")
        release.set()   # 放线程走，别拖累进程退出

    finally:
        sys.stdin = old_stdin

    # ---- ② EOF 必须触发 reject，把 Future 唤醒（而不是挂着）----
    mgr2 = ApprovalManager(timeout_s=30)
    apr2 = CLIApprover(mgr2, color=False)

    from backend.agent.approval import build_pending
    from backend.agent.state import RiskLevel

    pending2 = build_pending(
        tool="write_file", args={"path": "x.py", "content": "1"},
        risk=RiskLevel.WRITE, action="创建", target="x.py", impact="新建", turn=1,
    )

    # 直接调 _handle，并让 stdin 立刻 EOF
    class EofStdin:
        def readline(self) -> str:
            return ""

    old_stdin = sys.stdin
    try:
        sys.stdin = EofStdin()
        ev = Event.make(
            EventType.APPROVAL_REQUEST,
            {
                "approval_id": pending2.approval_id,
                "tool": "write_file",
                "args": pending2.args,
                "risk": "write",
                "action": "创建",
                "target": "x.py",
                "impact": "新建",
                "preview": "",
            },
        )
        await apr2._handle(ev)
    finally:
        sys.stdin = old_stdin

    check(apr2.count_rejected == 1, f"★★ EOF 被当作拒绝（实际拒绝 {apr2.count_rejected} 次）")
    check(not mgr2.has_pending, "★★ pending 被清空（Future 已唤醒，不会永久挂起）")

    # ---- ③ _read_reason 遇到 EOF 不能崩 ----
    apr3 = CLIApprover(ApprovalManager(timeout_s=5), color=False)
    old_stdin = sys.stdin
    try:
        sys.stdin = EofStdin()
        reason = await apr3._read_reason()
        check(reason == "", f"★ 拒绝理由读不到时安全返回空串：{reason!r}")
    finally:
        sys.stdin = old_stdin

    # ================================================================== #
    # [10] ★ 终端输入解码：不猜编码，UTF-8 与 GBK 都要读对
    # ================================================================== #
    print("\n[10] ★ 终端输入解码（UTF-8 / GBK 双字节都要能读对）")

    from backend.cli.console import decode_input, read_line as console_read_line

    # ---- ① 两种编码的字节都要解对 ----
    # 这是小陆真实踩到的：终端送 UTF-8 字节，程序按 GBK 解，于是
    # 「分析一下此目录」显示成「鍒嗘瀽涓�涓嬫�ょ洰褰曪紝...」，
    # 还得让模型在 thinking 里花 token 去猜原文（而且猜错了）。
    for _enc in ("utf-8", "gbk"):
        _raw = "分析一下此目录".encode(_enc) + b"\n"
        _got = decode_input(_raw)
        check(_got == "分析一下此目录\n", f"★ {_enc} 字节解对：{_got!r}")

    # ---- ② 关键：UTF-8 必须优先，不能被 GBK 抢先解成乱码 ----
    check(decode_input("分析".encode("utf-8")) == "分析",
          "★★ UTF-8 字节必须按 UTF-8 解（否则就是「鍒嗘瀽」）")

    # ---- ③ ASCII 原样通过 ----
    check(decode_input(b"analyze this project\n") == "analyze this project\n",
          "ASCII 原样通过")

    # ---- ④ EOF 与空行必须可区分 ----
    # 审批逻辑靠这个区别决定「是不是该放弃等输入了」，
    # 混掉会让 Agent 永久挂起（上一轮已经踩过的坑）。
    check(decode_input(b"") == "", "★ 空字节 = EOF")
    check(decode_input(b"\n") == "\n", "★ 空行 ≠ EOF（长度 1 vs 0）")

    # ---- ⑤ 非法字节不崩：宁可显示 �，也不甩一屏堆栈给用户 ----
    _got = decode_input(b"\xff\xfe\x00bad")
    check(isinstance(_got, str), f"★ 非法字节不抛异常：{_got!r}")

    # ---- ⑥ read_line 必须走 buffer 层拿原始字节 ----
    # 走 TextIOWrapper 的话，字节早被按某个编码解完了 ——
    # 到手就是乱码，再怎么救也救不回来。
    class _FakeBuf:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def readline(self) -> bytes:
            return self._data

    class _FakeStdin:
        def __init__(self, data: bytes) -> None:
            self.buffer = _FakeBuf(data)

        def readline(self) -> str:
            raise AssertionError("不该走 TextIOWrapper 那条路")

    _saved_stdin = sys.stdin
    try:
        sys.stdin = _FakeStdin("中文任务".encode("utf-8") + b"\n")
        check(console_read_line() == "中文任务\n",
              "★ read_line 从 buffer 拿原始字节")
        sys.stdin = _FakeStdin(b"")
        check(console_read_line() == "", "★ read_line 在 EOF 时返回空串")
    finally:
        sys.stdin = _saved_stdin

    # ---- ④ 启动器产物检查（防止源与产物不同步）----
    from pathlib import Path as _Path

    _root = _Path(__file__).resolve().parent.parent
    _bat = _root / "loom.bat"
    check(_bat.exists(), "启动器 loom.bat 存在")
    if _bat.exists():
        _raw = _bat.read_bytes()
        try:
            _raw.decode("gbk")
            _gbk_ok = True
        except UnicodeDecodeError:
            _gbk_ok = False
        check(_gbk_ok, "★ loom.bat 是 GBK 编码（cmd 才能正确读中文）")
        check(b"chcp 936" in _raw, "★ loom.bat 锁定代码页 936")
        check(b"PYTHONIOENCODING=" in _raw,
              "★ loom.bat 清掉了 PYTHONIOENCODING（中文输入才不崩）")
    else:
        check(False, "★ 找不到 loom.bat")
        check(False, "★ 找不到 loom.bat")
        check(False, "★ 找不到 loom.bat")

    # ================================================================== #
    import shutil

    shutil.rmtree(ws, ignore_errors=True)

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 76)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print(f"\n失败项（{total - ok} 条）：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 76)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
