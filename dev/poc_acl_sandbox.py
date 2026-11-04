"""
ACL 沙箱 PoC 验证脚本

验证三种模式下的写操作拦截效果：
1. read-only 写工作区外 → 应被拒
2. workspace-write 写工作区外 → 应被拒
3. workspace-write 写工作区内 → 应允许
4. full-access 写工作区外 → 应允许

运行：python dev/poc_acl_sandbox.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.sandbox import acl

# 工作区目录
WS = "D:/Loom_agent/loom/sandbox/poc_ws"
os.makedirs(WS, exist_ok=True)

# 工作区外目录
HOME = os.path.expanduser("~")
OUTSIDE = os.path.join(HOME, "poc_acl_outside")
os.makedirs(OUTSIDE, exist_ok=True)

OUTSIDE_FILE = os.path.join(OUTSIDE, "test_block.txt")
WS_FILE = os.path.join(WS, "test_write.txt")

# PowerShell 写文件脚本模板（路径用占位符替换）
# catch 里不调用任何方法：受限令牌下 PowerShell 可能进入约束语言模式，
# $_.Exception.GetType() 会报「无法调用方法」，而报错信息会把源码片段（含
# WRITE_BLOCKED 字样）打印出来，造成假阳性判定。只输出固定标记最干净。
PS_WRITE = "try { Set-Content -Path {target} -Value 'poc' -ErrorAction Stop; Write-Output 'WRITE_OK' } catch { Write-Output 'WRITE_BLOCKED' }"

# 结果汇总：(用例名, 判定)，供末尾逐项比对期望值
results = []


def run_test(name, mode, target_path):
    """用受限令牌 spawn PowerShell 写文件，看是否被拦"""
    print(f"\n{'=' * 60}")
    print(f"测试: {name}")
    print(f"模式: {mode}")
    print(f"目标: {target_path}")

    try:
        token, cap = acl.create_restricted_token(mode, WS)
    except Exception as exc:
        print(f"令牌创建失败: {exc!r}")
        results.append((name, "TOKEN_FAILED"))
        return
    print(f"令牌创建成功: token={token}, capability_sid={cap is not None}")

    ps_cmd = PS_WRITE.replace("{target}", f"'{target_path}'")
    rc, out, err = acl.spawn_with_token(
        token, "powershell",
        ["-NoLogo", "-NoProfile", "-Command", ps_cmd]
    )
    print(f"exit_code: {rc}")
    print(f"output: {out.strip()[:300] if out.strip() else '(empty)'}")
    if err:
        print(f"stderr : {err.strip()[:300]}")
    if "ACL_SPAWN_FAILED" in err:
        print("⚠ spawn 失败且未回退（这是设计行为，不是无沙箱执行）")

    # 硬证据：不能只信子进程自述，父进程自己看目标文件到底有没有被写出来
    wrote = os.path.exists(target_path)
    print(f"目标文件实际生成: {wrote}")
    if "WRITE_OK" in out:
        verdict = "WRITE_OK" if wrote else "OK_CLAIMED_BUT_NO_FILE"
    elif "WRITE_BLOCKED" in out:
        verdict = "WRITE_BLOCKED" if not wrote else "BLOCK_CLAIMED_BUT_FILE_EXISTS"
    else:
        verdict = "NO_OUTPUT"
    results.append((name, verdict))
    print(f"判定: {verdict}")

    # 清理目标文件
    if os.path.exists(target_path):
        try:
            os.remove(target_path)
        except OSError:
            pass

    acl.close_token(token)


if __name__ == "__main__":
    print("ACL 沙箱 PoC 验证")
    print(f"工作区: {WS}")
    print(f"工作区外: {OUTSIDE}")

    run_test("read-only 写工作区外", acl.READ_ONLY, OUTSIDE_FILE)
    run_test("workspace-write 写工作区外", acl.WORKSPACE_WRITE, OUTSIDE_FILE)
    run_test("workspace-write 写工作区内", acl.WORKSPACE_WRITE, WS_FILE)
    run_test("full-access 写工作区外", acl.FULL_ACCESS, OUTSIDE_FILE)

    print(f"\n{'=' * 60}")
    print("PoC 完成。WRITE_BLOCKED = 沙箱生效；WRITE_OK = 沙箱未拦")
    expected = {
        "read-only 写工作区外": "WRITE_BLOCKED",
        "workspace-write 写工作区外": "WRITE_BLOCKED",
        "workspace-write 写工作区内": "WRITE_OK",
        "full-access 写工作区外": "WRITE_OK",
    }
    passed = 0
    print("\n结果汇总:")
    for name, verdict in results:
        exp = expected.get(name, "?")
        ok = verdict == exp
        passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {verdict} (期望 {exp})")
    print(f"\n{passed}/{len(results)} 项通过")
