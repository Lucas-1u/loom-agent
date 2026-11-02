"""
Pruner replay-safe 自检（P1-#10）

验证：
  A. 单次修剪正常
  B. 重复修剪同一消息不会嵌套 marker
  C. shadowed_tool_call_ids 正确跟踪
  D. 不同 tool_call_id 互不干扰
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent.tool_result_pruner import PrunerConfig, prune_messages
from backend.models.adapter import Message

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} {detail}")


def main():
    print("=" * 70)
    print("Pruner replay-safe 自检（P1-#10 · 对齐 DSH dsh-compaction-tool-result-pruner）")
    print("=" * 70)

    # 构造测试消息
    long_text = "A" * 5000 + "B" * 5000  # 10000 字符
    msg_tool = Message(role="tool", content=long_text, tool_call_id="call_001")
    msgs = [msg_tool]

    # A. 单次修剪正常
    cfg = PrunerConfig(threshold_chars=8192, head_chars=100, tail_chars=100)
    new_msgs, pruned = prune_messages(msgs, cfg)
    check("A1 单次修剪 pruned=1", pruned == 1)
    check("A2 单次修剪后内容变短", len(new_msgs[0].content) < len(long_text))
    check("A3 单次修剪后含 marker", "[... 工具结果中间部分已修剪 ...]" in new_msgs[0].content)

    # B. 重复修剪同一消息不会嵌套 marker
    new_msgs2, pruned2 = prune_messages(new_msgs, cfg)
    check("B1 重复修剪 pruned=0", pruned2 == 0, f"got {pruned2}")
    check("B2 重复修剪后内容不变", len(new_msgs2[0].content) == len(new_msgs[0].content))
    check("B3 重复修剪后 marker 不嵌套", new_msgs2[0].content.count("[... 工具结果中间部分已修剪 ...]") == 1)

    # C. shadowed_tool_call_ids 正确跟踪
    check("C1 shadowed 集合含 tool_call_id", "call_001" in cfg.shadowed_tool_call_ids)

    # D. 不同 tool_call_id 互不干扰
    msg_tool2 = Message(role="tool", content=long_text, tool_call_id="call_002")
    msgs2 = [msg_tool2]
    new_msgs3, pruned3 = prune_messages(msgs2, cfg)
    check("D1 新 tool_call_id 可修剪", pruned3 == 1)
    check("D2 新 tool_call_id 被记录", "call_002" in cfg.shadowed_tool_call_ids)

    # 空 tool_call_id 不应被记录
    msg_tool3 = Message(role="tool", content=long_text, tool_call_id="")
    msgs3 = [msg_tool3]
    cfg3 = PrunerConfig(threshold_chars=8192, head_chars=100, tail_chars=100)
    new_msgs4, pruned4 = prune_messages(msgs3, cfg3)
    check("D3 空 tool_call_id 不记录", len(cfg3.shadowed_tool_call_ids) == 0)

    print("\n" + "=" * 70)
    print(f"结果：{PASS}/{PASS + FAIL} 通过")
    if FAILURES:
        print("\n失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
