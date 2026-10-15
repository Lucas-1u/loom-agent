"""
Loom Agent v0.1 · OpenAI 兼容适配器（百炼 + DeepSeek 共用）

★ 为什么一个实现能服务两家？
  百炼兼容 OpenAI 协议（base_url 用 /compatible-mode/v1），DeepSeek 本身就是 OpenAI 协议。
  差异只有三处：base_url / api_key / model 名。所以不需要两套代码。

────────────────────────────────────────────────────────────────────────
从老项目（小智）继承的四条硬经验（都曾真实踩坑）
────────────────────────────────────────────────────────────────────────

【1】思考模式「不传参数」而不是传 false
    百炼推理模型（qwen3.7-max-preview 之类的 think-mode-only）传入
    enable_thinking=false 会直接 400。正确做法是**压根不提这个参数**，
    让供应商用默认行为。这里通过 extra_body 只在显式需要时注入。

【2】400 自动重试：剥离可疑参数再试一次
    推理模型对参数很敏感。第一次请求带上 max_tokens / response_format 等，
    若返回 400 且错误信息指向参数，则剥掉这些参数重试一次。
    这比让用户看一堆 400 友好得多，且不需要预先知道每个模型的脾气。

【3】`_extract_reasoning` 两级兜底
    思维链字段名各供应商不一：
      - DeepSeek / 百炼: `reasoning_content`（直接在 message 上）
      - 部分实现:        `reasoning`，或塞在 `model_extra`
    所以 extract 要按「直接属性 → model_extra → 空」的顺序找。

【4】流式 tool_calls 必须按 index 拼接
    流式返回里 tool_calls 是碎片化的，按 `index` 分组累加 `arguments` 字符串，
    且 `id` / `function.name` 只在第一片出现。**不能**假设每次 chunk 都带全字段。

────────────────────────────────────────────────────────────────────────
本文件不做的事
────────────────────────────────────────────────────────────────────────
不 import agent/ 任何东西 —— 依赖方向必须是 models/ 独立，agent/ 依赖 models/。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Sequence

from .adapter import (
    Message,
    ModelAdapter,
    ModelError,
    ModelReply,
    StreamDelta,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger("loom.models")


# --------------------------------------------------------------------------- #
# 思维链提取（两级兜底）
# --------------------------------------------------------------------------- #
def _extract_reasoning(msg: Any) -> str:
    """
    从 SDK 的 message 对象里尽力抠出思维链文本。

    供应商差异：
      · DeepSeek-R1 / 百炼推理模型 → msg.reasoning_content
      · 部分兼容实现              → msg.reasoning
      · 还有些塞在 model_extra / 原始 dict 里

    返回 "" 表示没拿到 —— 调用方应当容忍（非推理模型本来就没有）。
    """
    if msg is None:
        return ""

    # 第一级：显式属性
    for attr in ("reasoning_content", "reasoning"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val:
            return val

    # 第二级：model_extra（SDK 对未知字段的收纳处）
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning"):
            val = extra.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class OpenAICompatAdapter(ModelAdapter):
    """
    OpenAI 兼容协议的通用适配器。

    实例化：
        OpenAICompatAdapter(provider="bailian", model="qwen3.7-plus",
                            base_url="...", api_key="...")

    或从配置直接造：
        OpenAICompatAdapter.from_config(model_cfg)
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.2,
        timeout_s: float = 35.0,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ModelError(
                f"{provider} 未配置 API Key（环境变量或 .env）",
                kind="missing_api_key",
                retryable=False,
            )
        self.name = provider
        self.model = model
        self.base_url = base_url
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = self._build_client(api_key, base_url, timeout_s, max_retries)

    # ---- 构造 --------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: Any) -> "OpenAICompatAdapter":
        return cls(
            provider=cfg.provider,
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            temperature=cfg.temperature,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
        )

    @staticmethod
    def _build_client(api_key: str, base_url: str, timeout_s: float, max_retries: int) -> Any:
        from openai import AsyncOpenAI  # 延迟 import：不让 models/ 在导入期就依赖 SDK

        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=max_retries,
        )

    @property
    def client(self) -> Any:
        """暴露底层 client，供诊断脚本 / 探针使用。"""
        return self._client

    # ===================================================================== #
    # 非流式
    # ===================================================================== #
    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = [t.to_dict() for t in tools]
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        t0 = time.time()
        try:
            resp = await self._create_with_fallback(payload)
        except Exception as exc:  # noqa: BLE001
            raise self._translate_exception(exc) from exc

        duration_ms = int((time.time() - t0) * 1000)
        return self._parse_response(resp, duration_ms)

    async def _create_with_fallback(self, payload: dict[str, Any]) -> Any:
        """
        首次请求 → 若 400 且疑似参数问题 → 剥掉可疑参数重试一次。

        【老项目经验 2】的实现。可疑参数按「对兼容性最不友好」的顺序剥离：
          response_format > max_tokens > temperature > tool_choice
        """
        try:
            return await self._client.chat.completions.create(**payload)
        except Exception as first:  # noqa: BLE001
            text = str(first)
            if "400" not in text and "invalid" not in text.lower():
                raise

            stripped = dict(payload)
            for key in ("response_format", "max_tokens", "temperature", "tool_choice"):
                stripped.pop(key, None)

            # 若被剥掉的是 tool_choice，保留 tools（工具能力比采样参数重要）
            if not tools_present(stripped):
                raise first

            log.warning("[%s] 首次请求 400，剥离可选参数后重试：%s", self.name, text[:200])
            return await self._client.chat.completions.create(**stripped)

    # ---- 解析 --------------------------------------------------------------
    def _parse_response(self, resp: Any, duration_ms: int) -> ModelReply:
        if not getattr(resp, "choices", None):
            raise ModelError("模型返回不含 choices", kind="empty_response", raw=str(resp)[:500])

        choice = resp.choices[0]
        msg = getattr(choice, "message", None)

        reply = ModelReply(
            model=getattr(resp, "model", self.model) or self.model,
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            duration_ms=duration_ms,
        )

        if msg is not None:
            reply.content = getattr(msg, "content", "") or ""
            reply.reasoning = _extract_reasoning(msg)
            reply.tool_calls = self._parse_tool_calls(getattr(msg, "tool_calls", None))

        usage = getattr(resp, "usage", None)
        if usage is not None:
            reply.prompt_tokens = _to_int(getattr(usage, "prompt_tokens", 0))
            reply.completion_tokens = _to_int(getattr(usage, "completion_tokens", 0))
            # 推理 token 各家字段名不一，两级兜底
            rt = getattr(usage, "reasoning_tokens", None)
            if rt is None:
                details = getattr(usage, "completion_tokens_details", None)
                rt = getattr(details, "reasoning_tokens", 0) if details else 0
            reply.reasoning_tokens = _to_int(rt)

        return reply

    def _parse_tool_calls(self, raw_calls: Any) -> list[ToolCall]:
        """
        解析非流式返回的 tool_calls。

        ★ 关键容错：arguments 是**字符串形式的 JSON**，模型可能吐出：
            · 正常 JSON:              '{"path": "a.py"}'
            · 带 markdown 围栏:        '```json\\n{...}\\n```'
            · 空字符串:                ''
            · 非法 JSON:               '{"path": "a.py"'  （少右括号）
        这些都不能让整个 Agent 崩掉。解析失败时记 parse_error，
        让 Policy 层返回 invalid_arguments 错误，模型有机会自我修正。
        """
        out: list[ToolCall] = []
        for i, rc in enumerate(raw_calls or []):
            fn = getattr(rc, "function", None)
            name = getattr(fn, "name", "") or ""
            raw = getattr(fn, "arguments", "") or ""
            tc = ToolCall(
                id=getattr(rc, "id", "") or f"call_{i}",
                name=name,
                arguments_raw=raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
            )
            self._fill_arguments(tc)
            out.append(tc)
        return out

    @staticmethod
    def _fill_arguments(tc: ToolCall) -> None:
        """把 arguments_raw 解析进 tc.arguments，失败则写 parse_error。"""
        text = (tc.arguments_raw or "").strip()
        if not text:
            tc.arguments = {}
            return

        # 剥掉可能存在的 markdown 代码围栏
        if text.startswith("```"):
            lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            tc.parse_error = f"JSON 解析失败：{exc.msg} (pos {exc.pos})"
            tc.arguments = {}
            return

        if not isinstance(parsed, dict):
            tc.parse_error = f"参数不是对象而是 {type(parsed).__name__}"
            tc.arguments = {}
            return

        tc.arguments = parsed

    # ===================================================================== #
    # 流式
    # ===================================================================== #
    async def chat_stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """
        流式：把 reasoning / content 的增量实时吐出来，tool_calls 按 index 拼接。

        【老项目经验 4】：tool_calls 在流里是碎片。
          正确做法是用 dict[index] 累加 arguments 字符串；
          id / name 只在第一片出现，后续片为 None，**不可覆盖已有值**。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = [t.to_dict() for t in tools]
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        # 累加器：index -> {id, name, args_str}
        acc: dict[int, dict[str, str]] = {}
        emitted: set[int] = set()

        try:
            stream = await self._client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001
            raise self._translate_exception(exc) from exc

        try:
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = getattr(chunk.choices[0], "delta", None)
                if delta is None:
                    continue

                # ---- reasoning 增量 -------------------------------------
                r = _extract_reasoning(delta)
                if r:
                    yield StreamDelta("reasoning", r)

                # ---- content 增量 ---------------------------------------
                c = getattr(delta, "content", None)
                if isinstance(c, str) and c:
                    yield StreamDelta("content", c)

                # ---- tool_calls 碎片 ------------------------------------
                for piece in getattr(delta, "tool_calls", None) or []:
                    idx = _to_int(getattr(piece, "index", 0))
                    slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})

                    pid = getattr(piece, "id", None)
                    if pid:
                        slot["id"] = pid          # 只在有值时覆盖

                    fn = getattr(piece, "function", None)
                    if fn is not None:
                        fname = getattr(fn, "name", None)
                        if fname:
                            slot["name"] = fname
                        fargs = getattr(fn, "arguments", None)
                        if fargs:
                            slot["args"] += fargs  # ★ 累加，不是替换

                    # finish_reason 出现时该 index 完成 → 吐完整 ToolCall
                    fr = getattr(chunk.choices[0], "finish_reason", None)
                    if fr in ("tool_calls", "stop") and idx not in emitted:
                        emitted.add(idx)
                        tc = ToolCall(
                            id=slot["id"] or f"call_{idx}",
                            name=slot["name"],
                            arguments_raw=slot["args"],
                        )
                        self._fill_arguments(tc)
                        yield StreamDelta("tool_call", tool_call=tc)

            # ---- 收尾：把还没吐出的 index 补上（有些供应商不发 finish_reason）----
            for idx, slot in acc.items():
                if idx in emitted:
                    continue
                tc = ToolCall(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments_raw=slot["args"],
                )
                self._fill_arguments(tc)
                yield StreamDelta("tool_call", tool_call=tc)

        finally:
            # 确保连接/生成器被释放
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

        yield StreamDelta("done")

    # ---- 流式便捷方法在基类实现（ModelAdapter.chat_stream_collect）---------

    # ===================================================================== #
    # 异常翻译
    # ===================================================================== #
    def _translate_exception(self, exc: Exception) -> ModelError:
        """
        把 SDK / httpx 的各种异常统一成 ModelError。

        为什么要做：Agent Core 只 catch ModelError 一种，
        且需要知道「可不可以重试」来推进 REFLECT 逻辑。
        """
        name = type(exc).__name__
        text = str(exc)
        low = text.lower()

        # 认证/权限
        if "401" in text or "invalid_api_key" in low or "unauthorized" in low:
            return ModelError(f"认证失败：{text[:300]}", kind="auth_error", retryable=False, raw=text)
        if "403" in text or "permission" in low:
            return ModelError(f"权限不足：{text[:300]}", kind="permission_error", retryable=False, raw=text)

        # 额度
        if "quota" in low or "insufficient" in low or "balance" in low or "arrearage" in low:
            return ModelError(f"额度不足/账户欠费：{text[:300]}", kind="quota_error", retryable=False, raw=text)

        # 限流 / 服务端
        if "429" in text or "rate limit" in low:
            return ModelError(f"触发限流：{text[:300]}", kind="rate_limit", retryable=True, raw=text)
        if "500" in text or "502" in text or "503" in text or "504" in text:
            return ModelError(f"服务端错误：{text[:300]}", kind="server_error", retryable=True, raw=text)

        # 超时
        if "timeout" in low or "Timeout" in name:
            return ModelError(f"请求超时（{self._timeout_s}s）：{text[:200]}", kind="timeout", retryable=True, raw=text)

        # 网络
        if "connect" in low or "ConnectionError" in name or "proxy" in low:
            return ModelError(
                f"网络不可达：{text[:300]}（本机代理可能拦截，试试 unset http_proxy）",
                kind="network_error",
                retryable=True,
                raw=text,
            )

        # 参数
        if "400" in text or "invalid_request" in low:
            return ModelError(f"请求参数被拒：{text[:300]}", kind="bad_request", retryable=False, raw=text)

        return ModelError(f"模型调用失败（{name}）：{text[:300]}", kind="unknown", retryable=False, raw=text)


def tools_present(payload: dict[str, Any]) -> bool:
    """剥离参数后检查 tools 是否还在 —— 用于判断该不该放弃重试。"""
    return bool(payload.get("tools"))
