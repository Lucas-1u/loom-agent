"""
Loom Agent v0.1 · 终端事件渲染器

★ 这是小陆要求的「Agent 运行日志区」在 CLI 上的实现。

设计原则
--------
1. **事件是事实，渲染是观点**。本模块只负责把事件**翻译成人看的文案**，
   不做任何判断、不丢信息。所有逻辑判断都在事件产生处，不在这里。

2. **标签化**。严格对齐小陆要求的标签体系，并扩展到 16 种事件：
       [USER] [PLAN] [THINK] [ACT] [TOOL] [RESULT] [OBSERVE] [REFLECT]
       [APPROVE] [DENY] [EXPIRE] [ANSWER] [ERROR] [BUDGET] [END] [SYS]

3. **可回放**。同一个事件流，用不同的渲染器可以得到不同视图
   （终端彩色版 / JSON 版 / 精简版）。未来 GUI 用的是第 4 种。
   —— 这就是「事件驱动」的另半个好处：**同一份事实，多种呈现**。
   老项目直接把日志 print 出来，这种复用就做不到了。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from ..events import Event, EventType

# --------------------------------------------------------------------------- #
# ANSI 配色（Windows 10+ 终端支持；不支持时自动降级为无色）
# --------------------------------------------------------------------------- #
class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    B_RED = "\033[91m"
    B_GREEN = "\033[92m"
    B_YELLOW = "\033[93m"
    B_BLUE = "\033[94m"
    B_MAGENTA = "\033[95m"
    B_CYAN = "\033[96m"


def _enable_ansi() -> None:
    """Windows 终端开启 ANSI 转义支持。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle_id in (-11, -12):  # stdout, stderr
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 标签定义
# --------------------------------------------------------------------------- #
# (标签, 颜色) —— 顺序对齐小陆要求的 [USER][PLAN][THINKING][TOOL][RESULT][ANSWER]
TAGS: dict[str, tuple[str, str]] = {
    EventType.TASK_START.value:     ("SYS    ", C.B_CYAN),
    EventType.TASK_END.value:       ("END    ", C.B_CYAN),
    EventType.PLAN.value:           ("PLAN   ", C.B_MAGENTA),
    EventType.THINKING.value:       ("THINK  ", C.GRAY),
    EventType.ACT.value:            ("ACT    ", C.B_BLUE),
    EventType.TOOL_CALL.value:      ("TOOL   ", C.B_BLUE),
    EventType.TOOL_RESULT.value:    ("RESULT ", C.CYAN),
    EventType.OBSERVE.value:        ("OBSERVE", C.CYAN),
    EventType.APPROVAL_REQUEST.value: ("APPROVE", C.B_YELLOW),
    EventType.APPROVAL_DECISION.value: ("DECIDE ", C.B_YELLOW),
    EventType.APPROVAL_EXPIRED.value:  ("EXPIRE ", C.B_YELLOW),
    EventType.REFLECT.value:        ("REFLECT", C.MAGENTA),
    EventType.FINAL_ANSWER.value:   ("ANSWER ", C.B_GREEN),
    EventType.ERROR.value:          ("ERROR  ", C.B_RED),
    EventType.BUDGET_EXCEEDED.value: ("BUDGET ", C.B_RED),
    EventType.STATE_SNAPSHOT.value: ("STATE  ", C.GRAY),
}


def tag_of(ev_type: str) -> tuple[str, str]:
    return TAGS.get(ev_type, ("?      ", C.RESET))


# --------------------------------------------------------------------------- #
# 渲染器
# --------------------------------------------------------------------------- #
class TerminalRenderer:
    """
    终端渲染器。把它作为订阅者挂到 EventBus 上即可。

        renderer = TerminalRenderer()
        bus.subscribe(renderer.on_event)

    ★ 注意它是 async 的 —— 因为事件总线的订阅者签名是 async。
      但内部只做 print，不 await 任何东西（渲染绝不该阻塞 Agent）。
    """

    def __init__(
        self,
        out: TextIO | None = None,
        *,
        color: bool = True,
        show_thinking: bool = True,
        show_tool_data: bool = False,
        max_data_chars: int = 600,
        compact: bool = False,
    ) -> None:
        self.out = out or sys.stdout
        self.color = color
        self.show_thinking = show_thinking
        self.show_tool_data = show_tool_data
        self.max_data_chars = max_data_chars
        self.compact = compact
        self._thinking_open = False   # thinking 是流式的，需要管理「上一行是否未闭合」
        if color:
            _enable_ansi()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _c(self, text: str, color: str) -> str:
        if not self.color:
            return text
        return f"{color}{text}{C.RESET}"

    def _w(self, text: str = "") -> None:
        # thinking 流式输出会一行行写，需要先闭合上一行
        if self._thinking_open:
            self.out.write("\n")
            self._thinking_open = False
        self.out.write(text + "\n")
        self.out.flush()

    def _prefix(self, ev: Event) -> str:
        tag, color = tag_of(ev.type)
        turn = f"t{ev.turn:<2}" if ev.turn else "   "
        return f"{self._c(f'[{tag}]', color)} {self._c(turn, C.DIM)} "

    def _truncate(self, text: str, limit: int | None = None) -> str:
        limit = limit or self.max_data_chars
        text = str(text)
        if len(text) <= limit:
            return text
        return text[:limit] + self._c(f"…(+{len(text) - limit})", C.DIM)

    # ------------------------------------------------------------------ #
    # 事件分发
    # ------------------------------------------------------------------ #
    async def on_event(self, ev: Event) -> None:
        handler = getattr(self, f"_on_{ev.type.replace('.', '_')}", None)
        if handler is not None:
            handler(ev)
        else:
            self._w(self._prefix(ev) + self._c(f"({ev.type})", C.DIM))

    # ---- 各类型 ------------------------------------------------------- #
    def _on_task_start(self, ev: Event) -> None:
        d = ev.data
        w = 74
        self._w(self._c("┌" + "─" * w, C.CYAN))
        self._w(
            self._c("│ ", C.CYAN)
            + self._c("LOOM AGENT", C.B_CYAN + C.BOLD)
            + self._c(f"  ·  {d.get('model', '')}", C.DIM)
        )
        self._w(self._c("│ ", C.CYAN) + f"任务：{self._truncate(d.get('goal', ''), 60)}")
        self._w(self._c("│ ", C.CYAN) + self._c(f"工作区：{d.get('workspace', '')}", C.DIM))
        self._w(self._c("└" + "─" * w, C.CYAN))

    def _on_task_end(self, ev: Event) -> None:
        d = ev.data
        status = d.get("status", "")
        color = {"done": C.B_GREEN, "aborted": C.B_YELLOW, "failed": C.B_RED}.get(status, C.RESET)
        self._w()
        self._w(self._c(f"═══ 任务结束：{status}  ({d.get('reason', '')}) ", color) + self._c("═" * 40, C.DIM))
        if d.get("answer"):
            self._w()
            self._w(self._c(d["answer"], color))
        self._w()

    def _on_plan(self, ev: Event) -> None:
        p = self._prefix(ev)
        steps = ev.data.get("steps") or []
        note = ev.data.get("note") or ""
        if not steps:
            self._w(p + self._c("（无结构化计划）", C.DIM) + (f"  {note}" if note else ""))
            return

        # 计算完成度，让「计划推进」这件事在终端上看得见
        done = sum(1 for s in steps if s.get("status") == "done")
        head = f"计划 {len(steps)} 步"
        if done:
            head += self._c(f"（已完成 {done}/{len(steps)}）", C.GREEN)
        self._w(p + self._c(head + "：", C.BOLD))

        marks = {
            "done": ("[x]", C.GREEN),
            "in_progress": ("[>]", C.B_YELLOW),
            "failed": ("[!]", C.B_RED),
            "skipped": ("[-]", C.GRAY),
            "pending": ("[ ]", C.DIM),
        }
        for i, s in enumerate(steps, 1):
            mark, color = marks.get(s.get("status", "pending"), ("[ ]", C.DIM))
            title = s.get("title", "")
            detail = s.get("detail", "")
            line = f"{' ' * 11}{self._c(mark, color)} {i}. {title}"
            if detail and not self.compact:
                line += self._c(f"  — {detail}", C.DIM)
            self._w(line)
        if note:
            self._w(f"{' ' * 11}" + self._c(f"备注：{note}", C.DIM))

    def _on_thinking(self, ev: Event) -> None:
        """
        流式思维链：不换行地追加，形成「模型正在实时思考」的观感。

        ★ 这是整个 CLI 体验里最能体现「Agent 内部过程可见」的地方。
          老项目也有思考链，但只是攒起来最后一次性塞进回复里。
        """
        if not self.show_thinking:
            return
        text = ev.data.get("text", "")
        done = ev.data.get("done", False)
        if done:
            if self._thinking_open:
                self.out.write("\n")
                self._thinking_open = False
            return
        if not text:
            return
        if not self._thinking_open:
            self.out.write(self._prefix(ev) + self._c("思考中… ", C.GRAY))
            self._thinking_open = True
        self.out.write(self._c(self._truncate(text, 200), C.GRAY))
        self.out.flush()

    def _on_act(self, ev: Event) -> None:
        intent = ev.data.get("intent", "")
        tool = ev.data.get("tool", "")
        self._w(self._prefix(ev) + f"{intent}" + (self._c(f"  [{tool}]", C.DIM) if tool else ""))

    def _on_tool_call(self, ev: Event) -> None:
        d = ev.data
        args = d.get("args", {})
        self._w(
            self._prefix(ev)
            + self._c("→ ", C.B_BLUE)
            + self._c(d.get("tool", ""), C.BOLD)
            + self._c(self._fmt_args(args), C.DIM)
        )

    def _on_tool_result(self, ev: Event) -> None:
        d = ev.data
        ok = d.get("ok")
        mark = self._c("✓", C.B_GREEN) if ok else self._c("✗", C.B_RED)
        dur = d.get("duration_ms") or 0
        line = self._prefix(ev) + f"{mark} {self._truncate(d.get('summary', ''), 110)}"
        if dur:
            line += self._c(f"  {dur}ms", C.DIM)
        self._w(line)

        err = d.get("error")
        if err and not ok:
            kind = err.get("kind", "")
            hint = err.get("hint", "")
            flags = []
            if err.get("security"):
                flags.append(self._c("安全事件", C.B_RED))
            if err.get("retryable"):
                flags.append(self._c("可重试", C.DIM))
            head = f"{' ' * 11}" + self._c(f"└ {kind}", C.RED)
            if flags:
                head += " " + " ".join(flags)
            self._w(head)
            if hint and not self.compact:
                self._w(f"{' ' * 13}" + self._c(f"建议：{hint}", C.DIM))

        if self.show_tool_data and d.get("data") is not None:
            payload = json.dumps(d["data"], ensure_ascii=False, indent=2, default=str)
            for line in payload.splitlines()[:30]:
                self._w(f"{' ' * 11}" + self._c(line, C.DIM))

    def _on_observe(self, ev: Event) -> None:
        if self.compact:
            return
        digest = ev.data.get("digest", "")
        if digest:
            self._w(f"{' ' * 11}" + self._c(f"└ 已记入观测：{self._truncate(digest, 90)}", C.DIM))

    def _on_reflect(self, ev: Event) -> None:
        d = ev.data
        done = d.get("done")
        mark = self._c("完成", C.B_GREEN) if done else self._c("继续", C.YELLOW)
        self._w(
            self._prefix(ev)
            + f"{mark}  "
            + self._truncate(d.get("progress", ""), 90)
        )
        nxt = d.get("next_action", "")
        if nxt and not self.compact:
            self._w(f"{' ' * 11}" + self._c(f"下一步：{self._truncate(nxt, 80)}", C.DIM))

    def _on_approval_request(self, ev: Event) -> None:
        """★ 审批请求的展示 —— 这是 CLI 最关键的界面。三要素必须齐全。"""
        d = ev.data
        self._w()
        self._w(self._c("┏━━ 需要你的批准 ", C.B_YELLOW) + self._c("━" * 50, C.YELLOW))
        self._w(self._c("┃ ", C.YELLOW) + self._c(f"{d.get('action', '')}", C.BOLD) + f"  {self._c(d.get('target', ''), C.B_YELLOW)}")
        if d.get("impact"):
            for line in str(d["impact"]).splitlines():
                self._w(self._c("┃ ", C.YELLOW) + self._c(line, C.DIM))
        if d.get("preview"):
            self._w(self._c("┃ ", C.YELLOW) + self._c("内容预览：", C.DIM))
            for line in str(d["preview"]).splitlines()[:20]:
                self._w(self._c("┃   ", C.YELLOW) + line)
        if d.get("args_hash"):
            self._w(self._c("┃ ", C.YELLOW) + self._c(f"指纹 {d['args_hash']}   id {d.get('approval_id', '')}", C.DIM))
        self._w(self._c("┗" + "━" * 60, C.YELLOW))

    def _on_approval_decision(self, ev: Event) -> None:
        d = ev.data
        if d.get("approved"):
            self._w(self._c("  ✔ 已批准", C.B_GREEN) + self._c(f"（{d.get('waited_s', 0)}s）", C.DIM))
        else:
            self._w(self._c("  ✘ 已拒绝", C.B_RED) + self._c(f"（{d.get('reason', '')}）", C.DIM))

    def _on_approval_expired(self, ev: Event) -> None:
        d = ev.data
        self._w(self._c("  ⏱ 等待超时", C.B_YELLOW) + self._c(f"（{d.get('reason', '')}）", C.DIM))

    def _on_final_answer(self, ev: Event) -> None:
        text = ev.data.get("text", "")
        self._w()
        self._w(self._c("─" * 76, C.GREEN))
        for line in str(text).splitlines():
            self._w(self._c("  ", C.GREEN) + line)
        self._w(self._c("─" * 76, C.GREEN))

    def _on_error(self, ev: Event) -> None:
        d = ev.data
        fatal = d.get("fatal")
        color = C.B_RED if fatal else C.YELLOW
        self._w(
            self._prefix(ev)
            + self._c(("致命错误 " if fatal else "错误 ") + f"[{d.get('kind', '')}]", color)
            + f" {self._truncate(d.get('message', ''), 120)}"
        )

    def _on_budget_exceeded(self, ev: Event) -> None:
        d = ev.data
        self._w(
            self._prefix(ev)
            + self._c(f"预算耗尽：{d.get('kind')} {d.get('used')}/{d.get('limit')} → 强制收敛", C.B_RED)
        )

    def _on_state_snapshot(self, ev: Event) -> None:
        if self.compact:
            return
        d = ev.data
        self._w(
            self._prefix(ev)
            + self._c(
                f"状态 {d.get('status')}  轮次 {d.get('turn')}  "
                f"工具 {len(d.get('tool_events', []))}  证据 {len(d.get('evidence_summary', []))}",
                C.DIM,
            )
        )

    # ------------------------------------------------------------------ #
    def _fmt_args(self, args: dict[str, Any]) -> str:
        if not args:
            return "()"
        parts = []
        for k, v in args.items():
            if isinstance(v, str) and len(v) > 60:
                parts.append(f"{k}={v[:50]!r}…")
            else:
                parts.append(f"{k}={v!r}")
        return "(" + ", ".join(parts) + ")"
