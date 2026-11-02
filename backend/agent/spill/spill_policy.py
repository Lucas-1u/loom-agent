"""
Spill 策略 —— 对齐 DeepSeek Harness 的 dsh-spill-policy。

核心逻辑
--------
工具结果超过预算 → 把完整内容存盘 → 上下文只保留 head + tail → 追加 spill notice。

和 DeepSeek 的对应关系
---------------------
- DeepSeek `retainContent` 的 head/tail 平分预算 → 本模块 `retain_head_tail`
- DeepSeek `formatSpillNotice` → 本模块 `format_spill_notice`
- DeepSeek `bound` 的 best-effort 降级 → 本模块 `apply_spill` 的 try/except
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .spill_store import SpillStore, SpillRef


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: head/tail 之间的省略标记
GAP = "\n\n[...]\n\n"


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #

def format_spill_notice(ref: SpillRef, omitted_bytes: int) -> str:
    """
    生成 spill notice（追加在保留内容末尾）。

    对齐 DeepSeek `formatSpillNotice`：告诉模型完整内容在哪、被省略了多少。
    """
    return (
        f"\n\n（省略了 {omitted_bytes} 字符。完整内容存于：{ref.locator}。"
        f"{ref.retrieval_hint}）"
    )


# --------------------------------------------------------------------------- #
# 核心保留逻辑
# --------------------------------------------------------------------------- #

def retain_head_tail(
    text: str,
    budget: int,
) -> tuple[str, int]:
    """
    保留 text 的开头 + 结尾，预算平分，中间用省略标记。

    参数
    ----
    text : 完整工具结果文本
    budget : token 预算（字符数近似）

    返回
    ----
    (保留文本, 被省略的字符数)

    示例
    ----
    >>> retain_head_tail("abcdefghij", 6)
    ('abc[...]ghi', 2)
    """
    if len(text) <= budget:
        return text, 0

    head_budget = budget // 2
    tail_budget = budget - head_budget  # 允许 tail 略多

    head = text[:head_budget]
    tail = text[-tail_budget:]

    # 被省略的部分 = 原文 - head - tail
    omitted = len(text) - len(head) - len(tail)
    omitted = max(0, omitted)

    kept = head + GAP + tail
    return kept, omitted


# --------------------------------------------------------------------------- #
# 策略应用
# --------------------------------------------------------------------------- #

@dataclass
class SpillPolicyConfig:
    """Spill 策略配置。"""

    #: 最大内联字符数，超过则触发 spill
    max_inline_chars: int = 12_000

    #: 是否启用 spill
    enabled: bool = True


def apply_spill(
    text: str,
    *,
    tool_name: str,
    owner: str,
    store: SpillStore | None,
    config: SpillPolicyConfig | None = None,
) -> tuple[str, SpillRef | None]:
    """
    对工具结果文本应用 spill 策略。

    返回 (处理后文本, SpillRef 或 None)
    """
    cfg = config or SpillPolicyConfig()
    if not cfg.enabled or store is None:
        return text, None

    if len(text) <= cfg.max_inline_chars:
        return text, None

    try:
        ref = store.save_text(
            owner=owner,
            tool_name=tool_name,
            content=text,
        )
    except Exception:
        # 降级：存盘失败时保留原内容（best-effort）
        return text, None

    kept, omitted = retain_head_tail(text, cfg.max_inline_chars)

    if omitted > 0:
        notice = format_spill_notice(ref, omitted)
        kept = kept + notice
    else:
        notice = format_spill_notice(ref, 0)
        kept = kept + notice

    return kept, ref


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #

def estimate_chars(content: Any) -> int:
    """近似字符数估算（用于预算判断）。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        return len(str(content))
    return len(str(content))
