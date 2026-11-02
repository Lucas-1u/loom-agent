"""
Spill 溢出存储机制 —— 对齐 DeepSeek Harness 的 dsh-spill / dsh-spill-policy。

解决的问题
----------
当工具结果（读文件、跑命令）超过上下文预算时，传统做法是「截断后把前 N 字符
塞进去」。但这有两个问题：
  ① 大文件只读前段，后段永远看不到 → 片面
  ② 每次读 40k 全进上下文，反复读大文件 → 上下文膨胀、token 消费增加

Spill 的做法
------------
  1. **完整内容存盘**（SpillStore）：把完整原文写到工作区下的临时文件，不丢信息
  2. **上下文只保留 head + tail**（SpillPolicy）：开头一半 + 结尾一半，中间用省略标记
  3. **spill notice**：告诉模型完整内容存放路径 + 如何按需读取

这样模型既能看到文件的关键信息（开头定结构、结尾看结论），又能按需读取中间部分，
上下文消耗从 O(文件大小) 降到 O(预算上限)。

设计要点
--------
- **不改工具内部逻辑**：spill 在统一出口（_execute_tools）介入，和 DeepSeek 的
  tools/post-execute hook 一致。工具该返回什么还返回什么，spill 只是"包装"一下。
- **可降级**：存盘失败时保留原内容（和 DeepSeek 的 best-effort 一致），不报错。
- **会话级隔离**：按 task_id 建子目录，不跨任务串味。
"""

from .spill_store import SpillStore, SpillRef, LocalSpillStore
from .spill_policy import SpillPolicyConfig, apply_spill, retain_head_tail, format_spill_notice

__all__ = [
    "SpillStore",
    "SpillRef",
    "LocalSpillStore",
    "SpillPolicyConfig",
    "apply_spill",
    "retain_head_tail",
    "format_spill_notice",
]
