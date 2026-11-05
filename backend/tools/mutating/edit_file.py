"""
edit_file —— 在工作区内对文件做**局部修改**：把 old_string 替换成 new_string。

★ v0.1b 的第一个工具。它是 write_file 的精细化替代：
  write_file 只能"整篇覆盖"，改一行也得重写整个文件 —— 既危险（
  一次全量覆盖容易丢掉别处的改动），又浪费（模型要重新吐一遍全文）。
  edit_file 只动需要动的地方。

────────────────────────────────────────────────────────────────
它比 write_file 难在哪？（难点不在写，在**定位**）
────────────────────────────────────────────────────────────────
write_file 的语义是确定的："把这段内容写进这个文件"。没有歧义。

edit_file 引入了一个新问题：**模型给的 old_string，在文件里对不上怎么办？**

现实里这件事**经常发生**，原因有六七种：

    ① 缩进差一个空格        —— 最常见
    ② 行尾多了空白
    ③ 换行符不同（\n vs \r\n）—— Windows 上高发
    ④ 模型记错了内容（凭印象写的）
    ⑤ 文件在模型读取之后被改过（stale）
    ⑥ 模型想改的片段出现了多次，它没意识到
    ⑦ 模型只给了片段的一部分（想改三行只抄了两行）

这些**都不会崩**，只会静默地改错地方或改不动。所以本工具的重点全在
「定位」和「定位失败时怎么告诉模型」。

★ 三条核心规则

  1. **必须唯一匹配**（除非显式 replace_all=true）
     出现多次 = 无法确定要改哪一处 → 拒绝，而不是随便挑一个。
     这是最容易被忽略、后果最严重的一条：改错地方可能破坏无关代码。

  2. **换行符归一化后再匹配**
     模型输出的换行永远是 `\n`，而 Windows 文件的换行常常是 `\r\n`。
     直接匹配永远失败，而报错只会说"没找到" —— 模型根本猜不到是换行符的锅。
     所以：**内部统一按 `\n` 匹配，写回时按原文件的风格还原**。

  3. **失败时给出「能直接用来修正」的信息**
     光说"old_string 未找到"没用。要告诉它：
       · 文件里第几行有相近内容
       · 是不是缩进/空格不一致
       · 还是文件已经变了（配合 expect_hash）
     —— 一条 hint 能让模型一次改对，还是让它瞎试三轮，差别就在这里。

────────────────────────────────────────────────────────────────
安全设计（和 write_file 同一套）
────────────────────────────────────────────────────────────────
① 无授权闸门（步骤 5：审批已移除，安全边界由沙箱承担）
② 写前备份原文件到 .loom_backup/<时间戳>/，备份失败则拒绝执行
③ 内容上限，防止把磁盘塞满
④ 敏感文件额外警示
"""

from __future__ import annotations

import os
from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ..path_safety import (
    PathViolation,
    display_path,
    human_size,
    is_sensitive,
    resolve_in_workspace,
)
from ...agent.state import ChangeRecord, RiskLevel
from ._shared import make_backup, read_text_file, sha256_file, sha256_text


MAX_NEW_STRING_CHARS = 200_000    # 单次新增内容上限（字符）
PREVIEW_LINES = 20                # 审批时 diff 每侧最多显示几行
HINT_SNIPPET_CHARS = 100          # 定位线索里每行的截断长度


# --------------------------------------------------------------------------- #
# 定位辅助：匹配失败时，给模型一条「能直接用来修正」的线索
# --------------------------------------------------------------------------- #
def locate_hint(content: str, old_string: str) -> str:
    """
    精确匹配失败后，努力找出「为什么没匹配上」，并给一条可操作的线索。

    ★ 这一段是整个工具里对模型最友好的部分。
      没有它，模型只会收到"未找到"，然后很可能**原样再试一次**（浪费一个轮次），
      或者凭猜测乱改一通。有了它，模型通常一次就能改对。

    搜索顺序（从强到弱）：
      ① 首行精确出现 → 直接指出行号
      ② 去掉所有空格后出现 → 提示"缩进/空格可能不一致"
      ③ 都找不到 → 给出文件概况，让它 read_file 重看
    """
    lines = content.splitlines()
    probe = [ln for ln in old_string.splitlines() if ln.strip()]
    if not lines:
        return "文件当前是空的。"
    if not probe:
        return ""

    key = probe[0].strip()

    # ---- ① 首行（去空白后）在文件里出现 --------------------------------
    for idx, line in enumerate(lines, 1):
        if key and key in line:
            return (
                f"文件中第 {idx} 行含相近内容：{line.strip()[:HINT_SNIPPET_CHARS]!r}。"
                "请以实际内容（含缩进）为准重新组织 old_string。"
            )

    # ---- ② 抹掉所有空格后出现 → 典型是缩进不一致 ------------------------
    squeezed = key.replace(" ", "").replace("\t", "")
    if squeezed:
        for idx, line in enumerate(lines, 1):
            if squeezed in line.replace(" ", "").replace("\t", ""):
                return (
                    f"第 {idx} 行看起来就是你要找的，但**缩进或空格不一致**："
                    f"{line.rstrip()[:HINT_SNIPPET_CHARS]!r}。"
                    "建议先用 read_file 原样复制这段内容再改。"
                )

    # ---- ③ 放弃定位，给文件概况 ----------------------------------------
    head = "\n".join(f"    {i}| {ln}" for i, ln in enumerate(lines[:5], 1))
    return (
        f"文件共 {len(lines)} 行，开头几行是：\n{head}\n"
        "old_string 与当前内容对不上。请先 read_file 查看实际内容，"
        "再原样复制要修改的部分。"
    )


def _count_occurrences(content: str, needle: str) -> int:
    """数 needle 在 content 里出现了几次（不重叠）。"""
    return content.count(needle)


def _find_occurrence_at_line(content: str, old_string: str, target_line: int) -> int | None:
    """
    找到 old_string 在 target_line 处的字节偏移量。

    用于多匹配时的行号锚定（对齐 DSH dsh-tool-str-replace-editor）：
    当 old_string 出现多次时，用行号确定要改哪一处，而不是随便挑一个。
    """
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx < 0:
            return None
        line = content.count("\n", 0, idx) + 1
        if line == target_line:
            return idx
        start = idx + len(old_string)


def _fuzzy_match_at_line(content: str, old_string: str, target_line: int) -> str | None:
    """
    在 target_line 处尝试模糊匹配 old_string。

    归一化空白后比较：如果 target_line 处的文本块与 old_string
    仅在缩进/空格上有差异，返回实际内容。

    这解决了模型凭印象给出缩进（差一个空格）时编辑失败的问题 ——
    模型记得行号但记不准缩进时，仍能成功编辑。
    """
    lines = content.splitlines(keepends=True)
    if target_line < 1 or target_line > len(lines):
        return None

    old_lines = old_string.splitlines()
    if not old_lines:
        return None

    start_idx = target_line - 1
    n = min(len(old_lines), len(lines) - start_idx)
    actual_block = "".join(lines[start_idx:start_idx + n])

    # 归一化空白：去掉所有空格和制表符，统一换行符
    def _norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in " \t\r").strip()

    if _norm(actual_block) == _norm(old_string):
        # 去掉行尾换行符：模型给的 old_string 通常不含 \n，
        # 替换时 new_string 也不含 \n，这样替换后行结构正确。
        # 行号定位靠 _find_occurrence_at_line，不依赖 \n。
        return actual_block.rstrip("\r\n")
    return None


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "对工作区内一个已存在的文本文件做**局部修改**：把 old_string 替换成 new_string。"
        "old_string 必须在文件中**唯一出现**（除非设 replace_all=true），"
        "且要与文件里的实际内容完全一致（包括缩进与换行）。"
        "只改一小段时用它，比 write_file 整篇覆盖更安全。"
        "此操作会修改磁盘上的文件，需要用户批准后才会真正执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区根目录的文件路径。文件必须已存在。",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "要被替换掉的原文本。必须与文件中实际内容**逐字符一致**（含缩进），"
                    "并且在文件中**只出现一次**。想删掉某段内容时，把它填在这里、"
                    "new_string 留空即可。"
                ),
            },
            "new_string": {
                "type": "string",
                "description": "替换成的新文本。留空字符串表示删除 old_string 那一段。",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "是否替换文件中**所有**匹配位置，默认 false。"
                    "false 时若 old_string 出现多次会失败并提示——"
                    "这是有意的，避免改错地方。确认要全改时才设 true。"
                ),
            },
            "expect_hash": {
                "type": "string",
                "description": (
                    "可选。如果你是先 read_file 拿到内容再改的，把 read_file 返回的 hash 填进来。"
                    "若文件在读取之后已被改动，本次编辑会被拒绝（stale_content），"
                    "避免用旧内容去覆盖新内容。"
                ),
            },
            "line": {
                "type": "integer",
                "description": (
                    "可选。当 old_string 在文件中出现多次时，用此参数指定要修改的那一处所在的行号（1-based）。"
                    "配合缩进不一致时也能定位：若该行的文本块与 old_string 仅在缩进/空格上有差异，"
                    "会自动模糊匹配（对齐 DSH dsh-tool-str-replace-editor 的行号锚定机制）。"
                ),
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    risk = RiskLevel.WRITE       # 有副作用 → 走审批

    # ===================================================================== #
    # 执行
    # ===================================================================== #
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            # ---- 0. 参数校验 -----------------------------------------------
            # ★ 步骤 5：删除 ctx.authorized 授权闸门。审批已整体移除，
            #   安全边界由沙箱（Docker / ACL）承担。
            raw_path = args.get("path", "")
            old_string = args.get("old_string")
            new_string = args.get("new_string")
            replace_all = bool(args.get("replace_all", False))
            expect_hash = str(args.get("expect_hash", "") or "").strip()
            target_line = args.get("line")
            if target_line is not None and not isinstance(target_line, int):
                try:
                    target_line = int(target_line)
                except (ValueError, TypeError):
                    target_line = None

            if not raw_path:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, "缺少必填参数 'path'", duration_ms=t.ms
                )
            if not isinstance(old_string, str) or old_string == "":
                # ★ old_string 为空是**必须**拦的：空串在哪儿都匹配，
                #   替换它就等于在文件开头插入 —— 几乎不可能是模型的本意。
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "old_string 不能为空",
                    hint=(
                        "old_string 是要被替换掉的原文本，必须给出具体内容。"
                        "如果想新建文件或整篇重写，请改用 write_file。"
                    ),
                    duration_ms=t.ms,
                )
            if not isinstance(new_string, str):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"new_string 必须是字符串，收到 {type(new_string).__name__}",
                    hint="如果想删除这段内容，把 new_string 设为空字符串。",
                    duration_ms=t.ms,
                )
            if old_string == new_string:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "old_string 与 new_string 完全相同，这次编辑不会产生任何变化",
                    hint="请给出真正要改成的内容，或不要调用本工具。",
                    duration_ms=t.ms,
                )
            if len(new_string) > MAX_NEW_STRING_CHARS:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.TOO_LARGE,
                    f"new_string 过长（{len(new_string)} 字符 > 上限 {MAX_NEW_STRING_CHARS}）",
                    hint="改动太大时请改用 write_file 整篇重写，或拆成多次编辑。",
                    duration_ms=t.ms,
                )

            # ---- 2. 路径校验 -----------------------------------------------
            try:
                rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            except PathViolation as exc:
                outside = "越出工作区" in str(exc)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATH_OUTSIDE_WORKSPACE if outside else ErrorKind.INVALID_ARGUMENTS,
                    str(exc),
                    hint="只能修改工作区内的路径。" if outside else "请检查路径写法。",
                    duration_ms=t.ms,
                )

            if os.path.isdir(rp.absolute):
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_A_FILE,
                    f"目标是目录，不是文件：{rp.relative}",
                    hint="edit_file 只能改文件。请给出具体文件名。",
                    duration_ms=t.ms,
                )
            if not os.path.isfile(rp.absolute):
                # edit_file 不负责创建文件 —— 那是 write_file 的职责。
                # 分开是为了让「改」和「建」在审计上可区分。
                return ToolResult.failure(
                    self.name,
                    ErrorKind.NOT_FOUND,
                    f"文件不存在：{rp.relative}",
                    hint=(
                        "edit_file 只能修改已存在的文件。"
                        "如果这是新文件，请改用 write_file；"
                        "如果路径写错了，可以先用 list_files 确认。"
                    ),
                    duration_ms=t.ms,
                )

            # ---- 3. 读入当前内容 -------------------------------------------
            tf, kind, msg = read_text_file(rp.absolute)
            if tf is None:
                return ToolResult.failure(
                    self.name,
                    ErrorKind(kind),
                    msg,
                    hint="请确认文件是文本文件，且路径正确。",
                    duration_ms=t.ms,
                )

            content = tf.text          # 已归一化为 \n
            current_hash = sha256_file(rp.absolute)

            # ---- 4. ★ stale_content 检测 -----------------------------------
            # 模型先 read_file 拿到内容与 hash，再据此组织 old_string。
            # 若这期间文件被改过，它手上的内容就是过期的 ——
            # 此时继续编辑可能覆盖别人的改动，必须拒绝。
            if expect_hash and expect_hash != current_hash:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.STALE_CONTENT,
                    (
                        f"文件在读取之后已被改动（你持有的 hash={expect_hash}，"
                        f"当前 hash={current_hash}），本次编辑已中止"
                    ),
                    hint=(
                        "请重新 read_file 获取最新内容与 hash，再基于新内容组织 old_string。"
                        "不要沿用旧内容，否则会覆盖掉别人的修改。"
                    ),
                    data={"expected": expect_hash, "actual": current_hash, "path": rp.relative},
                    duration_ms=t.ms,
                )

            # ---- 5. ★ 定位：匹配并检查唯一性 -------------------------------
            count = _count_occurrences(content, old_string)

            # ★ 行号锚定（对齐 DSH dsh-tool-str-replace-editor）：
            #   当 count==0 时，尝试在 target_line 处模糊匹配（缩进差异容错）；
            #   当 count>1 时，用 target_line 定位要改的那一处。
            effective_old = old_string  # 实际用于替换的文本（可能是模糊匹配到的实际内容）
            replace_at_idx = None       # 指定位置的字节偏移（行号锚定后用）

            # ---- count == 0：尝试行号模糊匹配 --------------------------------
            if count == 0 and target_line:
                fuzzy = _fuzzy_match_at_line(content, old_string, target_line)
                if fuzzy:
                    effective_old = fuzzy
                    # 用行号定位，避免多匹配时 find 到错误位置
                    replace_at_idx = _find_occurrence_at_line(content, effective_old, target_line)
                    if replace_at_idx is None:
                        replace_at_idx = content.find(effective_old)
                    count = 1
                    log_info = f"行号 {target_line} 处模糊匹配成功（缩进/空格差异已容错）"
                else:
                    log_info = f"行号 {target_line} 处模糊匹配也失败"
            else:
                log_info = ""

            if count == 0:
                hint = locate_hint(content, old_string)
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PATCH_CONFLICT,
                    f"old_string 在 {rp.relative} 中未找到（文件共 {len(content.splitlines())} 行）",
                    hint=hint or "请先 read_file 查看当前内容，再原样复制要修改的部分。",
                    data={"path": rp.relative, "occurrences": 0, "current_hash": current_hash},
                    duration_ms=t.ms,
                )

            # ---- count > 1 且未 replace_all：行号锚定消歧 --------------------
            if count > 1 and not replace_all:
                if target_line:
                    # 尝试在 target_line 处找到匹配
                    idx = _find_occurrence_at_line(content, effective_old, target_line)
                    if idx is not None:
                        # 行号锚定成功：只替换这一处
                        replace_at_idx = idx
                        count = 1
                        effective_old = content[idx:idx + len(old_string)]
                    else:
                        # 行号处找不到，尝试模糊匹配
                        fuzzy = _fuzzy_match_at_line(content, old_string, target_line)
                        if fuzzy:
                            replace_at_idx = content.find(fuzzy)
                            count = 1
                            effective_old = fuzzy
                            log_info = f"行号 {target_line} 处模糊匹配成功（缩进/空格差异已容错）"
                        else:
                            positions = _first_positions(content, old_string, limit=5)
                            return ToolResult.failure(
                                self.name,
                                ErrorKind.PATCH_CONFLICT,
                                (
                                    f"old_string 在 {rp.relative} 中出现了 {count} 次，"
                                    f"指定的行号 {target_line} 处未找到匹配"
                                ),
                                hint=(
                                    f"匹配位置的行号：{positions}。请选择其一：\n"
                                    "  · 在 old_string 里**多带上几行上下文**（比如包含上一行或下一行），"
                                    "让它变得唯一；\n"
                                    "  · 或者，如果确实要把这 {count} 处全部改掉，把 replace_all 设为 true。"
                                ),
                                data={
                                    "path": rp.relative,
                                    "occurrences": count,
                                    "lines": positions,
                                    "current_hash": current_hash,
                                },
                                duration_ms=t.ms,
                            )
                else:
                    # 没有行号 → 拒绝（保持原有安全语义）
                    positions = _first_positions(content, old_string, limit=5)
                    return ToolResult.failure(
                        self.name,
                        ErrorKind.PATCH_CONFLICT,
                        (
                            f"old_string 在 {rp.relative} 中出现了 {count} 次，"
                            f"无法确定要修改哪一处"
                        ),
                        hint=(
                            f"匹配位置的行号：{positions}。请选择其一：\n"
                            "  · 在 old_string 里**多带上几行上下文**（比如包含上一行或下一行），"
                            "让它变得唯一；\n"
                            "  · 或者，如果确实要把这 {count} 处全部改掉，把 replace_all 设为 true。\n"
                            "  · 或者，用 line 参数指定要修改的那一处的行号（1-based）。"
                        ),
                        data={
                            "path": rp.relative,
                            "occurrences": count,
                            "lines": positions,
                            "current_hash": current_hash,
                        },
                        duration_ms=t.ms,
                    )

            # ---- 6. 生成新内容 ---------------------------------------------
            if replace_all:
                new_content = content.replace(old_string, new_string)
                replaced = _count_occurrences(content, old_string)
            elif replace_at_idx is not None:
                # 行号锚定：只替换指定位置的那一处
                before = content[:replace_at_idx]
                after = content[replace_at_idx + len(effective_old):]
                new_content = before + new_string + after
                replaced = 1
            else:
                new_content = content.replace(old_string, new_string, 1)
                replaced = 1

            if new_content == content:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.INVALID_ARGUMENTS,
                    "替换后内容没有变化",
                    hint="请检查 old_string / new_string 是否写反了。",
                    duration_ms=t.ms,
                )

            # ---- 7. 备份（★ 一定在写之前）----------------------------------
            backup_path, err = make_backup(rp.absolute, ctx)
            if err:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR, err, duration_ms=t.ms
                )

            # ---- 8. 写回（按原文件的换行风格还原）--------------------------
            write_text = new_content
            if tf.had_crlf:
                # 原文件是 CRLF → 还原，别因为一次编辑就把整个文件的行尾改掉
                write_text = new_content.replace("\n", "\r\n")

            # ★ 编码名要用干净的（utf-8 / gbk）；BOM 按原文件的实际情况决定 ——
            #   读取时用的是 utf-8-sig（为了吞掉 BOM，否则文件开头的 invisible
            #   \ufeff 会让模型给的 old_string 永远匹配不上第一行），
            #   但写回时若沿用它，会给一个本来没有 BOM 的文件硬加一个（实测过）。
            write_enc = "utf-8-sig" if tf.had_bom else tf.encoding
            try:
                with open(rp.absolute, "w", encoding=write_enc,
                          errors="replace", newline="") as f:
                    f.write(write_text)
            except PermissionError:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.PERMISSION_DENIED,
                    f"系统拒绝写入：{rp.relative}",
                    duration_ms=t.ms,
                )
            except OSError as exc:
                return ToolResult.failure(
                    self.name,
                    ErrorKind.EXECUTION_ERROR,
                    f"写入失败：{exc}",
                    hint="原文件已备份，可在 .loom_backup/ 下找回。",
                    duration_ms=t.ms,
                )

            # ---- 9. 结果与变更记录 -----------------------------------------
            old_lines = old_string.count("\n") + 1
            new_lines = new_string.count("\n") + 1 if new_string else 0
            after_hash = sha256_file(rp.absolute)

            change = ChangeRecord(
                path=rp.relative,
                action="edit",
                before_hash=current_hash,
                after_hash=after_hash,
                removed=old_lines * replaced,
                added=new_lines * replaced,
                approval_id="",  # 步骤 5：审批移除，留痕字段保留但恒空（审计链不断裂）
                backup_path=display_path(ctx.workspace, backup_path),
            )

            delta = new_lines - old_lines
            delta_txt = f"{delta:+d} 行" if delta else "行数不变"
            if replaced > 1:
                where = f"{replaced} 处"
            else:
                # 用 effective_old 定位行号；fuzzy 路径下 old_string 可能未匹配
                positions = _first_positions(content, effective_old, 1)
                if positions:
                    where = f"第 {positions[0]} 行"
                elif replace_at_idx is not None:
                    where = f"第 {content.count(chr(10), 0, replace_at_idx) + 1} 行"
                else:
                    where = "1 处"
            summary = (
                f"修改 {rp.relative}（{where}，{delta_txt}，"
                f"{human_size(len(write_text.encode('utf-8')))}）；"
                f"原内容已备份到 {change.backup_path}"
            )

            return ToolResult.success(
                self.name,
                summary,
                data={
                    "path": rp.relative,
                    "action": "edit",
                    "replaced": replaced,
                    "removed_lines": change.removed,
                    "added_lines": change.added,
                    "before_hash": current_hash,
                    "after_hash": after_hash,
                    "change": change.to_dict(),
                },
                side_effects=[
                    {
                        "kind": "file_write",
                        "path": rp.relative,
                        "action": "edit",
                        "backup": change.backup_path,
                    }
                ],
                duration_ms=t.ms,
            )

    # ===================================================================== #
    # 审批展示（C4 三要素）—— 这里展示的是 diff，不是整个文件
    # ===================================================================== #
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        """
        生成审批界面要展示的信息。

        ★ 与 write_file 的区别：这里展示 **old → new 的差异**，而不是新内容全文。
          理由：用户要判断的是「这次改动对不对」，看 diff 比看全文快得多 ——
          尤其文件有几百行、只改三行的时候。
          这也正是 edit_file 相对 write_file 在**审批体验**上的优势。
        """
        raw_path = str(args.get("path", ""))
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        if not isinstance(old_string, str):
            old_string = str(old_string)
        if not isinstance(new_string, str):
            new_string = str(new_string)

        # ---- 路径解析（失败也不抛，用原文展示让用户自己判断）-------------
        abs_path = ""
        rel = raw_path
        try:
            rp = resolve_in_workspace(ctx.workspace, raw_path, must_exist=False)
            abs_path, rel = rp.absolute, rp.relative
        except PathViolation:
            pass

        exists = bool(abs_path) and os.path.isfile(abs_path)

        # ---- 影响说明 --------------------------------------------------
        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1 if new_string else 0
        delta = new_lines - old_lines

        if not exists:
            impact = f"⚠ **文件不存在**：{rel}。edit_file 只能改已存在的文件，这次调用会失败。"
        else:
            occurrences = 0
            try:
                tf, _, _ = read_text_file(abs_path)
                if tf is not None:
                    occurrences = _count_occurrences(tf.text, old_string)
            except Exception:  # noqa: BLE001 —— 审批展示不该崩
                pass

            if occurrences == 0:
                impact = (
                    f"⚠ **old_string 在当前文件中找不到**，这次调用会失败。"
                    f"（文件 {rel} 存在，共 {os.path.getsize(abs_path)} 字节）"
                )
            elif occurrences > 1 and not replace_all:
                impact = (
                    f"⚠ **old_string 出现了 {occurrences} 次**，无法确定改哪一处，"
                    f"这次调用会失败。需要带上更多上下文，或设 replace_all=true。"
                )
            else:
                times = f"{occurrences} 处" if occurrences > 1 else "1 处"
                impact = (
                    f"将修改 **{times}**：{old_lines} 行 → {new_lines} 行"
                    f"（{delta:+d} 行）。原内容会先备份到 {ctx.backup_dir_name}/，可手工还原。"
                )

        # ---- 敏感文件警示 ----------------------------------------------
        if abs_path:
            sensitive, why = is_sensitive(
                abs_path,
                rel,
                sensitive_names=ctx.sensitive_names or set(),
                sensitive_dir_parts=ctx.sensitive_dir_parts or set(),
            )
            if sensitive:
                impact += f"\n⚠ **敏感文件**：{why}。请确认这确实是你要 Agent 修改的内容。"

        # ---- diff 预览 --------------------------------------------------
        preview = self._render_diff(old_string, new_string)

        return {
            "action": "修改文件",
            "target": rel,
            "impact": impact,
            "preview": preview,
        }

    @staticmethod
    def _render_diff(old_string: str, new_string: str) -> str:
        """
        把 old → new 渲染成一段尽量小的 diff。

        只标出「被替换的那段」：`-` 行是删掉的，`+` 行是加上的。
        超过 PREVIEW_LINES 的部分截断并注明 —— 审批界面不该被几百行淹没。
        """
        out: list[str] = []
        old_lines = old_string.splitlines() or [""]
        new_lines = new_string.splitlines() if new_string else []

        for ln in old_lines[:PREVIEW_LINES]:
            out.append(f"- {ln}")
        if len(old_lines) > PREVIEW_LINES:
            out.append(f"- …（原内容还有 {len(old_lines) - PREVIEW_LINES} 行）")

        for ln in new_lines[:PREVIEW_LINES]:
            out.append(f"+ {ln}")
        if len(new_lines) > PREVIEW_LINES:
            out.append(f"+ …（新内容还有 {len(new_lines) - PREVIEW_LINES} 行）")

        if not new_lines:
            out.append("+ （删除这段内容）")

        return "\n".join(out)


# --------------------------------------------------------------------------- #
# 内部小工具
# --------------------------------------------------------------------------- #
def _first_positions(content: str, needle: str, limit: int = 3) -> list[int]:
    """返回前 limit 个匹配位置的行号（1-based）。给模型定位用。"""
    positions: list[int] = []
    start = 0
    while len(positions) < limit:
        idx = content.find(needle, start)
        if idx < 0:
            break
        positions.append(content.count("\n", 0, idx) + 1)
        start = idx + max(len(needle), 1)
    return positions


def make() -> Tool:
    return EditFileTool()
