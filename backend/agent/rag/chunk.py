"""
文本切分：把文件切成"可检索、且读得懂"的片段。

★ 第一版的取舍
--------------
不做 AST 解析、不做语法树切分，但**必须避免把结构切碎**：
  · Markdown 按标题切，标题留在片段里（否则片段读起来没头没尾）
  · 代码按顶层 `def` / `class` 切，函数体不被拦腰截断
  · 其余按空行分段
过长的段再按行硬切，宁可多一片，也不让一片超预算。

★ 为什么片段要带行号
------------------
检索结果必须能定位（"docs/x.md:12-30"），否则模型拿着片段去 read_file
还得自己找，等于没省事；前端也没法显示"命中在哪"。

★ 为什么标题要单独存一份
--------------------
排序与展示需要短标题；把标题混在正文里，前端只能截前 20 个字，
经常截到"## "这种没信息量的前缀。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
PY_TOPLEVEL = re.compile(r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
JS_TOPLEVEL = re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)")

DEFAULT_MAX_CHARS = 1200
DEFAULT_MIN_CHARS = 120   # 太小的片段会被合并进前一片


@dataclass
class Chunk:
    path: str
    ord: int
    title: str
    start_line: int
    end_line: int
    text: str

    @property
    def locator(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def _split_oversize(title: str, text: str, start_line: int, max_chars: int) -> list[tuple[str, int, str]]:
    """一段太长 → 按行硬切（切点仍在行边界，不会切坏一行）。"""
    out: list[tuple[str, int, str]] = []
    buf: list[str] = []
    size = 0
    line_no = start_line
    buf_start = start_line
    for line in text.splitlines():
        if size + len(line) + 1 > max_chars and buf:
            out.append((title, buf_start, "\n".join(buf)))
            buf, size = [], 0
            buf_start = line_no
        buf.append(line)
        size += len(line) + 1
        line_no += 1
    if buf:
        out.append((title, buf_start, "\n".join(buf)))
    return out


def _pack(sections: list[tuple[str, int, str]], max_chars: int) -> list[tuple[str, int, str]]:
    """
    把相邻的小段合并到预算内；超预算的段再硬切。

    ★ 为什么先合并再切：Markdown 里一句话一行的小节特别多，
      每行一个片段既浪费索引体积，也会让检索结果变成碎片。
    """
    out: list[tuple[str, int, str]] = []
    cur_title = ""
    cur_start = 0
    cur: list[str] = []
    cur_size = 0

    def flush() -> None:
        nonlocal cur, cur_size, cur_title, cur_start
        if cur:
            out.append((cur_title, cur_start, "\n".join(cur)))
            cur, cur_size = [], 0

    for title, start, text in sections:
        if not text.strip():
            continue
        if len(text) > max_chars:
            flush()
            out.extend(_split_oversize(title, text, start, max_chars))
            continue
        if cur and (cur_size + len(text) + 1 > max_chars or title != cur_title):
            flush()
        if not cur:
            cur_title, cur_start = title, start
        cur.append(text)
        cur_size += len(text) + 1
    flush()
    return out


def _sections_markdown(text: str) -> list[tuple[str, int, str]]:
    """按 `#` 标题切；代码围栏里的 `#` 不算标题。"""
    out: list[tuple[str, int, str]] = []
    title = ""
    start = 1
    buf: list[str] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else MD_HEADING.match(line)
        if m:
            if buf:
                out.append((title, start, "\n".join(buf)))
            title = m.group(2).strip()
            start = i
            buf = [line]
        else:
            if not buf:
                start = i
            buf.append(line)
    if buf:
        out.append((title, start, "\n".join(buf)))
    return out


def _sections_code(text: str, path: str) -> list[tuple[str, int, str]]:
    """按顶层符号切；第一个符号之前的内容算"模块头部"。"""
    pattern = PY_TOPLEVEL if path.endswith((".py", ".pyi")) else JS_TOPLEVEL
    module = Path(path).stem
    out: list[tuple[str, int, str]] = []
    title = f"{module}（模块头部）"
    start = 1
    buf: list[str] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = pattern.match(line)
        if m:
            if buf:
                out.append((title, start, "\n".join(buf)))
            title = m.group(1)
            start = i
            buf = [line]
        else:
            if not buf:
                start = i
            buf.append(line)
    if buf:
        out.append((title, start, "\n".join(buf)))
    return out


def _sections_text(text: str) -> list[tuple[str, int, str]]:
    """按空行分段（保留原行号）。"""
    out: list[tuple[str, int, str]] = []
    buf: list[str] = []
    start = 1
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip() == "" and buf:
            out.append(("", start, "\n".join(buf)))
            buf = []
            start = i + 1
        else:
            if not buf:
                start = i
            buf.append(line)
    if buf:
        out.append(("", start, "\n".join(buf)))
    return out


def chunk_text(path: str, text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """按后缀选择切分策略，产出带行号的片段。"""
    suffix = Path(path).suffix.lower()
    if suffix in (".md", ".markdown", ".rst"):
        sections = _sections_markdown(text)
    elif suffix in (".py", ".pyi", ".js", ".ts", ".tsx", ".jsx"):
        sections = _sections_code(text, path)
    else:
        sections = _sections_text(text)

    packed = _pack(sections, max_chars)
    chunks: list[Chunk] = []
    for idx, (title, start, body) in enumerate(packed):
        raw = body.strip("\n")
        if not raw.strip():
            continue
        end = start + body.count("\n")
        chunks.append(Chunk(
            path=path, ord=idx, title=title[:120] or Path(path).name,
            start_line=start, end_line=end, text=raw,
        ))
    return chunks
