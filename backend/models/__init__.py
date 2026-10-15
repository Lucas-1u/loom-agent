"""Loom Agent · 模型适配层

职责：把「供应商各自的 API 差异」挡在这一层里，Agent Core 只面对统一接口。

    agent/core.py
         │  只用 Message / ToolSpec / ModelReply
         ▼
    models/adapter.py          ← 抽象接口（本文件）
         │
         ▼
    models/openai_compat.py    ← 唯一实现（百炼 / DeepSeek 共用）

★ 为什么值得单独抽一层？
  1. 【换模型不改 Agent】Agent Core 永远 import adapter，不 import openai。
     将来接 Laya 本地模型、或换成 Anthropic 格式，只加一个 adapter 实现。
  2. 【可对比】能拿同一批任务跑不同模型，比较它们的 Agent 行为差异 —— 这是 C5 的直接动机。
  3. 【可测试】测试里注入一个 FakeAdapter，Agent 循环就能离线跑，不烧 token。
"""
