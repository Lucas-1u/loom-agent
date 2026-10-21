"""
从 dev/*.bat.src 生成对应的 .bat（GBK 编码）。

★ 为什么需要这个构建步骤？
────────────────────────────────────────────────────────────
Windows 的 cmd.exe 读 .bat 文件时，**按系统 ANSI 代码页（中文 Windows = GBK）
逐字节解释**。如果 .bat 存成 UTF-8，会出现两种崩法：

  1. 中文直接乱码 —— 因为 GBK 把 UTF-8 的多字节序列解释成了别的字。
  2. 更隐蔽的：cmd 定位文件位置时按**字节偏移** seek，
     而 UTF-8 下一个汉字占 3 字节，行长度算错 → 指针落在字符中间 →
     整行被切碎。实测报错长这样：

         'ps_proxy' 不是内部或外部命令
         '══════════════════════════════?echo' 不是内部或外部命令

     （第一条是 `set "https_proxy="` 被从中间切开的结果 ——
       报错信息里根本看不出是编码问题，极容易误判成语法错误。）

  加 `chcp 65001` 也救不了：cmd 执行 chcp 时已经用旧代码页读了前面的行，
  且 seek 偏移问题依然存在。

所以：**源文件用 UTF-8（方便编辑和版本管理），产物用 GBK（cmd 能正确读）。**

用法：
    python dev/build_bat.py            # 构建全部
    python dev/build_bat.py loom       # 只构建 loom.bat
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 源文件（dev/*.bat.src）→ 产物（项目根/*.bat）
TARGETS: dict[str, Path] = {
    "loom": ROOT / "loom.bat",
    "loom-gui": ROOT / "loom-gui.bat",
}

# GBK 之外还允许这几个字符 —— 其实一个都编不进去就该报错，
# 这里留空表示「严格」：任何编不进的字符都直接在下面报错指出位置。
ALLOWED_EXTRA = ""


def build_one(name: str) -> int:
    """构建单个 .bat，返回 0 表示成功。"""
    src = ROOT / "dev" / f"{name}.bat.src"
    dst = TARGETS[name]

    if not src.exists():
        print(f"[X] 找不到源文件：{src}")
        return 1

    text = src.read_text(encoding="utf-8")

    # ---- 落地前先验证：GBK 编不进去的字符要明确指出在哪一行 --------------
    bad: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for ch in line:
            if ch in ALLOWED_EXTRA:
                continue
            try:
                ch.encode("gbk")
            except UnicodeEncodeError:
                bad.append((lineno, ch, f"U+{ord(ch):04X}"))

    if bad:
        print(f"[X] {src.name} 中下列字符无法编入 GBK —— cmd 会显示成乱码，请先替换掉：")
        for lineno, ch, code in bad:
            print(f"    第 {lineno} 行  {ch!r}  ({code})")
        return 2

    # ---- 换行统一成 CRLF（Windows 批处理的稳妥格式）---------------------
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    dst.write_bytes(text.encode("gbk"))

    print(f"[OK] {src.relative_to(ROOT)}  ->  {dst.relative_to(ROOT)}")
    print(f"     {len(text.splitlines())} 行，{dst.stat().st_size} 字节，GBK + CRLF")
    return 0


def main(argv: list[str]) -> int:
    names = argv or list(TARGETS.keys())
    bad_names = [n for n in names if n not in TARGETS]
    if bad_names:
        print(f"[X] 不认识的目标：{', '.join(bad_names)}")
        print(f"     可选：{', '.join(TARGETS.keys())}")
        return 1

    rc = 0
    for n in names:
        rc |= build_one(n)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

