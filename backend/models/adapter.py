"""
Loom Agent v0.1 · ModelAdapter 抽象接口

本文件定义三件事：
  ① 中立的数据结构：Message / ToolSpec / ToolCall / ModelReply
  ② 抽象基类 ModelAdapter
  ③ 一个 FakeAdapter，用于离线测试 Agent 循环（不烧 token）

★ 关键取舍：为什么 Message 不用 openai 的原生类型？
  因为那样 Agent Core 就被 SDK 绑死了。这里用自己的一等结构，
  openai_compat.py 负责「翻译」成 SDK 要求的 dict。
  代价是多写 30 行转换代码；收益是 Agent Core 与供应商彻底解耦。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Sequence

Role = Literal["system", "user", "assistant", "tool"]


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
@dataclass
class ImageRef:
    """
    上下文里的一张图 —— **引用，不是内联 base64**（对比表 #12 的地基）。

    ★ 为什么存路径、发送时才读文件：
      ① 2MB 的截图 base64 后约 2.7MB 字符串，要进消息内存、进 state_json、
         甚至可能在事件里落盘，处处是负担；
      ② 「卸载」只需要丢掉这个引用，不用重写几 MB 的字符串；
      ③ 文件被删了就降级成一行占位文本 —— 整个请求不会因为一张图挂掉。

    ★ 为什么 `label` 是必填语义：模型看到的是一行说明 + 一张图。
      "屏幕截图 20261118_2240" 比裸图更容易让它知道这是什么时候的什么。
    """

    path: str = ""
    mime: str = "image/png"
    bytes: int = 0
    label: str = ""

    def to_part(self, *, max_bytes: int = 0) -> dict[str, Any] | None:
        """
        读文件 → `{"type":"image_url", ...}`。
        读不到 / 超过 max_bytes 返回 None，由调用方降级成文本占位符。
        """
        try:
            if max_bytes and self.bytes and self.bytes > max_bytes:
                return None
            with open(self.path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime};base64,{b64}"},
        }

    def to_dict(self) -> dict[str, Any]:
        """落库用：**只存引用**，不存图片数据。"""
        return {"path": self.path, "mime": self.mime, "bytes": self.bytes, "label": self.label}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ImageRef":
        return ImageRef(
            path=str(d.get("path") or ""),
            mime=str(d.get("mime") or "image/png"),
            bytes=int(d.get("bytes") or 0),
            label=str(d.get("label") or ""),
        )


@dataclass
class Message:
    """
    一条喂给模型的消息（中立格式）。

    role:
        system    —— 系统提示
        user      —— 用户任务
        assistant —— 模型上一轮的输出（可能带 tool_calls）
        tool      —— 工具执行结果（必须带 tool_call_id 回指）

    为什么 assistant 消息要带 tool_calls？
      OpenAI 协议要求：带 tool_calls 的 assistant 消息后面必须紧跟对应的 tool 消息，
      否则 400。这是「落库顺序」约束的协议层根源。
    """

    role: Role
    content: str = ""
    # 仅 assistant：本轮的思维链（推理模型的 reasoning_content）
    reasoning: str = ""
    # 仅 assistant：本轮的 function calling 请求
    tool_calls: list["ToolCall"] = field(default_factory=list)
    # 仅 tool：回指的是哪个调用
    tool_call_id: str = ""
    tool_name: str = ""       # 仅 tool：工具名（部分供应商要求）
    # 随本条消息一起送出的图片（对比表 #12）。**只存引用**，见 ImageRef。
    images: list[ImageRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        # 注意：content 为空但有 tool_calls 时必须显式给 ""，不能给 None，
        # 否则部分供应商（含百炼）会报参数错误。
        d["content"] = self.content if self.content else ""

        # ---- 多模态：有图时 content 必须变成 part 数组 --------------------
        # ★ 格式约定：`[{"type":"text"}, {"type":"image_url","image_url":{"url":"data:..."}}]`
        #   读不到文件的那张图**跳过**并在文本里留一句说明，绝不让整个请求失败。
        if self.images:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            dropped: list[str] = []
            for img in self.images:
                p = img.to_part()
                if p is None:
                    dropped.append(img.label or img.path)
                    continue
                parts.append(p)
            if dropped:
                parts.append({
                    "type": "text",
                    "text": "（以下图片无法读取，已省略：" + "、".join(dropped) + "）",
                })
            d["content"] = parts

        if self.role == "tool":
            d["tool_call_id"] = self.tool_call_id
            if self.tool_name:
                d["name"] = self.tool_name
        if self.role == "assistant" and self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]

        # ---- 思维链回传（推理模型必需）------------------------------------
        # ★ DeepSeek thinking 模式要求：**每一轮** assistant 都要带
        #   reasoning_content 字段，哪怕是空字符串。有些轮次（如工具结果后的
        #   反思）模型确实不吐思维链，此时字段仍必须在，否则下一轮 400：
        #   "The `reasoning_content` in the thinking mode must be passed back"
        # ★ 实测：回传空字符串 → 200；完全不带这个字段 → 400。
        # ★ 但只在字段名此前出现过时才补：非推理模型的历史里没有 reasoning，
        #   凭空加一个空字段可能被视为非法参数。
        if self.role == "assistant":
            d["reasoning_content"] = self.reasoning or ""
        return d

    # ---- 便捷构造 ----------------------------------------------------------
    @staticmethod
    def system(text: str) -> "Message":
        return Message(role="system", content=text)

    @staticmethod
    def user(text: str, images: list[ImageRef] | None = None) -> "Message":
        return Message(role="user", content=text, images=list(images or []))

    @staticmethod
    def assistant(text: str = "", tool_calls: list["ToolCall"] | None = None, reasoning: str = "") -> "Message":
        return Message(role="assistant", content=text, tool_calls=tool_calls or [], reasoning=reasoning)

    @staticmethod
    def tool_result(tool_call_id: str, name: str, content: str) -> "Message":
        return Message(role="tool", content=content, tool_call_id=tool_call_id, tool_name=name)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
@dataclass
class ToolSpec:
    """
    工具的「对外声明」—— 给模型看的那部分。

    刻意不含 handler：模型只需要知道名字、作用、参数 schema。
    handler 在 tools/ 层注册，两者在 registry 里汇合。
    """

    name: str
    description: str
    parameters: dict[str, Any]      # JSON Schema
    strict: bool = False            # 是否要求供应商做严格模式

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    """模型请求的一次函数调用。"""

    id: str
    name: str
    arguments_raw: str = ""                      # 原始 JSON 字符串（可能被流式拼接过）
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str = ""                        # JSON 解析失败时记原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_raw or "{}"},
        }


# --------------------------------------------------------------------------- #
# 模型回复
# --------------------------------------------------------------------------- #
@dataclass
class ModelReply:
    """
    模型一次回复的完整结果（中立格式）。

    Agent Core 只需要读这几个字段就能推进状态机：
        if reply.tool_calls:   → 走 ACT/OBSERVE
        elif reply.content:    → 走 REFLECT / FINAL
    """

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    # ---- 观测指标（用于调试页与成本分析）--------------------------------
    model: str = ""
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    duration_ms: int = 0
    raw_error: str = ""          # 供应商返回的错误原文（便于排查）

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "reasoning": self.reasoning,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "model": self.model,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "duration_ms": self.duration_ms,
        }


# --------------------------------------------------------------------------- #
# 流式增量
# --------------------------------------------------------------------------- #
@dataclass
class StreamDelta:
    """
    流式回复的一块增量。

    为什么 Agent Core 关心流式？
      → 因为 THINKING 事件要实时推给界面。用户能看到模型在想什么，
        是「观察 Agent 内部过程」这个项目目标的核心体验。
    """

    kind: Literal["reasoning", "content", "tool_call", "done", "usage"]
    text: str = ""              # reasoning / content 的增量文本
    tool_call: ToolCall | None = None   # tool_call 时携带（已拼接完整的那个）

    # ★ 流式 usage（对比表 #4）：OpenAI 兼容协议里它出现在**收尾帧**
    #   —— 那一帧的 choices 是空数组。旧代码 `if not chunk.choices: continue`
    #   直接把这帧扔了，所以流式路径（默认路径）永远拿不到 token。
    #   这里单独带出来，由 chat_stream_collect 填进 ModelReply。
    usage: dict[str, int] | None = None


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class ModelError(RuntimeError):
    """模型层统一异常。Agent Core 只 catch 这一个，不关心底层是 httpx 还是 openai。"""

    def __init__(self, message: str, *, kind: str = "model_error", retryable: bool = False, raw: str = ""):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.raw = raw


# --------------------------------------------------------------------------- #
# 抽象基类
# --------------------------------------------------------------------------- #
class ModelAdapter:
    """
    模型适配器抽象接口。

    只定义两个方法，够 Agent Core 用：
        chat()        一次性拿到完整回复（P2 先实现这个）
        chat_stream() 拿到增量流（P2 实现，GUI 阶段重点用）
    """

    name: str = "abstract"
    model: str = ""

    async def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelReply:
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        raise NotImplementedError
        yield  # pragma: no cover —— 让它成为 async generator

    # ------------------------------------------------------------------ #
    # 通用工具方法（在基类实现，所有适配器自动获得）
    # ------------------------------------------------------------------ #
    async def chat_stream_collect(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: Any = None,
    ) -> ModelReply:
        """
        流式收集：既能在增量到达时回调（推 THINKING 事件），又返回完整回复。

        ★ 为什么放在基类而不是每个实现里重写？
          Agent Core 用的就是这个方法（既要实时可观测，又要完整结果）。
          把它放在基类，任何适配器——真实供应商或测试用的 Fake——
          都自动具备这个能力，Agent Core 不必关心底层是否真的支持流式。
          这是一个「接口收敛」的例子：把便利方法放在抽象层，实现层只管最原语的能力。
        """
        import time

        t0 = time.time()
        reasoning: list[str] = []
        content: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] | None = None

        async for d in self.chat_stream(messages, tools, temperature=temperature, max_tokens=max_tokens):
            # ★ usage 不进 on_delta：那个回调是给界面推 THINKING 的，
            #   把 token 数字塞过去会让界面多出一堆无意义片段。
            if d.kind == "usage":
                if d.usage:
                    usage = d.usage
                continue

            if d.kind in ("reasoning", "content") and on_delta is not None:
                res = on_delta(d)
                if hasattr(res, "__await__"):
                    await res

            if d.kind == "reasoning":
                reasoning.append(d.text)
            elif d.kind == "content":
                content.append(d.text)
            elif d.kind == "tool_call" and d.tool_call is not None:
                tool_calls.append(d.tool_call)

        reply = ModelReply(
            content="".join(content),
            reasoning="".join(reasoning),
            tool_calls=tool_calls,
            model=self.model,
            duration_ms=int((time.time() - t0) * 1000),
        )
        if usage:
            reply.prompt_tokens = int(usage.get("prompt", 0) or 0)
            reply.completion_tokens = int(usage.get("completion", 0) or 0)
            reply.reasoning_tokens = int(usage.get("reasoning", 0) or 0)
        return reply

    def spec(self) -> ToolSpec | None:  # 便于统一日志
        return None


# --------------------------------------------------------------------------- #
# 离线适配器（测试用）
# --------------------------------------------------------------------------- #
class FakeAdapter(ModelAdapter):
    """
    剧本式假适配器：按预设脚本依次吐出回复，不联网。

    用途：P3 开发 Agent 五阶段循环时，先用它把循环跑通、把事件流打出来看，
    完全不需要 API Key，也不烧 token。等循环逻辑稳定了再接真模型。

    用法：
        fake = FakeAdapter([
            ModelReply(tool_calls=[ToolCall("c1", "list_files", '{"path":"."}', {"path": "."})]),
            ModelReply(content="我看到了 3 个文件……"),
            # 剧本里也可以放异常来测错误路径（chat_stream 会原样抛出）
            ModelError("模拟服务端 500", kind="server_error", retryable=True),
        ])
    """

    name = "fake"

    def __init__(self, script: list[ModelReply | ModelError] | None = None, *, model: str = "fake-1"):
        # ★ 剧本元素类型是 ModelReply | ModelError —— 后者用于测模型层错误路径。
        self.script: list[ModelReply | ModelError] = list(script or [])
        self.model = model
        self.calls: list[list[Message]] = []   # 记录每次收到的消息，便于断言
        # ★ 也记下每次调用带没带 tools。
        #   为什么值得单独记：Agent 的「阶段门控」靠它来验证 ——
        #   PLAN / REFLECT / FINAL 阶段必须**不带工具**（否则模型可能在该收口的
        #   时候又去调工具，阶段就形同虚设）。没有这个记录，测试只能靠
        #   「看模型有没有调工具」间接推断，测不出「工具压根没给」这层保证。
        self.tools_seen: list[list | None] = []
        self.last_tools: list | None = None

    def push(self, reply: "ModelReply | ModelError") -> None:
        self.script.append(reply)

    @property
    def tools_gate(self) -> list[bool]:
        """每次调用是否**提供了工具**。用于断言阶段门控。"""
        return [bool(t) for t in self.tools_seen]

    async def chat(self, messages, tools=None, *, temperature=None, max_tokens=None) -> ModelReply:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools) if tools else None)
        self.last_tools = list(tools) if tools else None
        if not self.script:
            return ModelReply(content="(剧本已用尽)", model=self.model, finish_reason="stop")
        return self.script.pop(0)

    async def chat_stream(self, messages, tools=None, *, temperature=None, max_tokens=None) -> AsyncIterator[StreamDelta]:
        reply = await self.chat(messages, tools, temperature=temperature, max_tokens=max_tokens)

        # ★ 剧本里放 ModelError 时，要**原样抛出去**，而不是当成 ModelReply 用。
        #   为什么这是个真问题：真实适配器在流式过程中遇到网络/配额错误，
        #   抛的就是 ModelError。如果 Fake 在这里不抛（而是拿它当回复对象读
        #   `.reasoning`），测试里就会变成 AttributeError → 被 Agent 归类为
        #   internal_error，于是「模型错误」和「我们自己的 bug」在事件流里
        #   长得一模一样 —— 测试等于没覆盖到错误路径。
        #   让 Fake 与真实适配器行为一致，这条路径才真的被测到。
        if isinstance(reply, BaseException):
            raise reply

        if reply.reasoning:
            for i in range(0, len(reply.reasoning), 8):
                yield StreamDelta("reasoning", reply.reasoning[i : i + 8])
                await asyncio.sleep(0)
        if reply.content:
            for i in range(0, len(reply.content), 8):
                yield StreamDelta("content", reply.content[i : i + 8])
                await asyncio.sleep(0)
        for tc in reply.tool_calls:
            yield StreamDelta("tool_call", tool_call=tc)
        yield StreamDelta("done")
