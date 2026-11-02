"""
工具结果修剪器 —— 对齐 DeepSeek Harness 的 dsh-compaction-tool-result-pruner。

解决的问题
----------
工具结果（尤其读大文件）在历史轮次里累积，compaction 压缩的是**对话历史**，
但工具结果的中间部分早就没用了 —— 模型只需要知道"读过、大概什么内容"。

本模块在 compaction 时遍历历史 tool 消息，把超预算的中间部分替换为 prune marker，
保留 head + tail。

和 DeepSeek 的对应关系
---------------------
- DeepSeek `pruneContent` → 本模块 `prune_text`
- DeepSeek `PRUNE_MARKER` → 本模块 `PRUNE_MARKER`
- DeepSeek `pruneSession` → 本模块 `prune_messages`
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 替换被修剪中间部分的固定标记
PRUNE_MARKER = "\n\n[... 工具结果中间部分已修剪 ...]\n\n"

#: 默认配置
DEFAULT_THRESHOLD = 8_192
DEFAULT_HEAD = 4_096
DEFAULT_TAIL = 1_024


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass
class PrunerConfig:
    """修剪器配置。"""

    #: 超过此字符数触发修剪
    threshold_chars: int = DEFAULT_THRESHOLD
    #: 保留开头字符数
    head_chars: int = DEFAULT_HEAD
    #: 保留结尾字符数
    tail_chars: int = DEFAULT_TAIL
    #: 已修剪过的 tool_call_id（replay-safe：避免重复修剪导致 marker 嵌套）
    shadowed_tool_call_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        emitted = self.head_chars + len(PRUNE_MARKER) + self.tail_chars
        if emitted > self.threshold_chars:
            raise ValueError(
                f"head_chars + marker + tail_chars ({emitted}) "
                f"must be at most threshold_chars ({self.threshold_chars})"
            )


# --------------------------------------------------------------------------- #
# 核心修剪逻辑
# --------------------------------------------------------------------------- #

def prune_text(text: str, config: PrunerConfig | None = None) -> str | None:
    """
    修剪单段文本的中间部分，保留 head + tail。

    返回修剪后的文本，或 None（未超阈值，无需修剪）。
    """
    cfg = config or PrunerConfig()

    if len(text) <= cfg.threshold_chars:
        return None

    removed_start = cfg.head_chars
    removed_end = len(text) - cfg.tail_chars

    head = text[:removed_start]
    tail = text[removed_end:]

    return head + PRUNE_MARKER + tail


def prune_messages(
    messages: list,
    config: PrunerConfig | None = None,
) -> tuple[list, int]:
    """
    修剪消息列表中所有 tool 消息的内容。

    只修剪 role == "tool" 的消息，其他消息不动。

    ★ replay-safe（对齐 DSH dsh-compaction-tool-result-pruner 的 shadowedSeqs）：
      通过 tool_call_id 跟踪已修剪的消息，避免重复修剪导致 marker 嵌套。
      如果一条消息的 tool_call_id 已在 shadowed_tool_call_ids 集合中，跳过它。

    返回 (新消息列表, 被修剪的消息数)
    """
    cfg = config or PrunerConfig()
    new_msgs = []
    pruned_count = 0

    for msg in messages:
        role = getattr(msg, "role", "")
        content = getattr(msg, "content", "")
        tool_call_id = getattr(msg, "tool_call_id", "")

        # 判断是否需要修剪
        if role == "tool" and isinstance(content, str) and len(content) > cfg.threshold_chars:
            # replay-safe：已修剪过的消息跳过
            if not (tool_call_id and tool_call_id in cfg.shadowed_tool_call_ids):
                pruned = prune_text(content, cfg)
                if pruned is not None:
                    msg.content = pruned
                    pruned_count += 1
                    # 记录到 shadowed 集合（只有非空 tool_call_id 才记录）
                    if tool_call_id:
                        cfg.shadowed_tool_call_ids.add(tool_call_id)

        new_msgs.append(msg)

    return new_msgs, pruned_count


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def measure_chars(content: str) -> int:
    """统计 Unicode 码点数（不拆代理对）。"""
    return len(content)
