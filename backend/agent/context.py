"""
Loom Agent v0.1 · 上下文构建（State → messages）

★ 本模块的定位：**单向派生**。

    TaskState ──派生──▶ messages[] ──▶ 模型

  反向**绝不成立**。程序永远是 TaskState 说了算。
  这样做的直接好处：上下文可以被任意裁剪、压缩、重排，
  而程序的判断（证据够不够、轮次用完没）丝毫不受影响。

────────────────────────────────────────────────────────────────
为什么需要单独一层，而不是边跑边 append 消息？
────────────────────────────────────────────────────────────────
老项目就是边跑边 append（messages 数组从头传到尾），带来三个问题：

  ① **无法裁剪**。上下文一长，要么爆窗口，要么历史全丢。
  ② **无法重放**。要重现一次运行，得把消息数组存下来。
  ③ **协议脆弱**。OpenAI 要求带 tool_calls 的 assistant 消息后面
     必须紧跟对应数量的 tool 消息。手工 append 极易破坏这个不变量
     （比如工具被 Policy 拒绝、没产生 tool 消息，就 400 了）。

  用「每次重新构建」的方式，这些不变量能在**一个地方**统一保证。

────────────────────────────────────────────────────────────────
上下文组成（从稳定到易变，这是刻意的顺序）
────────────────────────────────────────────────────────────────
    [0] system      稳定：身份 + 边界 + 工具清单   ← 尽量不变，可命中前缀缓存
    [1] user        稳定：用户任务原文
    [2] plan        ★ 半稳定：当前计划（PLAN 阶段产物）
    [3..n] 历史轮次   易变：assistant(tool_calls) / tool(result) / assistant(text)
    [n+1] 提醒      ★ 易变：当前进度提醒（轮次、证据数、待办）

为什么要单独注入「计划」和「当前进度提醒」？
  → 因为模型的注意力有限。把「我们要做什么」和「现在到哪了」
    显式放在离回答最近的位置，比让它自己从 20 条历史里推断要可靠得多。
"""

from __future__ import annotations

from typing import Any

from ..models.adapter import Message, ToolCall
from ..config import MAX_TOOL_RESULT_CHARS
from .state import TaskState, TaskStatus


# --------------------------------------------------------------------------- #
# 轮次消息组
# --------------------------------------------------------------------------- #
class TurnBuilder:
    """
    构建一个轮次的消息组，并保证 OpenAI 协议不变量。

    ★ 不变量：「带 tool_calls 的 assistant，后面必须紧跟**每个** tool_call 对应的 tool 消息」

    破坏它的典型场景：
      · 模型请求调 2 个工具，Policy 拒绝了 1 个 → 只 append 1 条 tool 消息 → 400
      · 工具抛异常没产出消息 → 少一条 → 400

    本类通过在 finish() 时**补齐缺失的 tool 消息**来保证不变量。
    """

    def __init__(self) -> None:
        self._assistant: Message | None = None
        self._tool_msgs: list[Message] = []
        self._expected_ids: list[str] = []
        self._seen_ids: set[str] = set()
        # 缺失 tool 消息时的默认占位文案
        self._default_reason: str = "该调用未被执行（被策略拒绝或未获批准）。请换一种方式，或询问用户。"

    # ---- 添加 assistant（可能带 tool_calls）-------------------------------
    def add_assistant(self, content: str, tool_calls: list[ToolCall], reasoning: str = "") -> "TurnBuilder":
        self._assistant = Message.assistant(text=content, tool_calls=tool_calls, reasoning=reasoning)
        self._expected_ids = [tc.id for tc in tool_calls]
        self._default_reason = "该调用未被执行（被策略拒绝或未获批准）。请换一种方式，或询问用户。"
        self._tool_msgs = []
        self._seen_ids = set()
        return self

    # ---- 添加一条 tool 结果 ----------------------------------------------
    def add_tool(self, call_id: str, name: str, content: str) -> "TurnBuilder":
        self._tool_msgs.append(Message.tool_result(call_id, name, content))
        self._seen_ids.add(call_id)
        return self

    def add_missing_tools(self, name_map: dict[str, str], reason: str) -> "TurnBuilder":
        """
        为「没有产生实际 tool 消息」的调用补齐占位消息。

        什么情况下会有缺失？
          · Policy 拒绝（路径越界 / 熔断 / 预算耗尽）—— 这是主要来源
          · 工具执行抛了未捕获异常
        这两种都是**正常会发生**的，不是异常情况，所以必须处理。
        """
        for cid in self._expected_ids:
            if cid in self._seen_ids:
                continue
            self.add_tool(cid, name_map.get(cid, "unknown"), reason)
        return self

    # ---- 产出 --------------------------------------------------------------
    def build(self) -> list[Message]:
        """
        产出消息组。

        ★ 这里**强制保证协议不变量**，而不是依赖调用方记得调 add_missing_tools：
          ① 补齐缺失的 tool 消息（否则 OpenAI 会 400）
          ② 按 tool_calls 的顺序重排（否则顺序错乱，部分供应商也会报错）

        把不变量放在「产出那一刻」统一保证，比散落在各个调用点更可靠 ——
        调用方漏掉一步不会造成线上 400，这正是「一个地方统一保证」的价值。
        """
        out: list[Message] = []
        if self._assistant is None:
            return out
        out.append(self._assistant)

        # 按 call_id 归集，再按 _expected_ids 顺序输出
        by_id: dict[str, Message] = {m.tool_call_id: m for m in self._tool_msgs}

        for cid in self._expected_ids:
            msg = by_id.pop(cid, None)
            if msg is None:
                msg = Message.tool_result(cid, "unknown", self._default_reason)
            out.append(msg)

        # 不在 expected 里的（模型给了未声明的 id，罕见）追加到末尾，不静默丢弃
        for extra in self._tool_msgs:
            if extra.tool_call_id in by_id:
                out.append(extra)

        return out


# --------------------------------------------------------------------------- #
# 上下文构建器
# --------------------------------------------------------------------------- #
class ContextBuilder:
    """
    由 TaskState 构建完整的消息序列。

    刻意做成**无状态**的纯函数式组件 —— 同样的 State 构建两次得到同样的消息。
    这让「重放一次运行」变成可能：只要有 State 快照，就能重建上下文。
    """

    def __init__(
        self,
        *,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
        keep_full_results: int = 3,
    ) -> None:
        # 最近 N 个工具结果喂完整内容，更早的降级成摘要
        self.max_tool_result_chars = max_tool_result_chars
        self.keep_full_results = keep_full_results

    # ===================================================================== #
    # 主入口
    # ===================================================================== #
    def build(
        self,
        state: TaskState,
        *,
        system_prompt: str,
        history: list[Message],
        include_plan: bool = True,
        include_reminder: bool = True,
        phase_hint: str = "",
    ) -> list[Message]:
        """
        组装消息序列。

        参数 history 是「已发生的轮次消息」（由 AgentCore 累积维护）。
        ★ 为什么不从 state.tool_events 重建 history？
          因为 tool 消息的内容（完整文件内容）比 ToolEvent.summary 大得多，
          重建会丢信息。所以 history 单独维护，state 只存摘要。
          这也解释了为什么 State ≠ messages —— 两者信息量不同，各有用途。
        """
        msgs: list[Message] = [Message.system(system_prompt), Message.user(state.goal)]

        # ---- 计划（半稳定，放前面）---------------------------------------
        if include_plan and state.plan:
            msgs.append(Message.system(self.render_plan(state)))
        elif include_plan and state.plan_note:
            msgs.append(Message.system(f"# 关于计划\n{state.plan_note}"))

        # ---- 历史轮次（已做压缩）-----------------------------------------
        msgs.extend(self.compress_history(history))

        # ---- 当前进度提醒（最易变，放最后）-------------------------------
        if include_reminder:
            msgs.append(Message.system(self.render_progress(state)))

        # ---- 阶段提示（如 PLAN / REFLECT 要求 JSON）----------------------
        if phase_hint:
            msgs.append(Message.system(phase_hint))

        return msgs

    # ===================================================================== #
    # 渲染
    # ===================================================================== #
    @staticmethod
    def render_plan(state: TaskState) -> str:
        lines = ["# 当前计划"]
        for step in state.plan:
            mark = {
                "pending": "[ ]",
                "in_progress": "[→]",
                "done": "[x]",
                "skipped": "[-]",
                "failed": "[!]",
            }.get(step.status.value, "[ ]")
            lines.append(f"{mark} {step.index + 1}. {step.title}")
        if state.plan_note:
            lines.append(f"\n备注：{state.plan_note}")
        return "\n".join(lines)

    @staticmethod
    def render_progress(state: TaskState) -> str:
        """
        进度提醒。

        ★ 为什么要把「证据数」写进提示词？
          因为 C3 要求至少一条证据才能收尾。如果模型不知道这条规则，
          它会在第一轮就急着给结论，然后被程序拒绝，白跑一轮。

          把约束**提前告知**，比事后拒绝便宜得多。
          这是一个通用经验：程序侧的硬规则，应当尽量体现在提示词里。
        """
        b = state.budgets
        lines = [
            "# 当前进度",
            f"- 轮次：{state.turn} / {b.max_turns}",
            f"- 已调用工具：{b.tool_calls_used} / {b.max_tool_calls} 次",
            f"- 已获得有效观测：{len([e for e in state.evidence_summary if e.ok])} 条"
            f"（收尾前至少需要 {b.min_evidence} 条）",
        ]

        if state.consecutive_errors >= 2:
            lines.append(f"- ⚠ 已连续失败 {state.consecutive_errors} 次，请换一种方法，不要重复同样的调用")

        if state.tool_events:
            lines.append("\n# 最近的工具调用")
            for ev in state.tool_events[-5:]:
                flag = "成功" if ev.ok else f"失败({ev.error_kind})"
                lines.append(f"- 第{ev.turn}轮 `{ev.tool}` → {flag}：{ev.summary[:80]}")

        remaining = b.max_turns - state.turn
        if remaining <= 3:
            lines.append(f"\n⚠ 剩余轮次不多（{remaining} 轮）。请优先收敛，给出结论。")

        # ---- 第五批：范围冲突与读取缓存状态 ------------------------------
        # ★ 这两样必须在**每一轮**都出现在模型眼前：
        #   冲突 → 模型才会知道自己撞了权限墙，而不是反复重试；
        #   缓存 → 模型才知道某个文件它已经读过，不必重复读。
        if getattr(state, "scope_conflicts", None):
            lines.append("\n# ⚠ 任务要求与权限范围存在冲突（需要用户授权，不要擅自扩大权限）")
            for c in state.scope_conflicts[:5]:
                lines.append(f"- {c.get('path')}：{c.get('reason')}")
        if getattr(state, "cache_notes", None):
            lines.append("\n# 文件读取状态")
            for note in state.cache_notes[-4:]:
                lines.append(f"- {note}")

        # ---- 第六批：已读取文件清单 + 复用规则 --------------------------
        # ★ 目的：让模型**主动复用**已读内容，而不是机械地再读一遍。
        #   只放索引与状态（不放全文），避免"为了省读取而涨上下文"的反效果。
        idx = getattr(state, "read_index", None)
        if idx:
            lines.append("\n# 已读取文件（复用指南）")
            lines.append("规则：原文仍在上下文且文件未变化 → 直接用，不必重读；"
                         "原文已被压缩 → 需要精确内容时重新读取；"
                         "不确定文件是否变化 → 重新读取；此清单不改变任何权限。")
            for item in idx:
                avail = "原文在上下文中可用" if item.get("in_context") else "原文已被压缩（需要时请重读）"
                rng = f"，范围 {item.get('range')}" if item.get("range") else ""
                lines.append(
                    f"- {item.get('path')}｜hash:{item.get('hash')}｜"
                    f"第 {item.get('turn')} 轮读｜{item.get('lines')} 行{rng}｜{avail}"
                )

        return "\n".join(lines)

    def compress_history(self, history: list[Message]) -> list[Message]:
        """
        历史压缩：最近 N 个工具结果保留完整内容，更早的截断。

        ★ 为什么按「工具结果」而不是按「消息条数」来压缩？
          因为消息大小极不均匀 —— 一条 read_file 结果可能有 3 万字符，
          而一条 assistant 文本只有 50 字符。按条数压缩等于没压。

        ★ 为什么保留最近的、压缩更早的？
          因为 Agent 的工作有局部性：当前正在处理的东西最相关。
          早期读过的大文件，其关键信息通常已经被模型提炼进了后续的
          assistant 文本里，原文可以降级。

        这是「上下文工程」最小可用的一种实现。更高级的手段（摘要、
        检索、分层记忆）属于第三阶段的学习内容。
        """
        # 找出所有 tool 消息的位置
        tool_idx = [i for i, m in enumerate(history) if m.role == "tool"]
        if len(tool_idx) <= self.keep_full_results:
            return list(history)

        # 保留最后 keep_full_results 条，其余截断
        keep_from = tool_idx[-self.keep_full_results]
        out: list[Message] = []
        for i, m in enumerate(history):
            if m.role == "tool" and i < keep_from and len(m.content) > 400:
                truncated = self._summarize_tool_content(m.content)
                out.append(
                    Message(
                        role="tool",
                        content=truncated,
                        tool_call_id=m.tool_call_id,
                        tool_name=m.tool_name,
                    )
                )
            else:
                out.append(m)
        return out

    @staticmethod
    def _summarize_tool_content(content: str, head: int = 300) -> str:
        """把一条过长的工具结果压成「开头小结 + 截断标记」。"""
        head_text = content[:head].rstrip()
        return (
            f"{head_text}\n\n…（此结果因上下文长度限制被压缩，"
            f"原长 {len(content)} 字符。如需完整内容可重新读取）"
        )

    # ===================================================================== #
    # 估计
    # ===================================================================== #
    @staticmethod
    def estimate_chars(messages: list[Message]) -> int:
        """粗估字符数（用于日志与预算观察）。1 token ≈ 1.5 中文字符，仅供参考。"""
        n = 0
        for m in messages:
            n += len(m.content) + len(m.reasoning) + 40  # 40 大致是 role/结构开销
            for tc in m.tool_calls:
                n += len(tc.arguments_raw) + len(tc.name) + 20
        return n


# --------------------------------------------------------------------------- #
# 阶段提示
# --------------------------------------------------------------------------- #
PHASE_HINTS: dict[str, str] = {
    "plan": (
        "现在处于 PLAN 阶段。请先给出执行计划（JSON，见系统提示中的格式要求），"
        "不要在此阶段调用任何工具。"
    ),
    "act": (
        "现在处于 ACT 阶段。请根据计划决定下一步动作："
        "需要调用工具就调用；若已掌握足够信息，就直接给出最终答复。"
    ),
    "reflect": (
        "现在处于 REFLECT 阶段。请评估进展（JSON，见系统提示），"
        "不要在此阶段调用工具。"
    ),
    "final": (
        "现在处于 FINAL 阶段。工具观测已经结束，请直接给出面向用户的最终答复，"
        "不要调用任何工具。"
    ),
}


def phase_hint(phase: str, *, extra: str = "") -> str:
    base = PHASE_HINTS.get(phase, "")
    if extra:
        return f"{base}\n\n{extra}".strip()
    return base
