"""
Loom Agent v0.1 · CLI 审批交互

★ 这是小陆明确要求的「CLI Approval Manager」。

────────────────────────────────────────────────────────────────
核心挑战：审核输入不能在事件循环里阻塞
────────────────────────────────────────────────────────────────
`input()` 是**同步阻塞**的。在 async 代码里直接调用它，会把整个事件循环
卡住 —— 那时 Agent 挂起在 await 上、事件渲染也在等，表面看好像「正好」，
但一旦你以后要在等待审批时同时做别的事（比如从 HTTP 收决策、
或同时维护心跳、或做超时倒数），就会立刻死锁。

所以这里用 **run_in_executor** 把 `input()` 丢到线程池：
    line = await loop.run_in_executor(None, input, prompt)

这样主事件循环始终能转，`asyncio.wait_for(...)` 的超时才能真正生效。

★ 这是「为什么审批用 Future 挂起」这个设计能成立的最后一环：
   挂起（approval.py）  +  不阻塞地读输入（本文件） = 完整的挂起-恢复
   少了任一半，系统都会卡住。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from ..agent.approval import ApprovalManager
from ..events import Event, EventType
from .console import read_line
from .render import C, TerminalRenderer


class CLIApprover:
    """
    从终端读审批决策，塞进 ApprovalManager。

    用法：
        approver = CLIApprover(manager)
        bus.subscribe(approver.on_event)      # 监听 approval.request
        task = asyncio.create_task(core.run(...))
        await approver.attend()               # 同时驱动输入循环
    """

    def __init__(
        self,
        manager: ApprovalManager,
        *,
        renderer: TerminalRenderer | None = None,
        auto: str = "",
        color: bool = True,
    ) -> None:
        self.manager = manager
        self.renderer = renderer or TerminalRenderer(color=color)
        self.color = color
        # auto: "" 手动 / "approve" 自动批准 / "reject" 自动拒绝（用于批跑与测试）
        self.auto = auto
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._handled: set[str] = set()
        self.count_approved = 0
        self.count_rejected = 0

    # ------------------------------------------------------------------ #
    # 事件接入
    # ------------------------------------------------------------------ #
    async def on_event(self, ev: Event) -> None:
        if ev.type == EventType.APPROVAL_REQUEST.value:
            await self._queue.put(ev)

    def _c(self, text: str, color: str) -> str:
        if not self.color:
            return text
        return f"{color}{text}{C.RESET}"

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    async def attend(self) -> None:
        """
        持续等待审批请求并处理，直到被取消。

        注意它是 `await` 的 —— 调用方应当 create_task 它，与 Agent 并行跑。
        """
        try:
            while True:
                ev = await self._queue.get()
                await self._handle(ev)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------ #
    async def _handle(self, ev: Event) -> None:
        d = ev.data
        aid = d.get("approval_id", "")
        if not aid or aid in self._handled:
            return
        self._handled.add(aid)

        # ---- 自动模式 ---------------------------------------------------
        if self.auto:
            await asyncio.sleep(0.05)  # 给渲染一点时间，日志顺序好看
            if self.auto == "approve":
                self.manager.approve(aid, "自动批准（--yes）")
                self.count_approved += 1
            else:
                self.manager.reject(aid, "自动拒绝（--no）")
                self.count_rejected += 1
            return

        # ---- 手动模式：不阻塞事件循环地读输入 ---------------------------
        while True:
            remaining = self.manager.remaining_of(aid)
            if remaining <= 0:
                return  # 已超时，approval.py 会自己处理

            prompt = self._build_prompt(remaining)
            try:
                raw = await self._read_line(prompt, timeout=remaining)
            except (EOFError, KeyboardInterrupt):
                print()
                self.manager.reject(aid, "输入中断")
                self.count_rejected += 1
                return

            if raw is self._EOF:
                # stdin 已关闭 —— 不可能再有用户输入，直接拒绝并唤醒 Future。
                # 不这么做就会永久挂起（见 _read_line 的注释）。
                self.manager.reject(aid, "stdin 已关闭（非交互环境）")
                self.count_rejected += 1
                return

            if raw is None:
                # 等超时了：交给 approval 层的超时机制（它会把状态置为 expired）
                return

            verdict = self._parse(raw)
            if verdict == "approve":
                self.manager.approve(aid, "用户在 CLI 批准")
                self.count_approved += 1
                return
            if verdict == "reject":
                reason = await self._read_reason()
                self.manager.reject(aid, reason or "用户在 CLI 拒绝")
                self.count_rejected += 1
                return
            if verdict == "abort":
                self.manager.reject(aid, "用户中止任务")
                self.count_rejected += 1
                raise KeyboardInterrupt("用户中止")
            # 无效输入 → 重问
            print(self._c("  无法识别，请输入 y / n / v / a", C.YELLOW))

    def _build_prompt(self, remaining: float) -> str:
        return (
            f"  {self._c('批准执行？', C.B_YELLOW)} "
            f"[{self._c('y', C.B_GREEN)}]批准  "
            f"[{self._c('n', C.B_RED)}]拒绝  "
            f"[{self._c('v', C.BLUE)}]查看完整内容  "
            f"[{self._c('a', C.B_RED)}]中止任务  "
            f"{self._c(f'（{int(remaining)}s 后超时）', C.DIM)} > "
        )

    # ------------------------------------------------------------------ #
    # 读一行输入的三种结局（★ 必须区分，否则会挂死）
    # ------------------------------------------------------------------ #
    #   "y\n"  → 正常拿到一行
    #   None    → 等超时（应交给 approval 层的超时机制处理）
    #   EOF     → stdin 关闭（管道跑完 / 重定向 </dev/null / Ctrl+D）
    #
    # ★ 为什么 EOF 必须单独区分：最初把 EOF 和超时都返回 None，
    #   上层 `if raw is None: return` 就直接不管了 —— 但 Future 从未被
    #   resolve，Agent 于是一直等在那里**挂死**（实测表现：跑批测时进程
    #   只能靠外层 timeout 强杀）。EOF 是「用户不可能再给输入了」，
    #   语义上明确等于拒绝，必须立刻 reject 把 Future 唤醒。
    _EOF = object()

    async def _read_line(self, prompt: str, timeout: float):
        """
        非阻塞读一行。关键：run_in_executor 把读取丢线程池 ——
        readline 是同步阻塞的，直接在协程里调会卡死事件循环，
        连 asyncio.wait_for 的超时都不会触发。

        ★ 读取走 console.read_line()（拿原始字节 + 宽容解码），
          不是 sys.stdin.readline()。原因见 console.py 的模块注释：
          stdin 那一层的编码假设在 MSYS / UTF-8 终端下不成立，
          会让用户输入的中文变成「鍒嗘瀽」这种乱码。

        返回：str（正常） / None（超时） / CLIApprover._EOF（stdin 已关闭）
        """
        loop = asyncio.get_running_loop()
        write = sys.stdout.write
        write(prompt)
        sys.stdout.flush()

        fut = loop.run_in_executor(None, read_line)
        try:
            line = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            write("\n")
            return None
        if line == "":     # EOF（stdin 关闭）
            write("\n")
            return self._EOF
        return line.strip()

    async def _read_reason(self) -> str:
        """可选：让用户补一句拒绝理由，会回灌给模型。"""
        try:
            raw = await self._read_line(
                f"  {self._c('拒绝理由（可留空，会告诉 Agent）：', C.DIM)}", timeout=30
            )
        except Exception:  # noqa: BLE001
            return ""
        if raw is None or raw is self._EOF:
            return ""
        return (raw or "").strip()

    @staticmethod
    def _parse(raw: str) -> str:
        s = raw.strip().lower()
        if s in ("y", "yes", "是", "批准", "同意", ""):
            # 空回车 = 批准（默认动作）；想要更保守可改成 reject
            return "approve"
        if s in ("n", "no", "否", "拒绝", "不"):
            return "reject"
        if s in ("a", "abort", "中止", "退出", "q"):
            return "abort"
        if s in ("v", "view"):
            return "view"
        return ""


# --------------------------------------------------------------------------- #
# 便捷入口：把「入队 + attend」包成一句话
# --------------------------------------------------------------------------- #
def attach(manager: ApprovalManager, bus: Any, *, auto: str = "", color: bool = True) -> CLIApprover:
    """
    把 CLI 审批接入事件总线与审批管理器。

        approver = attach(core.approvals, bus)
        task = asyncio.create_task(core.run(goal, workspace=ws))
        attender = asyncio.create_task(approver.attend())
        await task
        attender.cancel()
    """
    approver = CLIApprover(manager, auto=auto, color=color)
    bus.subscribe(approver.on_event)
    return approver
