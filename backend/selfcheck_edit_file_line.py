"""
edit_file 行号锚定 + 模糊匹配 自检（P2-#15）

覆盖两个层面：
  A. 辅助函数层（_find_occurrence_at_line / _fuzzy_match_at_line）
  B. 工具集成层（EditFileTool.run 端到端，含审批 ctx）

对齐目标：DSH dsh-tool-str-replace-editor 的行号锚定机制。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.tools.mutating.edit_file import (
    EditFileTool,
    _find_occurrence_at_line,
    _fuzzy_match_at_line,
)
from backend.tools.base import ToolContext

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


# --------------------------------------------------------------------------- #
# 辅助函数层
# --------------------------------------------------------------------------- #
def test_find_occurrence_at_line():
    print("\n== A1. _find_occurrence_at_line ==")
    content = "line one\nline two\nline three\nline two again\n"
    needle = "line two"

    # 第一次出现在第 2 行
    idx = _find_occurrence_at_line(content, needle, 2)
    check("A1a 第2行匹配", idx is not None and content[idx:idx+len(needle)] == needle,
          f"idx={idx}")

    # 第二次出现在第 4 行
    idx2 = _find_occurrence_at_line(content, needle, 4)
    check("A1b 第4行匹配", idx2 is not None, f"idx={idx2}")

    # 行号不在匹配位置
    idx3 = _find_occurrence_at_line(content, needle, 99)
    check("A1c 不存在的行号返回None", idx3 is None)

    # 多行 old_string
    multi = "aaa\nbbb\naaa\nbbb\n"
    idx4 = _find_occurrence_at_line(multi, "aaa\nbbb", 1)
    check("A1d 多行匹配第1行", idx4 == 0, f"idx={idx4}")
    idx5 = _find_occurrence_at_line(multi, "aaa\nbbb", 3)
    check("A1e 多行匹配第3行", idx5 is not None, f"idx={idx5}")


def test_fuzzy_match_at_line():
    print("\n== A2. _fuzzy_match_at_line ==")
    content = "    indented line\nother\n    indented line\n"

    # 精确匹配（返回不含行尾 \n）
    r1 = _fuzzy_match_at_line(content, "    indented line", 1)
    check("A2a 精确匹配", r1 == "    indented line", f"got={r1!r}")

    # 缩进不同（模糊匹配）
    r2 = _fuzzy_match_at_line(content, "  indented line", 1)
    check("A2b 缩进不同模糊匹配", r2 == "    indented line", f"got={r2!r}")

    # 完全对不上的内容
    r3 = _fuzzy_match_at_line(content, "nonexistent content", 1)
    check("A2c 内容不匹配返回None", r3 is None)

    # 行号超出范围
    r4 = _fuzzy_match_at_line(content, "indented line", 99)
    check("A2d 行号超范围返回None", r4 is None)

    # 多行模糊匹配（返回不含末尾 \n）
    multi_content = "x\n  line1\n  line2\ny\n"
    r5 = _fuzzy_match_at_line(multi_content, "line1\nline2", 2)
    check("A2e 多行模糊匹配", r5 == "  line1\n  line2", f"got={r5!r}")


# --------------------------------------------------------------------------- #
# 工具集成层
# --------------------------------------------------------------------------- #
def make_ctx(ws: str) -> ToolContext:
    # 步骤 5：authorized / approval_id 已删（审批移除）
    return ToolContext(
        workspace=ws,
        task_id="t_test",
        turn=1,
    )


def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


async def test_integration(ws: str):
    tool = EditFileTool()
    ctx = make_ctx(ws)

    print("\n== B1. 唯一匹配正常编辑（基线） ==")
    target = os.path.join(ws, "b1.txt")
    write_file(target, "hello world\nthis is a test\n")
    r = await tool.run({"path": "b1.txt", "old_string": "hello world",
                        "new_string": "hello loom"}, ctx)
    check("B1a 唯一匹配成功", r.ok, f"err={r.error}")
    check("B1b 内容已改", "hello loom" in read_file(target))

    print("\n== B2. 多匹配 + 行号锚定消歧 ==")
    target = os.path.join(ws, "b2.txt")
    write_file(target, "target\nother\ntarget\nother\n")
    # 不传 line → 拒绝
    r1 = await tool.run({"path": "b2.txt", "old_string": "target",
                         "new_string": "changed"}, ctx)
    check("B2a 无line多匹配拒绝", not r1.ok, f"ok={r1.ok}")
    check("B2b 错误类型patch_conflict",
          r1.error and r1.error.get("kind") == "patch_conflict",
          f"err={r1.error}")
    # 传 line=1 → 只改第一处
    r2 = await tool.run({"path": "b2.txt", "old_string": "target",
                         "new_string": "changed", "line": 1}, ctx)
    check("B2c 行号锚定成功", r2.ok, f"err={r2.error}")
    content = read_file(target)
    check("B2d 第一处已改", content.startswith("changed\n"), f"content={content!r}")
    check("B2e 第二处未改", "target" in content.split("\n", 1)[1],
          f"content={content!r}")

    print("\n== B3. count==0 + 行号模糊匹配（缩进差异） ==")
    target = os.path.join(ws, "b3.txt")
    write_file(target, "    indented line\nother\n")
    # old_string 内部空格数与文件不同 → count=0，但归一化后相同 → 模糊匹配
    r = await tool.run({"path": "b3.txt", "old_string": "  indented   line",
                        "new_string": "FIXED", "line": 1}, ctx)
    check("B3a 模糊匹配成功", r.ok, f"err={r.error}")
    check("B3b 内容已改", read_file(target).startswith("FIXED"),
          f"content={read_file(target)!r}")

    print("\n== B4. count==0 + 行号模糊匹配失败 → 拒绝 ==")
    target = os.path.join(ws, "b4.txt")
    write_file(target, "completely different\nother\n")
    r = await tool.run({"path": "b4.txt", "old_string": "not there at all",
                        "new_string": "FIXED", "line": 1}, ctx)
    check("B4a 模糊匹配失败拒绝", not r.ok)
    check("B4b 错误类型patch_conflict",
          r.error and r.error.get("kind") == "patch_conflict")

    print("\n== B5. 多匹配 + 行号指向不匹配的位置 → 拒绝 ==")
    target = os.path.join(ws, "b5.txt")
    write_file(target, "match\nother\nmatch\n")
    r = await tool.run({"path": "b5.txt", "old_string": "match",
                        "new_string": "changed", "line": 2}, ctx)
    check("B5a 行号不匹配拒绝", not r.ok, f"ok={r.ok}")
    # 行号 2 处没有匹配，但 line=3 有
    r2 = await tool.run({"path": "b5.txt", "old_string": "match",
                         "new_string": "changed", "line": 3}, ctx)
    check("B5b 正确行号成功", r2.ok, f"err={r2.error}")

    print("\n== B6. 行号锚定不破坏 replace_all 语义 ==")
    target = os.path.join(ws, "b6.txt")
    write_file(target, "aaa\naaa\naaa\n")
    r = await tool.run({"path": "b6.txt", "old_string": "aaa",
                        "new_string": "bbb", "replace_all": True}, ctx)
    check("B6a replace_all 全改", r.ok and read_file(target).count("bbb") == 3,
          f"content={read_file(target)!r}")

    print("\n== B7. 无行号时保持原有安全语义（多匹配拒绝） ==")
    target = os.path.join(ws, "b7.txt")
    write_file(target, "dup\nother\ndup\n")
    r = await tool.run({"path": "b7.txt", "old_string": "dup",
                        "new_string": "fixed"}, ctx)
    check("B7a 无line多匹配仍拒绝", not r.ok)
    # 确认数据里带了 line 参数的提示
    hint = r.error.get("hint", "") if r.error else ""
    check("B7b 提示含line参数说明", "line" in hint, f"hint={hint!r}")

    print("\n== B8. 模糊匹配后 backup 仍正常 ==")
    target = os.path.join(ws, "b8.txt")
    write_file(target, "  orig\nother\n")
    r = await tool.run({"path": "b8.txt", "old_string": "orig",
                        "new_string": "new", "line": 1}, ctx)
    check("B8a 模糊匹配+备份成功", r.ok, f"err={r.error}")
    # 备份目录存在
    backup_dir = os.path.join(ws, ".loom_backup")
    check("B8b 备份目录存在", os.path.isdir(backup_dir),
          f"dir={backup_dir}")


# --------------------------------------------------------------------------- #
async def main():
    print("=== edit_file 行号锚定自检（P2-#15）===")

    # 辅助函数层
    test_find_occurrence_at_line()
    test_fuzzy_match_at_line()

    # 集成层（需要临时目录）
    with tempfile.TemporaryDirectory() as tmp:
        # 写入一个工作区标记文件
        write_file(os.path.join(tmp, ".workspace"), "")
        await test_integration(tmp)

    print(f"\n{'='*40}")
    print(f"结果: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("失败项:")
        for f in FAILURES:
            print(f"  - {f}")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
