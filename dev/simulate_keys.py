"""
loom.bat 端到端测试：模拟真人敲键盘，验证启动器在各种终端编码下都能用。

★ 为什么不能用「一次性喂完输入」的方式测
────────────────────────────────────────────────────────────────────
cmd 的 `set /p` 在不同 stdin 类型下行为差别很大，这是实测结论：

  stdin 类型            set /p 行为
  ──────────────────────────────────────────────────────────────────
  控制台（真人键盘）     正常，每次读一行          ← 用户的真实场景
  匿名管道              能读，但**一次读走整个缓冲**
                        → 后面第二个 set /p 只能读到 EOF
  继承的文件句柄        读不到（errorlevel=1）

所以「把两行输入拼成一个 bytes 一次喂进去」的测法**必然失败** ——
那是管道语义造成的假象，不是脚本的 bug。第一次就被这个假象骗过：
现象是「请输入路径」之后立刻报「读不到输入」，看起来像脚本写错了。

要真测，就必须让每一行输入**分时到达**，模拟真人一停一敲。

★ 为什么要跑两种编码
────────────────────────────────────────────────────────────────────
Windows 上终端送进 stdin 的字节可能是两种编码，**取决于终端而不是代码**：

    UTF-8       现代终端 / Git Bash(MSYS) / Windows Terminal 的 UTF-8 模式
    GBK(cp936)  中文 Windows 的 cmd 传统模式

小陆踩的坑就是：程序假设了 GBK，而他的终端送的是 UTF-8 —— 结果
「分析一下此目录」变成「鍒嗘瀽涓�涓嬫�ょ洰褰曪紝...」，模型还得花 token 猜原文。

修复方案是 cli/console.py 的「宽容解码」（先试 UTF-8，失败回退 GBK）。
本脚本就跑两种字节，让你亲眼确认：**两种情况都要显示正常**。

用法：
    python dev/simulate_keys.py          # UTF-8 + GBK 两种都跑（各调 1 次模型，约 1-2 分钟）
    python dev/simulate_keys.py --quick  # 只跑 UTF-8（日常冒烟）
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TASK = "分析一下此目录下的代码文件作用都是什么"

# (编码名, 说明) —— 终端送出来的字节用的是哪一种
CASES: list[tuple[str, str]] = [
    ("utf-8", "现代终端 / Git Bash（MSYS）"),
    ("gbk", "传统 cmd（chcp 936）"),
]


def run_with_keystrokes(argv: list[str], keys: list[bytes], *, timeout: float = 240.0):
    """启动子进程，把 keys 逐条「敲」进它的 stdin，返回 (退出码, 解码后的输出)。"""
    p = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, cwd=str(ROOT),
    )

    chunks: list[bytes] = []

    def reader() -> None:
        assert p.stdout is not None
        while True:
            b = p.stdout.read(4096)
            if not b:
                break
            chunks.append(b)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    for k in keys:
        time.sleep(1.2)            # 留出上一步执行与刷屏的时间
        try:
            p.stdin.write(k)       # type: ignore[union-attr]
            p.stdin.flush()        # type: ignore[union-attr]
        except Exception as exc:   # noqa: BLE001
            print(f"  [!] 写入失败：{exc}")
            break

    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        print("  [!] 超时，已强杀")

    t.join(timeout=5)
    return p.returncode, b"".join(chunks).decode("gbk", errors="replace")


def run_case(encoding: str, label: str) -> bool:
    print(f"\n{'=' * 70}")
    print(f"  输入编码：{encoding.upper()}   （{label}）")
    print("=" * 70)

    keys: list[bytes] = [
        b"sandbox\\demo_project\r\n",                      # 工作目录
        b"--readonly --max-turns 1\r\n",                   # 只读 + 单轮，跑得快
        TASK.encode(encoding) + b"\r\n",                   # ★ 被测的那一行
        b":q\r\n",                                         # 退出
    ]

    code, out = run_with_keystrokes(["cmd", "/c", "loom.bat", "--no-pause"], keys)

    # bat 里清了 PYTHONIOENCODING，所以子进程 stdout 按 GBK 写出
    banner_ok = f"任务：{TASK}" in out
    exit_ok = code == 0 and "Loom 已退出" in out
    garbled = any(mark in out for mark in ("鍒嗘瀽", "锟", "\ufffd"))

    # 把 task.start 那几行拎出来给人看
    for line in out.splitlines():
        if "LOOM AGENT" in line or "任务：" in line or "工作区：" in line:
            print("  " + line)

    print(f"\n  {'[ok]' if banner_ok else '[X] '} 任务描述显示正确（没被解成乱码）")
    print(f"  {'[ok]' if not garbled else '[X] '} 输出里没有乱码字符")
    print(f"  {'[ok]' if exit_ok else '[X] '} 启动器正常退出（code={code}）")

    return banner_ok and exit_ok and not garbled


def main() -> int:
    cases = CASES[:1] if "--quick" in sys.argv else CASES

    results: list[tuple[str, bool]] = []
    for encoding, label in cases:
        results.append((encoding, run_case(encoding, label)))

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for encoding, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'}  {encoding}")
    passed = sum(1 for _, ok in results if ok)
    print(f"  {passed}/{len(results)} 通过")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
