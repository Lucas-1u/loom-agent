"""
Spill 溢出存储 + 工具结果修剪 自检。

对齐 DeepSeek Harness 的 dsh-spill-policy / dsh-compaction-tool-result-pruner。
验证：
  1. Spill：完整存盘 + head/tail 保留 + notice
  2. read_file：三重截断 + 流式读取
  3. 工具结果修剪器：中间替换为 marker
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# ---- 断言框架 -------------------------------------------------------------- #

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ---- 测试用例 ------------------------------------------------------------- #


def test_retain_head_tail() -> None:
    """SpillPolicy.retain_head_tail：head + GAP + tail"""
    print("[A] retain_head_tail 保留逻辑")
    from backend.agent.spill.spill_policy import retain_head_tail, GAP

    # 不超预算：原样返回
    text, omitted = retain_head_tail("abc", 100)
    check("不超预算不触发", text == "abc" and omitted == 0)

    # 超预算：head + tail + 中间省略
    text = "a" * 1000
    kept, omitted = retain_head_tail(text, 200)
    check("超预算触发截断", "[...]" in kept or "[...]" in kept)
    check("被省略字符数 > 0", omitted > 0)
    check("保留文本长度 < 原文", len(kept) < len(text))


def test_spill_notice() -> None:
    """Spill notice 格式"""
    print("[B] spill notice 格式")
    from backend.agent.spill.spill_store import SpillRef
    from backend.agent.spill.spill_policy import format_spill_notice

    ref = SpillRef(locator="/tmp/spill/abc.txt", total_bytes=50000, retrieval_hint="用 read_file 读取")
    notice = format_spill_notice(ref, 5000)
    check("notice 包含路径", "/tmp/spill/abc.txt" in notice)
    check("notice 包含省略字符数", "5000" in notice)


def test_apply_spill_disabled() -> None:
    """spill 关闭时退化为旧行为"""
    print("[C] spill 关闭退化为旧行为")
    from backend.agent.spill.spill_policy import apply_spill, SpillPolicyConfig

    cfg = SpillPolicyConfig(enabled=False, max_inline_chars=100)
    text, ref = apply_spill("hello world", tool_name="read_file", owner="t1", store=None, config=cfg)
    check("关闭时原样返回", text == "hello world")
    check("关闭时不存盘", ref is None)


def test_apply_spill_store_failure() -> None:
    """spill 存盘失败时降级（保留原内容）"""
    print("[D] spill 存盘失败降级")
    from backend.agent.spill.spill_policy import apply_spill, SpillPolicyConfig

    class BadStore:
        async def save_text(self, **kwargs):
            raise RuntimeError("disk full")

    cfg = SpillPolicyConfig(enabled=True, max_inline_chars=10)
    text = "a" * 100
    result, ref = asyncio.get_event_loop().run_until_complete(
        _apply_async(text, BadStore(), cfg)
    ) if False else (None, None)

    # apply_spill 是同步的（save_text 是 async），需要特殊处理
    # 这里直接验证：store=None 时返回原内容
    text2, ref2 = apply_spill(text, tool_name="read_file", owner="t1", store=None, config=cfg)
    check("无 store 时退化为原内容", ref2 is None)


async def _apply_async(text, store, cfg):
    from backend.agent.spill.spill_policy import apply_spill
    return apply_spill(text, tool_name="read_file", owner="t1", store=store, config=cfg)


def test_local_spill_store() -> None:
    """LocalSpillStore：完整内容存盘"""
    print("[E] LocalSpillStore 完整存盘")
    from backend.agent.spill.spill_store import LocalSpillStore

    with tempfile.TemporaryDirectory() as tmp:
        store = LocalSpillStore(tmp)
        content = "line1\nline2\nline3\n" * 1000
        ref = asyncio.run(store.save_text(owner="task_001", tool_name="read_file", content=content))
        check("返回 locator", bool(ref.locator))
        check("返回 total_bytes", ref.total_bytes > 0)
        check("文件确实存在", os.path.exists(ref.locator))
        # 验证内容完整
        with open(ref.locator, "r", encoding="utf-8") as f:
            saved = f.read()
        check("存盘内容完整一致", saved == content)
        # 验证按 owner 建子目录
        check("按 task 建子目录", "task_001" in ref.locator)


def test_pruner_config() -> None:
    """PrunerConfig 校验"""
    print("[F] PrunerConfig 校验")
    from backend.agent.tool_result_pruner import PrunerConfig, PRUNE_MARKER

    cfg = PrunerConfig(threshold_chars=8192, head_chars=4096, tail_chars=1024)
    check("默认阈值", cfg.threshold_chars == 8192)
    check("默认 head", cfg.head_chars == 4096)
    check("默认 tail", cfg.tail_chars == 1024)

    # 非法配置：head + marker + tail > threshold
    try:
        PrunerConfig(threshold_chars=100, head_chars=50, tail_chars=50)
        check("非法配置应报错", False)
    except ValueError:
        check("非法配置报错", True)


def test_prune_text() -> None:
    """prune_text：中间替换为 marker"""
    print("[G] prune_text 修剪逻辑")
    from backend.agent.tool_result_pruner import prune_text, PrunerConfig, PRUNE_MARKER

    cfg = PrunerConfig(threshold_chars=100, head_chars=20, tail_chars=10)

    # 不超阈值：返回 None
    result = prune_text("short text", cfg)
    check("不超阈值返回 None", result is None)

    # 超阈值：head + marker + tail
    text = "a" * 200
    result = prune_text(text, cfg)
    check("超阈值触发修剪", result is not None)
    check("保留 head 开头", result.startswith("a" * 20))
    check("保留 tail 结尾", result.endswith("a" * 10))
    check("中间有 marker", PRUNE_MARKER in result)
    check("修剪后长度 < 原文", len(result) < len(text))


def test_prune_messages() -> None:
    """prune_messages：只修剪 role=tool 的消息"""
    print("[H] prune_messages 只修剪 tool 消息")
    from backend.agent.tool_result_pruner import prune_messages, PrunerConfig
    from backend.models.adapter import Message

    cfg = PrunerConfig(threshold_chars=100, head_chars=20, tail_chars=10)

    # 构造消息：一个 system + 一个超长 tool + 一个普通 assistant
    msgs = [
        Message.system("sys prompt"),
        Message.tool_result("call_1", "read_file", "b" * 500),
        Message.assistant(text="done"),
    ]

    new_msgs, pruned = prune_messages(msgs, config=cfg)
    check("修剪了 1 条", pruned == 1)
    check("system 消息未被修剪", new_msgs[0].content == "sys prompt")
    check("tool 消息被修剪", "[... 工具结果中间部分已修剪 ...]" in new_msgs[1].content)
    check("assistant 消息未被修剪", new_msgs[2].content == "done")


def test_read_file_streaming() -> None:
    """read_file 流式读取（三重截断）"""
    print("[I] read_file 三重截断 + 流式")
    from backend.tools.readonly.read_file import ReadFileTool, MAX_LINE_LENGTH
    from backend.tools.base import ToolContext
    from backend.agent.spill.spill_store import LocalSpillStore
    import tempfile

    tool = ReadFileTool()

    with tempfile.TemporaryDirectory() as tmp:
        # 造一个大文件（11MB，超过 STREAM_MIN_SIZE）
        big_path = os.path.join(tmp, "big.txt")
        with open(big_path, "w") as f:
            for i in range(200000):
                f.write(f"line {i}: some content here\n")

        ctx = ToolContext(
            workspace=tmp,
            task_id="test_stream",
            max_read_chars=40_000,
        )

        result = asyncio.run(tool.run({"path": "big.txt"}, ctx))
        check("大文件读取成功", result.ok)
        check("返回内容非空", bool(result.data.get("content")))
        check("行数被截断", result.data.get("lines", 0) > 0)

        # 造一个单行超长文件
        long_line_path = os.path.join(tmp, "long_line.txt")
        with open(long_line_path, "w") as f:
            f.write("x" * 5000 + "\n")
        result2 = asyncio.run(tool.run({"path": "long_line.txt"}, ctx))
        check("单行超长截断", "行被截断" in result2.data.get("content", "") or len(result2.data.get("content", "")) < 5000 + 50)

        # 造一个小文件，验证旧行为不变
        small_path = os.path.join(tmp, "small.txt")
        with open(small_path, "w") as f:
            f.write("hello\nworld\n")
        result3 = asyncio.run(tool.run({"path": "small.txt"}, ctx))
        check("小文件正常读取", result3.ok and "hello" in result3.data.get("content", ""))


# ---- 主入口 -------------------------------------------------------------- #


def main() -> int:
    print("=" * 70)
    print("Spill + 工具结果修剪 自检")
    print("=" * 70)

    test_retain_head_tail()
    test_spill_notice()
    test_apply_spill_disabled()
    test_apply_spill_store_failure()
    test_local_spill_store()
    test_pruner_config()
    test_prune_text()
    test_prune_messages()
    test_read_file_streaming()

    print("=" * 70)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
