"""
检索查询构建（M4.2）。

★ 为什么需要单独一个模块
----------------------
M3 的实现直接把**整段用户目标**当查询，例如：

    "按照项目约定修复 src/paging.py 的 average()，让 pytest 全部通过。
     不要修改 tests/ 目录下的测试文件，也不要改变函数签名。"

后半段是给模型的约束，对检索毫无帮助，却会占掉 token 并把噪声词带进 bm25。
计划产出之后，步骤标题往往是**更好的检索词**（"读约定""改 average""跑 pytest"）。

所以这里做三件小事：
  1. 从"用户目标"和"当前计划步骤"里构造检索词，而不是把整段话塞进去；
  2. 限制长度（默认 300 字符），避免长目标稀释权重；
  3. **记录查询来源**（query_source），事件与快照里能看出"这次为什么查这个"——
     这是可观测性的一部分：出了问题要能知道查的是什么、为什么查。

★ 明确不做的事
--------------
不做查询改写（query rewriting）、不做同义词扩展、不调模型来生成查询。
第一版要的是**可解释、可复现**，不是聪明。
"""

from __future__ import annotations

import re

MAX_QUERY_CHARS = 300

# 目标里这些句子是给模型的约束/格式要求，检索时没有信息量（只做轻度裁剪，不做理解）
_NOISE_PATTERNS = (
    re.compile(r"不要(修改|改动|删除)[^。；\n]*"),
    re.compile(r"请(不要|务必)[^。；\n]*"),
    re.compile(r"不要(改变|改变函数签名)[^。；\n]*"),
)


def clean_goal(goal: str) -> str:
    """去掉明显的"约束性"从句，保留问题本身（保守裁剪，不猜测语义）。"""
    text = (goal or "").strip()
    for pat in _NOISE_PATTERNS:
        text = pat.sub(" ", text)
    return " ".join(text.split())


def build_query(goal: str, *, plan_steps: list[str] | None = None) -> tuple[str, str]:
    """
    构造检索查询，返回 `(query, query_source)`。

        query_source: "goal"        —— 只用目标（还没出计划，或计划为空）
                      "goal+plan"   —— 目标 + 前 3 个计划步骤标题

    ★ 为什么只取前 3 步：再多也不会显著提升召回，却会把查询词稀释。
    """
    base = clean_goal(goal)
    steps = [s.strip() for s in (plan_steps or []) if s and s.strip()]
    if not steps:
        return base[:MAX_QUERY_CHARS], "goal"

    extra = " ".join(steps[:3])
    query = f"{base} {extra}".strip()
    return query[:MAX_QUERY_CHARS], "goal+plan"
