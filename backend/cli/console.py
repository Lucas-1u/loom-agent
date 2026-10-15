"""
Loom Agent v0.1 · 终端输入适配

让 CLI 在任何终端、任何编码组合下都能正确读到中文 —— **不猜编码，两种都试**。

★ 为什么需要这个模块（真实踩到的坑，2026-10-13）
────────────────────────────────────────────────────────────
小陆在终端里输入：

    分析一下此目录下的代码文件作用都是什么

程序收到并显示成：

    鍒嗘瀽涓�涓嬫�ょ洰褰曚笅鐨勪唬鐮佹枃浠朵綔鐢ㄩ兘鏄�浠�涔�

更糟的是：模型只能在 thinking 里花 token 去"猜"原文 —— 还把「此目录」
猜成了「项目目录」，**信息直接丢了**。这不是模型该干的活，是工程问题。

★ 根因：编码假设错了
    Windows 上 stdin 送来的字节可能是两种完全不同的编码：

        UTF-8      现代终端 / Git Bash(MSYS) / Windows Terminal 的 UTF-8 模式
        GBK(cp936) 中文 Windows 的 cmd 传统模式

    而 Python 只能按**一个**编码去解。上一版的做法是「跟控制台代码页对齐」
    （GetConsoleCP）—— 在 cmd 里对，但在 MSYS 管道下必错，因为那种终端
    根本不受 chcp 影响：你在 bat 里 `chcp 936`，它照样送 UTF-8 字节。

★ 解法：宽容解码
    UTF-8 的字节结构约束很强（首字节 110xxxxx / 1110xxxx，
    续字节一律 10xxxxxx）。而 GBK 的双字节是 81-FE + 40-FE ——
    它几乎不可能碰巧构成一个合法的 UTF-8 序列。

    所以「**先按 UTF-8 解，失败了再回退 GBK**」在实践中非常可靠：
        合法 UTF-8  → 就是 UTF-8，解对
        非法 UTF-8  → 必然是别的编码，回退 GBK，解对
        纯 ASCII    → 两者等价，都对

★ 为什么要走 buffer 层
    `sys.stdin` 是 TextIOWrapper，它**已经按某个编码解完了** ——
    到那一层信息就丢了，拿到手的乱码再怎么救也救不回来。
    要拿原始字节，必须用 `sys.stdin.buffer`。
"""

from __future__ import annotations

import sys

# 解码尝试顺序。
#   utf-8 优先：现代终端 + MSYS/Git Bash + 大多数管道的约定
#   gbk / cp936 回退：中文 Windows 的传统编码（两者在 Python 里基本等价）
DECODE_ORDER: tuple[str, ...] = ("utf-8", "gbk", "cp936")


def decode_input(raw: bytes) -> str:
    """
    把 stdin 送来的原始字节解成字符串。

    这个函数是整个模块的核心，也是唯一需要理解的地方 —— 见模块开头
    「解法：宽容解码」那一段为什么「先试 UTF-8」是可靠的。
    """
    if not raw:
        return ""

    for enc in DECODE_ORDER:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    # 两条路都不通（比如流里混了非法字节）→ 带替换地解，至少不让 CLI 崩。
    # 宁可用户看到个别 �，也不要甩一屏 Python 堆栈给他。
    return raw.decode("utf-8", errors="replace")


def read_line() -> str:
    """
    读一行输入（**阻塞**，调用方负责放进线程池）。

    返回：
        ""       —— 已到输入末尾（EOF）
        "abc\\n"  —— 正常的一行（**保留换行符**，与 `sys.stdin.readline()` 一致）

    ★ 注意区分「EOF」和「用户敲了个空行」：
        EOF    → ""     （len 0）
        空行    → "\\n"  （len 1）
    上层（审批逻辑）靠这个区别决定「是不是该放弃等输入了」，
    混掉会导致 Agent 永久挂起 —— 这是上一轮已经踩过的坑。
    """
    if sys.stdin is None:
        return ""

    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        # stdin 被替换成了纯文本对象（自检夹具 / 特殊宿主环境）
        # → 拿到的本来就是 str，不该再走字节解码
        line = sys.stdin.readline()
        return line if isinstance(line, str) else decode_input(line)

    raw = buf.readline()
    if not raw:
        return ""
    return decode_input(raw)
