"""
文件 / 内容哈希（tools 层公共）

★ 为什么单独抽出来？
  `read_file` 要返回 hash 供前端展示和 `edit_file.expect_hash` 使用，
  而 `edit_file` 要用同一个 hash 做 stale 检测。
  **如果两处各写一份实现，迟早会漂**（比如一个截取前 8 位、一个前 16 位）。
  放在这里，谁用都是同一个函数，口径不可能不一致。

★ 为什么要 16 位而不是完整 64 位？
  这是给人看 + 给模型传的，不是密码学用途。16 位十六进制（64 bit）碰撞概率
  足够低，又不会让日志和 UI 变难看。edit_file 的 stale 检测也用它。
"""

from __future__ import annotations

import hashlib

HASH_LEN = 16


def sha256_text(text: str) -> str:
    """字符串内容的 hash（先按 utf-8 编码，errors=replace 兜底）。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def sha256_file(path: str) -> str:
    """
    文件**完整字节**的 hash。

    ★ 注意是完整文件，不是某个读取片段 —— 这样 `read_file(offset=..)` 分段读
      和 `edit_file` 的 stale 检测才能对上同一个值。
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:HASH_LEN]
