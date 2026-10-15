"""
loom.bat 工作目录选择器的回归测试。

★ 这个测试是为一个真实 bug 写的
────────────────────────────────────────────────────────────────────
旧版用 `if errorlevel 1` 判断「读不到输入」。但实测发现：

    set /p 在「用户直接敲回车」和「stdin 已关闭」两种情况下的表现
    **完全相同** —— 变量保持原值不变 + errorlevel=1。

于是「回车复用上次目录」被误判成「stdin 结束」，直接报错退出 ——
用户永远用不了回车复用。这个测试就是钉死这个行为。

覆盖场景
────────────────────────────────────────────────────────────────────
    Test 1  有历史目录   + 回车        → 复用历史目录
    Test 2  无历史目录   + 回车        → 提示重输（不退出）
    Test 3  历史目录已删 + 回车        → 提示不可用（不退出）
    Test 4  stdin 直接关闭（非交互）   → 有限次提示后退出（不挂死）

为什么能用 --dry-run 快速测
────────────────────────────────────────────────────────────────────
工作目录的选择发生在 CLI 启动**之前**（bat 层），而 --dry-run 会让 CLI
打完环境信息就退出 —— 不调模型，整个测试几秒跑完。

用法：python dev/test_workspace_reuse.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from simulate_keys import run_with_keystrokes  # noqa: E402

LAST = ROOT / ".loom_last_workspace"
DEMO = ROOT / "sandbox" / "demo_project"
GONE = ROOT / "sandbox" / "_this_dir_does_not_exist"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str) -> None:
    results.append((cond, label))
    print(f"  {'[ok]' if cond else '[X] '} {label}")


def run_bat(keys: list[bytes], *, timeout: float = 90.0):
    return run_with_keystrokes(
        ["cmd", "/c", "loom.bat", "--no-pause"], keys, timeout=timeout
    )


def has_workspace(out: str, path: Path) -> bool:
    """输出里有没有把工作区设成指定的目录（bat 层会打印「工作区   X」）。"""
    return str(path).lower() in out.lower()


def main() -> int:
    # 备份真实文件，测完还原
    backup = LAST.read_text(encoding="utf-8") if LAST.exists() else None

    try:
        # ============================================================== #
        print("\n[Test 1] 有历史目录 + 直接回车 → 应复用历史目录")

        LAST.write_text(str(DEMO), encoding="utf-8")
        code, out = run_bat([b"\r\n", b"--dry-run\r\n"])

        check(str(DEMO).lower() in out.lower(), f"显示「上次使用：{DEMO}」")
        check("沿用上次" in out, "回显了「沿用上次」")
        check(has_workspace(out, DEMO), "★ 工作区确实被设为历史目录")
        check("读不到输入" not in out, "★★ 不再误报「读不到输入」")
        check(code == 0, f"正常退出（退出码 {code}）")

        # ============================================================== #
        print("\n[Test 2] 没有历史目录 + 直接回车 → 应提示重输，不退出")

        LAST.unlink(missing_ok=True)
        code, out = run_bat([b"\r\n", b"sandbox\\demo_project\r\n", b"--dry-run\r\n"])

        check("还没有历史工作目录" in out, "★ 提示了「还没有历史工作目录」")
        check("读不到输入" not in out, "★ 没有当成错误直接退出")
        check(has_workspace(out, DEMO), "重输的路径被接受，工作区设置成功")
        check(code == 0, f"正常退出（退出码 {code}）")

        # ============================================================== #
        print("\n[Test 3] 历史目录已被删除 + 直接回车 → 应提示不可用，不退出")

        LAST.write_text(str(GONE), encoding="utf-8")
        code, out = run_bat([b"\r\n", b"sandbox\\demo_project\r\n", b"--dry-run\r\n"])

        check("已不可用" in out, "★ 提示了历史目录不可用")
        check("还没有历史工作目录" in out,
              "★ 回车后走到「无历史」分支重问（而不是拿坏路径硬上）")
        check(has_workspace(out, DEMO), "重输的路径被接受")
        check(code == 0, f"正常退出（退出码 {code}）")

        # ============================================================== #
        print("\n[Test 4] 非交互（stdin 直接关闭）→ 不能挂死")

        # 4a：有历史目录 → 一次 EOF 就复用，正常走完
        LAST.write_text(str(DEMO), encoding="utf-8")
        p = subprocess.run(
            ["cmd", "/c", "loom.bat", "--no-pause"], input=b"",
            capture_output=True, cwd=str(ROOT), timeout=60,
        )
        out4 = p.stdout.decode("gbk", errors="replace")
        check(has_workspace(out4, DEMO), "有历史目录时，EOF 也复用了历史目录")
        check("读不到输入" not in out4, "★ 不再把 EOF 单独当成错误")

        # 4b：没有历史目录 → 有限次提示后自己退出，不无限刷屏
        LAST.unlink(missing_ok=True)
        p = subprocess.run(
            ["cmd", "/c", "loom.bat", "--no-pause"], input=b"",
            capture_output=True, cwd=str(ROOT), timeout=60,
        )
        out4b = p.stdout.decode("gbk", errors="replace")
        check("次没拿到可用路径" in out4b,
              "★ 无历史 + EOF → 有限次提示后主动退出（不无限循环）")
        check(p.returncode == 0, f"进程干净退出（退出码 {p.returncode}）")

    finally:
        # 还原真实的使用记录
        if backup is not None:
            LAST.write_text(backup, encoding="utf-8")
        elif LAST.exists():
            LAST.unlink()

    # ---------------------------------------------------------------- #
    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 68)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print("\n失败项：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 68)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
