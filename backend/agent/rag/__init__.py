"""
Loom · 项目知识检索（M3，第一版）

★ 这是什么
----------
基于 **SQLite FTS5 的全文关键词检索**，给 Agent 提供"当前工作区内的项目资料"。
它不是向量检索、不是语义相似度搜索 —— 没有 embedding、没有向量库、没有重排模型。
排序依据是 bm25（关键词命中统计），只能说明"哪些片段命中了查询词"，
不能说明"语义上最相关"。

★ 为什么放在 agent/ 而不是 server/
-------------------------------
索引是 **Agent 的上下文来源**，不是 Web 层功能：
  · CLI、服务端、测试都要能用同一套；
  · `agent/` 禁止 import `server/`（依赖铁律），所以这里自带 SQLite 连接，
    不复用 `server/store.py`。
  · 数据库是**一个**文件（默认 `sandbox/index/knowledge.db`），
    所有工作区共用它、按 workspace 列隔离 —— 不为每个项目造一个库。

★ 模块划分
----------
    discover.py  发现哪些文件该进索引（排除目录 / 敏感文件 / 二进制 / 超大）
    chunk.py     把文本切成可检索片段（保留 Markdown 标题 / 代码符号 / 段落）
    store.py     SQLite 存储：元数据 + 片段 + FTS5 索引，增量更新
    service.py   KnowledgeBase 门面：ensure_indexed / retrieve / 组装上下文块

依赖方向：`rag/` 只依赖标准库 + `agent.state`（RiskLevel 不用），
不依赖 tools/server —— 反过来 tools 的 search_docs 依赖它。
"""

from ...config import RagConfig
from .service import KnowledgeBase, RetrieveOutcome, get_knowledge_base

__all__ = ["KnowledgeBase", "RetrieveOutcome", "RagConfig", "get_knowledge_base"]
