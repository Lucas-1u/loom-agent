"""
把 Loom 桌面启动器装到桌面。

做三件事：
  1. 读 dev/loom-desktop.bat.src（UTF-8 源，进版本库）
  2. 把 __LOOM_HOME__ 替换成本项目的真实路径
  3. 以 GBK + CRLF 写到桌面上的 Loom.bat

★ 为什么必须转成 GBK？
  和 loom.bat / loom-gui.bat 同一个原因：cmd 按系统 ANSI 代码页逐字节读 .bat。
  详见 dev/build_bat.py 顶部说明。

★ 为什么用占位符而不是硬编码路径？
  这样项目挪位置后，重跑一次本脚本就能修正，不用手改 .bat。

用法：
    python dev/install_desktop.py            # 装到当前用户桌面
    python dev/install_desktop.py --where    # 只看会装到哪，不写文件
    python dev/install_desktop.py --remove   # 从桌面删掉
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # loom/
SRC = ROOT / "dev" / "loom-desktop.bat.src"

FILENAME = "Loom.bat"
PLACEHOLDER = "__LOOM_HOME__"


def desktop_dir() -> Path:
    """
    找当前用户的桌面。

    Windows 的桌面可能被 OneDrive 重定向，所以不能直接拼 ~/Desktop。
    优先读注册表里的真实路径，读不到再退回 USERPROFILE\\Desktop。
    """
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))

    # 1) 注册表（最准）
    try:
        import winreg  # type: ignore

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        raw, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        p = Path(os.path.expandvars(raw))
        if p.exists():
            return p
    except Exception:  # noqa: BLE001 —— 读注册表失败不致命
        pass

    # 2) OneDrive 重定向
    for cand in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if cand.exists():
            return cand

    return home / "Desktop"


def build_bytes() -> bytes:
    """读源文件、替换路径、转 GBK + CRLF。返回可写入的字节。"""
    if not SRC.exists():
        raise FileNotFoundError(f"找不到源文件：{SRC}")

    text = SRC.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        raise ValueError(f"源文件里没有占位符 {PLACEHOLDER}")

    # 路径里的反斜杠是字面量，直接替换即可
    text = text.replace(PLACEHOLDER, str(ROOT))

    # 编码前先检查：GBK 编不进去的字符要指出来
    bad: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for ch in line:
            try:
                ch.encode("gbk")
            except UnicodeEncodeError:
                bad.append((lineno, ch))
    if bad:
        detail = "、".join(f"第{n}行 {c!r}" for n, c in bad[:5])
        raise ValueError(f"下列字符无法编入 GBK：{detail}")

    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode("gbk")


def main(argv: list[str]) -> int:
    where_only = "--where" in argv
    remove = "--remove" in argv

    desk = desktop_dir()
    target = desk / FILENAME

    if where_only:
        print(f"项目目录：{ROOT}")
        print(f"桌面目录：{desk}")
        print(f"将安装到：{target}")
        return 0

    if remove:
        if target.exists():
            target.unlink()
            print(f"[OK] 已删除：{target}")
        else:
            print(f"[--] 本来就不存在：{target}")
        return 0

    if not desk.exists():
        print(f"[X] 桌面目录不存在：{desk}")
        return 1

    data = build_bytes()

    # 已存在时先备份一份（万一是用户手改过的）
    if target.exists():
        bak = target.with_suffix(".bat.bak")
        try:
            shutil.copy2(target, bak)
            print(f"[--] 已存在，旧文件备份为：{bak.name}")
        except OSError:
            pass

    target.write_bytes(data)

    print(f"[OK] 桌面启动器已安装")
    print(f"     源文件  dev/{SRC.name}")
    print(f"     目标    {target}")
    print(f"     大小    {len(data)} 字节，GBK + CRLF")
    print()
    print(f"     双击桌面上的 {FILENAME} 即可启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
