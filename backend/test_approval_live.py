"""Test 2b：真实交互式审批 —— 用管道喂 stdin，验证挂起/恢复。

★ 为什么不能只用 --yes 测：
  --yes 走的是 CLIApprover 的 auto 分支（睡一会儿后直接批准），
  它**验证不到**「真实用户输入如何驱动挂起/恢复」这条路径。
  而这条路径才是 v0.1a 的技术核心：input() 是同步阻塞的，
  必须 run_in_executor 包一层，否则 asyncio.wait_for 的超时永不触发。

  这里用管道喂真实按键，覆盖四种输入：
    ① y      批准
    ② v → y  先看完整内容再批准
    ③ n      拒绝（Agent 应换思路，不是无限重试）
    ④ n+说明 拒绝并给原因（原因要能流到模型侧）

★ 实现注意（踩过的坑）：
  本文件**不能**写在 `asyncio.run()` 里再调 subprocess。
  asyncio.run 会装自己的事件循环与信号处理，父进程这样跑时，
  子进程里 `run_in_executor(None, sys.stdin.readline)` 读管道会拿到空
  （表现为：审批框都不出现，任务直接收尾）。
  改成同步 `subprocess.run` 就正常。教训：**测阻塞式 IO 不要套 asyncio 外壳**。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
# ★ 注意是 **项目根**（backend/ 的上一级），不是 backend/。
#   子进程用 `python -m backend.cli` 调用，cwd 必须是能找到 backend 包的那一级；
#   写成 `Path(__file__).parent` 会让子进程 import 失败、静默什么都不做。
LOOM = Path(__file__).resolve().parent.parent

# ★ 本机系统代理会拦 API 请求，子进程必须清掉
BASE_ENV = {
    k: v for k, v in os.environ.items()
    if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
}

GOAL = "创建 demo.py 文件，内容是 print(1)"
TARGET = "demo.py"


def make_ws() -> str:
    d = tempfile.mkdtemp(prefix="loom_approve_")
    Path(d, "main.py").write_text("x = 1\n", encoding="utf-8")
    Path(d, "README.md").write_text("# demo\n", encoding="utf-8")
    return d


def run_case(name: str, keys: str, expect_created: bool, expect_flag: str = "") -> bool:
    """跑一次，喂 keys 给 stdin，检查结果。"""
    ws = make_ws()
    print(f"\n{'=' * 74}")
    print(f"▶ {name}")
    print(f"  stdin: {keys!r}")
    print(f"{'=' * 74}")

    proc = subprocess.run(
        [PY, "-m", "backend.cli", "-w", ws, "--no-color", "--no-think",
         "-m", "bailian", "--approval-timeout", "30", GOAL],
        cwd=str(LOOM),
        input=keys,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=BASE_ENV,
        timeout=420,
    )
    out = proc.stdout + proc.stderr

    # ---- 提取关键行 ----
    for line in out.splitlines():
        s = line.strip()
        if any(k in s for k in ("需要你的批准", "已批准", "已拒绝", "等待超时",
                               "任务结束", "· create", "· overwrite", "审批 ")):
            print("   " + s[:110])

    target = Path(ws, TARGET)
    created = target.exists()

    ok = True
    if expect_created and not created:
        print(f"\n   ❌ 期望创建 {TARGET}，但它不存在")
        ok = False
    if not expect_created and created:
        print(f"\n   ❌ 期望不创建 {TARGET}，但它被创建了："
              f"{target.read_text(encoding='utf-8')!r}")
        ok = False
    if ok:
        print(f"\n   ✅ {TARGET} {'已创建' if created else '未被创建'}（符合预期）")
        if created:
            print(f"      内容：{target.read_text(encoding='utf-8')!r}")

    # ---- 关键标志检查 ----
    if expect_flag:
        hit = expect_flag in out
        print(f"   {'✅' if hit else '❌'} 输出含 {expect_flag!r}")
        ok = ok and hit

    # ---- 复发检查：Agent 不该反复重试同一个被拒的写操作 ----
    if not expect_created:
        asks = out.count("需要你的批准")
        print(f"   {'✅' if asks <= 1 else '❌'} 只请求了 {asks} 次审批（拒绝后不反复重试）")
        ok = ok and asks <= 1

    return ok


def main() -> int:
    results = [
        ("① 输入 y 批准", run_case(
            "① y 批准", "y\n", expect_created=True, expect_flag="已批准")),
        ("② v 查看完整内容后批准", run_case(
            "② v → y", "v\ny\n", expect_created=True, expect_flag="已批准")),
        ("③ n 拒绝", run_case(
            "③ n 拒绝", "n\n", expect_created=False, expect_flag="已拒绝")),
        ("④ n + 说明原因", run_case(
            "④ n 拒绝并说明原因", "n\n这个文件先不建\n",
            expect_created=False, expect_flag="已拒绝")),
    ]

    print(f"\n{'=' * 74}")
    print("汇总 · 真实交互式审批（管道喂 stdin）")
    print(f"{'=' * 74}")
    ok = 0
    for name, passed in results:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += bool(passed)
    print(f"\n  {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
