"""
search_docs —— 在当前工作区的项目资料里做全文检索（M3）。

★ 它是什么、不是什么
------------------
是：**FTS5 关键词检索**。查"哪个文档/源码片段出现过这些词"，返回路径 + 行号 + 片段。
不是：向量检索、语义搜索、问答。它不理解同义词，也不判断"哪段更权威"。

★ 为什么做成工具而不是只做自动预检索
--------------------------------
自动预检索只能覆盖"用户这句话本身"；真实任务里常出现"计划做到第 3 步才发现
需要查某个约定"的情况，这时模型应该能**自己再查一次**。
两条路径共用同一个 KnowledgeBase，区别只在谁发起。

★ 安全边界
----------
· 只读（RiskLevel.READ）→ 自动执行，不需要审批（读项目文档没有副作用）。
· 只能看工作区：`ctx.workspace` 由 Policy/Policy.make_context 传入，
  索引本身也只索引该工作区（discover 从 root 开始走，且拒绝符号链接外逃——
  注意：这里没有跟随目录符号链接，os.walk 默认 followlinks=False）。
· 不返回敏感文件：`.env`/密钥在**索引阶段**就被排除，检索不可能命中。
· 不执行任何命令、不写文件。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.rag.service import KnowledgeBase
from ...agent.state import RiskLevel


class SearchDocsTool(Tool):
    name = "search_docs"
    description = (
        "在当前工作区的项目资料里做**全文关键词检索**（SQLite FTS5，不是语义检索）。"
        "覆盖 Markdown/纯文本/源码/配置等文本文件，返回命中的文件路径、行号区间、标题与片段。"
        "适合：查项目约定、架构说明、历史设计决策、某个符号/命令出现在哪。"
        "注意：① 结果只是\"这些位置出现了相关词\"，不代表内容正确或最新；"
        "② 要修改某个文件前，仍必须用 read_file 读取它的真实内容；"
        "③ 检索不到就如实说明没找到，不要编造引用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词（中文或英文均可）。用具体名词/术语，不要写整段问题。",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "keyword", "semantic", "hybrid"],
                "description": (
                    "检索方式：keyword=关键词(BM25)；semantic=语义向量；hybrid=两路融合；"
                    "auto=用服务端默认配置。向量不可用时会自动降级为 keyword 并在结果里标注。"
                ),
            },
            "k": {
                "type": "integer",
                "description": "最多返回几条片段，默认 4，上限 10。",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.READ

    MAX_K = 10

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self.knowledge = knowledge

    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        if not str(args.get("query") or "").strip():
            return "query 不能为空：请给出要检索的关键词"
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            query = str(args.get("query") or "").strip()
            raw_k = args.get("k", 0)
            try:
                k = int(raw_k) if raw_k else self.knowledge.config.top_k
            except (TypeError, ValueError):
                k = self.knowledge.config.top_k
            k = max(1, min(self.MAX_K, k))
            mode = str(args.get("mode") or "auto").strip().lower()
            if mode not in ("auto", "keyword", "semantic", "hybrid"):
                mode = "auto"

            if not self.knowledge.usable:
                return ToolResult.failure(
                    self.name, ErrorKind.NOT_IMPLEMENTED,
                    "项目知识库未启用（LOOM_RAG=0 或未配置索引库路径）",
                    hint="这不是你的参数问题；请改用 list_files/read_file 直接查看文件。",
                    duration_ms=t.ms,
                )

            outcome = await asyncio.to_thread(
                self.knowledge.retrieve, ctx.workspace, query, k=k,
                mode="" if mode == "auto" else mode
            )
            if outcome.error:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"检索失败：{outcome.error}",
                    hint="可以改用 list_files/read_file 继续任务；不要把检索失败当成\"没有资料\"。",
                    duration_ms=t.ms,
                )

            if not outcome.hits:
                # ★ 空结果不是错误：明确告诉模型"没找到"，并让它继续用别的办法。
                return ToolResult.success(
                    self.name,
                    f"没有检索到与「{query}」匹配的项目资料（索引 {outcome.stats.chunks if outcome.stats else 0} 个片段）",
                    data={"query": query, "hits": [], "reason": outcome.reason or "no_hit",
                          "retrieval": outcome.retrieval, "degraded": outcome.degraded,
                          "elapsed_ms": outcome.elapsed_ms},
                    duration_ms=t.ms,
                )

            paths = ", ".join(dict.fromkeys(h.path for h in outcome.hits))
            return ToolResult.success(
                self.name,
                f"检索到 {len(outcome.hits)} 条资料（{paths}）",
                data={
                    "query": query,
                    "retrieval": outcome.retrieval,
                    "degraded": outcome.degraded,
                    "found": outcome.found,
                    "hits": [
                        {**h.to_dict(), "text": h.text} for h in outcome.hits
                    ],
                    "elapsed_ms": outcome.elapsed_ms,
                    "index": {
                        "files": outcome.stats.files if outcome.stats else 0,
                        "chunks": outcome.stats.chunks if outcome.stats else 0,
                    },
                },
                duration_ms=t.ms,
            )


def make(knowledge: KnowledgeBase) -> SearchDocsTool:
    return SearchDocsTool(knowledge)
