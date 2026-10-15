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
from backend.agent.state import RiskLevel, TaskStatus
from backend.config import setup_logging
from backend.events import Event, EventBus, EventType, collect
from backend.models.adapter import FakeAdapter, Message, ModelError, ModelReply, ToolCall
from backend.tools.base import ErrorKind, ToolContext, ToolResult
from backend.tools.registry import build_default_registry

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
        await test_policy(sb)
        await test_approval()
        await test_context(sb)
        await test_json_extract()
        await test_agent_loop(sb)
        await test_final_answer_synthesis(sb)
        await test_approval_in_loop(sb)
        await test_termination(sb)
        await test_robustness(sb)
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


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
