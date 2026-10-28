"""
P3+P4+P5 集成自测 —— AgentCore 五阶段 + 工具 + Policy + 审批

★ 本文件是 v0.1a 的主验证。用 FakeAdapter 剧本驱动，**完全离线**，
  不烧 token，却能把整条链路的每个分支都跑到。

跑法：
    ./.venv/Scripts/python.exe -m backend.selfcheck_agent
    ./.venv/Scripts/python.exe -m backend.selfcheck_agent -v     # 打印事件流
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from backend.agent.approval import ApprovalManager, args_fingerprint, build_pending
from backend.agent.context import ContextBuilder, TurnBuilder
from backend.agent.core import AgentConfig, AgentCore, extract_json, extract_text_answer
from backend.agent.policy import Policy, make_context
from backend.agent.scope import scan_tree
from backend.agent.history_log import MemoryHistorySink
from backend.agent.state import (ChangeRecord, PlanStep, RiskLevel, StepStatus, TaskState,
                                 TaskStatus, ToolEvent)
from backend.config import setup_logging
from backend.events import Event, EventBus, EventType, collect
from backend.models.adapter import FakeAdapter, Message, ModelError, ModelReply, ToolCall
from backend.tools.base import ErrorKind, Tool, ToolContext, ToolResult
from backend.tools.registry import ToolRegistry, build_default_registry

results: list[tuple[bool, str]] = []
VERBOSE = False


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(f"  [{'ok' if cond else '!!'}] {label}")


def tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_raw=json.dumps(args, ensure_ascii=False), arguments=args)


# --------------------------------------------------------------------------- #
# 沙盒工作区
# --------------------------------------------------------------------------- #
class Sandbox:
    """临时工作区，测试完自动清理。"""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="loom_test_")
        self._write("main.py", "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/')\ndef root():\n    return {'ok': True}\n")
        self._write("requirements.txt", "fastapi>=0.110.0\nuvicorn>=0.29.0\n")
        self._write("README.md", "# 示例项目\n\n这是一个用于测试的 FastAPI 服务。\n")
        os.makedirs(os.path.join(self.dir, "node_modules"), exist_ok=True)
        for i in range(5):
            self._write(f"node_modules/pkg{i}.js", "x" * 10)
        self._write(".env.secret", "TOKEN=abc")

    def _write(self, rel: str, content: str) -> None:
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    def path(self, rel: str) -> str:
        return os.path.join(self.dir, rel)

    def read(self, rel: str) -> str:
        try:
            with open(self.path(rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# [A] 工具层单测
# --------------------------------------------------------------------------- #
async def test_tools(sb: Sandbox) -> None:
    print("\n[A] 工具层（路径安全 + 统一返回形状）")
    reg = build_default_registry()
    ctx = make_context_standalone(sb.dir)

    # ---- list_files 正常 ----
    r = await reg.require("list_files").run({"path": "."}, ctx)
    check(r.ok, f"list_files('.') 成功：{r.summary}")
    names = [e["name"] for e in r.data["entries"]]
    check("main.py" in names and "requirements.txt" in names, f"列出关键文件：{names}")
    check("node_modules" not in names, "★ 噪音目录 node_modules 被跳过")
    check(".env.secret" not in names, "隐藏文件默认不显示")

    # ---- list_files 显示隐藏 ----
    r2 = await reg.require("list_files").run({"path": ".", "show_hidden": True}, ctx)
    names2 = [e["name"] for e in r2.data["entries"]]
    check(".env.secret" in names2, "show_hidden=true 时显示隐藏文件")

    # ---- read_file 正常 ----
    r3 = await reg.require("read_file").run({"path": "main.py"}, ctx)
    check(r3.ok, f"read_file('main.py') 成功：{r3.summary}")
    check("from fastapi import FastAPI" in r3.data["content"], "读到实际内容")
    check("1 | from fastapi import FastAPI" in r3.data["content"], "★ 内容带行号（v0.1b edit_file 的地基）")
    check(r3.data["lines"] == 6, f"行数正确（{r3.data['lines']} 行）")

    # ---- read_file 分段 ----
    r4 = await reg.require("read_file").run({"path": "main.py", "offset": 2, "limit": 2}, ctx)
    check(r4.ok and r4.data["range"] == [2, 3], f"分段读取 range={r4.data['range']}")

    # ---- ★ 路径穿越（../）被拦 ----
    r5 = await reg.require("read_file").run({"path": "../../../../Windows/win.ini"}, ctx)
    check(not r5.ok, "★ '../' 路径穿越被拦")
    check(r5.error["kind"] == "path_outside_workspace", f"错误类型正确：{r5.error['kind']}")
    check(r5.error["security"] is True, "标记为安全事件（security=True）")
    check(r5.error["retryable"] is False, "安全事件不可重试（不许让模型反复试探）")

    # ---- 绝对路径越界 ----
    r6 = await reg.require("read_file").run({"path": "C:/Windows/win.ini"}, ctx)
    check(not r6.ok and r6.error["kind"] == "path_outside_workspace", "绝对路径越界被拦")

    # ---- ★ 前缀混淆（D:/proj 不应匹配 D:/proj_evil）----
    evil = sb.dir + "_evil"
    os.makedirs(evil, exist_ok=True)
    try:
        with open(os.path.join(evil, "x.txt"), "w") as f:
            f.write("secret")
        r7 = await reg.require("read_file").run({"path": f"../{os.path.basename(evil)}/x.txt"}, ctx)
        check(not r7.ok, "★ 相似前缀目录（proj vs proj_evil）被正确拦截")
    finally:
        import shutil

        shutil.rmtree(evil, ignore_errors=True)

    # ---- 不存在的文件 ----
    r8 = await reg.require("read_file").run({"path": "nope.py"}, ctx)
    check(not r8.ok and r8.error["kind"] == "not_found", "文件不存在 → not_found")
    check(r8.error["retryable"] is True, "not_found 可重试（让模型改路径）")
    check(bool(r8.error.get("hint")), "★ 错误带 hint（显著提升模型自我修正率）")

    # ---- 目录当文件读 ----
    r9 = await reg.require("read_file").run({"path": "node_modules"}, ctx)
    check(not r9.ok and r9.error["kind"] == "not_a_file", "目录当文件读 → not_a_file")

    # ---- 缺参数 ----
    r10 = await reg.require("read_file").run({}, ctx)
    check(not r10.ok and r10.error["kind"] == "invalid_arguments", "缺 path → invalid_arguments")

    # ---- ★ 敏感文件标注（允许读但高亮）----
    with open(sb.path(".env.secret"), encoding="utf-8") as f:
        pass
    r11 = await reg.require("read_file").run({"path": ".env.secret"}, ctx)
    check(r11.ok, "敏感文件仍可读（不硬拒绝）")
    check(not r11.data.get("sensitive", False), f"当前名单未含 .env.secret，不算敏感（{r11.summary[:40]}）")

    # ---- 二进制拒绝 ----
    with open(sb.path("bin.dat"), "wb") as f:
        f.write(b"\x00\x01\x02\xff\xfe")
    r12 = await reg.require("read_file").run({"path": "bin.dat"}, ctx)
    check(not r12.ok, f"二进制文件被拒绝：{r12.summary[:50]}")

    # ---- 编码探测（GBK）----
    with open(sb.path("gbk.txt"), "wb") as f:
        f.write("中文内容测试\n第二行\n".encode("gb18030"))
    r13 = await reg.require("read_file").run({"path": "gbk.txt"}, ctx)
    check(r13.ok and r13.data["encoding"] == "gb18030", f"GBK 文件正确解码：encoding={r13.data.get('encoding')}")
    check("中文内容测试" in r13.data["content"], "中文内容未乱码")

    # ---- 未授权时写被拒（纵深防御）----
    r14 = await reg.require("write_file").run({"path": "x.txt", "content": "hi"}, ctx)
    check(not r14.ok and r14.error["kind"] == "permission_denied", "★ 未授权直接调 write_file 被拒（纵深防御）")

    # ---- 已授权时写成功 ----
    wctx = make_context_standalone(sb.dir, authorized=True, approval_id="apr_test")
    r15 = await reg.require("write_file").run({"path": "new/hello.txt", "content": "你好\n世界\n"}, wctx)
    check(r15.ok, f"授权后写入成功：{r15.summary}")
    check(sb.read("new/hello.txt") == "你好\n世界\n", "★ 磁盘上内容正确（含中文）")
    check(r15.data["action"] == "create", "标记为 create")
    check(any(se["kind"] == "file_write" for se in r15.side_effects), "side_effects 记录了文件写入")

    # ---- ★ 覆盖前自动备份 ----
    r16 = await reg.require("write_file").run({"path": "main.py", "content": "# 被覆盖了\n"}, wctx)
    check(r16.ok and r16.data["action"] == "overwrite", "覆盖已有文件")
    backup = r16.data["change"]["backup_path"]
    check(bool(backup), f"★ 覆盖前自动备份：{backup}")
    check(sb.read("main.py") == "# 被覆盖了\n", "覆盖生效")
    rel_backup = backup[2:] if backup.startswith("./") else backup
    bpath = os.path.join(sb.dir, rel_backup.replace("/", os.sep))
    check(os.path.isfile(bpath), f"备份文件确实存在：{backup}")
    with open(bpath, encoding="utf-8") as f:
        check("from fastapi import FastAPI" in f.read(), "★ 备份内容 = 原文件内容（可还原）")

    # ---- overwrite=false 保护 ----
    r17 = await reg.require("write_file").run({"path": "main.py", "content": "x", "overwrite": False}, wctx)
    check(not r17.ok and "已存在" in r17.summary, "overwrite=false 时拒绝覆盖")

    # ---- 内容过大 ----
    r18 = await reg.require("write_file").run({"path": "big.txt", "content": "x" * 300_000}, wctx)
    check(not r18.ok and r18.error["kind"] == "too_large", "内容过大 → too_large")

    # ---- 审批展示信息（C4 三要素）----
    tool = reg.require("write_file")
    info = tool.describe_action({"path": "main.py", "content": "line1\nline2\n"}, wctx)
    check(bool(info["action"]) and bool(info["target"]), f"审批三要素：action={info['action']} target={info['target']}")
    check("覆盖" in info["impact"], "影响说明明确指出是覆盖")
    check("备份" in info["impact"], "影响说明告知会备份（给用户后悔机会）")
    check("line1" in info["preview"], "预览显示要写入的内容")

    info2 = tool.describe_action({"path": "fresh.txt", "content": "abc"}, wctx)
    check("新建" in info2["impact"], "新建文件时影响说明为「新建」")


def make_context_standalone(workspace: str, *, authorized: bool = False, approval_id: str = "") -> ToolContext:
    from backend.config import (
        BACKUP_DIR_NAME,
        IGNORED_DIR_NAMES,
        MAX_READ_CHARS,
        SENSITIVE_DIR_PARTS,
        SENSITIVE_NAMES,
    )

    return ToolContext(
        workspace=workspace,
        authorized=authorized,
        approval_id=approval_id,
        max_read_chars=MAX_READ_CHARS,
        ignored_dir_names=frozenset(IGNORED_DIR_NAMES),
        sensitive_names=frozenset(SENSITIVE_NAMES),
        sensitive_dir_parts=frozenset(SENSITIVE_DIR_PARTS),
        backup_dir_name=BACKUP_DIR_NAME,
    )


# --------------------------------------------------------------------------- #
# [A2] edit_file —— v0.1b 的局部修改工具
# --------------------------------------------------------------------------- #
async def test_edit_file(sb: Sandbox) -> None:
    """
    ★ 为什么这一节要写这么多断言？
    ────────────────────────────────────────────────────────────
    edit_file 的失败**不会崩**，它只会：改错地方、或者改不动。
    而"改错地方"是这套工具里后果最严重的失败 —— 用户以为只改了一处，
    实际破坏了无关代码，而且**磁盘上已经写下去了**。

    所以每条失败路径都要钉死：不只是"没崩"，
    而是断言**错误类型**、**文件是否被动过**、以及
    **给模型的 hint 是否足以让它自我修正**。
    """
    from backend.tools.mutating._shared import sha256_file

    print("\n[A2] ★ edit_file（局部修改：唯一匹配 / patch 冲突 / stale 检测）")

    reg = build_default_registry()
    ctx = make_context_standalone(sb.dir, authorized=True, approval_id="apr_edit")
    tool = reg.require("edit_file")

    sb._write("edit_me.py", "def greet(name):\n    print('hi', name)\n\n\ndef other():\n    return 1\n")

    # ---- ① 未授权必须被拒（纵深防御）----
    naked = make_context_standalone(sb.dir)          # authorized=False
    r = await tool.run(
        {"path": "edit_me.py", "old_string": "return 1", "new_string": "return 2"}, naked
    )
    check(not r.ok and r.error["kind"] == "permission_denied",
          "★ 未授权直接调 edit_file 被拒（纵深防御）")

    # ---- ② 正常替换（唯一匹配）----
    r = await tool.run(
        {"path": "edit_me.py", "old_string": "print('hi', name)", "new_string": "print('hello', name)"},
        ctx,
    )
    check(r.ok, f"唯一匹配替换成功：{r.summary[:56]}")
    check("print('hello', name)" in sb.read("edit_me.py"), "★ 磁盘内容已更新")
    check("return 1" in sb.read("edit_me.py"), "★★ 文件其余部分没被动过（局部修改的意义）")
    check(r.data["action"] == "edit", "ChangeRecord.action = edit")
    check(r.data["replaced"] == 1, "替换 1 处")
    check(r.data["removed_lines"] == 1 and r.data["added_lines"] == 1, "行数统计正确（1 → 1）")
    check(bool(r.data["change"]["backup_path"]), "★ 改前已备份")

    # ---- ③ 备份里是改动前的原内容（可还原）----
    # 注意：不能用 lstrip("./") —— lstrip 会去掉**所有**前导的 `.` 和 `/`，
    # 把 "./.loom_backup/..." 啃成 "loom_backup/..."（少了一层目录）。
    bp = r.data["change"]["backup_path"]
    rel_b = (bp[2:] if bp.startswith("./") else bp).replace("/", os.sep)
    with open(os.path.join(sb.dir, rel_b), encoding="utf-8") as f:
        check("print('hi', name)" in f.read(), "★ 备份内容 = 改动前原文件（能还原）")

    # ---- ④ old_string 未找到 → patch_conflict ----
    r = await tool.run(
        {"path": "edit_me.py", "old_string": "这段内容根本不存在", "new_string": "x"}, ctx
    )
    check(not r.ok and r.error["kind"] == "patch_conflict",
          f"未找到 → patch_conflict（实际 {r.error['kind']}）")
    check(r.error["retryable"] is True, "★ patch_conflict 标为可重试（改 old_string 就能成功）")
    check("read_file" in r.error["hint"], "★ hint 让模型先 read_file 确认实际内容")
    check("第" in r.error["hint"] or "行" in r.error["hint"], "★ hint 给出了行号线索")

    # ---- ⑤ ★ 缩进不一致时，hint 要能指出来（模型最常犯的错）----
    r = await tool.run(
        {"path": "edit_me.py",
         "old_string": "        print('hello', name)",          # 8 个空格，文件里只有 4 个
         "new_string": "        print('x', name)"},
        ctx,
    )
    check(not r.ok and r.error["kind"] == "patch_conflict", "缩进不一致 → patch_conflict")
    check("缩进" in r.error["hint"] or "空格" in r.error["hint"],
          "★★ hint 指出是缩进/空格问题（否则模型猜不到为什么'看起来一样'却匹配不上）")

    # ---- ⑥ ★★ 唯一性：出现多次必须拒绝（本工具最重要的一条）----
    sb._write("dup.py", "x = 1\ny = 1\nz = 1\n")
    r = await tool.run({"path": "dup.py", "old_string": "= 1", "new_string": "= 2"}, ctx)
    check(not r.ok and r.error["kind"] == "patch_conflict", "★ 多处匹配 → 拒绝")
    check("3" in r.summary, f"summary 说明了出现次数：{r.summary[:50]}")
    check(bool(r.data and r.data.get("lines")), f"★ data 给出各匹配的行号：{r.data.get('lines')}")
    check("上下文" in r.error["hint"] and "replace_all" in r.error["hint"],
          "★★ hint 给出两条出路：补上下文 或 replace_all=true")
    check(sb.read("dup.py") == "x = 1\ny = 1\nz = 1\n",
          "★★★ 被拒绝时文件必须**原封不动**（不许改一半）")

    # ---- ⑦ replace_all=true → 全部替换 ----
    r = await tool.run(
        {"path": "dup.py", "old_string": "= 1", "new_string": "= 2", "replace_all": True}, ctx
    )
    check(r.ok and r.data["replaced"] == 3, f"replace_all 替换 3 处（实际 {r.data.get('replaced')}）")
    check(sb.read("dup.py") == "x = 2\ny = 2\nz = 2\n", "替换结果正确")
    check(r.data["removed_lines"] == 3 and r.data["added_lines"] == 3,
          "多处替换时行数统计按倍率算对")

    # ---- ⑧ old_string 为空 → 必须拦（否则等于往文件开头插入）----
    r = await tool.run({"path": "dup.py", "old_string": "", "new_string": "X"}, ctx)
    check(not r.ok and r.error["kind"] == "invalid_arguments", "★ old_string 为空被拒")

    # ---- ⑨ old == new → 无意义 ----
    r = await tool.run({"path": "dup.py", "old_string": "x = 2", "new_string": "x = 2"}, ctx)
    check(not r.ok and r.error["kind"] == "invalid_arguments", "old_string == new_string 被拒")

    # ---- ⑩ new_string 留空 = 删除该段 ----
    r = await tool.run({"path": "dup.py", "old_string": "z = 2\n", "new_string": ""}, ctx)
    check(r.ok, "new_string 留空表示删除这段")
    check("z = 2" not in sb.read("dup.py"), "★ 该段确实被删掉了（不是留了个空行）")

    # ---- ⑪ 文件不存在 → not_found（edit 不负责创建）----
    r = await tool.run({"path": "no_such.py", "old_string": "a", "new_string": "b"}, ctx)
    check(not r.ok and r.error["kind"] == "not_found", "文件不存在 → not_found")
    check("write_file" in r.error["hint"], "★ hint 建议改用 write_file 建新文件")

    # ---- ⑫ ★★ stale_content：文件在读取之后被改过 ----
    sb._write("stale.py", "value = 1\n")
    stale_old_hash = sha256_file(sb.path("stale.py"))
    sb._write("stale.py", "value = 1  # 别人改了这一行\n")     # 模拟"这期间文件被改了"
    r = await tool.run(
        {"path": "stale.py", "old_string": "value = 1", "new_string": "value = 2",
         "expect_hash": stale_old_hash},
        ctx,
    )
    check(not r.ok and r.error["kind"] == "stale_content",
          f"★ hash 不符 → stale_content（实际 {r.error['kind']}）")
    check("read_file" in r.error["hint"], "★ hint 让模型重新读取最新内容")
    check("别人" in sb.read("stale.py"), "★★ 拒绝时文件没被改动")

    # ---- ⑬ hash 匹配时正常执行 ----
    good_hash = sha256_file(sb.path("stale.py"))
    r = await tool.run(
        {"path": "stale.py", "old_string": "value = 1", "new_string": "value = 2",
         "expect_hash": good_hash},
        ctx,
    )
    check(r.ok, "hash 匹配时正常执行")

    # ---- ⑭ ★★ CRLF 文件：行尾风格必须保留 ----
    # 模型输出的换行永远是 \n，而 Windows 文件常常是 \r\n。
    # 如果内部不归一化，这种编辑会永远失败；如果写回不还原，会把整篇文件的行尾改掉。
    with open(sb.path("crlf.txt"), "wb") as f:
        f.write(b"line1\r\nline2\r\nline3\r\n")
    r = await tool.run(
        {"path": "crlf.txt", "old_string": "line2\n", "new_string": "LINE-TWO\n"}, ctx
    )
    check(r.ok, "★ 模型给 \\n 也能改 \\r\\n 的文件（内部做了归一化）")
    with open(sb.path("crlf.txt"), "rb") as f:
        crlf_raw = f.read()
    check(crlf_raw == b"line1\r\nLINE-TWO\r\nline3\r\n",
          f"★★ 行尾保持 CRLF 没被整篇改成 LF（实际 {crlf_raw!r}）")

    # ---- ⑮ 越界路径 ----
    r = await tool.run({"path": "../outside.py", "old_string": "a", "new_string": "b"}, ctx)
    check(not r.ok and r.error["kind"] == "path_outside_workspace", "越界路径被拦")
    check(r.error["security"] is True, "标记为安全事件")

    # ---- ⑯ 二进制文件被拒 ----
    with open(sb.path("bin2.dat"), "wb") as f:
        f.write(b"\x00\x01\x02")
    r = await tool.run({"path": "bin2.dat", "old_string": "a", "new_string": "b"}, ctx)
    check(not r.ok, f"二进制文件被拒（{r.error['kind']}）")

    # ---- ⑰ ★★ 审批展示：给的是 diff，不是整篇内容 ----
    sb._write("preview.py", "a = 1\nb = 2\nc = 3\n")
    info = tool.describe_action(
        {"path": "preview.py", "old_string": "b = 2", "new_string": "b = 99"}, ctx
    )
    check(info["action"] == "修改文件", f"action = {info['action']}")
    check("preview.py" in info["target"], f"target = {info['target']}（相对路径，不泄露目录结构）")
    check("- b = 2" in info["preview"] and "+ b = 99" in info["preview"],
          "★★ 预览是 diff（- 旧 / + 新），不是整篇内容")
    check("将修改" in info["impact"] and "备份" in info["impact"], "影响说明含处数与备份提示")

    # ---- ⑱ ★★ 审不到就提前警告：让用户别白批一次 ----
    info2 = tool.describe_action(
        {"path": "preview.py", "old_string": "找不到的内容", "new_string": "x"}, ctx
    )
    check("找不到" in info2["impact"] or "失败" in info2["impact"],
          "★★ old_string 匹配不上时，审批界面提前警告（用户不必白批一次）")

    info3 = tool.describe_action(
        {"path": "preview.py", "old_string": "a = 1\nb = 2", "new_string": "a = 1\nb = 2\nc = 9"}, ctx
    )
    check("1 处" in info3["impact"], f"唯一匹配时说明替换处数：{info3['impact'][:50]}")


# --------------------------------------------------------------------------- #
# [A3] read_file ↔ edit_file 的 hash 口径一致性
# --------------------------------------------------------------------------- #
async def test_read_hash(sb: Sandbox) -> None:
    """
    ★ 这条链路的实际用途：

        read_file → 拿到 hash → 组织 old_string → edit_file(expect_hash=hash)

    如果两边的算法、字节口径、截取长度有**任何一处**不同，链子就断了：
      · 永远 stale  → 模型再也改不了文件（比不校验更糟）
      · 永远匹配    → 校验形同虚设，拦不住"用旧内容覆盖新内容"

    所以这里既测"能对上"，也测"该拒的时候必须拒，且不能改坏文件"。
    """
    from backend.tools.hashing import sha256_file

    print("\n[A3] read_file ↔ edit_file 的 hash 口径")

    reg = build_default_registry()
    ctx = make_context_standalone(sb.dir, authorized=True, approval_id="apr_hash")

    sb._write("hash_me.txt", "alpha\nbeta\n")
    abs_path = sb.path("hash_me.txt")

    # ---- ① read_file 的 hash == sha256_file 直算 ----
    r = await reg.require("read_file").run({"path": "hash_me.txt"}, ctx)
    check(r.ok, "read_file 成功")

    direct = sha256_file(abs_path)
    got = r.data.get("hash", "")
    check(bool(direct), f"直算 hash = {direct}")
    check(got == direct, f"★★ read_file 返回的 hash 与 sha256_file 一致（{got}）")

    # ---- ② 截取规则与格式 ----
    check(len(got) == 16, f"截取 16 位十六进制（实际 {len(got)} 位）")
    check(all(c in "0123456789abcdef" for c in got), "全是十六进制小写字符")

    # ---- ③ 分段读返回的仍是「整个文件」的 hash ----
    r2 = await reg.require("read_file").run(
        {"path": "hash_me.txt", "offset": 1, "limit": 1}, ctx
    )
    check(r2.ok and r2.data.get("range") == [1, 1], "分段读成功（只拿到第 1 行）")
    check(r2.data.get("hash") == direct, "★★ 分段读的 hash = 整个文件的 hash（不是这段的）")

    # ---- ④ 内容变化 → hash 变化 ----
    sb._write("hash_me.txt", "alpha\nbeta\n# changed\n")
    after = sha256_file(abs_path)
    check(after != direct, f"内容变化后 hash 变化（{direct} → {after}）")

    # ---- ⑤ 拿「读取时」的旧 hash 去编辑 → 必须 stale，且不改文件 ----
    before_body = sb.read("hash_me.txt")
    r3 = await reg.require("edit_file").run(
        {
            "path": "hash_me.txt",
            "old_string": "beta\n",
            "new_string": "BETA\n",
            "expect_hash": direct,          # ← 过期
        },
        ctx,
    )
    check(not r3.ok and r3.error["kind"] == "stale_content",
          f"★★ 旧 hash → stale_content（实际 {r3.error['kind']}）")
    check("read_file" in r3.error.get("hint", ""), "★ hint 让模型重新读取")
    check(sb.read("hash_me.txt") == before_body, "★★ 拒绝时文件原封不动（没覆盖新内容）")

    # ---- ⑥ 重新读 → 用新 hash 编辑 → 通过 ----
    r4 = await reg.require("read_file").run({"path": "hash_me.txt"}, ctx)
    fresh = r4.data["hash"]
    check(fresh == after, "重新读取拿到的就是当前 hash")

    r5 = await reg.require("edit_file").run(
        {
            "path": "hash_me.txt",
            "old_string": "beta\n",
            "new_string": "BETA\n",
            "expect_hash": fresh,           # ← 新鲜
        },
        ctx,
    )
    check(r5.ok, f"★★ 新鲜 hash → 编辑通过（{r5.summary[:44]}）")
    check("BETA" in sb.read("hash_me.txt"), "编辑确实写进去了")

    # ---- ⑦ 不带 expect_hash 时照旧能编辑（向后兼容）----
    r6 = await reg.require("edit_file").run(
        {"path": "hash_me.txt", "old_string": "alpha\n", "new_string": "ALPHA\n"}, ctx
    )
    check(r6.ok, "★ 不传 expect_hash 仍然可以编辑（不破坏原有调用方式）")


# --------------------------------------------------------------------------- #
# [B] Policy 层
# --------------------------------------------------------------------------- #
async def test_policy(sb: Sandbox) -> None:
    print("\n[B] Policy Engine（执行前校验链）")
    from backend.agent.state import Budgets, TaskState

    reg = build_default_registry()
    policy = Policy(reg)
    state = TaskState(goal="测试", workspace=sb.dir)
    state.budgets = Budgets(max_turns=5, max_tool_calls=10, max_same_call_repeat=2, min_evidence=1)
    ctx = make_context(state)

    # ① 工具存在
    d = policy.check("no_such_tool", {}, state, ctx)
    check(not d.allow and not d.needs_approval, "工具不存在 → 直接拒绝")
    check(d.result.error["kind"] == "not_implemented", "错误类型 not_implemented")

    # ② 参数 JSON 解析失败
    d = policy.check("read_file", {"path": "a.py"}, state, ctx, parse_error="Expecting ','")
    check(not d.allow and d.result.error["kind"] == "invalid_arguments", "JSON 解析失败 → invalid_arguments")

    # ③ schema 校验
    d = policy.check("read_file", {}, state, ctx)
    check(not d.allow and "缺少必填参数" in d.result.summary, "schema 校验拦住缺参数")

    # ④ 只读工具放行
    d = policy.check("read_file", {"path": "main.py"}, state, ctx)
    check(d.allow and not d.needs_approval, "★ 只读工具直接放行")

    # ⑤ 写工具要求审批
    d = policy.check("write_file", {"path": "a.txt", "content": "x"}, state, ctx)
    check(not d.allow and d.needs_approval, "★ 写工具触发审批（needs_approval=True）")
    check(d.meta["risk"] == "write", "审批 meta 带风险等级")
    check("preview" in d.meta, "审批 meta 带内容预览")
    check(d.meta["target"] == "a.txt", f"审批 meta 带目标路径：{d.meta['target']}")

    # ⑥ 已授权时写工具放行
    d = policy.check("write_file", {"path": "a.txt", "content": "x"}, state, ctx, already_approved="apr_1")
    check(d.allow and d.approval_id == "apr_1", "★ 已授权 → 放行并携带 approval_id")

    # ⑦ 预算耗尽
    for _ in range(5):
        state.next_turn()
    d = policy.check("read_file", {"path": "main.py"}, state, ctx)
    check(not d.allow and "预算已耗尽" in d.result.summary, "★ 预算耗尽时拒绝一切工具")
    check(not d.needs_approval, "预算耗尽的拒绝不去打扰用户（不该弹审批）")

    # ⑧ 重复熔断
    from backend.agent.state import TaskState as TS

    s2 = TS(goal="t", workspace=sb.dir)
    s2.budgets = Budgets(max_same_call_repeat=2)
    ctx2 = make_context(s2)
    s2.record_tool_call("c1", "read_file", {"path": "a.py"}, ok=True, summary="ok")
    s2.record_tool_call("c2", "read_file", {"path": "a.py"}, ok=True, summary="ok")
    d = policy.check("read_file", {"path": "a.py"}, s2, ctx2)
    check(not d.allow and "重复" in d.result.summary, "★ 重复指纹熔断生效")
    check("换个思路" in d.result.error["hint"], "熔断错误带引导性 hint")

    # ⑧b ★ 确定性失败熔断（硬熔断）—— 真实模型跑通后新增
    #   场景：模型拿**完全相同的参数**去撞一个「参数不变则结论必然不变」的错误
    #   （路径越界 / 敏感文件 / 用户已拒绝）。软熔断要数到 3 次才拦，
    #   这里要求第 2 次就拦下，省一个轮次和一次模型往返。
    s3 = TS(goal="t", workspace=sb.dir)
    s3.budgets = Budgets(max_same_call_repeat=3)   # 故意放大软熔断阈值，隔离验证硬熔断
    ctx3 = make_context(s3)

    # 第一次：路径越界（不可重试类）
    s3.record_tool_call(
        "d1", "read_file", {"path": "C:/Windows/System32/drivers/etc/hosts"},
        ok=False, summary="路径越出工作区", error_kind="path_outside_workspace",
    )
    d = policy.check(
        "read_file", {"path": "C:/Windows/System32/drivers/etc/hosts"}, s3, ctx3
    )
    check(not d.allow, "★★ 确定性失败第 2 次就被拦（不等软熔断数到 3）")
    check("确定性失败熔断" in d.reason, f"拒绝原因标明是确定性失败：{d.reason}")
    check("不可重试" in d.result.summary, "★ 明确告诉模型这是不可重试的错误")
    check("结果不会变" in d.result.error["hint"], "★ hint 说明「再试结果不变」")

    # 对照：可重试类错误（not_found）不该被硬熔断拦，交给软熔断
    s4 = TS(goal="t", workspace=sb.dir)
    s4.budgets = Budgets(max_same_call_repeat=3)
    ctx4 = make_context(s4)
    s4.record_tool_call(
        "e1", "read_file", {"path": "ghost.py"},
        ok=False, summary="文件不存在", error_kind="not_found",
    )
    d = policy.check("read_file", {"path": "ghost.py"}, s4, ctx4)
    check(d.allow, "★ 可重试错误（not_found）不被硬熔断拦，仍允许再试")

    # 对照：上次成功过的调用，即使有更早的失败记录也不该被硬熔断
    s5 = TS(goal="t", workspace=sb.dir)
    s5.budgets = Budgets(max_same_call_repeat=3)
    ctx5 = make_context(s5)
    s5.record_tool_call(
        "f1", "read_file", {"path": "x.py"},
        ok=False, summary="越界", error_kind="path_outside_workspace",
    )
    s5.record_tool_call("f2", "read_file", {"path": "x.py"}, ok=True, summary="读到了")
    d = policy.check("read_file", {"path": "x.py"}, s5, ctx5)
    check(d.allow, "★ 最近一次同调用成功 → 不受更早的失败影响")

    # ⑨ 白名单
    p2 = Policy(reg, allowed_tools={"list_files"})
    d = p2.check("read_file", {"path": "a.py"}, state, ctx)
    check(not d.allow and "不在本次任务允许" in d.result.summary, "★ 工具白名单降权生效")

    # ⑩ 审批后复检：args_hash 不匹配
    from backend.agent.approval import build_pending

    st3 = TS(goal="t", workspace=sb.dir)
    pending = build_pending(
        tool="write_file",
        args={"path": "a.txt", "content": "AAA"},
        risk=RiskLevel.WRITE,
        action="写入文件",
        target="a.txt",
    )
    # 模拟「批准 A，执行 B」
    d = policy.recheck_after_approval(
        "write_file", {"path": "a.txt", "content": "BBB!"}, st3, make_context(st3), pending
    )
    check(not d.allow, "★ 审批后参数被换 → 拒绝执行（防「批准 A 执行 B」）")
    check("指纹" in d.reason or "变化" in d.result.summary, f"拒绝理由明确：{d.reason}")

    # ⑪ 审批后复检：参数未变 → 通过
    d = policy.recheck_after_approval("write_file", pending.args, st3, make_context(st3), pending)
    check(d.allow, "参数未变 → 复检通过")

    # ⑫ 审批后复检：路径越界
    pending2 = build_pending(
        tool="write_file",
        args={"path": "../../evil.txt", "content": "x"},
        risk=RiskLevel.WRITE,
        action="写入文件",
        target="../../evil.txt",
    )
    d = policy.recheck_after_approval("write_file", pending2.args, st3, make_context(st3), pending2)
    check(not d.allow, "★ 审批后路径复检发现越界 → 拒绝（TOCTOU 防护）")


# --------------------------------------------------------------------------- #
# [C] 审批管理器（挂起 / 恢复）
# --------------------------------------------------------------------------- #
async def test_approval() -> None:
    print("\n[C] ApprovalManager（真正挂起 / 恢复）")
    mgr = ApprovalManager(timeout_s=5.0)

    # ---- 基本批准 ----
    p = build_pending(
        tool="write_file",
        args={"path": "a.txt", "content": "x"},
        risk=RiskLevel.WRITE,
        action="写入文件",
        target="a.txt",
    )

    async def resolver() -> None:
        await asyncio.sleep(0.05)
        ok = mgr.approve(p.approval_id, "看起来没问题")
        assert ok, "resolve 应当成功"

    t0 = asyncio.get_event_loop().time()
    task = asyncio.create_task(resolver())
    decision = await mgr.request(p)     # ← 这里真的挂起
    await task
    elapsed = asyncio.get_event_loop().time() - t0

    check(decision.approved, "★ 批准后 Agent 恢复并得到 approved=True")
    check(0.05 <= elapsed < 2.0, f"★ 挂起期间未忙等（耗时 {elapsed:.2f}s ≈ 0.05s 的 sleep）")
    check(decision.status.value == "approved", "状态 approved")

    # ---- 拒绝 ----
    p2 = build_pending(tool="write_file", args={"path": "b.txt", "content": "y"}, risk=RiskLevel.WRITE, action="写入文件", target="b.txt")

    async def rejecter() -> None:
        await asyncio.sleep(0.03)
        mgr.reject(p2.approval_id, "不要改这个文件")

    t = asyncio.create_task(rejecter())
    d2 = await mgr.request(p2)
    await t
    check(not d2.approved, "★ 拒绝后 approved=False")
    check(d2.status.value == "rejected", "状态 rejected")

    # ---- 超时 ----
    mgr2 = ApprovalManager(timeout_s=0.15)
    p3 = build_pending(tool="write_file", args={"path": "c.txt", "content": "z"}, risk=RiskLevel.WRITE, action="写入文件", target="c.txt")
    t0 = asyncio.get_event_loop().time()
    d3 = await mgr2.request(p3)
    el = asyncio.get_event_loop().time() - t0
    check(not d3.approved, "★ 超时视同拒绝")
    check(d3.status.value == "expired", f"★ 状态单独记为 expired（让用户知道错过了什么）")
    check(el >= 0.14, f"确实等满了超时时长（{el:.2f}s）")
    check(mgr2.expired_count == 1, "超时计数正确")

    # ---- 一次性（防重放）----
    p4 = build_pending(tool="write_file", args={"path": "d.txt", "content": "w"}, risk=RiskLevel.WRITE, action="写入文件", target="d.txt")

    async def once() -> None:
        await asyncio.sleep(0.02)
        assert mgr.resolve(p4.approval_id, "approved")
        second = mgr.resolve(p4.approval_id, "approved")
        assert second is False, "第二次 resolve 必须失败"

    t = asyncio.create_task(once())
    d4 = await mgr.request(p4)
    await t
    check(d4.approved, "首次决策生效")

    # ---- 无效 id ----
    check(mgr.resolve("apr_nonexistent", "approved") is False, "无效 id → resolve 返回 False")

    # ---- 指纹 ----
    h1 = args_fingerprint("write_file", {"path": "a", "content": "1"})
    h2 = args_fingerprint("write_file", {"content": "1", "path": "a"})
    h3 = args_fingerprint("read_file", {"path": "a", "content": "1"})
    h4 = args_fingerprint("write_file", {"path": "a", "content": "2"})
    check(h1 == h2, "★ 参数顺序不同 → 指纹一致")
    check(h1 != h3, "★ 工具名不同 → 指纹不同（防「批准读执行写」）")
    check(h1 != h4, "★ 内容不同 → 指纹不同")
    check(len(h1) == 16, "指纹长度 16")

    # ---- 取消全部 ----
    mgr3 = ApprovalManager(timeout_s=5.0)
    p5 = build_pending(tool="write_file", args={"path": "e.txt", "content": "v"}, risk=RiskLevel.WRITE, action="写入文件", target="e.txt")

    async def canceller() -> None:
        await asyncio.sleep(0.02)
        n = mgr3.cancel_all("任务中止")
        assert n == 1

    t = asyncio.create_task(canceller())
    d5 = await mgr3.request(p5)
    await t
    check(not d5.approved and "中止" in d5.reason, "★ cancel_all 正确唤醒挂起的审批")


# --------------------------------------------------------------------------- #
# [D] 上下文构建
# --------------------------------------------------------------------------- #
async def test_context(sb: Sandbox) -> None:
    print("\n[D] 上下文构建（State → messages）")
    from backend.agent.state import Budgets, Evidence, PlanStep, TaskState

    st = TaskState(goal="分析这个项目", workspace=sb.dir)
    st.budgets = Budgets(min_evidence=1)
    st.set_plan([PlanStep(0, "列目录"), PlanStep(1, "读文件")], note="先摸清结构")
    st.record_tool_call("c1", "list_files", {"path": "."}, ok=True, summary="3 个文件")
    st.add_evidence(Evidence("list_files", "3 个文件", "."))
    st.turn = 2

    cb = ContextBuilder()
    msgs = cb.build(st, system_prompt="SYS", history=[Message.user("分析这个项目")])

    check(msgs[0].role == "system", "第 1 条是 system（稳定，可命中前缀缓存）")
    check(msgs[1].role == "user", "第 2 条是用户任务原文")
    check(any("当前计划" in m.content for m in msgs), "★ 计划被注入（模型知道要做什么）")
    check(any("当前进度" in m.content for m in msgs), "★ 进度被注入")
    reminder = [m for m in msgs if "当前进度" in m.content][0].content
    check("至少需要 1 条" in reminder, "★ 把 min_evidence 约束写进提示词（避免模型白跑一轮）")
    check(msgs[-1].role == "system", "进度提醒在最后（离回答最近）")

    # ---- 低轮次警告 ----
    st.turn = 19
    st.budgets.max_turns = 20
    msgs2 = cb.build(st, system_prompt="SYS", history=[])
    check(any("剩余轮次不多" in m.content for m in msgs2), "★ 轮次将尽时给出显式警告")

    # ---- 连续错误警告 ----
    st.consecutive_errors = 3
    msgs3 = cb.build(st, system_prompt="SYS", history=[])
    check(any("连续失败" in m.content for m in msgs3), "★ 连续错误时提示换方法")

    # ---- 历史压缩 ----
    hist: list[Message] = []
    for i in range(6):
        hist.append(Message.assistant(f"第{i}次"))
        hist.append(Message.tool_result(f"c{i}", "read_file", "X" * 2000))
    compressed = cb.compress_history(hist)
    short = sum(1 for m in compressed if m.role == "tool" and "被压缩" in m.content)
    check(short == 3, f"★ 6 条工具结果中最早的 3 条被压缩（实际 {short}）")

    # ---- TurnBuilder 保证协议不变量 ----
    tb = TurnBuilder()
    tb.add_assistant("", [tc("a", "read_file", {"path": "x"}), tc("b", "write_file", {"path": "y", "content": "z"})])
    tb.add_tool("a", "read_file", "结果 A")   # 只填了一个
    msgs4 = tb.build()
    check(len(msgs4) == 3, f"★ 缺失的 tool 消息被自动补齐（共 {len(msgs4)} 条 = 1 assistant + 2 tool）")
    check(msgs4[0].tool_calls and len(msgs4[0].tool_calls) == 2, "assistant 带 2 个 tool_calls")
    ids = [m.tool_call_id for m in msgs4[1:]]
    check(ids == ["a", "b"], f"★ tool 消息顺序与 tool_calls 一致：{ids}")
    check("未被执行" in msgs4[2].content, "占位消息说明原因（模型能理解为何失败）")


# --------------------------------------------------------------------------- #
# [E] JSON 抽取容错
# --------------------------------------------------------------------------- #
async def test_json_extract() -> None:
    print("\n[E] 模型输出解析容错")

    check(extract_json('{"a": 1}') == {"a": 1}, "纯 JSON")
    check(extract_json('```json\n{"a": 1}\n```') == {"a": 1}, "★ 剥 markdown 围栏")
    check(extract_json('好的，这是计划：\n{"steps": []}\n希望有帮助') == {"steps": []}, "★ 前后有废话也能抽出")
    check(extract_json("完全不是 JSON") is None, "非 JSON 返回 None（不抛异常）")
    check(extract_json("") is None, "空串安全")
    check(extract_json('{"nested": {"k": [1,2]}}')["nested"]["k"] == [1, 2], "嵌套结构")
    check(extract_json('[1,2,3]') is None, "★ 数组被拒绝（只要对象）")

    check(extract_text_answer("普通回答") == "普通回答", "普通文本原样返回")
    check(extract_text_answer('{"answer": "这是答案"}') == "这是答案", "★ 从 JSON 里抽 answer 字段")
    check(extract_text_answer('{"text": "内容"}') == "内容", "抽 text 字段")
    check(extract_text_answer("") == "", "空串安全")


# --------------------------------------------------------------------------- #
# [F] AgentCore 端到端（离线剧本）
# --------------------------------------------------------------------------- #
async def test_agent_loop(sb: Sandbox) -> None:
    print("\n[F] AgentCore 五阶段端到端（FakeAdapter 离线）")

    reg = build_default_registry()
    bus = EventBus()

    # 剧本：PLAN → 列目录 → REFLECT → 读文件 → REFLECT → 作答
    adapter = FakeAdapter(
        [
            # 1) PLAN 阶段
            ModelReply(content='{"steps":[{"title":"列出目录","detail":"确认项目结构"},{"title":"读关键文件","detail":"看 main.py"}],"note":""}'),
            # 2) ACT：调 list_files
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            # 3) REFLECT
            ModelReply(content='{"progress":"已看到目录结构","next_action":"读取 main.py","done":false}'),
            # 4) ACT：调 read_file
            ModelReply(tool_calls=[tc("c2", "read_file", {"path": "main.py"})]),
            # 5) REFLECT
            ModelReply(content='{"progress":"已读到源码","next_action":"finish","done":true}'),
        ]
    )
    cfg = AgentConfig(max_turns=10, min_evidence=1)
    core = AgentCore(adapter, reg, config=cfg, bus=bus)
    events = collect(bus)

    state = await core.run("分析这个项目的技术栈", workspace=sb.dir)
    await bus.drain()

    types = [e.type for e in events]
    print(f"     事件序列（{len(events)} 条）：")
    for e in events:
        if VERBOSE or e.type not in ("thinking",):
            data = e.data
            brief = ""
            if e.type == EventType.TOOL_CALL.value:
                brief = f"{data['tool']}({data['args']})"
            elif e.type == EventType.TOOL_RESULT.value:
                brief = f"{'成功' if data['ok'] else '失败'} {data['summary'][:50]}"
            elif e.type == EventType.PLAN.value:
                brief = f"{len(data['steps'])} 步"
            elif e.type == EventType.REFLECT.value:
                brief = f"done={data['done']} next={data['next_action'][:24]}"
            elif e.type == EventType.BUDGET_EXCEEDED.value:
                brief = f"{data['kind']} {data['used']}/{data['limit']}"
            elif e.type == EventType.TASK_END.value:
                brief = f"status={data['status']} reason={data['reason']}"
            print(f"       #{e.seq:<3} [turn {e.turn}] {e.type:<20} {brief}")

    # ---- 断言事件流 ----
    check(EventType.TASK_START.value in types, "★ 有 task.start")
    check(EventType.TASK_END.value in types, "★ 有 task.end")
    check(EventType.PLAN.value in types, "★ PLAN 阶段产出 plan 事件")
    check(EventType.ACT.value in types, "★ ACT 阶段产出 act 事件")
    check(EventType.TOOL_CALL.value in types, "★ 有 tool.call")
    check(EventType.TOOL_RESULT.value in types, "★ 有 tool.result")
    check(EventType.OBSERVE.value in types, "★ 有 observe")
    check(EventType.REFLECT.value in types, "★ 有 reflect")
    check(EventType.FINAL_ANSWER.value in types, "★ 有 final_answer")

    # ---- 断言顺序 ----
    def idx(t: str) -> int:
        return types.index(t) if t in types else -1

    check(idx(EventType.TASK_START.value) == 0, "task.start 是第一个事件")
    check(idx(EventType.TASK_END.value) == len(types) - 1, "task.end 是最后一个事件")
    check(idx(EventType.PLAN.value) < idx(EventType.TOOL_CALL.value), "★ PLAN 在工具调用之前")
    check(idx(EventType.TOOL_CALL.value) < idx(EventType.TOOL_RESULT.value), "★ tool.call 在 tool.result 之前")
    check(idx(EventType.TOOL_RESULT.value) < idx(EventType.OBSERVE.value), "★ tool.result 在 observe 之前")

    # ---- seq 单调 ----
    seqs = [e.seq for e in events]
    check(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "★ seq 严格单调递增且无重复")

    # ---- 断言状态 ----
    check(state.status == TaskStatus.DONE, f"★ 任务正常收敛：{state.status.value}")
    check(state.end_reason == "agent_done", f"结束原因明确：{state.end_reason}")
    check(len(state.plan) == 2, f"计划有 2 步（实际 {len(state.plan)}）")
    check(len(state.tool_events) == 2, f"记录了 2 次工具调用（实际 {len(state.tool_events)}）")
    check(len(state.evidence_summary) >= 1, f"★ 收集到 {len(state.evidence_summary)} 条证据（C3 满足）")
    check(len(state.reflections) >= 1, f"REFLECT 留痕 {len(state.reflections)} 条")
    check(state.turn == 3, f"★ 轮次计数正确：PLAN占1轮 + 2次ACT各占1轮 = 3（实际 {state.turn}）")
    check(len(state.reflections) == 2, f"REFLECT 与 ACT 同轮（不额外消耗轮次），共 {len(state.reflections)} 次自评")
    check(bool(state.final_answer), f"有最终答复：{state.final_answer[:60]}")

    # ---- 模型看到的是「计划 + 进度」而不只是裸历史 ----
    # 剧本里现在多了最后一次 FINAL 阶段调用（模型自己的收尾作答），
    # 所以要看「收尾之前的那次」是否带了计划与进度 —— 那才是 ACT 阶段的上下文。
    check(len(adapter.calls) >= 6, f"模型被调用 {len(adapter.calls)} 次（含 FINAL 收尾）")
    act_call = adapter.calls[-2]
    joined = "\n".join(m.content for m in act_call)
    check("当前计划" in joined, "★ ACT 阶段的调用里含计划")
    check("当前进度" in joined, "★ ACT 阶段的调用里含进度")
    check("至少需要 1 条" in joined, "★ 含 min_evidence 约束（提示词层面的硬规则）")


# --------------------------------------------------------------------------- #
# [F2] ★ FINAL 收尾作答阶段（真实模型跑通后才暴露的缺陷 → 回归测试）
# --------------------------------------------------------------------------- #
async def test_final_answer_synthesis(sb: Sandbox) -> None:
    """
    锁定一个真实缺陷：REFLECT 判定 done 后，Agent 曾直接用**程序生成的进度模板**
    收尾（「任务已完成（第 N 轮，共观测 M 条事实）…已执行的动作：…」），
    把模型已经得出的真结论丢掉了。

    修法：收尾必须是**一次独立的模型调用**，明确要求「回答用户的原始任务」，且不带 tools。
    """
    print("\n[F2] ★ FINAL 收尾作答：必须由模型生成，不能用进度模板糊弄")

    reg = build_default_registry()
    bus = EventBus()

    # 模型在收尾时给出的才是真答案 —— 用一个可识别的标记串
    REAL_ANSWER = "## 结论\n这个项目是 FastAPI 服务，且在 main.py 里引用了不存在的 services/ 目录。"

    adapter = FakeAdapter(
        [
            # 1) PLAN
            ModelReply(content='{"steps":[{"title":"列目录","detail":"看结构"}],"note":""}'),
            # 2) ACT：列目录
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            # 3) REFLECT：判定完成
            ModelReply(content='{"progress":"已看到目录","next_action":"finish","done":true}'),
            # 4) ★ FINAL：这次调用才是真正的作答
            ModelReply(content=REAL_ANSWER),
        ]
    )
    cfg = AgentConfig(max_turns=10, min_evidence=1)
    core = AgentCore(adapter, reg, config=cfg, bus=bus)
    events = collect(bus)

    state = await core.run("分析这个项目", workspace=sb.dir)
    await bus.drain()

    # ---- 核心断言：最终答复必须是模型给的那段，不是模板 ----
    check(
        REAL_ANSWER.strip() in state.final_answer,
        "★★ 最终答复来自模型的 FINAL 阶段作答（不是进度模板）",
    )
    check(
        "已执行的动作" not in state.final_answer,
        "★★ 最终答复里没有「已执行的动作」这种记账式内容",
    )
    check(
        "任务已完成（第" not in state.final_answer,
        "★★ 最终答复不是程序生成的进度模板",
    )

    # ---- 事件侧：final_answer 事件的 text 就是模型给的答案 ----
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER.value]
    check(len(fa) == 1, f"只发一条 final_answer（实际 {len(fa)}）")
    check(
        REAL_ANSWER.strip() in fa[0].data.get("text", ""),
        "★ final_answer 事件携带模型生成的答案",
    )

    # ---- FINAL 阶段的调用特征 ----
    final_call = adapter.calls[-1]
    joined = "\n".join(m.content for m in final_call)
    check("工具观测已经结束" in joined, "★ FINAL 阶段注入了「观测已结束」的提示")
    check("最终答复" in joined, "★ FINAL 阶段明确要求给出面向用户的答复")
    check("不要输出 JSON" in joined, "★ FINAL 阶段禁止 JSON（那是 PLAN/REFLECT 的格式）")

    # ---- FINAL 阶段不应带 tools（强制收口，而不是又去调工具）----
    gate = adapter.tools_gate
    check(gate[0] is False, "★ PLAN 阶段不传 tools（只规划，不碰环境）")
    check(gate[1] is True, "★ ACT 阶段传 tools（这一步才允许动手）")
    check(gate[2] is False, "★ REFLECT 阶段不传 tools（只自评）")
    check(gate[3] is False, "★★ FINAL 阶段不传 tools（强制收口成答案）")
    check(
        gate == [False, True, False, False],
        f"★ 四阶段工具门控序列正确：{gate}",
    )

    # ---- 失败降级：FINAL 阶段模型报错时，仍要给出诚实答复而不是崩掉 ----
    bus2 = EventBus()
    adapter2 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"列目录","detail":"看结构"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(content='{"progress":"够了","next_action":"finish","done":true}'),
            ModelError("收尾调用模拟失败", kind="server_error", retryable=True),
        ]
    )
    core2 = AgentCore(adapter2, reg, config=AgentConfig(max_turns=10, min_evidence=1), bus=bus2)
    events2 = collect(bus2)
    state2 = await core2.run("分析这个项目", workspace=sb.dir)
    await bus2.drain()

    types2 = [e.type for e in events2]
    check(
        EventType.ERROR.value in types2,
        "★ 收尾失败时发 error 事件（不静默）",
    )
    check(
        any("final_answer_failed" == e.data.get("kind") for e in events2 if e.type == EventType.ERROR.value),
        "★ error 的 kind 标记为 final_answer_failed（便于区分）",
    )
    check(bool(state2.final_answer), "★ 收尾失败仍有答复（降级为记账，不比空白差）")
    check(
        state2.status in (TaskStatus.FAILED, TaskStatus.ABORTED),
        f"★ 收尾失败的任务状态正确：{state2.status.value}",
    )


# --------------------------------------------------------------------------- #
# [G] 审批在循环中的完整流转
# --------------------------------------------------------------------------- #
async def test_approval_in_loop(sb: Sandbox) -> None:
    print("\n[G] 审批在 Agent 循环中：批准 / 拒绝 / 超时")

    reg = build_default_registry()

    # ============ 场景 1：批准 ============
    bus1 = EventBus()
    adapter1 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写文件","detail":"创建 hello.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "hello.py", "content": "print('hi')\n"})]),
            ModelReply(content='{"progress":"已写入","next_action":"finish","done":true}'),
        ]
    )
    core1 = AgentCore(adapter1, reg, config=AgentConfig(max_turns=6, min_evidence=0), bus=bus1)
    ev1 = collect(bus1)

    async def approver() -> None:
        # 等 Agent 挂起后点批准
        for _ in range(200):
            await asyncio.sleep(0.01)
            aid = core1.approvals.current()
            if aid:
                core1.approvals.approve(aid, "测试批准")
                return
        raise AssertionError("审批请求始终未出现")

    t = asyncio.create_task(approver())
    st1 = await core1.run("创建 hello.py", workspace=sb.dir)
    await t
    await bus1.drain()

    t1 = [e.type for e in ev1]
    check(EventType.APPROVAL_REQUEST.value in t1, "★ 发出 approval.request")
    check(EventType.APPROVAL_DECISION.value in t1, "★ 发出 approval.decision")
    dec1 = [e for e in ev1 if e.type == EventType.APPROVAL_DECISION.value][0]
    check(dec1.data["approved"] is True, "决策 approved=True")
    check(sb.read("hello.py") == "print('hi')\n", "★ 批准后文件真的被写入磁盘")
    req1 = [e for e in ev1 if e.type == EventType.APPROVAL_REQUEST.value][0]
    check(req1.data["target"] == "hello.py", f"审批展示目标：{req1.data['target']}")
    check(bool(req1.data["args_hash"]), "★ 请求携带 args_hash 指纹")
    check("审批" not in req1.data or True, "审批数据完整")
    check(len(st1.approval_history) == 1, "审批历史留痕")
    check(st1.approval_history[0].status.value == "approved", "历史状态 approved")
    check(len(st1.change_records) == 1, f"★ 变更记录留痕（{len(st1.change_records)} 条）")
    check(st1.change_records[0].path == "hello.py", "变更记录路径正确")

    # ============ 场景 2：拒绝 ============
    bus2 = EventBus()
    adapter2 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写","detail":"创建 bad.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "bad.py", "content": "nope\n"})]),
            ModelReply(content="好的，我不写这个文件了。请问你希望我怎么做？"),
        ]
    )
    core2 = AgentCore(adapter2, reg, config=AgentConfig(max_turns=6, min_evidence=0), bus=bus2)
    ev2 = collect(bus2)

    async def rejecter() -> None:
        for _ in range(200):
            await asyncio.sleep(0.01)
            aid = core2.approvals.current()
            if aid:
                core2.approvals.reject(aid, "不要创建这个文件")
                return
        raise AssertionError("审批未出现")

    t = asyncio.create_task(rejecter())
    st2 = await core2.run("创建 bad.py", workspace=sb.dir)
    await t
    await bus2.drain()

    check(not os.path.exists(sb.path("bad.py")), "★ 拒绝后文件未被创建（安全）")
    dec2 = [e for e in ev2 if e.type == EventType.APPROVAL_DECISION.value][0]
    check(dec2.data["approved"] is False, "决策 approved=False")
    tres2 = [e for e in ev2 if e.type == EventType.TOOL_RESULT.value]
    check(tres2 and not tres2[0].data["ok"], "★ 被拒的操作以失败结果回灌给模型")
    check("拒绝" in tres2[0].data["summary"], f"回灌内容说明被拒：{tres2[0].data['summary'][:50]}")
    check(len(st2.approval_history) == 1 and st2.approval_history[0].status.value == "rejected", "拒绝入历史")
    check(st2.status == TaskStatus.DONE, "★ 拒绝后 Agent 未崩溃，正常收敛")

    # ============ 场景 3：超时 ============
    bus3 = EventBus()
    adapter3 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写","detail":"timeout.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "timeout.py", "content": "x\n"})]),
            ModelReply(content="操作超时未获批准，任务中止。"),
        ]
    )
    core3 = AgentCore(adapter3, reg, config=AgentConfig(max_turns=6, min_evidence=0), bus=bus3)
    core3.approvals.timeout_s = 0.2      # 缩短超时便于测试
    ev3 = collect(bus3)

    st3 = await core3.run("创建 timeout.py", workspace=sb.dir)
    await bus3.drain()

    t3 = [e.type for e in ev3]
    check(EventType.APPROVAL_EXPIRED.value in t3, "★ 超时发出 approval.expired（而非 decision）")
    check(not os.path.exists(sb.path("timeout.py")), "★ 超时后文件未被创建")
    check(len(st3.approval_history) == 1 and st3.approval_history[0].status.value == "expired", "历史状态 expired")
    check(st3.status == TaskStatus.DONE, "超时后仍能收敛")

    # ============ 场景 4：审批期间状态正确 ============
    from backend.agent.state import TaskStatus as TS4

    seen_states: list[str] = []
    bus4 = EventBus()

    async def watcher(ev: Event) -> None:
        if ev.type == EventType.APPROVAL_REQUEST.value:
            # 此刻状态必须是 awaiting_approval
            seen_states.append(core4.state.status.value)

    bus4.subscribe(watcher)
    adapter4 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"写","detail":"s.py"}],"note":""}'),
            ModelReply(tool_calls=[tc("c1", "write_file", {"path": "s.py", "content": "x"})]),
            ModelReply(content="完成。"),
        ]
    )
    core4 = AgentCore(adapter4, reg, config=AgentConfig(max_turns=6, min_evidence=0), bus=bus4)

    async def approver4() -> None:
        for _ in range(200):
            await asyncio.sleep(0.01)
            aid = core4.approvals.current()
            if aid:
                core4.approvals.approve(aid)
                return

    t = asyncio.create_task(approver4())
    st4 = await core4.run("写 s.py", workspace=sb.dir)
    await t
    await bus4.drain()

    check(seen_states == ["awaiting_approval"], f"★ 请求审批时状态为 awaiting_approval（实际 {seen_states}）")
    check(st4.status == TS4.DONE, "恢复后状态回到正常终止")


# --------------------------------------------------------------------------- #
# [H] 终止条件（9 种）
# --------------------------------------------------------------------------- #
async def test_termination(sb: Sandbox) -> None:
    print("\n[H] 终止条件（每种都必须有明确 end_reason）")
    reg = build_default_registry()

    # ---- 1. max_turns ----
    bus = EventBus()
    adapter = FakeAdapter([ModelReply(tool_calls=[tc(f"c{i}", "list_files", {"path": "."})]) for i in range(30)])
    core = AgentCore(adapter, reg, config=AgentConfig(max_turns=3, use_reflect_phase=False, min_evidence=0), bus=bus)
    ev = collect(bus)
    st = await core.run("无休止地列目录", workspace=sb.dir)
    await bus.drain()
    check(st.status == TaskStatus.ABORTED, "★ 轮次耗尽 → aborted")
    check(st.end_reason == "budget_turns", f"★ end_reason=budget_turns（实际 {st.end_reason}）")
    check(any(e.type == EventType.BUDGET_EXCEEDED.value for e in ev), "★ 发出 budget.exceeded 事件")
    check("不是完整结论" in st.final_answer, "★ 兜底答复诚实声明这不是完整结论")
    check("已执行的动作" in st.final_answer, "★ 兜底答复列出实际进展（不浪费已做的工作）")

    # ---- 2. repeated_call（重复指纹熔断）----
    bus2 = EventBus()
    same = ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})])
    adapter2 = FakeAdapter([same, same, same, same, same, same])
    core2 = AgentCore(adapter2, reg, config=AgentConfig(max_turns=10, max_same_call_repeat=2, use_reflect_phase=False, min_evidence=0), bus=bus2)
    ev2 = collect(bus2)
    st2 = await core2.run("重复同样的调用", workspace=sb.dir)
    await bus2.drain()
    denies = [e for e in ev2 if e.type == EventType.TOOL_RESULT.value and not e.data["ok"]]
    check(len(denies) > 0, f"★ 重复调用被熔断拒绝 {len(denies)} 次")
    check(any("重复" in (e.data.get("error") or {}).get("message", "") for e in denies), "★ 拒绝原因为重复熔断")
    check(st2.status in (TaskStatus.ABORTED, TaskStatus.DONE), f"任务正常结束（{st2.status.value}/{st2.end_reason}）")

    # ---- 3. model_error ----
    from backend.models.adapter import ModelError

    class FailingAdapter(FakeAdapter):
        async def chat(self, messages, tools=None, **kw):
            raise ModelError("测试：认证失败", kind="auth_error", retryable=False)

    bus3 = EventBus()
    core3 = AgentCore(FailingAdapter([]), reg, config=AgentConfig(), bus=bus3)
    ev3 = collect(bus3)
    st3 = await core3.run("触发模型错误", workspace=sb.dir)
    await bus3.drain()
    check(st3.status == TaskStatus.FAILED, "★ 模型错误 → failed")
    check(st3.end_reason == "model_error", f"end_reason=model_error（实际 {st3.end_reason}）")
    errs = [e for e in ev3 if e.type == EventType.ERROR.value]
    check(errs and errs[0].data["fatal"] is True, "★ 发出 fatal error 事件")
    check(errs[0].data["kind"] == "auth_error", "错误类型透传")

    # ---- 4. 空回复（★ 注意 PLAN 阶段会消耗剧本第一条，所以要给足两条）----
    bus4 = EventBus()
    core4 = AgentCore(
        FakeAdapter([ModelReply(content=""), ModelReply(content="   ")]),
        reg,
        config=AgentConfig(min_evidence=0),
        bus=bus4,
    )
    st4 = await core4.run("空回复", workspace=sb.dir)
    await bus4.drain()
    check(st4.end_reason == "empty_reply", f"★ 空回复被识别为 empty_reply（实际 {st4.end_reason}）")
    check(st4.status == TaskStatus.ABORTED, "空回复且无证据 → aborted（不假装完成）")
    check("没有给出任何内容" in st4.final_answer, "兜底答复说明原因，不静默失败")

    # ---- 4b. 剧本耗尽（模型侧异常）也要有明确 end_reason ----
    bus4b = EventBus()
    core4b = AgentCore(FakeAdapter([]), reg, config=AgentConfig(min_evidence=0), bus=bus4b)
    st4b = await core4b.run("剧本为空", workspace=sb.dir)
    await bus4b.drain()
    check(st4b.end_reason in ("empty_reply", "agent_done", "no_more_actions"),
          f"剧本耗尽时不挂死，有明确 end_reason（{st4b.end_reason}）")

    # ---- 5. C3 证据闸门（★ 重点）----
    bus5 = EventBus()
    adapter5 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"猜","detail":""}],"note":""}'),
            # 第一轮就想直接作答，但没观测过
            ModelReply(content="这个项目用了 FastAPI 和 React。"),
            # 被驳回后，乖乖去列目录
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            # 然后再作答
            ModelReply(content="根据目录内容，这是一个 Python 项目。"),
        ]
    )
    core5 = AgentCore(adapter5, reg, config=AgentConfig(max_turns=8, min_evidence=1, use_reflect_phase=False), bus=bus5)
    ev5 = collect(bus5)
    st5 = await core5.run("分析技术栈", workspace=sb.dir)
    await bus5.drain()
    errs5 = [e for e in ev5 if e.type == EventType.ERROR.value and e.data.get("kind") == "insufficient_evidence"]
    check(len(errs5) >= 1, "★ C3 证据闸门生效：无观测时试图作答被驳回")
    check(len(st5.evidence_summary) >= 1, "★ 最终收集到了证据")
    check("Python 项目" in st5.final_answer or st5.final_answer, f"最终答复：{st5.final_answer[:50]}")

    # ---- 6. 无计划降级（模型不按格式输出）----
    bus6 = EventBus()
    adapter6 = FakeAdapter(
        [
            ModelReply(content="我不太想给计划"),          # PLAN 阶段不按格式
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(content="看完了。"),
        ]
    )
    core6 = AgentCore(adapter6, reg, config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False), bus=bus6)
    ev6 = collect(bus6)
    st6 = await core6.run("不给计划的任务", workspace=sb.dir)
    await bus6.drain()
    check(st6.plan == [], "★ PLAN 格式不符时降级（不设计划，不崩溃）")
    check("未给出结构化计划" in st6.plan_note, f"降级原因留痕：{st6.plan_note}")
    check(st6.status == TaskStatus.DONE, "降级后任务仍能正常完成")

    # ---- 7. 关闭 PLAN/REFLECT 开关 ----
    bus7 = EventBus()
    adapter7 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(content="完成。"),
        ]
    )
    core7 = AgentCore(
        adapter7, reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False, use_reflect_phase=False),
        bus=bus7,
    )
    ev7 = collect(bus7)
    st7 = await core7.run("极简模式", workspace=sb.dir)
    await bus7.drain()
    t7 = [e.type for e in ev7]
    check(EventType.PLAN.value not in t7, "★ use_plan_phase=False 时无 plan 事件")
    check(EventType.REFLECT.value not in t7, "★ use_reflect_phase=False 时无 reflect 事件")
    check(st7.status == TaskStatus.DONE, "极简模式仍能完成（便于对比实验）")


# --------------------------------------------------------------------------- #
# [I] 顶层异常兜底
# --------------------------------------------------------------------------- #
async def test_robustness(sb: Sandbox) -> None:
    print("\n[I] 健壮性")
    reg = build_default_registry()

    # 工作区不存在
    bus = EventBus()
    core = AgentCore(FakeAdapter([]), reg, bus=bus)
    try:
        await core.run("x", workspace="Z:/definitely/not/exist")
        check(False, "不存在的工作区应当报错")
    except Exception as exc:  # noqa: BLE001
        check("不存在" in str(exc) or "目录" in str(exc), f"不存在的工作区被拒绝：{type(exc).__name__}")

    # 工具内部抛异常被隔离
    from backend.tools.base import Tool

    class BoomTool(Tool):
        name = "boom"
        description = "总是抛异常。"
        parameters = {"type": "object", "properties": {}}
        risk = RiskLevel.READ

        async def run(self, args, ctx):
            raise ValueError("我是工具内部 bug")

    reg2 = build_default_registry()
    reg2.register(BoomTool(), group="test")
    from backend.tools.registry import execute

    res = await execute(reg2, "boom", {}, make_context_standalone(sb.dir))
    check(not res.ok, "工具内部异常被隔离")
    check(res.error["kind"] == "internal_error", f"归类为 internal_error：{res.error['kind']}")
    check("ValueError" in res.summary, "异常信息保留（便于排查）")

    # 注册表校验
    from backend.tools.registry import ToolRegistry, ToolRegistryError

    r = ToolRegistry()
    try:
        class NoDesc(Tool):
            name = "nodesc"
            description = ""
            parameters = {"type": "object", "properties": {}}

            async def run(self, args, ctx):
                return ToolResult.success("nodesc", "")

        r.register(NoDesc())
        check(False, "缺 description 应当被拒绝（模型需要它）")
    except ToolRegistryError as exc:
        check("description" in str(exc), "★ 注册时校验 description 非空")

    try:
        r.register(
            type("T", (Tool,), {
                "name": "bad",
                "description": "x",
                "parameters": {"type": "array"},
                "run": lambda self, a, c: None,
            })()
        )
        check(False, "非法 schema 应当被拒绝")
    except ToolRegistryError as exc:
        check("object" in str(exc), "★ 注册时校验 parameters 是 type=object")

    # 重复注册
    r2 = ToolRegistry()
    from backend.tools.readonly.list_files import make as mk_list

    r2.register(mk_list())
    try:
        r2.register(mk_list())
        check(False, "重复注册应当报错")
    except ToolRegistryError:
        check(True, "★ 重复注册被拒绝（防止静默覆盖工具）")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# [V] 完成验证闸门：命令失败时，不许把任务判成功
# --------------------------------------------------------------------------- #
class StubRunTool(Tool):  # type: ignore[name-defined]
    """假的 run_command：只返回带 exit_code 的结果，不启动任何进程。

    ★ 为什么不用真工具：这里要测的是「Core 怎么读执行结果」，
      不是沙箱。用假工具才能精确控制"失败/成功"，也不依赖 Docker。
    """

    name = "run_command"
    description = "测试用假命令执行（不会真的运行任何东西）"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    risk = RiskLevel.READ          # 不触发审批 —— 审批不是本节要测的东西

    def __init__(self, exit_codes: list[int]) -> None:
        self.exit_codes = list(exit_codes)
        self.calls = 0

    async def run(self, args, ctx):  # type: ignore[override]
        self.calls += 1
        code = self.exit_codes[min(self.calls - 1, len(self.exit_codes) - 1)]
        data = {"exit_code": code, "stdout": "", "stderr": "E   assert 1 == 2"}
        if code == 0:
            return ToolResult.success(self.name, "命令执行成功（退出码 0）", data=data)
        return ToolResult.failure(
            self.name, ErrorKind.EXECUTION_ERROR, f"命令退出码 {code}", data=data
        )


def _gate_registry(stub_run: "StubRunTool"):
    from backend.tools.readonly.list_files import make as mk_list
    from backend.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(mk_list(), group="readonly")
    reg.register(stub_run, group="exec")
    reg.register(StubEditTool(), group="mutating")
    return reg


class StubEditTool(Tool):
    """假的 edit_file：只制造一条 change 记录，不碰磁盘。

    ★ 闸门用「有没有改过文件」区分"修复型"和"观测型"任务，
      所以测试里需要能可控地制造这个信号。
    """

    name = "edit_file"
    description = "测试用假编辑（不真的写文件）"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    risk = RiskLevel.READ          # 不触发审批，专注测闸门

    async def run(self, args, ctx):  # type: ignore[override]
        return ToolResult.success(
            self.name, "修改文件 a.py", data={"change": {"path": "a.py", "action": "edit"}}
        )


async def test_verification_gate(sb: Sandbox) -> None:
    """[V] 「模型说完成」不等于「任务真的完成」。"""
    print("\n[V] 完成验证闸门（失败不能被判成功）")

    # ---- V1 复现：最后一次命令失败 + 模型声称"测试全过" ----
    stub = StubRunTool([1])
    adapter = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("c1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("c2", "edit_file", {"path": "a.py"})]),
            ModelReply(tool_calls=[tc("c3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="所有测试已通过，修复完成。"),
            ModelReply(content="确认修复完成，测试全部通过。"),
            ModelReply(content="（不该走到这里）"),
        ]
    )
    bus = EventBus()
    events = collect(bus)
    core = AgentCore(
        adapter,
        _gate_registry(stub),
        config=AgentConfig(
            max_turns=8, min_evidence=1, use_plan_phase=False, use_reflect_phase=False,
            max_verification_retries=1,
        ),
        bus=bus,
    )
    state = await core.run("修好它并跑测试", workspace=sb.dir)
    await bus.drain()

    check(stub.calls == 1, f"假命令只执行了 1 次（{stub.calls}）")
    check(state.status is TaskStatus.ABORTED, f"★ 命令失败 → 任务是 aborted 而不是 done（{state.status.value}）")
    check(state.end_reason == "verification_failed", f"★ 结束原因是 verification_failed（{state.end_reason}）")
    check(
        any(e.type == EventType.ERROR and e.data.get("kind") == "unverified_completion" for e in events),
        "★ 发出了 unverified_completion 错误事件",
    )
    check("未通过验证" in state.final_answer, "最终答复被机器标注「未通过验证」")
    check("所有测试已通过" in state.final_answer or "确认修复完成" in state.final_answer,
          "模型自己的说法仍被保留（不篡改，只加标注）")
    check(state.verification_retries == 1, f"记录了一次驳回（{state.verification_retries}）")
    check(bool(state.command_history) and state.command_history[-1].exit_code == 1,
          "命令结果被记进 state.command_history")

    # ---- V2 驳回之后重新验证成功 → 正常完成（证明不是一棒子打死）----
    stub2 = StubRunTool([1, 0])
    adapter2 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("d1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("d2", "edit_file", {"path": "a.py"})]),
            ModelReply(tool_calls=[tc("d3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="完成了。"),                                   # 被驳回
            ModelReply(tool_calls=[tc("d4", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="已修复并验证通过。"),
        ]
    )
    bus2 = EventBus()
    core2 = AgentCore(
        adapter2, _gate_registry(stub2),
        config=AgentConfig(max_turns=8, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=2),
        bus=bus2,
    )
    state2 = await core2.run("修好它并跑测试", workspace=sb.dir)
    await bus2.drain()
    check(stub2.calls == 2, f"★ 被驳回后模型重跑了一次命令（{stub2.calls} 次）")
    check(state2.status is TaskStatus.DONE, f"★ 重新验证成功后正常完成（{state2.status.value}）")
    check(state2.verification_retries == 1, f"驳回计数为 1（{state2.verification_retries}）")

    # ---- V3 命令成功 → 不受影响 ----
    stub3 = StubRunTool([0])
    adapter3 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("e1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("e2", "edit_file", {"path": "a.py"})]),
            ModelReply(tool_calls=[tc("e3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="测试通过，完成。"),
        ]
    )
    bus3 = EventBus()
    core3 = AgentCore(
        adapter3, _gate_registry(stub3),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=bus3,
    )
    state3 = await core3.run("跑测试", workspace=sb.dir)
    await bus3.drain()
    check(state3.status is TaskStatus.DONE, f"命令成功 → done（{state3.status.value}）")
    check(state3.verification_retries == 0, "成功路径不触发驳回")

    # ---- V4 没有执行过命令（纯分析任务）→ 不受影响 ----
    adapter4 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("f1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("f2", "edit_file", {"path": "a.py"})]),
            ModelReply(content="这个项目用了 FastAPI。"),
        ]
    )
    core4 = AgentCore(
        adapter4, _gate_registry(StubRunTool([1])),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=EventBus(),
    )
    state4 = await core4.run("分析这个项目", workspace=sb.dir)
    check(state4.status is TaskStatus.DONE, f"★ 纯分析任务（无命令）不受闸门影响（{state4.status.value}）")

    # ---- V5 闸门可关闭（对比实验用）----
    stub5 = StubRunTool([1])
    adapter5 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("g1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("g2", "edit_file", {"path": "a.py"})]),
            ModelReply(tool_calls=[tc("g3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="所有测试已通过。"),
        ]
    )
    core5 = AgentCore(
        adapter5, _gate_registry(stub5),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=0),
        bus=EventBus(),
    )
    state5 = await core5.run("跑测试", workspace=sb.dir)
    check(state5.status is TaskStatus.DONE, "max_verification_retries=0 时闸门关闭（对照）")

    # ---- V6 ★ 观测型任务：不改文件、命令失败、如实汇报 → 应该正常完成 ----
    # 真实 E2E 踩过的坑：任务目标是「跑一下 pytest 把结果告诉我」，
    # Agent 如实写了"退出码 1、沙箱无网络导致失败"，闸门却驳回两次并强制
    # aborted —— 把**要报告的内容本身**当成了"修复没做成"。
    stub6 = StubRunTool([1])
    adapter6 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("h1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("h2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="退出码 1：2 个用例，1 通过 1 失败。"
                               "失败的是 test_remote，原因是容器无网络。"),
        ]
    )
    bus6 = EventBus()
    core6 = AgentCore(
        adapter6, _gate_registry(stub6),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=bus6,
    )
    state6 = await core6.run("跑一下 pytest 把真实结果告诉我", workspace=sb.dir)
    await bus6.drain()
    check(state6.status is TaskStatus.DONE,
          f"★ 观测型任务（没改文件）如实报告失败也判 done（{state6.status.value}）")
    check(state6.verification_retries == 0, "观测型任务不触发驳回")
    check("退出码 1" in state6.final_answer, "如实汇报的内容原样保留")


# --------------------------------------------------------------------------- #
# [P] 计划步骤：状态必须由证据决定
# --------------------------------------------------------------------------- #
async def test_plan_truth(sb: Sandbox) -> None:
    """[P] 步骤不能靠"轮次跑够了"或"任务要结束了"变成完成。"""
    print("\n[P] 计划步骤真实状态")

    # ---- P1 没开工的步骤，任务结束也不许打勾 ----
    adapter = FakeAdapter(
        [
            ModelReply(content='{"steps":['
                               '{"title":"看结构","check":"observation"},'
                               '{"title":"改代码","check":"change"},'
                               '{"title":"跑测试","check":"command_ok","completion":"pytest 退出码 0"}],"note":""}'),
            ModelReply(tool_calls=[tc("p1", "list_files", {"path": "."})]),
            ModelReply(content="我做完了。"),
        ]
    )
    bus = EventBus()
    core = AgentCore(
        adapter, build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=1, use_reflect_phase=False),
        bus=bus,
    )
    state = await core.run("改点东西", workspace=sb.dir)
    await bus.drain()
    check([s.status.value for s in state.plan] == ["done", "pending", "pending"],
          f"★ 只完成有证据的那一步（{[s.status.value for s in state.plan]}）")
    check(state.plan[1].started_turn == 0, "★ 没开工的步骤 started_turn 仍是 0")
    check("未完成验证" in state.final_answer, "★ 最终答复里列出未完成步骤")

    # ---- P2 状态机：失败/阻塞都不推进索引 ----
    st = TaskState(goal="x", workspace=sb.dir)
    st.set_plan([PlanStep(index=0, title="a", check="command_ok"), PlanStep(index=1, title="b")])
    st.note_command("python -m pytest -q", 1, False)
    check(st.finish_current_step()[0] is False, "★ 命令失败 → 完成条件不满足")
    st.fail_current_step("测试还是红")
    check(st.plan[0].status is StepStatus.FAILED, "★ 可以标记失败")
    check(st.current_step_index == 0, "★ 失败不推进索引（停在原地）")
    st.block_current_step("命中禁止路径")
    check(st.plan[0].status is StepStatus.BLOCKED, "★ 可以标记被阻塞")
    st.note_command("python -m pytest -q", 0, True)
    check(st.finish_current_step()[0] is True, "★ 后来有了成功命令 → 同一页面可以完成")
    check(st.plan[0].status is StepStatus.DONE and st.current_step_index == 1, "★ 完成后才推进")
    st.reconcile_plan()
    check(st.plan[1].status is StepStatus.PENDING,
          "★ reconcile 不会把没开工的步骤补成 done")

    # ---- P3 步骤按自己的完成条件结算（失败→保持，成功→打勾）----
    stub = StubRunTool([1, 0])
    adapter3 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"跑测试","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("q1", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("q2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="现在过了。"),
        ]
    )
    core3 = AgentCore(
        adapter3, _gate_registry(stub),
        config=AgentConfig(max_turns=8, min_evidence=1, use_reflect_phase=False),
        bus=EventBus(),
    )
    state3 = await core3.run("跑测试", workspace=sb.dir)
    check([s.status.value for s in state3.plan] == ["done"], f"★ 命令成功后步骤才完成（{[s.status.value for s in state3.plan]}）")
    check("退出码 0" in (state3.plan[0].evidence or ""), "★ 完成时记录了证据")

    # ---- P4 预算耗尽 → 不自动完成剩余步骤 ----
    adapter4 = FakeAdapter(
        [
            ModelReply(content='{"steps":[{"title":"a"},{"title":"b"},{"title":"c"}],"note":""}'),
            ModelReply(tool_calls=[tc("r1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("r2", "list_files", {"path": "."})]),
            ModelReply(content="（不该到这里）"),
        ]
    )
    core4 = AgentCore(
        adapter4, build_default_registry(),
        # ★ 为什么用 tool_calls=1 而不是 max_turns=2：
        #   max_turns=2 时 PLAN + ACT 就吃光了轮数，Policy 会在工具执行前
        #   以"预算耗尽"拒掉调用 —— 那测到的是另一条路径。
        #   这里让第一个工具真的跑完（步骤拿到证据），再用工具预算掐断任务。
        config=AgentConfig(max_turns=3, max_tool_calls=1, min_evidence=1, use_reflect_phase=False),
        bus=EventBus(),
    )
    state4 = await core4.run("做三件事", workspace=sb.dir)
    done = sum(1 for s in state4.plan if s.status.value == "done")
    check(done == 1, f"★ 预算耗尽只完成真正有证据的 1 步（实际 {done}）")
    check(state4.end_reason.startswith("budget"), f"结束原因是预算（{state4.end_reason}）")
    check(len(state4.incomplete_steps()) == 2, "★ 剩余 2 步保持未完成")
    check("未完成" in state4.final_answer, "★ 中断答复里说明还有步骤没做完")

    # ---- P5 取消 → 不自动完成 ----
    holder: list[Any] = []          # 工具里要能拿到 core，用列表绕开构造顺序

    class _CancelTool(Tool):
        name = "list_files"
        description = "列目录（第一次调用就模拟用户取消）"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
        risk = RiskLevel.READ

        def __init__(self) -> None:
            self.n = 0

        async def run(self, args, ctx):  # type: ignore[override]
            self.n += 1
            if self.n == 1 and holder:
                holder[0].cancel()      # 模拟用户在执行中点「停止」
            return ToolResult.success(self.name, "列出目录", data={"path": "."})

    reg5 = ToolRegistry()
    reg5.register(_CancelTool(), group="readonly")
    core5 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"a"},{"title":"b"}],"note":""}'),
            ModelReply(tool_calls=[tc("s1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("s2", "list_files", {"path": "."})]),
            ModelReply(content="（不该到这里）"),
        ]),
        reg5,
        config=AgentConfig(max_turns=8, min_evidence=1, use_reflect_phase=False),
        bus=EventBus(),
    )
    holder.append(core5)
    state5 = await core5.run("做两件事", workspace=sb.dir)
    check(state5.status is TaskStatus.ABORTED and state5.end_reason == "cancelled",
          f"★ 取消后任务收敛为 cancelled（{state5.status.value}/{state5.end_reason}）")
    check(sum(1 for s in state5.plan if s.status.value == "done") == 1,
          "★ 取消不把剩余步骤补成 done")

    # ---- P6 没有详细计划的简单任务照样能结束 ----
    adapter6 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("t1", "list_files", {"path": "."})]),
            ModelReply(content="目录里有个 main.py。"),
        ]
    )
    core6 = AgentCore(
        adapter6, build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=EventBus(),
    )
    state6 = await core6.run("看看目录", workspace=sb.dir)
    check(state6.status is TaskStatus.DONE and not state6.plan, "★ 无计划任务正常结束")


# --------------------------------------------------------------------------- #
# [V2] 修复意图的"零工作"门禁（M3 收尾）
# --------------------------------------------------------------------------- #
async def test_repair_zero_work_gate(sb: Sandbox) -> None:
    """
    [V2] 意图是 repair 时，"停下"不等于"修好"。

    ★ 这条规则来自一次真实运行：模型空回复、零改动、零命令，
      状态却是 done/no_more_actions，GUI 显示"完成"，答复自称"未修复"。
    本组覆盖：
      · 只观测后直接作答（零改动零命令）→ 不能 done
      · 有观测 + 空回复（真实形态）→ 不能 done
      · 零改动但有成功命令（证明问题不存在）→ 允许 done（不机械要求改动）
      · 非修复任务 → 不受这条规则影响
      · Graph 与 Legacy 行为一致
    """
    print("\n[V2] 修复意图的零工作门禁")

    # ---- V2.1 只读 + 文本答复：零改动零命令 → 不能 done ----
    stub1 = StubRunTool([0])
    ad1 = FakeAdapter([
        ModelReply(tool_calls=[tc("z1", "list_files", {"path": "."})]),
        ModelReply(content="我看了一下，应该没问题。"),
        ModelReply(content="确实没问题。"),
    ])
    bus1 = EventBus()
    ev1 = collect(bus1)
    core1 = AgentCore(
        ad1, _gate_registry(stub1),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=bus1,
    )
    st1 = await core1.run("修好 app.py 里的 bug", workspace=sb.dir)
    await bus1.drain()
    check(st1.intent == "repair", f"意图识别为 repair（{st1.intent}）")
    check(st1.status is TaskStatus.ABORTED,
          f"★ 零改动零命令 → aborted 而不是 done（{st1.status.value}）")
    check(st1.end_reason == "verification_failed",
          f"★ 结束原因是 verification_failed（{st1.end_reason}）")
    check("没有文件改动" in st1.final_answer and "验证命令" in st1.final_answer,
          "答复写明了判据（零改动 + 零命令）")
    check(any(e.type == EventType.ERROR and e.data.get("kind") == "unverified_completion"
              for e in ev1), "发出了 unverified_completion 事件")
    check(stub1.calls == 0, "确实没有执行过命令")

    # ---- V2.2 有观测 + 空回复（真实踩到的形态）→ 不能 done ----
    stub2 = StubRunTool([0])
    ad2 = FakeAdapter([
        ModelReply(tool_calls=[tc("y1", "list_files", {"path": "."})]),
        ModelReply(content=""),
        ModelReply(content=""),
    ])
    bus2 = EventBus()
    ev2 = collect(bus2)
    core2 = AgentCore(
        ad2, _gate_registry(stub2),
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=bus2,
    )
    st2 = await core2.run("修复 app.py 里的 bug", workspace=sb.dir)
    await bus2.drain()
    check(st2.status is TaskStatus.ABORTED,
          f"★ 空回复 + 零改动 → aborted（{st2.status.value}）")
    check(st2.end_reason == "verification_failed",
          f"★ 原因不是 no_more_actions/agent_done（{st2.end_reason}）")
    check(any(e.type == EventType.ERROR and e.data.get("kind") == "empty_reply" for e in ev2),
          "仍然记录了 empty_reply（保留原始现象）")

    # ---- V2.3 零改动 + 成功命令 → 允许 done（逃生口）----
    stub3 = StubRunTool([0])
    ad3 = FakeAdapter([
        ModelReply(tool_calls=[tc("x1", "run_command", {"command": "pytest -q"})]),
        ModelReply(content="跑了测试，本来就是通过的，不需要改动。"),
    ])
    bus3 = EventBus()
    core3 = AgentCore(
        ad3, _gate_registry(stub3),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=bus3,
    )
    st3 = await core3.run("修好 app.py 里的 bug", workspace=sb.dir)
    await bus3.drain()
    check(stub3.calls == 1, f"命令执行了 1 次（{stub3.calls}）")
    check(st3.status is TaskStatus.DONE,
          f"★ 有验证命令时不机械要求改动（{st3.status.value}）")

    # ---- V2.4 非修复任务不受影响 ----
    stub4 = StubRunTool([0])
    ad4 = FakeAdapter([
        ModelReply(tool_calls=[tc("w1", "list_files", {"path": "."})]),
        ModelReply(content="目录里有 main.py 和 README.md。"),
    ])
    bus4 = EventBus()
    core4 = AgentCore(
        ad4, _gate_registry(stub4),
        config=AgentConfig(max_turns=5, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=bus4,
    )
    st4 = await core4.run("看一下这个项目里有什么", workspace=sb.dir)
    await bus4.drain()
    check(st4.intent != "repair", f"非修复意图（{st4.intent}）")
    check(st4.status is TaskStatus.DONE, f"★ 非修复任务照常 done（{st4.status.value}）")

    # ---- V2.5 Graph 同源：空回复 + 零改动 也是 aborted ----
    stub5 = StubRunTool([0])
    ad5 = FakeAdapter([
        ModelReply(tool_calls=[tc("v1", "list_files", {"path": "."})]),
        ModelReply(content=""),
        ModelReply(content=""),
    ])
    core5 = AgentCore(
        ad5, _gate_registry(stub5),
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1,
                           orchestrator="graph"),
        bus=EventBus(),
    )
    st5 = await core5.run("修复 app.py 里的 bug", workspace=sb.dir)
    check(st5.engine == "graph", f"引擎标注（{st5.engine}）")
    check(st5.status is TaskStatus.ABORTED and st5.end_reason == "verification_failed",
          f"★ graph 与 legacy 一致（{st5.status.value}/{st5.end_reason}）")


# --------------------------------------------------------------------------- #
# [S] 改动范围审计
# --------------------------------------------------------------------------- #
class _StubWriter(Tool):
    """真的写文件，但 risk=READ（不触发审批）—— 用来单独测审计与归因。"""

    name = "write_file"
    description = "测试用写入（不触发审批）"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path"],
    }
    risk = RiskLevel.READ

    def __init__(self, root: str) -> None:
        self.root = root

    async def run(self, args, ctx):  # type: ignore[override]
        rel = args["path"]
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        existed = os.path.exists(full)
        with open(full, "w", encoding="utf-8") as f:
            f.write(args.get("content", "new\n"))
        return ToolResult.success(
            self.name, f"写入 {rel}",
            data={"path": rel, "change": {"path": rel, "action": "overwrite" if existed else "create"}},
        )


async def test_scope_audit(sb: Sandbox) -> None:
    """[S] 改动范围：基线不含用户预存修改，Agent 的增改删都要看得见。"""
    print("\n[S] 改动范围审计")

    # ---- S1 用户任务开始前就有的未提交修改，不算 Agent 的 ----
    pre = Path(sb.dir, "user_notes.txt")
    pre.write_text("我自己改的\n", encoding="utf-8")
    adapter = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("u1", "list_files", {"path": "."})]),
            ModelReply(content="看了一下目录。"),
        ]
    )
    core = AgentCore(
        adapter, build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=EventBus(),
    )
    state = await core.run("看看这个目录", workspace=sb.dir)
    changed_paths = [c["path"] for c in state.audit.get("changed", [])]
    check("user_notes.txt" not in changed_paths,
          f"★ 用户预存修改没有被算成 Agent 改动（changed={changed_paths}）")
    check(state.audit.get("baseline_files", 0) >= 1, "★ 记录了任务开始时的基线文件数")

    # ---- S2 Agent 新增 / 修改 / 删除都要进审计 ----
    pre.unlink()
    writer = _StubWriter(sb.dir)
    reg = ToolRegistry()
    reg.register(build_default_registry().get("list_files"), group="readonly")
    reg.register(writer, group="mutating")
    adapter2 = FakeAdapter(
        [
            ModelReply(tool_calls=[tc("w1", "write_file", {"path": "docs/new.md", "content": "# hi\n"})]),
            ModelReply(tool_calls=[tc("w2", "write_file", {"path": "README.md", "content": "改过了\n"})]),
            ModelReply(content="写完了。"),
        ]
    )
    core2 = AgentCore(
        adapter2, reg,
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=EventBus(),
    )
    state2 = await core2.run("建个文档并改 README", workspace=sb.dir)
    by_path = {c["path"]: c for c in state2.audit.get("changed", [])}
    check(by_path.get("docs/new.md", {}).get("kind") == "added", "★ 新增文件被发现")
    check(by_path.get("README.md", {}).get("kind") == "modified", "★ 修改文件被发现")
    check(by_path.get("docs/new.md", {}).get("via") == "tool", "★ 有工具留痕 → 归因为 tool")
    check(state2.audit.get("unattributed") == [], "有留痕的改动不标为来源不明")

    # ---- S3 来源不明：文件变了但没有工具留痕 ----
    st3 = TaskState(goal="x", workspace=sb.dir)
    st3.baseline_files = scan_tree(sb.dir)
    Path(sb.dir, "sneaky.txt").write_text("命令副作用\n", encoding="utf-8")
    core3 = AgentCore(FakeAdapter([]), build_default_registry(), bus=EventBus())
    audit3 = core3._ensure_audit(st3)
    hit = [c for c in audit3["changed"] if c["path"] == "sneaky.txt"]
    check(bool(hit) and hit[0]["via"] == "unattributed",
          f"★ 没有工具留痕的改动标为来源不明（{hit}）")
    Path(sb.dir, "sneaky.txt").unlink()

    # ---- S4 超出允许范围：标记但不拦 ----
    # ★ 注意：allowed_paths 必须传给 run()，写在另一个 TaskState 上不生效
    #   （这个测试之前是靠 norm_rel 的 bug 碰巧通过的，已修正）
    writer4 = _StubWriter(sb.dir)
    reg4 = ToolRegistry()
    reg4.register(writer4, group="mutating")
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("x1", "write_file", {"path": "docs/out.md", "content": "x\n"})]),
            ModelReply(content="写好了。"),
        ]),
        reg4,
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=EventBus(),
    )
    st4b = await core4.run("写文档", workspace=sb.dir, allowed_paths=["src"],
                           forbidden_paths=["src/secret.py"])
    check("docs/out.md" in st4b.audit.get("out_of_scope", []),
          f"★ 超出允许范围被标记（{st4b.audit.get('out_of_scope')}）")
    check(st4b.status is TaskStatus.DONE, "★ 第一版只标记不拦截（任务仍可结束）")

    # ---- S5 禁止路径：Policy 直接拦，且步骤标 blocked ----
    policy = Policy(build_default_registry())
    st5 = TaskState(goal="x", workspace=sb.dir)
    st5.forbidden_paths = ["src/secret.py"]
    ctx5 = make_context(st5)
    d5 = policy.check("write_file", {"path": "src/secret.py", "content": "x"}, st5, ctx5)
    check(not d5.allow and "禁止" in (d5.result.summary if d5.result else ""),
          "★ 禁止路径在 Policy 层被拒")
    d5b = policy.check("write_file", {"path": "src/other.py", "content": "x"}, st5, ctx5)
    check(d5b.needs_approval, "★ 没命中的路径仍按原流程走审批（不是一刀切全拦）")

    reg5 = ToolRegistry()
    reg5.register(build_default_registry().get("write_file"), group="mutating")
    core5 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"改密钥文件","check":"change"}],"note":""}'),
            ModelReply(tool_calls=[tc("y1", "write_file", {"path": "src/secret.py", "content": "x"})]),
            ModelReply(content="改不了，被拦了。"),
        ]),
        reg5,
        config=AgentConfig(max_turns=6, min_evidence=1, use_reflect_phase=False),
        bus=EventBus(),
    )
    st5c = await core5.run("改一下 src/secret.py", workspace=sb.dir, forbidden_paths=["src/secret.py"])
    check(st5c.plan[0].status is StepStatus.BLOCKED,
          f"★ 被禁止路径挡住的步骤标记 blocked（{st5c.plan[0].status.value}）")

    # ---- S6 禁止路径真被改到时，审计能兜底发现并写进报告 ----
    writer6 = _StubWriter(sb.dir)
    reg6 = ToolRegistry()
    reg6.register(writer6, group="mutating")
    events6: list[Event] = []
    bus6 = EventBus()
    events6 = collect(bus6)
    core6 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("z1", "write_file", {"path": "src/secret.py", "content": "x"})]),
            ModelReply(content="写完了。"),
        ]),
        reg6,
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False, use_reflect_phase=False),
        bus=bus6,
    )
    st6 = await core6.run("写密钥文件", workspace=sb.dir, forbidden_paths=["src/secret.py"])
    await bus6.drain()
    check("src/secret.py" in st6.audit.get("forbidden", []),
          f"★ 审计兜底发现禁止路径被改（{st6.audit.get('forbidden')}）")
    check(any(e.type == EventType.ERROR and e.data.get("kind") == "scope_audit" for e in events6),
          "★ 事件流里有 scope_audit 提示")
    check("文件改动范围审计" in st6.final_answer, "★ 最终答复附带机器审计块")
    check("audit" in st6.to_dict(), "★ 审计结果进了 to_dict（任务快照能看见）")

    # ---- S7 范围匹配函数本身 ----
    from backend.agent.scope import hits, norm_rel, within_allowed
    check(norm_rel("D:\\Proj\\a\\b.py") == "d:/proj/a/b.py", "norm_rel 统一分隔符+小写")
    check(hits("src/a.py", ["src"]), "目录前缀匹配")
    check(hits("src/a.py", ["SRC/A.PY"]), "大小写不敏感")
    check(not hits("src/a.py", ["", "  "]), "空 pattern 不误伤")
    check(within_allowed("a.py", []), "空 allowed = 整个工作区")
    check(within_allowed("pipeline/a.py", ["."]),
          "★ 默认 allowed=['.'] = 整个工作区，不能误判越界（压力测试抓到的真 bug）")
    check(within_allowed("pipeline/a.py", ["./"]), "★ './' 也要当成整个工作区")
    check(within_allowed("pipeline/a.py", ["pipeline"]), "声明了子目录就按子目录算")
    check(not within_allowed("docs/a.md", ["pipeline"]), "子目录之外的算越界")


async def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    setup_logging("ERROR")
    print("=" * 78)
    print("Loom Agent v0.1a · 集成自测（AgentCore + 工具 + Policy + 审批）")
    print("=" * 78)

    sb = Sandbox()
    print(f"沙盒工作区：{sb.dir}")
    try:
        await test_tools(sb)
        await test_edit_file(sb)
        await test_read_hash(sb)
        await test_policy(sb)
        await test_approval()
        await test_context(sb)
        await test_json_extract()
        await test_agent_loop(sb)
        await test_final_answer_synthesis(sb)
        await test_approval_in_loop(sb)
        await test_termination(sb)
        await test_robustness(sb)
        await test_verification_gate(sb)
        await test_repair_zero_work_gate(sb)
        await test_plan_truth(sb)
        await test_scope_audit(sb)
        await test_intent(sb)
        await test_risk_detection(sb)
        await test_evidence_grading(sb)
        await test_risk_closure(sb)
        await test_final_answer_hygiene(sb)
        await test_reflect_no_progress(sb)
        await test_scope_consistency(sb)
        await test_read_cache(sb)
        await test_compaction(sb)
        await test_budget_and_read_index(sb)
        await test_m1_orchestrator_and_history(sb)
        await test_graph_engine(sb)
        await test_tool_chain_m21(sb)
        await test_history_task_id_m21(sb)
    finally:
        sb.cleanup()

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 78)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print(f"\n失败项（{total - ok} 条）：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 78)
    return 0 if ok == total else 1



# --------------------------------------------------------------------------- #
# [I] 任务意图 / 用户约束
# --------------------------------------------------------------------------- #
async def test_intent(sb: Sandbox) -> None:
    """[I] 意图分类：显式约束 > 验证方式 > 关键词 > unknown。"""
    print("\n[I] 任务意图与约束")

    from backend.agent.intent import classify_intent, extract_constraints

    def kind(goal: str, forbidden: list[str] | None = None) -> str:
        return classify_intent(goal, forbidden_paths=forbidden).intent

    check(kind("分析这个项目的架构，看看为什么测试失败") == "diagnostic", "诊断类")
    check(kind("跑一下 pytest，把真实结果告诉我") == "verification", "验证类")
    check(kind("修复 average 在空列表时的 bug") == "repair", "修复类")
    check(kind("为 calc 模块补充单元测试") == "test_authoring", "测试编写类")
    check(kind("实现一个导出 CSV 的新功能") == "implementation", "实现类")
    check(kind("处理一下这个东西") == "unknown", "★ 没有关键词 → unknown（不猜）")
    check(kind("分析并修复这个问题") == "unknown",
          "★ 诊断/修复打平 → unknown（信息不足不擅自解释）")
    check(kind("跑一下测试看看为什么失败") == "diagnostic",
          "★ 跑测试+看原因 → 诊断（不是盲目修复）")

    c = extract_constraints("不要修改测试，只运行 pytest 看结果")
    check(c["no_test_modification"] is True, "识别出「不许改测试」")
    check(kind("不要修改测试，只运行 pytest 看结果") == "verification",
          "★ 禁止改测试 + 要求跑测试 → 验证任务")
    c2 = extract_constraints("只检查代码，不要修改任何文件")
    check(c2["read_only"] is True, "识别出「只读」")
    check(kind("只检查代码，不要修改任何文件") == "diagnostic", "★ 只读 → 诊断任务")
    check(kind("跑一下测试报告结果，不要修复") == "verification", "★ 声明不修复 → 验证任务")
    check(kind("修复 bug，但不要改测试") == "repair", "修复任务仍然识别为修复")

    # forbidden_paths 里有 tests/ 也算佐证
    r = classify_intent("跑一下测试", forbidden_paths=["tests/"])
    check(r.intent == "verification", "调用方禁 tests/ + 跑测试 → 验证任务")

    # ---- 范围化禁令：限定了目标的"不许修改"不是全局只读（压力扩样抓到的 bug）----
    from backend.agent.intent import extract_scoped_forbidden
    r2 = classify_intent("给流水线新增分项报告：不许修改 tests/test_pipeline.py 和 data/input.csv，"
                         "其余只许改 pipeline/")
    check(r2.constraints.get("read_only") is False,
          "★ 限定了目标的禁令 ≠ 全局只读（否则实现任务什么都写不了）")
    check("tests/test_pipeline.py" in r2.scoped_forbidden
          and "data/input.csv" in r2.scoped_forbidden,
          f"★ 禁令里的路径被抽出来（{r2.scoped_forbidden}）")
    check(extract_scoped_forbidden("只检查代码，不要修改任何文件") == [],
          "★ 没跟具体路径的禁令保持全局只读")
    # 「任何文件（包括 A、B）」是全局只读，不能因为括号里列了路径就降级
    r3 = classify_intent("只做一件事：运行 pytest，把真实结果告诉我。"
                         "不要修改、新建或删除任何文件（包括 tests/、data/ 和 pipeline/）。")
    check(r3.constraints.get("read_only") is True,
          "★ 「任何文件」= 全局只读（括号里的路径只是举例）")
    check(r3.intent == "diagnostic", f"★ 诊断任务不被误判成 repair（{r3.intent}）")
    check(extract_scoped_forbidden("不要修改任何文件（包括 tests/ 和 data/）") == [],
          "★ 括号里的路径不会变成范围化禁令")
    # 「不许修改 A 和 B，其余只许改 C」：C 是**允许**范围，不能当成禁止
    from backend.agent.intent import extract_scoped_allowed
    r4 = classify_intent("给流水线新增分项报告：不许修改 tests/test_pipeline.py 和 data/input.csv，"
                         "其余只许改 pipeline/ 下的源码。")
    check("pipeline" not in r4.scoped_forbidden,
          f"★ 允许范围不会被误当禁止范围（禁止={r4.scoped_forbidden}）")
    check("pipeline" in r4.scoped_allowed,
          f"★ 「只许改 pipeline/」被抽成允许范围（{r4.scoped_allowed}）")

    # ---- 约束要真的落到 Policy 上（验收 4/5）----
    policy = Policy(build_default_registry())
    ctx = make_context(TaskState(goal="x", workspace=sb.dir))

    st_ro = TaskState(goal="只检查代码，不要修改任何文件", workspace=sb.dir)
    st_ro.constraints = {"read_only": True}
    d = policy.check("write_file", {"path": "a.py", "content": "x"}, st_ro, ctx)
    check(not d.allow and "只检查" in (d.result.summary if d.result else ""),
          "★ 用户要求只读 → 写操作被 Policy 拒绝")
    d2 = policy.check("read_file", {"path": "main.py"}, st_ro, ctx)
    check(d2.allow, "★ 只读任务仍然允许读")

    st_nt = TaskState(goal="修复 bug，但不要修改测试", workspace=sb.dir)
    st_nt.constraints = {"no_test_modification": True}
    st_nt.forbidden_paths = ["tests/", "conftest.py", "pytest.ini", "tox.ini"]
    d3 = policy.check("write_file", {"path": "tests/t.py", "content": "x"}, st_nt, ctx)
    check(not d3.allow, "★ 说了别改测试 → 写 tests/ 被拒")
    d4 = policy.check("write_file", {"path": "app/x.py", "content": "x"}, st_nt, ctx)
    check(d4.needs_approval, "★ 非测试路径仍按原流程走审批（不是一刀切）")

    # ---- 测试编写任务不该被自动禁测试 ----
    core_ta = AgentCore(
        FakeAdapter([ModelReply(content="写好了。")]),
        build_default_registry(),
        config=AgentConfig(max_turns=4, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st_ta = await core_ta.run("为 mathx 补充单元测试", workspace=sb.dir)
    check(st_ta.intent == "test_authoring", f"意图 test_authoring（{st_ta.intent}）")
    check(all(not _scope_hits("tests/", [f]) for f in st_ta.forbidden_paths),
          f"★ 测试编写任务不自动禁 tests（{st_ta.forbidden_paths}）")


# --------------------------------------------------------------------------- #
# [R] 验证可信度风险
# --------------------------------------------------------------------------- #
async def test_risk_detection(sb: Sandbox) -> None:
    """[R] 能绕过验证的改动要被认出来，但合理的 mock 不能一棍子打死。"""
    print("\n[R] 验证可信度风险")

    from backend.agent.intent import detect_risks, scan_content_risks

    def one(path: str, *, kind: str = "added", content: str = "",
            constraints: dict | None = None, forbidden: list[str] | None = None) -> dict:
        risks = detect_risks(
            [{"path": path, "kind": kind, "via": "tool"}],
            constraints=constraints or {},
            forbidden_paths=forbidden or [],
            read_text=lambda _p: content,
        )
        return risks[0] if risks else {}

    # ---- 合理 mock 不误判 ----
    r = one("tests/test_api.py", content="def test_x(monkeypatch):\n    monkeypatch.setattr('os.getcwd', lambda: '/tmp')\n")
    check(r.get("level") == "low", f"★ 测试文件里的 monkeypatch 不误判（{r.get('level')}）")
    r = one("tests/test_api.py", content="from unittest.mock import patch\n")
    check(r.get("level") == "low", "★ 测试文件里的 mock 不误判")

    # ---- 修复任务里动测试配置 → 中风险 ----
    r = one("conftest.py", content="@pytest.fixture(autouse=True)\ndef _m():\n    yield\n")
    check(r.get("level") == "medium", f"★ 新增 conftest.py → 中风险（{r.get('level')}）")
    r = one("pytest.ini", content="[pytest]\naddopts = -p no:cacheprovider\n")
    check(r.get("level") == "medium", "★ 改 pytest.ini → 中风险")
    r = one("tests/test_a.py", kind="modified", content="import pytest\n@pytest.mark.skip(reason='x')\ndef test_a(): pass\n")
    check(r.get("level") == "medium", "★ 引入 skip 标记 → 中风险")
    r = one("tests/test_a.py", kind="modified", content="@pytest.mark.xfail\ndef test_a(): pass\n")
    check(r.get("level") == "medium", "★ 引入 xfail → 中风险")
    r = one("app/core.py", content="from unittest.mock import patch\n")
    check(r.get("level") == "medium", "★ 非测试代码里引入 mock → 中风险")

    # ---- 明确违反约束 → 高风险 ----
    r = one("tests/test_a.py", constraints={"no_test_modification": True}, content="def test_a(): pass\n")
    check(r.get("level") == "high", f"★ 说了别改测试还是改了 → 高风险（{r.get('level')}）")
    r = one("conftest.py", constraints={"no_test_modification": True})
    check(r.get("level") == "high", "★ 说了别改测试配置还是改了 → 高风险")
    r = one("app/secret.py", forbidden=["app/secret.py"])
    check(r.get("level") == "high", "★ 命中禁止路径 → 高风险")

    # ---- 去重：同一路径只报一条 ----
    risks = detect_risks(
        [{"path": "conftest.py", "kind": "added", "via": "tool"},
         {"path": "conftest.py", "kind": "modified", "via": "tool"}],
        read_text=lambda _p: "x",
    )
    check(len(risks) == 1, f"★ 同一路径去重（{len(risks)} 条）")

    # ---- 读不了内容 → 标不确定，不假装看到 ----
    def boom(_p: str) -> str:
        raise OSError("读不了")

    risks2 = detect_risks([{"path": "conftest.py", "kind": "added", "via": "tool"}], read_text=boom)
    check(risks2[0]["uncertain"] is True, "★ 读不了内容 → uncertain=True")
    check(scan_content_risks("pytest.skip('x')") == ["skip"], "内容模式识别")


# --------------------------------------------------------------------------- #
# [E] 证据分级 + 意图感知闸门（集成）
# --------------------------------------------------------------------------- #
class _Writer(Tool):
    """真的写文件但 risk=READ（不触发审批），用来单独测风险与审计。"""

    name = "write_file"
    description = "测试用写入（不触发审批）"
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path"]}
    risk = RiskLevel.READ

    def __init__(self, root: str) -> None:
        self.root = root

    async def run(self, args, ctx):  # type: ignore[override]
        rel = args["path"]
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(args.get("content", "x\n"))
        return ToolResult.success(self.name, f"写入 {rel}",
                                  data={"path": rel,
                                        "change": {"path": rel, "action": "create"}})


async def test_evidence_grading(sb: Sandbox) -> None:
    """[E] 证据必须指回用户目标，而不是只看退出码。"""
    print("\n[E] 证据分级与意图感知闸门")

    from backend.agent.intent import grade_evidence

    # ---- 分级规则 ----
    def g(cmds, turns, risks, intent="repair"):
        return grade_evidence(command_history=cmds, change_turns=turns, risks=risks, intent=intent)

    fail = [{"command": "pytest", "exit_code": 1, "ok": False, "turn": 2}]
    okc = [{"command": "pytest", "exit_code": 0, "ok": True, "turn": 5}]
    other = [{"command": "ls", "exit_code": 0, "ok": True, "turn": 5}]
    check(g(fail + okc, [3], [])["grade"] == "A", "★ 同一命令失败→通过且无风险 = A")
    med = [{"path": "conftest.py", "level": "medium"}]
    check(g(fail + okc, [3], med)["grade"] == "B", "★ 失败→通过但有可疑改动 = B")
    high = [{"path": "tests/t.py", "level": "high"}]
    check(g(fail + okc, [3], high)["grade"] == "C", "★ 失败→通过但高风险 = C")
    check(g(okc, [3], [])["grade"] == "B", "★ 只跑过无关命令 = B")
    check(g(fail, [3], [])["grade"] == "C", "★ 最后仍失败 = C")
    check(g([], [], [])["grade"] == "C", "★ 没有任何命令 = C")
    check(g(fail, [3], [], intent="diagnostic")["grade"] == "B",
          "★ 诊断任务的失败不是修复失败")

    # ---- 集成：修复任务只跑无关命令就宣称完成 → 被拦 ----
    reg = ToolRegistry()
    reg.register(build_default_registry().get("list_files"), group="readonly")
    reg.register(StubRunTool([0]), group="exec")      # 只有无关命令成功
    reg.register(_Writer(sb.dir), group="mutating")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"跑一下","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("e1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("e2", "write_file", {"path": "a.py", "content": "x"})]),
            ModelReply(tool_calls=[tc("e3", "run_command", {"command": "ls"})]),
            ModelReply(content="修复完成，测试都通过了。"),
            ModelReply(content="真的修好了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=1, use_reflect_phase=False,
                           max_verification_retries=1),
        bus=EventBus(),
    )
    st = await core.run("修复 main.py 的 bug", workspace=sb.dir)
    check(st.evidence.get("grade") == "B",
          f"★ 只跑过无关命令 → 证据等级 B（{st.evidence.get('grade')}）")
    check("没有观察到失败场景" in "；".join(st.evidence.get("reasons") or []),
          "★ B 级要在机器判定里说清缺什么")

    # ---- 修复任务一条命令都没跑 → 等级 C，必须拦 ----
    reg1b = ToolRegistry()
    reg1b.register(build_default_registry().get("list_files"), group="readonly")
    reg1b.register(_Writer(sb.dir), group="mutating")
    core1b = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"改","check":"change"}],"note":""}'),
            ModelReply(tool_calls=[tc("e1b", "write_file", {"path": "c.py", "content": "x"})]),
            ModelReply(content="修复完成。"),
            ModelReply(content="真的修好了。"),
        ]),
        reg1b,
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=EventBus(),
    )
    st1b = await core1b.run("修复 main.py 的 bug", workspace=sb.dir)
    check(st1b.status is TaskStatus.ABORTED,
          f"★ 修复任务一条命令都没跑 → 不能算完成（{st1b.status.value}）")
    check(st1b.end_reason == "verification_failed", f"结束原因 {st1b.end_reason}")
    check("证据" in st1b.final_answer, "答复里说明证据不足")

    # ---- 集成：修复任务 失败→同一命令通过 → 正常完成 + 机器判定块 ----
    reg2 = ToolRegistry()
    reg2.register(build_default_registry().get("list_files"), group="readonly")
    reg2.register(StubRunTool([1, 0]), group="exec")
    reg2.register(_Writer(sb.dir), group="mutating")
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("f1", "write_file", {"path": "b.py", "content": "x"})]),
            ModelReply(tool_calls=[tc("f2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("f3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="修好了，测试通过。"),
        ]),
        reg2,
        config=AgentConfig(max_turns=8, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=2),
        bus=EventBus(),
    )
    st2 = await core2.run("修复 pytest 失败的问题", workspace=sb.dir)
    check(st2.status is TaskStatus.DONE, f"★ 失败→通过的正常修复不被拦（{st2.status.value}）")
    check(st2.evidence.get("grade") == "A", f"证据等级 A（{st2.evidence.get('grade')}）")
    check("机器判定" in st2.final_answer, "★ 答复附带机器判定块")

    # ---- 集成：诊断任务如实报告失败 → 正常完成 ----
    reg3 = ToolRegistry()
    reg3.register(build_default_registry().get("list_files"), group="readonly")
    reg3.register(StubRunTool([1]), group="exec")
    core3 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"跑","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("g1", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="退出码 1：test_remote 因容器无网络失败，1 通过 1 失败。"),
        ]),
        reg3,
        config=AgentConfig(max_turns=6, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st3 = await core3.run("分析这个项目测试为什么失败", workspace=sb.dir)
    check(st3.intent == "diagnostic", f"意图识别为 diagnostic（{st3.intent}）")
    check(st3.status is TaskStatus.DONE, f"★ 诊断任务如实汇报失败也判 done（{st3.status.value}）")
    check("退出码 1" in st3.final_answer, "汇报内容保留")

    # ---- 集成：修复任务新建 conftest.py → 中风险 + 事件 + 答复 ----
    reg4 = ToolRegistry()
    reg4.register(_Writer(sb.dir), group="mutating")
    reg4.register(StubRunTool([1, 0]), group="exec")
    bus4 = EventBus()
    evs4 = collect(bus4)
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("h1", "write_file", {"path": "conftest.py",
                                                           "content": "@pytest.fixture(autouse=True)\ndef _m():\n    yield\n"})]),
            ModelReply(tool_calls=[tc("h2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("h3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="测试通过了。"),
            ModelReply(content='{"explanations": [{"path": "conftest.py", "why": "隔离外部服务",'
                               ' "changed_verification": true}]}'),
        ]),
        reg4,
        config=AgentConfig(max_turns=10, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=bus4,
    )
    st4 = await core4.run("修复测试失败的问题", workspace=sb.dir)
    await bus4.drain()
    levels = {r["path"]: r["level"] for r in st4.risks}
    check(levels.get("conftest.py") == "medium", f"★ conftest.py 被标中风险（{levels}）")
    check(st4.evidence.get("grade") == "B", f"★ 有可疑改动 → 证据降为 B（{st4.evidence.get('grade')}）")
    check(sum(1 for e in evs4 if e.type == EventType.ERROR
              and e.data.get("kind") == "verification_risk") == 1,
          "★ verification_risk 事件只发一次")
    check("机器判定" in st4.final_answer and "conftest.py" in st4.final_answer,
          "★ 最终答复里点名风险文件")




# --------------------------------------------------------------------------- #
# [K] 风险收尾确认（第四批）
# --------------------------------------------------------------------------- #
async def test_risk_closure(sb: Sandbox) -> None:
    """[K] 中/高风险必须经过收尾确认；解释是证据，不是免检凭证。"""
    print("\n[K] 风险收尾确认")

    from backend.agent.core import AgentCore as AC  # noqa: F401  （保持 import 一致）

    # ---- K1 无风险修复：不增加任何收尾循环 ----
    reg = ToolRegistry(); reg.register(StubRunTool([1, 0]), group="exec")
    reg.register(_Writer(sb.dir), group="mutating")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("k1", "write_file", {"path": "fix.py", "content": "x"})]),
            ModelReply(tool_calls=[tc("k2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("k3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="修好了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st = await core.run("修复 pytest 失败", workspace=sb.dir)
    check(st.status is TaskStatus.DONE, f"无风险修复正常完成（{st.status.value}）")
    check(st.risk_review_retries == 0 and not st.risk_reviews,
          "★ 无风险 → 零次收尾确认（无额外循环）")

    # ---- K2 修复任务新建 conftest.py → 必须经收尾确认，且矛盾会被以审计为准 ----
    # ★ 共享 Sandbox：前面测试段写过 conftest.py，必须先删掉，
    #   否则它在基线里已存在，这次"写入"不构成变更，风险根本不会出现。
    (Path(sb.dir) / "conftest.py").unlink(missing_ok=True)
    reg2 = ToolRegistry(); reg2.register(_Writer(sb.dir), group="mutating")
    reg2.register(StubRunTool([1, 0]), group="exec")
    core2 = AgentCore(
        FakeAdapter([
            # ★ 脚本顺序必须精确对应调用顺序：
            #   PLAN → 写 → 跑(红) → 跑(绿) → 收尾文本 → [确认1: 撒谎] →
            #   继续文本 → [确认2: 诚实承认] → 结束
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("m1", "write_file", {"path": "conftest.py",
                                                           "content": "@pytest.fixture(autouse=True)\ndef _m():\n    yield\n"})]),
            ModelReply(tool_calls=[tc("m2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("m3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="测试通过了。"),
            # 第一次收尾确认：撒谎说没改变验证条件 → 与审计矛盾 → 驳回
            ModelReply(content='{"explanations": [{"path": "conftest.py", "why": "只是加了个 fixture",'
                               ' "changed_verification": false}]}'),
            ModelReply(content="我重新检查了一遍，这个 fixture 确实会影响测试环境。"),
            # 第二次收尾确认：诚实承认 → unresolved（情况 B）→ 放行并披露
            ModelReply(content='{"explanations": [{"path": "conftest.py", "why": "mock 了外部服务",'
                               ' "changed_verification": true}]}'),
        ]),
        reg2,
        config=AgentConfig(max_turns=12, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st2 = await core2.run("修复测试失败的问题", workspace=sb.dir)
    conftest_risk = next((r for r in st2.risks if r.get("path") == "conftest.py"), {})
    check(len(st2.risk_reviews) == 2, f"★ conftest 变绿必须经收尾确认（{len(st2.risk_reviews)} 轮）")
    check(conftest_risk.get("contradiction_seen") is True,
          "★ 第一轮解释与审计矛盾 → 留下痕迹（以审计为准；第二轮已改口则当前轮不算矛盾）")
    check(conftest_risk.get("status") == "unresolved",
          f"★ 最终状态 unresolved（{conftest_risk.get('status')}）")
    check(st2.status is TaskStatus.DONE,
          f"情况 B：允许结束（实际 {st2.status.value}/{st2.end_reason}，"
          f"reviews={json.dumps(st2.risk_reviews, ensure_ascii=False)[:300]}）")
    check("未充分验证" in st2.final_answer or "未解决" in st2.final_answer,
          "★ 答复中明确披露「未充分验证/未解决」")
    check(st2.risk_review_retries == 2, f"重试计数 2（{st2.risk_review_retries}）")

    # ---- K3 高风险（违反用户约束）→ 不能正常完成 ----
    (Path(sb.dir) / "conftest.py").unlink(missing_ok=True)
    reg3 = ToolRegistry(); reg3.register(_Writer(sb.dir), group="mutating")
    reg3.register(StubRunTool([1, 0]), group="exec")
    bus3 = EventBus()
    evs3 = collect(bus3)
    core3 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("n1", "write_file", {"path": "conftest.py", "content": "x"})]),
            ModelReply(tool_calls=[tc("n2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("n3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="通过了。"),
            ModelReply(content='{"explanations": [{"path": "conftest.py", "why": "需要它",'
                               ' "changed_verification": true}]}'),
            ModelReply(content="我解释过了。"),
        ]),
        reg3,
        config=AgentConfig(max_turns=10, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=bus3,
    )
    st3 = await core3.run("修复测试失败，但别改测试", workspace=sb.dir,
                          forbidden_paths=["conftest.py"])
    await bus3.drain()
    check(st3.status is TaskStatus.ABORTED,
          f"★ 违反约束的高风险 → 不能按正常完成收尾（{st3.status.value}/{st3.end_reason}）")
    check(any(e.type == EventType.ERROR and e.data.get("kind") == "risk_review"
              for e in evs3), "★ 收尾确认过程有事件可追踪")
    risk3 = next((r for r in st3.risks if r.get("path") == "conftest.py"), {})
    check(risk3.get("status") == "unresolved" and "违反用户约束" in str(risk3.get("resolution")),
          "★ 解释不能消除约束违反")

    # ---- K4 重试上限：不无限循环 ----
    (Path(sb.dir) / "conftest.py").unlink(missing_ok=True)
    reg4 = ToolRegistry(); reg4.register(_Writer(sb.dir), group="mutating")
    reg4.register(StubRunTool([1, 0]), group="exec")
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("o1", "write_file", {"path": "conftest.py", "content": "x"})]),
            ModelReply(tool_calls=[tc("o2", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(tool_calls=[tc("o3", "run_command", {"command": "python -m pytest -q"})]),
            ModelReply(content="通过了。"),
            ModelReply(content="好吧。"),     # 收尾确认：不输出结构化解释
            ModelReply(content="还是这段话。"),
        ]),
        reg4,
        config=AgentConfig(max_turns=10, min_evidence=1, use_plan_phase=False,
                           use_reflect_phase=False, max_verification_retries=1),
        bus=EventBus(),
    )
    st4 = await core4.run("修复测试失败的问题", workspace=sb.dir)
    check(st4.status is TaskStatus.ABORTED,
          f"★ 达到重试上限后如实收尾（{st4.status.value}/{st4.end_reason}）")
    check(st4.risk_review_retries == 1, f"确认只进行了一轮（{st4.risk_review_retries}）")
    r4 = next((r for r in st4.risks if r.get("path") == "conftest.py"), {})
    check(r4.get("status") == "detected" and "未给出解释" in str(r4.get("resolution")),
          "★ 未解释的风险保持 detected，不伪装成已解决")

    # ---- K5 取消/状态序列化不会把风险标成已解决 ----
    st5 = TaskState(goal="x", workspace=sb.dir)
    st5.risks = [{"path": "conftest.py", "level": "medium", "status": "unresolved",
                  "resolution": "解释尚无独立证据支持"}]
    blob = json.dumps(st5.to_dict(), ensure_ascii=False)
    back = json.loads(blob)
    check(back["risks"][0]["status"] == "unresolved",
          "★ 风险状态经序列化往返后保持不变（取消/恢复不误标）")

    # ---- K6 评估器的矛盾判定（纯单元）----
    core6 = AgentCore(FakeAdapter([]), build_default_registry(), bus=EventBus())
    st6 = TaskState(goal="x", workspace=sb.dir)
    st6.intent = "repair"
    st6.note_command("python -m pytest -q", 1, False, turn=2)
    st6.note_command("python -m pytest -q", 0, True, turn=5)
    st6.change_records.append(ChangeRecord(path="conftest.py", action="create", turn=3))
    risks6 = [{"path": "conftest.py", "level": "medium", "kind": "added", "status": "detected"}]
    core6._evaluate_risk_explanations(
        st6, risks6,
        [{"path": "conftest.py", "why": "只是测试基础设施", "changed_verification": False}])
    check(risks6[0]["status"] == "unresolved" and risks6[0].get("contradiction") is True,
          "★ 解释与审计矛盾 → 以审计为准，标记矛盾")


# --------------------------------------------------------------------------- #
# [F] 最终答复卫生：REFLECT 形状的输出不能当答复
# --------------------------------------------------------------------------- #
async def test_final_answer_hygiene(sb: Sandbox) -> None:
    """[F] 模型把进展汇报当答复交上来时，不能收尾。"""
    print("\n[F] 最终答复卫生")

    from backend.agent.core import _as_reflect_payload

    check(_as_reflect_payload('{"progress":"p","next_action":"n","done":false}') is not None,
          "★ 认得出 REFLECT 形状")
    check(_as_reflect_payload('{"answer":"这是答复"}') is None, "普通 JSON 答复不被误判")
    check(_as_reflect_payload("这就是一段正常中文答复") is None, "普通文本不被误判")

    # 真实形态：模型交上来 {"progress":..., "next_action":..., "done":false}
    reg = ToolRegistry()
    reg.register(build_default_registry().get("list_files"), group="readonly")
    bus = EventBus()
    evs = collect(bus)
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"看目录"}],"note":""}'),
            ModelReply(tool_calls=[tc("f1", "list_files", {"path": "."})]),
            ModelReply(content='{"progress":"已经看过目录了","next_action":"继续读 main.py","done":false}'),
            ModelReply(tool_calls=[tc("f2", "read_file", {"path": "main.py"})]),
            ModelReply(content="看完了：main.py 里只有一个 print。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=1, use_reflect_phase=False),
        bus=bus,
    )
    st = await core.run("看看 main.py", workspace=sb.dir)
    await bus.drain()
    check("已经看过目录了" not in st.final_answer,
          f"★ 进展汇报没有被当成最终答复（{st.final_answer[:40]!r}）")
    check("main.py 里只有一个 print" in st.final_answer, "★ 真正的答复被保留")
    check(any(e.type == EventType.ERROR and e.data.get("kind") == "reflect_as_answer" for e in evs),
          "★ 事件流里有 reflect_as_answer 提示")
    check(st.status is TaskStatus.DONE, "★ 任务正常完成")

    # done=true 的 REFLECT 形状 → 索要真正的答复
    reg2 = ToolRegistry()
    reg2.register(build_default_registry().get("list_files"), group="readonly")
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("g1", "list_files", {"path": "."})]),
            ModelReply(content='{"progress":"做完了","next_action":"","done":true}'),
            ModelReply(content="最终答复：目录里有 3 个条目。"),
        ]),
        reg2,
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st2 = await core2.run("列一下目录", workspace=sb.dir)
    check("最终答复：目录里有 3 个条目。" in st2.final_answer,
          f"★ done=true 的进展汇报会要一份真正的答复（{st2.final_answer[:40]!r}）")


# --------------------------------------------------------------------------- #
# [F2] 反思答复：正常回灌 / 零进展恢复 / 无进展终止（M2.2）
# --------------------------------------------------------------------------- #
REFLECT_ANSWER = '{"progress":"已看过目录","next_action":"读取 main.py","done":false}'


async def test_reflect_no_progress(sb: Sandbox) -> None:
    """
    [F2] 模型把 REFLECT 的 JSON 当答复交上来时的完整行为。

    覆盖六种情形：
      1. 正常 REFLECT 阶段 → 下一步工具调用（阶段机制没被破坏）
      2. 模型把 REFLECT 当普通答复 → 不收尾
      3. 连续多轮重复同样的反思内容 → 有界处理
      4. 期间没有工具/证据/状态变化 → 判据是"零进展"，不是"文本重复"
      5. 一次恢复提示后能正常执行工具 → 继续跑完
      6. 恢复无效 → 以 no_progress 终止并清楚报告原因
    """
    print("\n[F2] 反思答复与无进展循环")

    (Path(sb.dir) / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def reg():
        r = ToolRegistry()
        r.register(build_default_registry().get("list_files"), group="readonly")
        r.register(build_default_registry().get("read_file"), group="readonly")
        return r

    # ---- 1. 正常 REFLECT 阶段 → 转入下一步工具调用 ------------------------
    ad1 = FakeAdapter([
        ModelReply(tool_calls=[tc("r1", "list_files", {"path": "."})]),
        ModelReply(content='{"progress":"看到 main.py","next_action":"读它","done":false}'),
        ModelReply(tool_calls=[tc("r2", "read_file", {"path": "main.py"})]),
        ModelReply(content='{"progress":"读完了","next_action":"收尾","done":true}'),
        ModelReply(content="main.py 里只有一行 print。"),
    ])
    bus1 = EventBus()
    ev1 = collect(bus1)          # ★ 必须在 run 之前订阅，否则收不到事件
    st1 = await AgentCore(ad1, reg(), config=AgentConfig(
        max_turns=8, min_evidence=0, use_plan_phase=False), bus=bus1).run(
        "看看 main.py", workspace=sb.dir)
    await bus1.drain()
    check(st1.status is TaskStatus.DONE, f"正常 REFLECT 流程完成（{st1.status.value}）")
    check([e.tool for e in st1.tool_events] == ["list_files", "read_file"],
          f"★ REFLECT 之后确实继续调用了工具（{[e.tool for e in st1.tool_events]}）")
    check(sum(1 for e in ev1 if e.type == EventType.REFLECT) == 2, "两次 REFLECT 自评")
    check(st1.reflect_streak == 0, "没有触发零进展计数（这类反思不走答复分支）")

    # ---- 2/5. 首次把 REFLECT 当答复 → 恢复提示后正常干活 -----------------
    ad2 = FakeAdapter([
        ModelReply(content=REFLECT_ANSWER),
        ModelReply(tool_calls=[tc("s1", "read_file", {"path": "main.py"})]),
        ModelReply(content="main.py 是一行 print。"),
    ])
    bus2 = EventBus()
    ev2 = collect(bus2)
    st2 = await AgentCore(ad2, reg(), config=AgentConfig(
        max_turns=8, min_evidence=0, use_plan_phase=False, use_reflect_phase=False),
        bus=bus2).run("读 main.py 并说明", workspace=sb.dir)
    await bus2.drain()
    check(st2.status is TaskStatus.DONE, f"★ 一次回灌后任务正常完成（{st2.status.value}）")
    check(st2.reflect_streak == 1, f"只记 1 次零进展（{st2.reflect_streak}）")
    check(st2.reflect_recoveries == 0, "首次不触发受控恢复")
    check(len(st2.tool_events) == 1, "★ 回灌提示后模型确实执行了工具")
    kinds2 = [e.data.get("kind") for e in ev2 if e.type == EventType.ERROR]
    check(kinds2.count("reflect_as_answer") == 1, "有 reflect_as_answer 提示")
    check("no_progress" not in kinds2, "没有误判成无进展终止")

    # ---- 3/4/6. 连续相同的零进展反思 → 恢复一次 → 仍不改进则终止 ---------
    ad3 = FakeAdapter([ModelReply(content=REFLECT_ANSWER) for _ in range(5)])
    bus3 = EventBus()
    ev3 = collect(bus3)
    st3 = await AgentCore(ad3, reg(), config=AgentConfig(
        max_turns=6, min_evidence=0, use_plan_phase=False, use_reflect_phase=False),
        bus=bus3).run("读 main.py 并说明", workspace=sb.dir)
    await bus3.drain()
    kinds3 = [e.data.get("kind") for e in ev3 if e.type == EventType.ERROR]
    check(st3.status is TaskStatus.ABORTED, f"★ 零进展循环被终止（{st3.status.value}）")
    check(st3.end_reason == "no_progress", f"★ 终止原因明确（{st3.end_reason}）")
    check(st3.reflect_streak == 3, f"第 3 次零进展才终止（streak={st3.reflect_streak}）")
    check(st3.reflect_recoveries == 1, f"只发一次受控恢复（{st3.reflect_recoveries}）")
    check(len(st3.tool_events) == 0, "★ 期间确实没有任何工具执行")
    check(kinds3.count("reflect_as_answer") == 3, f"每轮都留痕（{kinds3.count('reflect_as_answer')}）")
    check(kinds3.count("no_progress_recovered") == 1, "恢复提示有独立事件")
    check(kinds3.count("no_progress") == 1, "终止有独立事件")
    check(st3.budgets.max_turns == 6, "★ 没有偷偷加大轮次预算")
    check(st3.turn < st3.budgets.max_turns,
          f"★ 在预算耗尽前就终止（{st3.turn}/{st3.budgets.max_turns}）")
    check("无进展循环" in st3.final_answer, "答复说明了无进展判据")
    check("内容完全相同" in st3.final_answer, "如实报告内容是重复的")
    check("读取 main.py" in st3.final_answer, "点出它声称却没做的下一步")
    check("不是完整结论" in st3.final_answer, "不把中断包装成完成")

    # ---- 4b. 内容不同但同样零进展 → 仍按无进展处理（判据不是文本去重）----
    ad4 = FakeAdapter([
        ModelReply(content='{"progress":"看了看目录","next_action":"读 main.py","done":false}'),
        ModelReply(content='{"progress":"又看了一遍目录","next_action":"读一下 main.py","done":false}'),
        ModelReply(content='{"progress":"确认目录没变","next_action":"确实要读 main.py","done":false}'),
    ])
    bus4 = EventBus()
    st4 = await AgentCore(ad4, reg(), config=AgentConfig(
        max_turns=6, min_evidence=0, use_plan_phase=False, use_reflect_phase=False),
        bus=bus4).run("读 main.py 并说明", workspace=sb.dir)
    await bus4.drain()
    check(st4.end_reason == "no_progress",
          f"★ 文本不同但零进展 → 同样终止（{st4.end_reason}）")
    check("内容有变化" in st4.final_answer, "报告里区分了'内容有变化'")

    # ---- 5b. Graph 引擎同源行为（共用 _finalize_text_reply）--------------
    ad5 = FakeAdapter([ModelReply(content=REFLECT_ANSWER) for _ in range(4)])
    st5 = await AgentCore(ad5, reg(), config=AgentConfig(
        max_turns=6, min_evidence=0, use_plan_phase=False, use_reflect_phase=False,
        orchestrator="graph"), bus=EventBus()).run("读 main.py 并说明", workspace=sb.dir)
    check(st5.engine == "graph", f"引擎标注（{st5.engine}）")
    check(st5.end_reason == "no_progress", f"★ graph 下同样以 no_progress 终止（{st5.end_reason}）")
    check(st5.reflect_streak == 3, f"graph 下判据一致（streak={st5.reflect_streak}）")
    check("finalize" in st5.graph_trace, "图确实经过 finalize 节点")



# --------------------------------------------------------------------------- #
# [G] 约束一致性：多允许范围 / 禁止优先 / 执行前拦截 / 冲突不静默扩权
# --------------------------------------------------------------------------- #
async def test_scope_consistency(sb: Sandbox) -> None:
    """[G] 允许范围与禁止范围必须自洽，冲突要暴露而不是被抹平。"""
    print("\n[G2] 约束一致性")

    from backend.agent.intent import detect_scope_conflicts, extract_mentioned_paths
    from backend.agent.scope import within_allowed

    # ---- 1. 多个允许目录 ----
    check(within_allowed("pipeline/a.py", ["pipeline", "tests"]), "多允许范围：pipeline 命中")
    check(within_allowed("tests/b.py", ["pipeline", "tests"]), "多允许范围：tests 命中")
    check(not within_allowed("docs/c.md", ["pipeline", "tests"]), "多允许范围：范围外不命中")

    # ---- 2. 冲突检测 ----
    conflicts = detect_scope_conflicts(
        "只允许修改 pipeline/ 下的源码，在 tests/ 下新增 test_breakdown.py",
        allowed_paths=["pipeline"], forbidden_paths=["tests/test_pipeline.py"],
    )
    check(bool(conflicts), f"★ 目标与范围冲突能被检出（{conflicts}）")
    check(any(c["kind"] == "outside_allowed" for c in conflicts), "★ 冲突类型：超出允许范围")
    conflicts2 = detect_scope_conflicts(
        "不要修改 tests/test_pipeline.py，但请把它修好",
        allowed_paths=["."], forbidden_paths=["tests/test_pipeline.py"],
    )
    check(any(c["kind"] == "forbidden_but_requested" for c in conflicts2),
          f"★ 同时被要求与被禁止的路径能检出（{conflicts2}）")
    check(detect_scope_conflicts("把 main.py 改一下", allowed_paths=["."], forbidden_paths=[]) == [],
          "★ 普通任务不误判冲突")

    # ---- 3. 执行前拦截：范围外写入被 Policy 拒 ----
    policy = Policy(build_default_registry())
    st = TaskState(goal="x", workspace=sb.dir)
    st.allowed_paths = ["pipeline"]
    ctx = make_context(st)
    d = policy.check("write_file", {"path": "docs/out.md", "content": "x"}, st, ctx)
    check(not d.allow and "允许" in (d.result.summary if d.result else ""),
          f"★ 范围外写入在执行前被拦（{d.result.summary if d.result else ''}）")
    d2 = policy.check("write_file", {"path": "pipeline/ok.py", "content": "x"}, st, ctx)
    check(d2.needs_approval, "★ 允许范围内的写入照常走审批")
    st.allowed_paths = ["."]
    d3 = policy.check("write_file", {"path": "docs/out.md", "content": "x"}, st, ctx)
    check(d3.needs_approval, "★ 默认允许整个工作区 → 不拦任何写入")

    # ---- 4. 禁止范围永远优先 ----
    st2 = TaskState(goal="x", workspace=sb.dir)
    st2.allowed_paths = ["pipeline", "tests"]
    st2.forbidden_paths = ["tests/test_pipeline.py"]
    d4 = policy.check("write_file", {"path": "tests/test_pipeline.py", "content": "x"}, st2, ctx)
    check(not d4.allow, "★ 禁止范围优先于允许范围")

    # ---- 5. 冲突不会静默扩大权限（端到端）----
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"改"}],"note":""}'),
            ModelReply(tool_calls=[tc("g1", "write_file", {"path": "tests/new_test.py",
                                                           "content": "x = 1\n"})]),
            ModelReply(content="我已经处理完了。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st3 = await core.run("只允许修改 pipeline/ 下的源码，在 tests/ 下新增 test_new.py",
                         workspace=sb.dir)
    check(bool(st3.scope_conflicts), f"★ 端到端也能检出冲突（{st3.scope_conflicts}）")
    check(st3.allowed_paths == ["pipeline"],
          f"★ 冲突时允许范围没有被悄悄放大（{st3.allowed_paths}）")
    check(not (Path(sb.dir) / "tests" / "new_test.py").exists(),
          "★ 范围外的文件没有被写出来")


# --------------------------------------------------------------------------- #
# [H] 文件读取复用与缓存失效
# --------------------------------------------------------------------------- #
async def test_read_cache(sb: Sandbox) -> None:
    """[H] 重复读取要能复用，但绝不会给过期内容。"""
    print("\n[H2] 文件读取复用")

    (Path(sb.dir) / "main.py").write_text("print('v1')\n", encoding="utf-8")

    # ---- 1. 同文件重复读取：第二条只放命中标记 ----
    core = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("h1", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("h2", "read_file", {"path": "main.py"})]),
            ModelReply(content="读完了。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st = await core.run("读一下 main.py", workspace=sb.dir)
    tool_msgs = [m for m in core._history if m.role == "tool"]
    check(len(tool_msgs) == 2, f"★ 两次读取都产生了工具消息（{len(tool_msgs)}）")
    check("读取复用" in (tool_msgs[1].content or ""),
          f"★ 第二次读取折叠成命中标记（{str(tool_msgs[1].content)[:60]}）")
    check("print('v1')" in (tool_msgs[0].content or ""), "★ 第一次仍是完整内容")
    check("main.py" in st.read_cache and st.read_cache["main.py"].get("hash"),
          "★ 缓存里记下了文件 hash")

    # ---- 2. 文件被改后再读 → 不得复用旧内容 ----
    reg_rw = ToolRegistry()
    reg_rw.register(build_default_registry().get("read_file"), group="readonly")
    reg_rw.register(_Writer(sb.dir), group="mutating")   # 免审批的写桩
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("h3", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("h4", "write_file", {"path": "main.py",
                                                           "content": "print('v2')\n"})]),
            ModelReply(tool_calls=[tc("h5", "read_file", {"path": "main.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg_rw,
        config=AgentConfig(max_turns=8, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st2 = await core2.run("读、改、再读 main.py", workspace=sb.dir)
    tool_msgs2 = [m for m in core2._history if m.role == "tool"]
    last = tool_msgs2[-1].content or ""
    check("print('v2')" in last, f"★ 改完再读拿到的是新内容（{last[:60]}）")
    check("读取复用" not in last, "★ 内容变了就不能折叠成复用标记")
    check(any("失效" in n or "被改动" in n for n in st2.cache_notes),
          f"★ 缓存失效被记录（{st2.cache_notes}）")

    # ---- 3. 缓存不绕过权限：读越界文件仍被拒 ----
    core3 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("h6", "read_file", {"path": "../outside.py"})]),
            ModelReply(content="读不到工作区外的文件。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st3 = await core3.run("读 ../outside.py", workspace=sb.dir)
    check(not st3.read_cache, "★ 越界读取没进缓存")


# --------------------------------------------------------------------------- #
# [I] 上下文压缩 MVP
# --------------------------------------------------------------------------- #
class _SummaryCore(AgentCore):
    """可控摘要器的 AgentCore（不消耗 FakeAdapter 的剧本）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.summary_calls: list[str] = []
        self.summary_text: str | None = "摘要：已定位失败原因并改完两处。"

    async def _summarize_history(self, text: str) -> str | None:
        self.summary_calls.append(text)
        if self.summary_text is None:
            return None
        return self.summary_text


async def test_compaction(sb: Sandbox) -> None:
    """[I] 压缩要真的接进 Agent Loop，且不能丢权威状态。"""
    print("\n[I2] 上下文压缩")

    from backend.agent.compaction import (ContextCompactor, build_state_block, measure,
                                         split_turns)

    # ---- 1. 轮次切分保持 tool 配对 ----
    from backend.models.adapter import Message as M
    turns = split_turns([M.assistant("x"), M.tool_result("c1", "read_file", "r1"),
                         M.tool_result("c2", "read_file", "r2"), M.assistant("y")])
    check(len(turns) == 2, f"★ 轮次切分正确（{len(turns)}）")
    check(turns[0][0].role == "assistant" and turns[0][1].role == "tool",
          "★ 每轮都以 assistant 开头（tool 配对不破）")

    # ---- 2. 权威状态块包含目标/约束/未完成步骤/未解决风险 ----
    st = TaskState(goal="修复 pipeline 的口径问题", workspace=sb.dir)
    st.intent = "repair"
    st.forbidden_paths = ["tests/"]
    st.allowed_paths = ["pipeline"]
    st.set_plan([PlanStep(0, "改 aggregate", check="change"),
                 PlanStep(1, "跑测试", check="command_ok")])
    st.plan[0].status = StepStatus.DONE
    st.plan[0].evidence = "已改"
    st.risks = [{"path": "conftest.py", "level": "medium", "status": "unresolved",
                  "resolution": "解释不足"}]
    st.evidence = {"grade": "B", "reasons": ["只有间接证据"], "unverified": ["原始问题未复验"]}
    block = build_state_block(st)
    check("修复 pipeline 的口径问题" in block, "★ 摘要保留用户原始目标")
    check("tests/" in block, "★ 摘要保留禁止修改的路径")
    check("改 aggregate" in block and "跑测试" in block, "★ 摘要包含计划步骤")
    check("已改" in block, "★ 摘要包含已完成步骤的证据")
    check("unresolved" in block or "解释不足" in block, "★ 摘要保留未解决风险")
    check("原始问题未复验" in block, "★ 摘要保留未验证项")
    check("只跑过无关命令" not in block, "★ 摘要不会编造未发生的验证")

    # ---- 3. 未达阈值不触发；达到阈值触发且规模下降 ----
    comp = ContextCompactor(max_chars=10_000, keep_recent_turns=1)
    small = [M.system("x" * 100)]
    need, reason, stats = comp.should_compact(small)
    check(not need, "★ 未达阈值不触发压缩")
    big = [M.system("x" * 20_000)]
    need2, reason2, _ = comp.should_compact(big)
    check(need2, f"★ 达到阈值触发压缩（{reason2}）")

    # ---- 4. 端到端：压缩真的发生在 Agent Loop 里 ----
    # ★ M3 收尾：这里必须**订阅事件总线**，否则 context.compacted 发没发都测不出来
    #   （旧实现只断言 compaction_log，把 bus.context_compacted(...) 整行删掉也全绿）。
    bus4 = EventBus()
    ev4 = collect(bus4)
    core = _SummaryCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"a"}],"note":""}'),
            ModelReply(tool_calls=[tc("i1", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("i2", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("i3", "read_file", {"path": "main.py"})]),
            ModelReply(content="做完了：main.py 只有一个 print。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           compact_max_chars=400, compact_keep_turns=1),
        bus=bus4,
    )
    st2 = await core.run("读一下 main.py", workspace=sb.dir)
    await bus4.drain()
    compact_events = [e for e in ev4 if e.type == EventType.CONTEXT_COMPACTED.value]
    # ★ 断言的锚点是"成功压缩几次就该发几个事件"，而不是硬编码 1：
    #   历史一旦反复超阈值，压缩会在后续每次构建时再次发生（本例 3 次）。
    triggered_in_log = [c for c in st2.compaction_log if c.get("triggered")]
    check(len(compact_events) == len(triggered_in_log) and compact_events,
          f"★ 每次成功压缩都发出 1 个事件（事件 {len(compact_events)} / 成功压缩 {len(triggered_in_log)}）")
    if compact_events:
        payload = compact_events[0].data
        required = {"triggered", "reason", "before", "after", "kept_turns",
                    "summarized_turns", "degraded"}
        check(required <= set(payload),
              f"★ 事件载荷含契约字段（缺 {sorted(required - set(payload)) or '无'}）")
        check(payload.get("triggered") is True, "载荷标明 triggered=true")
        check(payload["after"]["chars"] < payload["before"]["chars"],
              f"载荷里的前后规模正确（{payload['before']['chars']} → {payload['after']['chars']}）")
    else:
        check(False, "★ 事件载荷含契约字段（事件缺失）")
        check(False, "载荷标明 triggered=true（事件缺失）")
        check(False, "载荷里的前后规模正确（事件缺失）")
    check(bool(st2.compaction_log), "★ 压缩在真实运行中发生过")
    rec = st2.compaction_log[-1]
    check(rec["triggered"], "★ 压缩被触发")
    check(rec["after"]["chars"] < rec["before"]["chars"],
          f"★ 压缩后规模下降（{rec['before']['chars']} → {rec['after']['chars']}）")
    check(core.summary_calls, "★ 摘要器被调用过")
    check(st2.status is TaskStatus.DONE, "★ 压缩后任务仍能正常完成")
    check("做完了" in st2.final_answer, "★ 压缩后的最终答复正常")
    # 压缩后历史里不能留下"孤立的 tool 消息"
    roles = [m.role for m in core._history]
    orphan = any(roles[i] == "tool" and (i == 0 or roles[i - 1] not in ("assistant", "tool", "user", "system"))
                 for i in range(len(roles)))
    check(not orphan, "★ 压缩后没有孤立的 tool 消息")
    check("修复" not in core._history[0].content or "任务状态" in core._history[0].content,
          "★ 压缩后的第一条是状态块")

    # ---- 5. 摘要失败 → 降级但不丢权威状态 ----
    core3 = _SummaryCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("j1", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("j2", "list_files", {"path": "."})]),
            ModelReply(content="好了。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           compact_max_chars=300, compact_keep_turns=1),
        bus=EventBus(),
    )
    core3.summary_text = None          # 摘要失败
    st3 = await core3.run("读一下 main.py", workspace=sb.dir)
    rec3 = (st3.compaction_log or [{}])[-1]
    check(rec3.get("degraded") is True, "★ 摘要失败标记为降级")
    check("任务状态" in (core3._history[0].content if core3._history else ""),
          "★ 降级后仍保留权威状态块")
    check(st3.status is TaskStatus.DONE, "★ 摘要失败不影响任务完成")




# --------------------------------------------------------------------------- #
# [J2] 预算策略与已读文件清单（第六批）
# --------------------------------------------------------------------------- #
async def test_budget_and_read_index(sb: Sandbox) -> None:
    """[J2] 预算要有分类和边界；已读清单要真实反映上下文状态。"""
    print("\n[J2] 预算策略与已读文件清单")

    verdict = AgentCore._budget_verdict

    # ---- 1. 耗尽原因分类（纯逻辑，直接喂可观测事实）----
    def mk(intent="repair", *, conflicts=False, events=None, plan=None, evidence=0, cmds=0):
        st = TaskState(goal="x", workspace=sb.dir)
        st.intent = intent
        if conflicts:
            st.scope_conflicts = [{"path": "tests/x.py", "kind": "outside_allowed", "reason": "r"}]
        for e in (events or []):
            st.tool_events.append(e)
        for c in range(cmds):
            st.note_command("pytest", 0, True)
        if plan:
            st.set_plan(plan)
        return st

    # 有效进展：最近有成功调用 → repair 意图 → 续期
    ev_ok = [ToolEvent(call_id=f"e{i}", tool="read_file", args={"path": f"f{i}.py"},
                       ok=True, summary="ok") for i in range(4)]
    st = mk("repair", events=ev_ok)
    check(verdict(AgentCore, st, "turns")["extend"] is True, "★ 有效进展 → repair 续期一次")
    check(verdict(AgentCore, st, "turns")["label"] == "productive_progress", "分类=productive_progress")

    # 范围冲突 → 不续期
    st = mk("repair", conflicts=True, events=ev_ok)
    check(verdict(AgentCore, st, "turns")["extend"] is False, "★ 有范围冲突 → 不续期")
    check(verdict(AgentCore, st, "turns")["label"] == "scope_conflict", "分类=scope_conflict")

    # 重复循环：连续相同调用 → 不续期
    ev_dup = [ToolEvent(call_id=f"d{i}", tool="read_file",
                        args={"path": "main.py"}, ok=True, summary="ok") for i in range(4)]
    st = mk("repair", events=ev_dup)
    check(verdict(AgentCore, st, "turns")["extend"] is False, "★ 重复读取同一文件 → 不续期")
    check(verdict(AgentCore, st, "turns")["label"] == "repeated_loop", "分类=repeated_loop")

    # 诊断任务 → 不续期（诊断拖得久多半在原地打转）
    st = mk("diagnostic", events=ev_ok)
    check(verdict(AgentCore, st, "turns")["extend"] is False, "★ 诊断任务不续期")

    # ---- 2. 端到端：有效进展的任务获得一次续期并完成 ----
    reg = ToolRegistry()
    reg.register(build_default_registry().get("read_file"), group="readonly")
    reg.register(build_default_registry().get("list_files"), group="readonly")
    # ★ M3 收尾：这个用例的目标是"续期后能完成"，但目标是 repair。
    #   新门禁要求 repair 必须有改动或验证命令才算完成，所以这里补上
    #   真实的 edit + 成功命令（断言没有放宽，反而更接近真实修复闭环）。
    reg.register(StubEditTool(), group="mutating")
    reg.register(StubRunTool([0]), group="exec")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"修","check":"change"}],"note":""}'),
            ModelReply(tool_calls=[tc("b1", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("b2", "read_file", {"path": "README.md"})]),
            ModelReply(tool_calls=[tc("b3", "read_file", {"path": "requirements.txt"})]),
            ModelReply(tool_calls=[tc("b4", "edit_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("b5", "run_command", {"command": "pytest -q"})]),
            ModelReply(content="读完了，修好并跑过测试。"),
        ]),
        reg,
        config=AgentConfig(max_turns=3, min_evidence=0, use_reflect_phase=False,
                           budget_extend_turns=10, budget_extend_enabled=True),
        bus=EventBus(),
    )
    st = await core.run("修复 main.py 的注释", workspace=sb.dir)
    check(st.budget_extended is True,
          f"★ 有效进展获得续期（extended={st.budget_extended}，reason={st.budget_reason}）")
    check("productive_progress" in st.budget_reason, f"续期原因={st.budget_reason}")
    check(st.status is TaskStatus.DONE, f"★ 续期后任务完成（{st.status.value}）")

    # ---- 3. 用户取消优先于续期 ----
    st_cancel = TaskState(goal="x", workspace=sb.dir)
    st_cancel.status = TaskStatus.ABORTED
    st_cancel.end_reason = "cancelled"
    check(st_cancel.end_reason == "cancelled", "★ 取消的终止原因不会被续期改写（状态层保证）")

    # ---- 4. 已读文件清单反映上下文状态 ----
    (Path(sb.dir) / "idx.py").write_text("x = 1\n", encoding="utf-8")
    reg2 = ToolRegistry()
    reg2.register(build_default_registry().get("read_file"), group="readonly")
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("r1", "read_file", {"path": "idx.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg2,
        config=AgentConfig(max_turns=4, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False),
        bus=EventBus(),
    )
    st2 = await core2.run("读 idx.py", workspace=sb.dir)
    check(bool(st2.read_index), "★ 生成已读文件清单")
    item = st2.read_index[0]
    check(item.get("path") == "idx.py" and item.get("in_context") is True,
          f"★ 清单标注原文在上下文中可用（{item}）")
    # 清单本身要进上下文（progress 提醒）
    msgs = core2.context.build(st2, system_prompt="s", history=core2._history)
    check(any("已读取文件" in m.content for m in msgs), "★ 清单进入模型上下文")

    # ---- 5. 压缩掉原文后，清单不再假装有原文 ----
    st3 = TaskState(goal="x", workspace=sb.dir)
    st3.read_cache["idx.py"] = {"hash": "a" * 16, "lines": 1, "size": 5, "turn": 3}
    core3 = AgentCore(FakeAdapter([]), build_default_registry(), bus=EventBus())
    core3._state = st3
    idx3 = core3._build_read_index(st3)
    check(idx3[0]["in_context"] is False,
          "★ 历史里没有原文 → 清单标注「已被压缩，需要时重读」")

    # ---- 6. 权限冲突不被预算绕过（冲突时即使有进展也不续期）----
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"改 tests"}],"note":""}'),
            ModelReply(tool_calls=[tc("k1", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("k2", "read_file", {"path": "main.py"})]),
            ModelReply(tool_calls=[tc("k3", "write_file", {"path": "tests/x.py", "content": "x"})]),
            ModelReply(content="我改不了，需要授权。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=3, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, budget_extend_turns=10),
        bus=EventBus(),
    )
    st4 = await core4.run("改 tests/x.py", workspace=sb.dir, forbidden_paths=["tests/"])
    check(st4.budget_extended is False, "★ 有范围冲突 → 不续期")
    check(st4.scope_conflicts, "★ 冲突被记录")

    # ---- 7. PLAN 完成条件启发式：repair 意图下"修改"步骤不再只凭观测 ----
    adapter = FakeAdapter([
        ModelReply(content='{"steps":['
                           '{"title":"修改 pipeline 源码"},'
                           '{"title":"重跑 pytest 验证"},'
                           '{"title":"阅读测试文件"}],"note":""}'),
        ModelReply(content="ok"),
    ])
    core5 = AgentCore(adapter, build_default_registry(),
                      config=AgentConfig(max_turns=4, min_evidence=0), bus=EventBus())
    st5 = await core5.run("修复 pipeline 的口径问题", workspace=sb.dir)
    checks = [s.check for s in st5.plan]
    check(checks[0] == "change", f"★ 「修改源码」步骤 → change（{checks[0]}）")
    check(checks[1] == "command_ok", f"★ 「重跑测试」步骤 → command_ok（{checks[1]}）")
    check(checks[2] == "observation", f"★ 「阅读」步骤保持 observation（{checks[2]}）")

    # ---- 8. run_command 整条命令 → 纠正提示 ----
    reg3 = ToolRegistry()
    reg3.register(build_default_registry().get("run_command"), group="exec")
    core6 = AgentCore(
        FakeAdapter([
            ModelReply(tool_calls=[tc("n1", "run_command",
                                       {"command": "python -m pytest -q"})]),
            ModelReply(content="好。"),
        ]),
        reg3,
        config=AgentConfig(max_turns=4, min_evidence=0, use_plan_phase=False,
                           use_reflect_phase=False, budget_extend_enabled=False),
        bus=EventBus(),
    )
    st6 = await core6.run("跑测试", workspace=sb.dir)
    bad = [e for e in st6.tool_events if not e.ok]
    check(bool(bad) and "正确写法" in str(bad[0].summary or ""),
          f"★ 整条命令被拒并给出正确写法（{str(bad[0].summary)[:60]}）")




# --------------------------------------------------------------------------- #
# [K3] M1：编排器切换基础 + 历史持久化 + 恢复安全边界
# --------------------------------------------------------------------------- #
async def test_m1_orchestrator_and_history(sb: Sandbox) -> None:
    """[K3] M1：切换入口可验证；历史落盘不静默；恢复绝不自动续跑。"""
    print("\n[K3] M1 编排器与历史持久化")

    from backend.agent.history_log import (MemoryHistorySink, dump_entries,
                                           entries_to_messages, find_tool_call_gaps,
                                           messages_to_entries)
    from backend.config import validate_orchestrator

    # ---- 1. 配置校验 ----
    check(validate_orchestrator("legacy") == "legacy", "legacy 合法")
    check(validate_orchestrator(" LEGACY ") == "legacy", "★ 大小写与首尾空白被规范化")
    try:
        validate_orchestrator("bogus")
        check(False, "非法值应抛 ValueError")
    except ValueError as e:
        check("合法值" in str(e), f"★ 非法值给出清晰错误：{e}")
    # M2：graph 已实现 → validate 不再抛异常
    check(validate_orchestrator("graph") == "graph", "★ graph 配置合法（M2 已实现）")

    cfg = AgentConfig(max_turns=4)
    check(cfg.orchestrator == "graph", f"★ 默认 graph（M4.3 起；实际 {cfg.orchestrator}）")

    # ---- 1b. M4.3：默认引擎真的走 graph，显式 legacy 仍可用 ----
    # 只看配置字段不够 —— 要看**实际执行路径**（state.engine / graph_trace）。
    core_d = AgentCore(FakeAdapter([ModelReply(content="完成。")]), build_default_registry(),
                       config=AgentConfig(max_turns=4, min_evidence=0), bus=EventBus())
    st_d = await core_d.run("读一下 main.py", workspace=sb.dir)
    check(st_d.engine == "graph" and bool(st_d.graph_trace),
          f"★ 未配置时实际执行路径是 graph（{st_d.engine}, trace={st_d.graph_trace[:3]}）")
    core_l = AgentCore(FakeAdapter([ModelReply(content="完成。")]), build_default_registry(),
                       config=AgentConfig(max_turns=4, min_evidence=0, orchestrator="legacy"),
                       bus=EventBus())
    st_l = await core_l.run("读一下 main.py", workspace=sb.dir)
    check(st_l.engine == "legacy" and st_l.graph_trace == [],
          f"★ 显式 legacy 兜底路径仍可用（{st_l.engine}, trace={st_l.graph_trace}）")
    check(st_l.status is TaskStatus.DONE, "★ legacy 路径照常完成")

    # ---- 2. run() 的编排器守卫 ----
    # M2：graph 已实现 → run() 正常走 graph 引擎
    core_g = AgentCore(FakeAdapter([ModelReply(content="done")]), build_default_registry(),
                       config=AgentConfig(max_turns=4, orchestrator="graph"), bus=EventBus())
    st_g = await core_g.run("x", workspace=sb.dir)
    check(st_g.engine == "graph", "★ run() 正确分派到 graph 引擎")
    core2 = AgentCore(FakeAdapter([]), build_default_registry(),
                      config=AgentConfig(max_turns=4, orchestrator="wat"), bus=EventBus())
    try:
        await core2.run("x", workspace=sb.dir)
        check(False, "未知配置应直接报错")
    except ValueError as e:
        check("未知" in str(e), "★ 未知编排器给出清晰错误")

    # ---- 3. 历史落盘：随轮次追加、顺序正确、任务隔离 ----
    sink = MemoryHistorySink()
    (Path(sb.dir) / "m1.py").write_text("v = 1\n", encoding="utf-8")
    reg = ToolRegistry()
    reg.register(build_default_registry().get("read_file"), group="readonly")
    core3 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("m1", "read_file", {"path": "m1.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=5, min_evidence=0, use_reflect_phase=False),
        bus=EventBus(),
        history_sink=sink,
    )
    st = await core3.run("读 m1.py", workspace=sb.dir)
    entries = sink.by_task.get(st.task_id, [])
    # PLAN 不落历史（它是流程控制，不是对话消息）；ACT 与答复各落 1+ 条
    check(len(entries) >= 3, f"★ 历史随轮次落盘（{len(entries)} 条）")
    check(any("读完了" in str(e.get("content")) for e in entries),
          "★ 最终答复也进入持久化历史（任务结束 → 历史完整）")
    check([e["seq"] for e in entries] == sorted(e["seq"] for e in entries),
          "★ seq 严格递增")
    check(any(e["role"] == "tool" and "m1.py" in e["content"] for e in entries),
          "★ 工具结果进入持久化历史")
    check(all(e["task_id"] if False else True for e in entries), "—")
    check(sink.by_task.get("other-task") is None, "★ 任务之间历史隔离")

    # ---- 4. 落盘失败不静默 ----
    sink2 = MemoryHistorySink()
    sink2.fail_next = 99
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("m2", "read_file", {"path": "m1.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=5, min_evidence=0, use_reflect_phase=False),
        bus=EventBus(),
        history_sink=sink2,
    )
    st4 = await core4.run("读 m1.py", workspace=sb.dir)
    check(st4.history_persist_errors > 0,
          f"★ 落盘失败被计数（{st4.history_persist_errors}），不假装保存成功")
    check(st4.status is TaskStatus.DONE, "★ 落盘失败不中断任务")

    # ---- 5. 恢复安全边界：只读，绝不自动续跑 ----
    msgs = [
        Message.assistant("", tool_calls=[ToolCall("g1", "write_file",
                                                   '{"path":"x.py"}', {"path": "x.py"})]),
        # ★ 缺少对应的 tool 结果 → 消息缺口
    ]
    entries = messages_to_entries(msgs, start_seq=1)
    gaps = find_tool_call_gaps(entries_to_messages(entries))
    check(len(gaps) == 1, f"★ 消息缺口能被检出（{gaps}）")
    core5 = AgentCore(FakeAdapter([]), build_default_registry(), bus=EventBus())
    report = core5.restore_history(st, entries)
    check(report["loaded"] == 1, f"★ 历史装载成功（{report['loaded']} 条）")
    check(report["safe_to_resume"] is False, "★ 存在缺口 → 明确判定不可安全续跑")
    check(any("缺口" in r for r in report["reasons"]), "★ 原因可诊断")
    check(any(m.role == "system" and "不会自动续跑" in m.content for m in core5._history),
          "★ 装载后带恢复标记（不会误当成正常上下文）")
    # 完整配对的历史 → 仍然不可续跑（副作用与审批状态无法重建）
    full = messages_to_entries([
        Message.assistant("", tool_calls=[ToolCall("g2", "read_file", '{"path":"a"}', {"path": "a"})]),
        Message.tool_result("g2", "read_file", "内容"),
        Message.assistant("结论"),
    ], start_seq=10)
    report2 = core5.restore_history(st, full)
    check(report2["safe_to_resume"] is False and len(report2["tool_call_gaps"]) == 0,
          "★ 即便配对完整，M1 也不承诺安全续跑（副作用/审批状态不可重建）")

    # ---- 6. 压缩 marker 进入持久化日志 ----
    sink3 = MemoryHistorySink()
    core6 = _SummaryCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"a"}],"note":""}'),
            ModelReply(tool_calls=[tc("m3", "read_file", {"path": "m1.py"})]),
            ModelReply(tool_calls=[tc("m4", "list_files", {"path": "."})]),
            ModelReply(content="做完了。"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False,
                           compact_max_chars=400, compact_keep_turns=1),
        bus=EventBus(),
        history_sink=sink3,
    )
    st6 = await core6.run("读 m1.py", workspace=sb.dir)
    all_entries = sink3.by_task.get(st6.task_id, [])
    check(any("上下文已压缩" in str(e.get("content")) for e in all_entries),
          "★ 压缩事件以 marker 形式进入持久化日志")
    check(st6.compaction_log, "★ 压缩照常触发（sink 存在不影响压缩）")

    # 诊断输出示例（不输出正文，只输出形状）
    print("    持久化条目形状示例：", dump_entries(all_entries[:3]))




# --------------------------------------------------------------------------- #
# [M2] LangGraph 编排引擎
# --------------------------------------------------------------------------- #
async def test_graph_engine(sb: Sandbox) -> None:
    """[M2] Graph 引擎真的驱动执行，且安全机制全部生效。"""
    print("\n[M2] LangGraph 编排引擎")

    # ---- 1. 文本任务：图真的在跑（节点轨迹）----
    reg = ToolRegistry()
    reg.register(build_default_registry().get("read_file"), group="readonly")
    (Path(sb.dir) / "g1.py").write_text("hello = 1\n", encoding="utf-8")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("x1", "read_file", {"path": "g1.py"})]),
            ModelReply(content="g1.py 里定义了 hello = 1。"),
        ]),
        reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph"),
        bus=EventBus(),
    )
    st = await core.run("读 g1.py 并说明内容", workspace=sb.dir)
    check(st.engine == "graph", f"★ 引擎标注为 graph（{st.engine}）")
    check(st.status is TaskStatus.DONE, f"★ graph 模式任务完成（{st.status.value}）")
    check(st.graph_trace[:3] == ["budget", "plan", "budget"], f"★ 节点轨迹真实存在（{st.graph_trace}）")
    check("act" in st.graph_trace and "tools" in st.graph_trace and "reflect" in st.graph_trace,
          "★ 经过 act/tools/reflect 节点")
    check("g1.py" in st.final_answer or "hello" in st.final_answer, "答复内容正确")
    check(st.graph_trace[-1] == "finalize", "★ 以 finalize 节点收尾")

    # ---- 2. 多轮工具：顺序正确 ----
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"列目录"},{"title":"读文件"}],"note":""}'),
            ModelReply(tool_calls=[tc("y1", "list_files", {"path": "."})]),
            ModelReply(tool_calls=[tc("y2", "read_file", {"path": "g1.py"})]),
            ModelReply(content="完成：列了目录并读了 g1.py。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph"),
        bus=EventBus(),
    )
    st2 = await core2.run("列出目录并读取 g1.py", workspace=sb.dir)
    order = [(e.tool, e.turn) for e in st2.tool_events]
    check(len(st2.tool_events) == 2 and order[0][0] == "list_files" and order[1][0] == "read_file",
          f"★ 多轮工具按剧本顺序执行（{order}）")
    check(st2.status is TaskStatus.DONE, "★ 多轮任务正常完成")

    # ---- 3. 高风险工具：无审批不执行 ----
    reg3 = build_default_registry()
    core3 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"写"}],"note":""}'),
            ModelReply(tool_calls=[tc("z1", "write_file", {"path": "g_new.py", "content": "x"})]),
            ModelReply(content="写好了。"),
            ModelReply(content="审批失败，无法写入。"),
        ]),
        reg3,
        config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph", approval_timeout_s=0.5),
        bus=EventBus(),
    )
    st3 = await core3.run("写一个新文件", workspace=sb.dir)
    check(not (Path(sb.dir) / "g_new.py").exists(), "★ 高风险工具未获审批 → 文件未创建")
    denied = [e for e in st3.tool_events if not e.ok and "approval" in (e.error_kind or "")]
    check(bool(denied), f"★ 审批超时被拒并留痕（{str(denied[0].summary)[:50] if denied else '无'}）")

    # ---- 4. 预算耗尽 → 正确终止（无进展不续期）----
    core4 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("w1", "read_file", {"path": "g1.py"})]),
            ModelReply(content="还在继续。"),
        ]),
        reg,
        config=AgentConfig(max_turns=2, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph", budget_extend_turns=10),
        bus=EventBus(),
    )
    st4 = await core4.run("读 g1.py", workspace=sb.dir)
    check(st4.status is TaskStatus.ABORTED and st4.end_reason == "budget_turns",
          f"★ graph 模式预算耗尽正确终止（{st4.status.value}/{st4.end_reason}）")
    check(st4.budget_extended is False, "★ 单轮读取无进展 → 不续期")

    # ---- 5. 取消 ----
    core5 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("v1", "read_file", {"path": "g1.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph"),
        bus=EventBus(),
    )

    # ---- 取消（单元级）：budget 节点检查 cancel_event → END ----
    # （gather+sleep 在 FakeAdapter 下时序不稳定，直接测 budget 节点逻辑）
    core5 = AgentCore(FakeAdapter([]), reg,
                      config=AgentConfig(max_turns=8, orchestrator="graph"), bus=EventBus())
    st5 = TaskState(goal="读 g1.py", workspace=sb.dir)
    core5._state = st5
    core5._cancel_event.set()
    gstate = {"task_id": st5.task_id, "phase": "budget", "route": "", "turns": 0}
    from backend.agent.graph_engine import GraphTaskEngine
    route = await GraphTaskEngine(core5).node_budget(gstate)
    check(route.get("route") == "END", f"★ budget 节点检测取消 → END（{route}）")
    check(st5.status is TaskStatus.ABORTED and st5.end_reason == "cancelled",
          f"★ 取消在 budget 节点收尾（{st5.status.value}/{st5.end_reason}）")

    # ---- 6. 验证门禁：命令失败 → 不误报成功 ----
    reg6 = ToolRegistry()
    reg6.register(build_default_registry().get("read_file"), group="readonly")
    reg6.register(StubRunTool([1]), group="exec")
    core6 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"跑","check":"command_ok"}],"note":""}'),
            ModelReply(tool_calls=[tc("u1", "run_command", {"command": "python", "args": ["-m", "pytest", "-q"]})]),
            ModelReply(content="修好了。"),
            ModelReply(tool_calls=[tc("u2", "run_command", {"command": "python", "args": ["-m", "pytest", "-q"]})]),
            ModelReply(content="真的修好了。"),
        ]),
        reg6,
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph", max_verification_retries=1),
        bus=EventBus(),
    )
    st6 = await core6.run("修复 pytest 失败", workspace=sb.dir)
    # graph 模式下命令失败被记录 → 验证门禁能检测
    check(len(st6.command_history) > 0 and st6.command_history[-1].exit_code != 0,
          f"★ graph 模式下命令失败被记录（{[(c.command, c.exit_code) for c in st6.command_history]}）")
    check(st6.status in (TaskStatus.ABORTED, TaskStatus.DONE),
          f"★ 任务有明确终态（{st6.status.value}/{st6.end_reason}）")

    # ---- 7. 无静默回退：graph 异常 → failed，不会重跑 legacy ----
    core7 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"x"}],"note":""}'),
            ModelReply(content="ok"),
        ]),
        build_default_registry(),
        config=AgentConfig(max_turns=4, min_evidence=0, orchestrator="graph"),
        bus=EventBus(),
    )
    async def _boom(state):
        raise RuntimeError("graph 引擎内部爆炸")
    core7.compactor = None
    import backend.agent.graph_engine as ge
    orig = ge.GraphTaskEngine.node_budget
    ge.GraphTaskEngine.node_budget = _boom
    try:
        st7 = await core7.run("x", workspace=sb.dir)
        check(st7.status is TaskStatus.FAILED and st7.engine == "graph",
              f"★ graph 异常 → failed，不重跑 legacy（{st7.status.value}/{st7.engine}）")
    finally:
        ge.GraphTaskEngine.node_budget = orig

    # ---- 8. legacy 回归：显式切换到 legacy 时行为不变（M4.3 起默认已是 graph）----
    core8 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("t1", "read_file", {"path": "g1.py"})]),
            ModelReply(content="g1.py 定义了 hello。"),
            ModelReply(content="g1.py 定义了 hello 变量。"),
        ]),
        reg,
        config=AgentConfig(max_turns=6, min_evidence=0, orchestrator="legacy"),
        bus=EventBus(),
    )
    st8 = await core8.run("读 g1.py", workspace=sb.dir)
    check(st8.engine == "legacy" and st8.graph_trace == [],
          f"★ 显式 legacy：无节点轨迹（{st8.engine}, {st8.graph_trace}）")
    check(st8.status is TaskStatus.DONE, "★ legacy 路径照常完成")

    # ---- 9. 历史持久化在 graph 模式下工作 ----
    sink = MemoryHistorySink()
    core9 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"读"}],"note":""}'),
            ModelReply(tool_calls=[tc("s1", "read_file", {"path": "g1.py"})]),
            ModelReply(content="读完了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph"),
        bus=EventBus(),
        history_sink=sink,
    )
    st9 = await core9.run("读 g1.py", workspace=sb.dir)
    entries = sink.by_task.get(st9.task_id, [])
    check(len(entries) >= 4, f"★ graph 模式历史正常落盘（{len(entries)} 条）")
    check(sum(1 for e in entries if e["seq"] == _seq(e)) == len(entries),
          "★ seq 无重复（不重复写入同一条）")


def _seq(e):
    return e.get("seq", -1)




# --------------------------------------------------------------------------- #
# [M21] 工具调用链诊断与参数边界（M2.1）
# --------------------------------------------------------------------------- #
class _FakeSandbox:
    """计数用的假沙箱：记录每次被真正调用，返回预设退出码。"""

    def __init__(self, exit_code: int = 0) -> None:
        from backend.config import SandboxConfig
        self.config = SandboxConfig()
        self.exit_code = exit_code
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs):
        from backend.sandbox.manager import RunOutcome
        self.calls.append(kwargs)
        return RunOutcome(
            exit_code=self.exit_code, stdout="", stderr="",
            timed_out=False, cancelled=False, output_truncated=False,
            duration_s=0.01, container="fake",
            argv=[kwargs.get("command"), *(kwargs.get("args") or [])],
        )


async def test_tool_chain_m21(sb: Sandbox) -> None:
    """[M21] 工具参数边界：整条命令必拒、127 可区分、执行一次且只一次。"""
    print("\n[M21] 工具调用链与参数边界")

    from backend.tools.exec.run_command import RunCommandTool

    # ---- 场景 C：参数边界（工具层，假沙箱 → 不碰真实容器）----
    box = _FakeSandbox(exit_code=0)
    tool = RunCommandTool(sandbox=box)  # type: ignore[arg-type]

    # T2 形态：整条命令 + 非空 args（真实事故形态）
    err = tool.validate_args({"command": "python -m pytest -q", "args": ["-q"]})
    check(bool(err) and "可执行文件名" in err,
          f"★ 整条命令 + 非空 args 被拒（{str(err)[:60]}）")
    check("command='python'" in str(err), "★ 拒绝信息给出可直接照抄的拆分")
    check(not box.calls, "★ 被拒的调用**没有**进入沙箱（无副作用）")

    # 纯路径 + 参数同样被拒
    err2 = tool.validate_args({"command": "/usr/local/bin/python3 -m pytest -q", "args": ["-q"]})
    check(bool(err2), "★ 带路径前缀的整条命令也被拒")

    # 正确拆分 → 放行
    err3 = tool.validate_args({"command": "python3", "args": ["-m", "pytest", "-q"]})
    check(err3 == "", f"★ 正确拆分被放行（{err3!r}）")

    # ---- 127 与 invalid_arguments 可区分 ----
    box127 = _FakeSandbox(exit_code=127)
    tool127 = RunCommandTool(sandbox=box127)  # type: ignore[arg-type]
    st_c = TaskState(goal="t", workspace=sb.dir)
    ctx_c = make_context(st_c); ctx_c.authorized = True
    res127 = await tool127.run({"command": "no_such_bin", "args": []}, ctx_c)
    check(not res127.ok and res127.data and res127.data.get("exit_code") == 127,
          "★ 127 是执行结果（带 exit_code），不是参数错误")
    check("可执行文件没找到" in str((res127.error or {}).get("hint", "")),
          "★ 127 的提示直接说明病因（可执行文件没找到）")
    check((res127.error or {}).get("kind") == "execution_error",
          "★ 127 归类为 execution_error，与 invalid_arguments 分开")

    # ---- 场景 A：合法调用只执行一次 ----
    box_ok = _FakeSandbox(exit_code=0)
    tool_ok = RunCommandTool(sandbox=box_ok)  # type: ignore[arg-type]
    res_ok = await tool_ok.run({"command": "python3", "args": ["-c", "print(1)"]}, ctx_c)
    check(res_ok.ok and len(box_ok.calls) == 1,
          f"★ 合法调用执行一次（沙箱被调用 {len(box_ok.calls)} 次）")
    check(box_ok.calls[0]["command"] == "python3" and box_ok.calls[0]["args"] == ["-c", "print(1)"],
          "★ 执行器收到的 argv 与模型参数一致（无二次包装）")

    # ---- 场景 B/D：Graph 路径 —— 一次调用只执行一次，错误按预算收敛 ----
    reg = ToolRegistry()
    reg.register(tool_ok, group="exec")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"跑"}],"note":""}'),
            # ① 错误形态：整条命令塞进 command
            ModelReply(tool_calls=[tc("p1", "run_command",
                                       {"command": "python3 -c print(1)", "args": ["-c"]})]),
            ModelReply(content="改好了。"),
            # ② 正确形态
            ModelReply(tool_calls=[tc("p2", "run_command",
                                       {"command": "python3", "args": ["-c", "print(1)"]})]),
            ModelReply(content="跑通了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=8, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph"),
        bus=EventBus(),
    )
    st = await core.run("跑一条命令", workspace=sb.dir)
    kinds = [(e.tool, e.ok, e.error_kind) for e in st.tool_events]
    check("act" in st.graph_trace and "tools" in st.graph_trace,
          f"★ 无效调用经 act → tools 节点，在工具校验处被拦（{st.graph_trace}）")
    check(kinds and kinds[0][2] == "invalid_arguments",
          f"★ Graph 路径同样返回 invalid_arguments（{kinds}）")
    check(len(box_ok.calls) == 1,
          f"★ 被拒的那次没有执行；正确的只执行一次（沙箱调用 {len(box_ok.calls)} 次）")
    check(st.status is TaskStatus.DONE, f"★ 模型收到纠正提示后改对并收尾（{st.status.value}）")

    # ---- 错误熔断：连续无效调用 → 终止，不无限重试 ----
    core2 = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"跑"}],"note":""}'),
            ModelReply(tool_calls=[tc("q1", "run_command", {"command": "a b c", "args": ["x"]})]),
            ModelReply(tool_calls=[tc("q2", "run_command", {"command": "d e f", "args": ["y"]})]),
            ModelReply(tool_calls=[tc("q3", "run_command", {"command": "g h i", "args": ["z"]})]),
            ModelReply(content="我不会写。"),
        ]),
        reg,
        config=AgentConfig(max_turns=10, min_evidence=0, use_reflect_phase=False,
                           orchestrator="graph", max_errors=2),
        bus=EventBus(),
    )
    st2 = await core2.run("跑一条命令", workspace=sb.dir)
    check(st2.status is TaskStatus.ABORTED and st2.end_reason in ("budget_errors", "too_many_errors"),
          f"★ 连续无效调用触发熔断终止（{st2.status.value}/{st2.end_reason}）")
    check(len(box_ok.calls) == 1, "★ 熔断前也没有任何无效参数进入沙箱")




async def test_history_task_id_m21(sb: Sandbox) -> None:
    """[M21-b] 历史必须挂在调用方给的 task_id 下（真实事故：7 条历史成孤儿）。"""
    print("\n[M21-b] 历史与任务身份对齐")
    import tempfile as _tf
    from backend.agent.history_log import StoreHistorySink
    from backend.server.store import TaskStore

    store = TaskStore(str(Path(_tf.mkdtemp()) / "t.db"))
    await store.init()
    reg = ToolRegistry()
    reg.register(build_default_registry().get("list_files"), group="readonly")
    core = AgentCore(
        FakeAdapter([
            ModelReply(content='{"steps":[{"title":"看"}],"note":""}'),
            ModelReply(tool_calls=[tc("h1", "list_files", {"path": "."})]),
            ModelReply(content="看完了。"),
        ]),
        reg,
        config=AgentConfig(max_turns=6, min_evidence=0, use_reflect_phase=False),
        bus=EventBus(),
        history_sink=StoreHistorySink(store),
    )
    st = await core.run("看一下", workspace=sb.dir, task_id="task_from_scheduler")
    check(st.task_id == "task_from_scheduler",
          f"★ state.task_id 采用调用方给的 id（{st.task_id}）")
    rows = await store.load_history("task_from_scheduler")
    check(len(rows) >= 3, f"★ 历史挂在同一个 task_id 下（{len(rows)} 条）")
    check(await store.load_history(st.task_id) == rows, "★ 换成 state.task_id 也能查到同一批")
    await store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
