"""
P2 模型层自测

跑法：
    # 离线部分（不需要 Key）
    ./.venv/Scripts/python.exe -m backend.selfcheck_p2

    # 在线部分（需要 .env 里配好 BAILIAN_API_KEY 或 DEEPSEEK_API_KEY）
    ./.venv/Scripts/python.exe -m backend.selfcheck_p2 --online
    ./.venv/Scripts/python.exe -m backend.selfcheck_p2 --online --provider deepseek
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.config import Config, setup_logging
from backend.models.adapter import (
    FakeAdapter,
    Message,
    ModelAdapter,
    ModelError,
    ModelReply,
    StreamDelta,
    ToolCall,
    ToolSpec,
)
from backend.models.openai_compat import OpenAICompatAdapter, _extract_reasoning

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(f"  [{'ok' if cond else '!!'}] {label}")


# --------------------------------------------------------------------------- #
# 离线测试
# --------------------------------------------------------------------------- #
async def offline() -> None:
    print("\n[1] Message 序列化（OpenAI 协议形状）")
    m = Message.system("你是 Loom")
    check(m.to_dict() == {"role": "system", "content": "你是 Loom"}, "system 消息")

    u = Message.user("分析项目")
    check(u.to_dict()["role"] == "user", "user 消息")

    # ★ 关键：assistant 带 tool_calls 时 content 必须是 ""，不能是 None
    a = Message.assistant("", tool_calls=[ToolCall("c1", "read_file", '{"path":"a.py"}', {"path": "a.py"})])
    d = a.to_dict()
    check(d["content"] == "", "带 tool_calls 的 assistant content 为 ''")
    check(d["tool_calls"][0]["type"] == "function", "tool_calls 元素带 type=function")
    check(d["tool_calls"][0]["function"]["name"] == "read_file", "tool_calls 内嵌 function.name")
    check(d["tool_calls"][0]["function"]["arguments"] == '{"path":"a.py"}', "arguments 保持原始 JSON 字符串")

    t = Message.tool_result("c1", "read_file", "文件内容")
    dt = t.to_dict()
    check(dt["role"] == "tool" and dt["tool_call_id"] == "c1", "tool 消息带 tool_call_id 回指")
    check(dt["name"] == "read_file", "tool 消息带 name")

    print("\n[2] ToolSpec 序列化")
    spec = ToolSpec(
        name="list_files",
        description="列出目录内容",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    ds = spec.to_dict()
    check(ds["type"] == "function", "外层 type=function")
    check(ds["function"]["parameters"]["required"] == ["path"], "JSON Schema 透传")

    print("\n[3] 全部消息可 JSON 序列化（发给供应商前的硬要求）")
    seq = [Message.system("s"), Message.user("u"), a, t]
    try:
        s = json.dumps([x.to_dict() for x in seq], ensure_ascii=False)
        check(True, f"消息序列可 JSON 化（{len(s)} 字节）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"JSON 化失败：{exc}")

    print("\n[4] FakeAdapter 剧本执行")
    fake = FakeAdapter(
        [
            ModelReply(tool_calls=[ToolCall("c1", "list_files", '{"path":"."}', {"path": "."})]),
            ModelReply(content="目录里有 3 个文件"),
        ]
    )
    r1 = await fake.chat([Message.user("看看有什么")])
    check(r1.has_tools and r1.tool_calls[0].name == "list_files", "第 1 步返回工具调用")
    r2 = await fake.chat([Message.user("看看有什么")])
    check(not r2.has_tools and "3 个文件" in r2.content, "第 2 步返回文本")
    check(len(fake.calls) == 2, "记录了 2 次调用入参")
    check(fake.calls[0][0].role == "user", "入参被完整留存供断言")

    print("\n[5] FakeAdapter 流式（THINKING 事件的数据源）")
    fake2 = FakeAdapter([ModelReply(reasoning="让我想想目录结构", content="结果是……")])
    kinds: list[str] = []
    buf: list[str] = []
    async for delta in fake2.chat_stream([Message.user("q")]):
        kinds.append(delta.kind)
        if delta.kind in ("reasoning", "content"):
            buf.append(delta.text)
    check("reasoning" in kinds and "content" in kinds and kinds[-1] == "done", f"流式种类完整 {set(kinds)}")
    check("".join(buf) == "让我想想目录结构结果是……", "增量拼接后与原文一致")

    print("\n[6] 思维链提取两级兜底")
    class _Msg1:
        reasoning_content = "一级命中的思维链"

    class _Msg2:
        model_extra = {"reasoning_content": "二级命中的思维链"}

    class _Msg3:
        pass

    check(_extract_reasoning(_Msg1()) == "一级命中的思维链", "第一级：直接属性 reasoning_content")
    check(_extract_reasoning(_Msg2()) == "二级命中的思维链", "第二级：model_extra 兜底")
    check(_extract_reasoning(_Msg3()) == "", "都没有时返回空串（不抛异常）")
    check(_extract_reasoning(None) == "", "None 安全")

    print("\n[7] tool_calls arguments 解析容错（★ 模型经常吐畸形 JSON）")
    from backend.models.openai_compat import OpenAICompatAdapter as A

    def mk(raw: str) -> ToolCall:
        tc = ToolCall(id="x", name="t", arguments_raw=raw)
        A._fill_arguments(tc)
        return tc

    check(mk('{"path": "a.py"}').arguments == {"path": "a.py"}, "正常 JSON")
    check(mk("").arguments == {} and not mk("").parse_error, "空字符串 → 空参数，不算错误")
    bad = mk('{"path": "a.py"')
    check(bad.arguments == {} and bad.parse_error, f"畸形 JSON → 记 parse_error（{bad.parse_error[:30]}…）")
    fenced = mk('```json\n{"path": "b.py"}\n```')
    check(fenced.arguments == {"path": "b.py"}, "剥掉 markdown 围栏后成功解析")
    arr = mk('["a", "b"]')
    check(arr.arguments == {} and arr.parse_error, "数组而非对象 → 拒绝")
    check(mk('{"n": 1, "s": "中文"}').arguments["s"] == "中文", "中文参数值正常")

    print("\n[8] ModelError 分类")
    e = ModelError("x", kind="timeout", retryable=True)
    check(e.retryable and e.kind == "timeout", "可重试标记正确保留")
    check(isinstance(e, RuntimeError), "ModelError 是 RuntimeError 子类（易被通用 except 接到）")

    print("\n[9] ModelReply 统计字段")
    rep = ModelReply(prompt_tokens=120, completion_tokens=80, reasoning_tokens=40)
    check(rep.total_tokens == 200, "total_tokens = prompt + completion")
    rd = rep.to_dict()
    check(rd["reasoning_tokens"] == 40, "reasoning_tokens 入序列化（成本分析用）")

    print("\n[10] 缺 Key 时报清晰错误（而不是底层 SDK 报错）")
    try:
        OpenAICompatAdapter(provider="bailian", model="m", base_url="http://x", api_key="")
        check(False, "空 Key 应当抛 ModelError")
    except ModelError as exc:
        check(exc.kind == "missing_api_key" and not exc.retryable, f"抛出 missing_api_key：{exc}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"抛了非 ModelError：{type(exc).__name__}")


# --------------------------------------------------------------------------- #
# 在线测试
# --------------------------------------------------------------------------- #
async def online(provider: str) -> None:
    cfg = Config.load(provider)
    mcfg = cfg.model
    print(f"\n[在线] provider={mcfg.provider}  model={mcfg.model}")
    print(f"        base_url={mcfg.base_url}")
    print(f"        api_key={'已配置(' + str(len(mcfg.api_key)) + ' 字符)' if mcfg.api_key else '★ 未配置 ★'}")

    if not mcfg.api_key:
        check(False, f"未配置 API Key，跳过在线测试（在 loom/.env 里填 {mcfg.provider.upper()}_API_KEY）")
        return

    adapter = OpenAICompatAdapter.from_config(mcfg)
    check(isinstance(adapter, ModelAdapter), "OpenAICompatAdapter 是 ModelAdapter 实例")

    tool = ToolSpec(
        name="read_file",
        description="读取工作区内某个文本文件的完整内容。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对于工作区根的路径"}},
            "required": ["path"],
        },
    )
    msgs = [
        Message.system("你是一个 Coding Agent。需要看文件时必须调用工具，不要凭空猜测。"),
        Message.user("请读取 main.py 的内容。"),
    ]

    print("\n[在线-1] 非流式 + function calling")
    try:
        r = await adapter.chat(msgs, [tool])
        check(True, f"请求成功（{r.duration_ms} ms，model={r.model}，finish={r.finish_reason}）")
        check(r.has_tools, f"模型选择调用工具：{[tc.name for tc in r.tool_calls]}")
        if r.has_tools:
            tc = r.tool_calls[0]
            check(tc.arguments.get("path") == "main.py", f"参数解析正确：{tc.arguments}")
            check(not tc.parse_error, "无 JSON 解析错误")
        else:
            check(False, f"模型未调用工具，而是直接回答：{r.content[:80]}")
        check(r.prompt_tokens > 0, f"token 统计可用：in={r.prompt_tokens} out={r.completion_tokens}")
        if r.reasoning:
            check(True, f"捕获到思维链 {len(r.reasoning)} 字符（推理模型）")
        else:
            check(True, "该模型未返回思维链字段（非推理模型，属正常）")
    except ModelError as exc:
        check(False, f"模型调用失败：kind={exc.kind} msg={exc}")

    print("\n[在线-2] 流式增量（GUI 实时观测的基础）")
    try:
        kinds: list[str] = []
        n_reason = n_content = 0
        tcs: list[ToolCall] = []
        async for d in adapter.chat_stream(msgs, [tool]):
            kinds.append(d.kind)
            if d.kind == "reasoning":
                n_reason += 1
            elif d.kind == "content":
                n_content += 1
            elif d.kind == "tool_call" and d.tool_call:
                tcs.append(d.tool_call)
        check("done" in kinds, f"流以 done 收尾；共 {len(kinds)} 个增量")
        check(bool(tcs) or n_content > 0, f"流式产出可用（reasoning 增量 {n_reason}，content 增量 {n_content}，tool_call {len(tcs)}）")
        if tcs:
            check(tcs[0].arguments.get("path") == "main.py", f"流式拼接后参数正确：{tcs[0].arguments}")
            check(not tcs[0].parse_error, "流式拼接未破坏 JSON")
    except ModelError as exc:
        check(False, f"流式调用失败：kind={exc.kind} msg={exc}")

    print("\n[在线-3] chat_stream_collect 边收边回调")
    seen: list[str] = []

    async def on_delta(d: StreamDelta) -> None:
        seen.append(d.kind)

    try:
        r = await adapter.chat_stream_collect(msgs, [tool], on_delta=on_delta)
        check(len(seen) > 0, f"回调被触发 {len(seen)} 次（THINKING 事件的实时来源）")
        check(r.has_tools or bool(r.content), "收集结果非空")
        check(r.duration_ms > 0, f"耗时记录 {r.duration_ms} ms")
    except ModelError as exc:
        check(False, f"流式收集失败：{exc}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="跑真实网络调用（需要 API Key）")
    ap.add_argument("--provider", default="", help="bailian | deepseek")
    args = ap.parse_args()

    setup_logging("WARNING")
    print("=" * 72)
    print("P2 模型层自测" + ("（含在线）" if args.online else "（离线）"))
    print("=" * 72)

    await offline()
    if args.online:
        await online(args.provider)

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print("\n失败项：")
        for c, label in results:
            if not c:
                print(f"  - {label}")
    print("=" * 72)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
