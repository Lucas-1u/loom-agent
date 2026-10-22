"""
Loom HTTP 服务层（Phase 3）

分三个文件，职责严格分开：

    store.py    SQLite 持久化：Session / Task / 幂等键。不含任何 Agent 知识。
    hub.py      进程级事件广播：全局 id、有限缓冲、补发与淘汰判定。
    runtime.py  一个 Task 的运行时：把 EventBus / ApprovalManager / AgentCore 串起来。

★ 这三层都**不 import agent core 的循环逻辑**，只是把 CLI 里 `run_one()` 干的事
  搬到服务端。Agent Loop、Policy、Approval Manager 一行没改。
"""
