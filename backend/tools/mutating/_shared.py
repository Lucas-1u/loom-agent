"""
mutating 工具的公共部件：哈希、备份、文本读取。

★ 为什么单独抽一个文件？
  `write_file` 和 `edit_file` 都要做这三件事，而且必须**完全一致** ——
  否则「新建/覆盖」和「局部修改」的备份策略、哈希算法不同，
  `ChangeRecord` 里的 before_hash 就对不上，回滚和熔断判断都会失准。
  放一处，两边共用。

★ 依赖方向
  本文件只依赖 `tools/base.py`（契约）和标准库，不 import 任何具体工具。
  这样 write_file / edit_file / 将来的 delete_file 都能安全地共用它。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass

from ..base import ToolContext


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #
def sha256_text(text: str) -> str:
    """内容哈希（16 位十六进制前缀，够用且好读）。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def sha256_file(path: str) -> str:
    """文件哈希。读不到时返回空串（调用方据此判断"没有原内容"）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def make_backup(abs_path: str, ctx: ToolContext) -> tuple[str, str]:
    """
    备份原文件。返回 (备份绝对路径, 错误信息)。

    备份目录结构：
        <workspace>/.loom_backup/20261012-143022/src/main.py

    ★ 为什么备份目录放在工作区内（而不是 %TEMP%）？
      ① 用户可以自己去翻、去还原，路径可预期
      ② 不跨盘复制，快
      ③ 工作区整体拷走时备份跟着走
      代价是需要在 .gitignore 里排除它（已配）。

    ★ 备份失败必须**中止操作**（宁可不写，也不能无声覆盖）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = os.path.relpath(abs_path, ctx.workspace)
    dst_dir = os.path.join(ctx.workspace, ctx.backup_dir_name or ".loom_backup", stamp)
    dst = os.path.join(dst_dir, rel)

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)
    except OSError as exc:
        return "", f"备份失败（{exc}），已放弃写入以保护原文件"
    return dst, ""


# --------------------------------------------------------------------------- #
# 文本读取（给 edit_file 用）
# --------------------------------------------------------------------------- #
@dataclass
class TextFile:
    """
    一个读进内存的文本文件。

    ★ 为什么要同时保留 text 和 had_crlf？
      模型输出的 old_string 里的换行永远是 `\\n`（LLM 不产出 `\\r\\n`）。
      而 Windows 上的文件常常是 `\\r\\n`。直接拿 `\\n` 去匹配 `\\r\\n` 的内容
      **永远匹配不上** —— 而报错只会说"没找到"，模型根本猜不到是换行符的锅。

      所以：**匹配时统一归一化成 `\\n`，写回时按原文件的风格还原**。
      对用户来说文件行尾没变，对模型来说不用关心平台差异。

    ★ 为什么还要 had_bom？
      读取时用 `utf-8-sig` 才能把 BOM 吞掉（否则内容开头会多一个不可见的
      `\\ufeff`，模型给的 old_string 永远匹配不上文件第一行）。
      但 `utf-8-sig` **编码时又会吐出 BOM** —— 要是写回时沿用它，
      就等于给一个本来没有 BOM 的文件硬加了一个。
      所以：读取时吞掉并记下有没有，写回时按原样决定加不加。
      （实测踩过：一次普通的行替换之后，文件凭空多了 3 个字节。）
    """

    text: str          # 归一化换行后的内容（全部是 \n）
    had_crlf: bool     # 原文件是否使用 \r\n
    had_bom: bool      # 原文件是否带 UTF-8 BOM
    encoding: str      # 干净的解码名（"utf-8" / "gbk" / ...），不含 BOM 变体
    size: int


def read_text_file(abs_path: str) -> tuple[TextFile | None, str, str]:
    """
    读一个文本文件。返回 (TextFile | None, error_kind, error_message)。

    error_kind 为空串表示成功。非空时取值为：
        "not_found"  —— 文件不存在 / 是目录
        "too_large"  —— 超过上限
        "not_a_file" —— 二进制文件（含 NUL 字节）
        "execution_error" —— 其他读取失败
    """
    if not os.path.isfile(abs_path):
        return None, "not_found", f"文件不存在：{abs_path}"

    try:
        size = os.path.getsize(abs_path)
    except OSError as exc:
        return None, "execution_error", f"无法读取文件信息：{exc}"

    if size > MAX_TEXT_BYTES:
        return None, "too_large", (
            f"文件过大（{size} 字节 > 上限 {MAX_TEXT_BYTES}），"
            f"edit_file 需要完整读入内存才能定位替换点"
        )

    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return None, "execution_error", f"读取失败：{exc}"

    # ---- 二进制检测：含 NUL 字节就基本可以断定不是文本 --------------------
    if b"\x00" in raw[:8192]:
        return None, "not_a_file", "这是二进制文件，edit_file 只处理文本文件"

    # ---- 解码：和 read_file 用同一套宽容策略 ------------------------------
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    text, encoding = _decode_bytes(raw)
    if text is None:
        return None, "execution_error", "无法解码文件内容（编码不受支持）"

    had_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    return (
        TextFile(
            text=normalized,
            had_crlf=had_crlf,
            had_bom=had_bom,
            encoding=encoding,
            size=size,
        ),
        "",
        "",
    )


def _decode_bytes(raw: bytes) -> tuple[str | None, str]:
    """
    宽容解码：依次尝试常见编码。返回 (文本, 干净编码名)。

    ★ 返回的编码名一定是「干净」的（不带 BOM 变体）——
      调用方拿它去写回时不会意外多出 BOM。
      BOM 的处理由调用方根据 `had_bom` 决定（读时吞掉、写时按原样还原）。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        # 带 BOM：用 utf-8-sig 解码会把 BOM 吃掉，但对外报告成普通的 utf-8
        try:
            return raw.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            pass

    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # 都失败 → 带替换地解，至少能处理（虽然可能有 �）
    return raw.decode("utf-8", errors="replace"), "utf-8"


# 单文件上限：edit_file 要把整个文件读进内存做匹配，比 write_file 更吃内存
MAX_TEXT_BYTES = 2 * 1024 * 1024      # 2 MB
