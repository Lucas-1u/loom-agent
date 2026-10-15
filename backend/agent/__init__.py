"""Loom Agent · Agent Core 包

本包承担「Agent 怎么想、怎么做」的全部逻辑：
    state.py     任务的程序状态（State ≠ 对话历史）
    context.py   由 State 派生喂给模型的消息序列
    policy.py    执行前校验链
    approval.py  人机协作审批（挂起/恢复）
    core.py      ★ 五阶段 Agent Loop

铁律：本包**不许** import server/。Agent 逻辑不依赖传输层，
      这样将来把 CLI 换成 Tauri GUI、或换成 Web、或嵌入别的程序，都不用改这里。
"""
