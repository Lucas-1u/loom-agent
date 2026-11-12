"""
web_search —— 联网搜索（DeepSeek Search API）。

★ 这是对比表 #21 的补齐：DSH 有 `dsh-tool-web` + `dsh-web-search-deepseek` /
  `dsh-web-deepseek-api`，Loom 此前完全没有联网能力，Agent 查不到文档只能瞎编。

────────────────────────────────────────────────────────────────
选型：独立 Search API（而非 Messages 轮次内搜索）
────────────────────────────────────────────────────────────────
DSH 有两条实现路线：
  ① `dsh-web-search-deepseek`：走 Anthropic 兼容 Messages API，让模型跑一轮、
     服务端执行 web_search 工具。**每次搜索消耗一次完整模型轮次**（延迟+token）。
  ② `dsh-web-deepseek-api`：独立端点 `POST https://api.deepseek.com/v1/tools/search`，
     一次直接请求，**无需辅助模型轮次**，不烧 token。

选 ②：对 Loom 来说搜索是「工具调用」，不该占用模型推理预算。
请求体 `{query, count, need_content}`，认证 `Authorization: Bearer <key>`。

────────────────────────────────────────────────────────────────
安全边界
────────────────────────────────────────────────────────────────
· risk = READ：搜索本身不改本地文件、不执行命令。
· **开关语义**：本工具默认**不注册**（`search=None` 时）。
  任务级开关（前端勾选"联网搜索"）决定该任务的注册表里有没有这个工具——
  与 desktop 模式剔除桌面工具同一套路（服务端真强制，不是提示词层面）。
· 凭据：复用 `DEEPSEEK_API_KEY`（与模型调用同一份 key）。
  key 缺失时工具**不注册**，而不是注册后报错——这样未配置时的工具清单
  与历史完全一致（老用户无感）。
· 超时/重试：urllib 直连，超时 20s，失败不自动重试（返回结构化错误，
  由模型决定是否换个查询词再试一次）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel

DEFAULT_BASE_URL = "https://api.deepseek.com/v1/tools"
DEFAULT_TIMEOUT = 20.0
MAX_COUNT = 10
MAX_QUERY_CHARS = 200


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索互联网上的信息，返回标题、URL 和摘要。"
        "用于查阅文档、API 参考、报错信息、最新新闻等本地资料没有的内容。"
        "搜索只读，不会修改任何文件。"
        "注意：搜索结果可能过时或不准确，重要信息请以官方文档为准。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词。尽量精炼、具体，例如 'python asyncio TaskGroup 用法'。",
            },
            "count": {
                "type": "integer",
                "description": f"返回结果条数，1-{MAX_COUNT}，默认 5。",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.READ

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            query = str(args.get("query", "") or "").strip()
            if not query:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    "缺少必填参数 'query'", duration_ms=t.ms,
                )
            if len(query) > MAX_QUERY_CHARS:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS,
                    f"query 过长（{len(query)} 字符 > 上限 {MAX_QUERY_CHARS}）",
                    hint="请精简搜索关键词。", duration_ms=t.ms,
                )

            count = args.get("count", 5)
            try:
                count = max(1, min(int(count), MAX_COUNT))
            except (TypeError, ValueError):
                count = 5

            if not self._api_key:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "未配置 DEEPSEEK_API_KEY，联网搜索不可用",
                    hint="在 loom/.env 中配置 DEEPSEEK_API_KEY 后重启服务。",
                    duration_ms=t.ms,
                )

            url = f"{self._base_url}/search"
            body = json.dumps(
                {"query": query, "count": count, "need_content": False},
                ensure_ascii=False,
            ).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                if exc.code == 401:
                    return ToolResult.failure(
                        self.name, ErrorKind.PERMISSION_DENIED,
                        f"搜索服务拒绝认证（HTTP 401）{('：' + detail) if detail else ''}",
                        hint="DEEPSEEK_API_KEY 无效或已过期，请检查 .env 配置。",
                        duration_ms=t.ms,
                    )
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    f"搜索服务返回 HTTP {exc.code}{('：' + detail) if detail else ''}",
                    hint="稍后重试或检查搜索服务状态。",
                    duration_ms=t.ms,
                )
            except urllib.error.URLError as exc:
                return ToolResult.failure(
                    self.name, ErrorKind.TIMEOUT,
                    f"搜索请求网络失败：{exc.reason}",
                    hint="检查本机网络/代理设置（调模型前需清系统代理）。",
                    duration_ms=t.ms,
                )
            except TimeoutError:
                return ToolResult.failure(
                    self.name, ErrorKind.TIMEOUT,
                    f"搜索请求超时（>{self._timeout:.0f}s）",
                    hint="稍后重试或缩短关键词。", duration_ms=t.ms,
                )

            # ---- 解析响应 ----
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "搜索服务返回了非 JSON 响应",
                    duration_ms=t.ms,
                )

            results_raw = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results_raw, list):
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "搜索响应缺少 results 数组",
                    hint="服务端返回了意外的响应格式。", duration_ms=t.ms,
                )

            results: list[dict[str, Any]] = []
            for item in results_raw:
                if not isinstance(item, dict):
                    continue
                url_val = str(item.get("url", "") or "")
                if not url_val:
                    continue
                results.append({
                    "title": str(item.get("title", "") or ""),
                    "url": url_val,
                    "snippet": str(item.get("snippet", "") or ""),
                    "published_at": str(item.get("date", "") or ""),
                })

            if not results:
                return ToolResult.failure(
                    self.name, ErrorKind.NOT_FOUND,
                    f"搜索「{query}」没有返回结果",
                    hint="换个关键词再试，或检查查询是否拼写正确。",
                    duration_ms=t.ms,
                )

            summary = f"搜索「{query}」返回 {len(results)} 条结果"
            data = {"query": query, "count": len(results), "results": results}
            return ToolResult.success(self.name, summary, data=data, duration_ms=t.ms)


def make(*, base_url: str = DEFAULT_BASE_URL, api_key: str = "",
         timeout: float = DEFAULT_TIMEOUT) -> Tool | None:
    """工厂：api_key 为空时返回 None（不注册），保持未配置时的工具清单不变。"""
    if not api_key:
        return None
    return WebSearchTool(base_url=base_url, api_key=api_key, timeout=timeout)
