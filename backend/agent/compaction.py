"""
上下文压缩 MVP（第三阶段第五批）

★ 这个模块解决什么
----------------
长任务里，消息历史会无控制地累积：每次 read_file 都可能带回几万字符，
每轮 assistant 文本、每次命令输出都留在上下文里。到后面：
  · 请求逼近模型上下文上限，甚至直接被拒
  · 真正重要的信息（用户目标、约束、计划状态、验证风险）被埋在几万行旧输出里
  · 模型开始重复读同一个文件（上下文里没有可用的副本了）

★ 核心设计：三类数据分开处理
--------------------------
A. 权威状态（不可丢）：用户原始目标、权限约束、意图、计划步骤与状态、
   已完成步骤的证据、验证风险、关键验证结论。
   → **从 TaskState 读**，不依赖模型回忆；压缩不能改写它们。
B. 过程历史（可压）：早期工具调用、已读过的大文件内容、冗长命令输出、
   已被后续结果取代的中间结论。
   → 压成摘要，并保留来源引用。
C. 外部状态（按需重取）：当前源码、文件哈希、git 状态、最新测试结果。
   → 不固化进摘要；需要时重新读取（read_file 每次都算哈希，天然不会给旧内容）。

★ 为什么不是"只写一个摘要函数"
------------------------------
压缩必须接在真实的 Agent Loop 里：每轮调用模型前判断规模、超了就压、
压完把新历史写回、状态与摘要的一致性由结构化状态保证。
这里提供的是**可被 core 调用的组件**，接线在 core._maybe_compact()。

★ token 估算说明
---------------
没有可靠的 tokenizer 时，**不假装字符数就是 token 数**。
本模块统一用「字符数」和「估算 token 数（字符数 ÷ 1.5，仅供触发判断）」两个口径，
日志里会明确标注是估算值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..models.adapter import Message
from .state import TaskState

log = logging.getLogger("loom.compaction")

# 字符 → token 的粗略换算（中文语境下 1 token ≈ 1.5 字符）。
# ★ 只是**触发阈值**用的估算，不对外宣称精确 token 数。
CHARS_PER_TOKEN = 1.5

# 单轮里出现这么大的内容就值得提前压一次（长输出场景）
LONG_TURN_CHARS = 20_000

COMPACT_PROMPT = """下面是一个编码任务到目前为止的工作过程记录。请把它压缩成**不超过 300 字**的中文工作记录。

只保留这四类信息：
1. 已经确认的结论（读懂了什么、问题在哪）
2. 失败原因与关键错误信息（具体的报错、退出码、失败用例名）
3. 已经改动的文件及改动目的
4. 下一步该做什么

不要复述原文，不要编造记录里没有的内容，没提到的就写"无"。只输出这段记录本身。"""


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
@dataclass
class ContextStats:
    """一次模型请求的上下文规模（字符数是实测，token 是估算）。"""

    messages: int = 0
    chars: int = 0
    est_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "chars": self.chars,
            "est_tokens": self.est_tokens,
            "est_tokens_note": "估算值（字符数 ÷ 1.5），非精确 token 计数",
        }


def measure(messages: list[Message]) -> ContextStats:
    chars = 0
    for m in messages:
        chars += len(m.content) + len(m.reasoning) + 40
        for tc in m.tool_calls:
            chars += len(tc.arguments_raw) + len(tc.name) + 20
    return ContextStats(
        messages=len(messages),
        chars=chars,
        est_tokens=int(chars / CHARS_PER_TOKEN),
    )


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class CompactionOutcome:
    triggered: bool = False
    reason: str = ""
    before: ContextStats = field(default_factory=ContextStats)
    after: ContextStats = field(default_factory=ContextStats)
    kept_turns: int = 0
    summarized_turns: int = 0
    summary: str = ""
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    new_history: list[Message] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "kept_turns": self.kept_turns,
            "summarized_turns": self.summarized_turns,
            "degraded": self.degraded,
            "notes": self.notes,
            "summary_chars": len(self.summary),
        }


# --------------------------------------------------------------------------- #
# 轮次切分
# --------------------------------------------------------------------------- #
def split_turns(history: list[Message]) -> list[list[Message]]:
    """
    把历史切成「轮次」。

    ★ 一轮 = 一条 assistant 消息 + 其后紧跟的所有 tool 消息。
      切分必须落在轮次边界上，否则会切出「tool 消息没有对应的 assistant」——
      OpenAI 协议会直接 400。

    ★ 连续的非 assistant 消息（user 提示等）各自单独成一轮，
      这样切分点永远落在 assistant 或 user 上，协议不变量就保住了。
    """
    turns: list[list[Message]] = []
    current: list[Message] = []
    for m in history:
        if m.role == "assistant" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)
    return turns


def render_turns(turns: list[list[Message]], *, max_chars: int = 24_000) -> str:
    """把被压缩的轮次渲染成给模型看的文本（超长时按比例截断）。"""
    parts: list[str] = []
    for turn in turns:
        for m in turn:
            if m.role == "assistant":
                body = (m.content or "").strip()
                if body:
                    parts.append(f"[助手] {body}")
                for tc in m.tool_calls:
                    parts.append(f"[调用] {tc.name}({tc.arguments_raw[:300]})")
            elif m.role == "tool":
                head = (m.content or "")[:400].strip()
                parts.append(f"[结果:{m.tool_name}] {head}")
            else:
                parts.append(f"[{m.role}] {(m.content or '')[:300].strip()}")
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（更早的过程已省略）"
    return text


# --------------------------------------------------------------------------- #
# 权威状态块（确定性生成，永远正确）
# --------------------------------------------------------------------------- #
def build_state_block(state: TaskState) -> str:
    """
    从 TaskState 生成「权威状态」文本。

    ★ 这是压缩后**唯一可信**的任务状态来源：
      它不是模型写的摘要，而是程序从结构化状态里渲染出来的，
      所以不可能把 pending 说成 done、把 unresolved 说成 resolved。
    """
    lines: list[str] = ["# 任务状态（压缩后以此为准，以下内容由程序从状态生成）"]

    lines.append(f"\n## 用户原始目标\n{state.goal}")

    if state.intent and state.intent != "unknown":
        lines.append(f"\n## 任务意图\n{state.intent}（置信度 {state.intent_confidence}）")

    cons: list[str] = []
    if state.forbidden_paths:
        cons.append(f"禁止修改：{', '.join(state.forbidden_paths)}")
    if state.allowed_paths:
        cons.append(f"允许修改：{', '.join(state.allowed_paths)}")
    for k, v in (state.constraints or {}).items():
        if v:
            cons.append(f"用户约束 {k}=True")
    if state.scope_conflicts:
        cons.append(f"⚠ 范围冲突 {len(state.scope_conflicts)} 项（见下）")
    if cons:
        lines.append("\n## 用户约束（不可违反）\n" + "\n".join(f"- {c}" for c in cons))

    if state.scope_conflicts:
        lines.append("\n## 范围冲突（需要用户授权，不要擅自扩大权限）")
        for c in state.scope_conflicts[:6]:
            lines.append(f"- {c.get('path')}：{c.get('reason')}")

    if state.plan:
        lines.append("\n## 计划步骤")
        for s in state.plan:
            mark = {"done": "✓", "in_progress": "▶", "pending": "○",
                    "failed": "✗", "blocked": "⛔", "skipped": "–"}.get(s.status.value, "·")
            extra = f" —— {s.evidence}" if s.evidence else ""
            lines.append(f"- {mark} 步骤{s.index + 1} {s.title}（{s.status.value}）{extra}")
        cur = state.current_step
        if cur is not None:
            lines.append(f"\n当前正在处理：步骤 {cur.index + 1} {cur.title}")

    changed = [c.path for c in (state.audit or {}).get("changed", [])]
    if changed:
        lines.append("\n## 已改动的文件\n" + "\n".join(f"- {p}" for p in changed[:20]))

    cmds = state.command_history[-5:]
    if cmds:
        lines.append("\n## 最近执行的命令")
        for c in cmds:
            lines.append(f"- `{c.command}` 退出码 {c.exit_code}（{'成功' if c.ok else '失败'}）")
            if not c.ok and c.stderr_tail:
                tail = " / ".join(c.stderr_tail.splitlines()[-2:])[:200]
                lines.append(f"  错误尾部：{tail}")

    ev = state.evidence or {}
    if ev:
        lines.append(f"\n## 验证证据\n- 等级 {ev.get('grade')}：{'；'.join(ev.get('reasons') or [])[:200]}")
        if ev.get("unverified"):
            lines.append(f"- 尚未验证：{'；'.join(ev['unverified'])}")

    risky = [r for r in (state.risks or []) if r.get("status") != "resolved"]
    if risky:
        lines.append("\n## 未解决的验证风险")
        for r in risky[:6]:
            lines.append(f"- [{r.get('level')}] {r.get('path')}：{r.get('resolution') or r.get('status')}")

    if state.cache_notes:
        lines.append("\n## 文件读取缓存状态")
        for note in state.cache_notes[:5]:
            lines.append(f"- {note}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 压缩器
# --------------------------------------------------------------------------- #
class ContextCompactor:
    """
    上下文压缩器。

    职责（保持简单：一个类 + 几个纯函数）：
      1. 统计上下文规模
      2. 判断是否该压
      3. 把早期轮次换成「权威状态块 + 过程摘要」
      4. 保留最近若干轮完整交互（不破坏 tool 配对）
      5. 记录压缩前后的规模与降级情况
    """

    def __init__(
        self,
        *,
        max_chars: int = 60_000,
        keep_recent_turns: int = 3,
        min_gain_ratio: float = 0.15,
        summarizer: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.max_chars = max_chars
        self.keep_recent_turns = keep_recent_turns
        self.min_gain_ratio = min_gain_ratio
        self.summarizer = summarizer

    # ---- 触发判断 --------------------------------------------------------
    def should_compact(self, messages: list[Message]) -> tuple[bool, str, ContextStats]:
        stats = measure(messages)
        if stats.chars > self.max_chars:
            return True, f"上下文超过阈值（{stats.chars} > {self.max_chars} 字符）", stats
        # 单轮超长：早压一次，免得下一次请求直接撞墙
        for turn in split_turns(messages):
            turn_chars = sum(len(m.content) for m in turn)
            if turn_chars > LONG_TURN_CHARS:
                return True, f"单轮内容过长（{turn_chars} 字符）", stats
        return False, "", stats

    # ---- 执行压缩 --------------------------------------------------------
    async def compact(
        self,
        state: TaskState,
        history: list[Message],
        *,
        before: ContextStats | None = None,
        reason: str = "",
    ) -> CompactionOutcome:
        """
        把历史里较早的轮次压成「状态块 + 过程摘要」。

        返回的 `new_history` 就是下一轮该用的历史。
        """
        outcome = CompactionOutcome(triggered=True, reason=reason)
        outcome.before = before or measure(history)

        turns = split_turns(history)
        if len(turns) <= self.keep_recent_turns:
            outcome.triggered = False
            outcome.notes.append("轮次太少，不压缩")
            outcome.new_history = list(history)
            outcome.after = outcome.before
            return outcome

        keep = turns[-self.keep_recent_turns:]
        old = turns[:-self.keep_recent_turns]
        outcome.kept_turns = len(keep)
        outcome.summarized_turns = len(old)

        # ---- 1. 权威状态块（确定性，永远正确）------------------------
        state_block = build_state_block(state)

        # ---- 2. 过程摘要（模型生成，失败则降级）----------------------
        narrative = ""
        if self.summarizer is not None:
            text = render_turns(old)
            try:
                got = await self.summarizer(text)
            except Exception as exc:  # noqa: BLE001 —— 摘要失败不能毁掉任务
                log.warning("上下文摘要生成失败：%s", exc)
                got = None
                outcome.degraded = True
                outcome.notes.append(f"摘要生成异常：{type(exc).__name__}")
            if got and got.strip():
                narrative = got.strip()
            elif self.summarizer is not None:
                outcome.degraded = True
                outcome.notes.append("摘要为空或未返回")
        else:
            outcome.degraded = True
            outcome.notes.append("未配置摘要器，只保留权威状态块")

        parts = [state_block]
        if narrative:
            parts.append("\n# 过程摘要（模型生成，仅供参考，状态以上面为准）\n" + narrative)
        summary_text = "\n".join(parts)
        outcome.summary = summary_text

        # ---- 3. 组装新历史：摘要 + 最近若干轮 --------------------------
        new_history: list[Message] = [Message.system(summary_text)]
        for turn in keep:
            new_history.extend(turn)
        outcome.new_history = new_history
        outcome.after = measure(new_history)

        # ---- 4. 收益太小就别压（避免每轮反复压）----------------------
        if outcome.before.chars and outcome.after.chars > outcome.before.chars * (1 - self.min_gain_ratio):
            outcome.triggered = False
            outcome.notes.append(
                f"压缩收益不足（{outcome.before.chars} → {outcome.after.chars}），放弃本次压缩"
            )
            outcome.new_history = list(history)
            outcome.after = outcome.before
            return outcome

        # ---- 5. 压完还是超阈值 → 明确降级，不再重复压 -------------------
        if outcome.after.chars > self.max_chars:
            outcome.degraded = True
            outcome.notes.append(
                f"压缩后仍超过阈值（{outcome.after.chars} > {self.max_chars}），"
                "停止自动压缩并保留更多原文"
            )
            # 退一步：只保留 1 轮原文，其余只留状态块
            minimal = [Message.system(summary_text)]
            for m in turns[-1]:
                minimal.append(m)
            outcome.new_history = minimal
            outcome.after = measure(minimal)

        return outcome
