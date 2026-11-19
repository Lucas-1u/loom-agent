"""会话历史查询（对比表 #25）。

`query.HistoryQuery` 是一个**只读叶子模块**：自带 SQLite 连接，不 import server/。
server 的 `/api/history` 与 agent 的 `session_history` 工具共用它，保证
"前端看到的"和"模型查到的"是同一套语义。
"""

from .query import HistoryQuery

__all__ = ["HistoryQuery"]
