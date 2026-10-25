"""
run_command + Docker 沙箱的**真实容器**集成测试

跑法：
    ./.venv/Scripts/python.exe -m backend.test_sandbox_docker
    ./.venv/Scripts/python.exe -m backend.test_sandbox_docker -v

★ 环境要求
    · Docker Desktop 已启动（`docker version` 能连上守护进程）
    · 已构建镜像：docker build -f dev/Dockerfile.sandbox -t loom-sandbox:py312 .
    缺任何一项 → 本文件会打印 [SKIP] 并以 0 退出。
    **跳过不等于验证过**：隔离效果只有在真的跑过容器后才算验证。

★ 这个文件验证的是「Docker 是否真的按这些 flag 办事」
   离线自检（selfcheck_sandbox.py）只能证明我们**拼对了参数**，
   证明不了 Docker 真的断网、真的非 root、真的只读。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from backend.agent.policy import make_context
from backend.agent.state import TaskState
from backend.config import SandboxConfig
from backend.sandbox import SandboxManager
from backend.tools.exec.run_command import RunCommandTool

VERBOSE = False
results: list[tuple[bool, str]] = []
skipped: list[str] = []


def check(cond: bool, label: str, actual: object = ...) -> None:
    text = label if actual is ... else f"{label}｜实际：{actual!r}"
    results.append((bool(cond), text))
    if cond:
        if VERBOSE:
            print(f"  [ok] {text}")
    else:
        print(f"  [X]  {text}")


async def amain() -> int:
    cfg = SandboxConfig.from_env()
    mgr = SandboxManager(cfg)
    status = await mgr.probe(force=True)

    print("=" * 78)
    print("Loom 沙箱 · 真实 Docker 集成测试")
    print("=" * 78)
    print(f"docker  : {status.available} {status.version}")
    print(f"镜像    : {cfg.image}（存在={status.image_present}）")

    if not status.ready:
        print("\n[SKIP] Docker 或镜像不可用，跳过真实容器测试：")
        print(f"       {status.detail or '未知原因'}")
        print("\n★ 注意：本次没有验证任何真实隔离效果（断网 / 非 root / 只读根 / 资源上限）。")
        print("=" * 78)
        return 0

    ws = tempfile.mkdtemp(prefix="loom_docker_ws_")
    Path(ws, "hello.py").write_text("print('from-file')\n", encoding="utf-8")
    tool = RunCommandTool(mgr)
    state = TaskState(goal="集成测试", workspace=ws, task_id="task_docker01")
    ctx = make_context(state, authorized=True)

    try:
        # ---- 1. 基本执行 ----
        print("\n[1] 基本执行")
        r = await tool.run({"command": "python", "args": ["-c", "print(6*7)"]}, ctx)
        check(r.ok and r.data["stdout"].strip() == "42", "python -c 正常执行", r.data.get("stdout"))

        r = await tool.run({"command": "python", "args": ["hello.py"]}, ctx)
        check(r.ok and "from-file" in r.data["stdout"], "运行工作区里的脚本")

        # ★ 带 cancel_event 但命令正常结束 —— 真实 E2E 里踩过的坑：
        #   finally 里 await 被 cancel 的辅助任务会抛 CancelledError
        #   （BaseException，suppress(Exception) 抓不住），把成功误判成取消。
        idle = asyncio.Event()      # 永不 set
        ctx_idle = make_context(
            TaskState(goal="正常结束", workspace=ws, task_id="task_docker_ok"),
            authorized=True, cancel_event=idle,
        )
        r = await tool.run({"command": "python", "args": ["-c", "print('ok')"]}, ctx_idle)
        check(r.ok and r.data.get("exit_code") == 0,
              "★★ 带 cancel_event 的正常执行不被误判为取消",
              {"cancelled": r.data.get("cancelled"), "exit": r.data.get("exit_code")})

        # ---- 2. 非零退出 + stderr ----
        print("\n[2] 非零退出")
        r = await tool.run(
            {"command": "python", "args": ["-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]},
            ctx,
        )
        check(not r.ok and r.data["exit_code"] == 3, "退出码 3 判为失败", r.data.get("exit_code"))
        check("boom" in r.data["stderr"], "stderr 原样带回", r.data.get("stderr"))

        # ---- 3. 工作区挂载（写回宿主机）----
        print("\n[3] 工作区挂载")
        r = await tool.run({"command": "python", "args": ["-c", "open('out.txt','w').write('hi')"]}, ctx)
        check(r.ok, "容器内写文件成功", r.data.get("stderr"))
        check(Path(ws, "out.txt").exists(), "★★ 文件真的落在宿主机工作区")

        # ---- 4. 非 root ----
        print("\n[4] 非 root")
        r = await tool.run({"command": "python", "args": ["-c", "import os; print(os.getuid())"]}, ctx)
        check(r.data.get("stdout", "").strip() == "1000", "★★ 容器内 uid=1000（非 root）", r.data.get("stdout"))

        # ---- 5. 只读根文件系统 ----
        print("\n[5] 只读根文件系统")
        r = await tool.run(
            {"command": "python", "args": ["-c", "open('/etc/loom_should_fail','w').write('x')"]}, ctx
        )
        check(not r.ok, "★★ 写 /etc 失败（根文件系统只读）", r.data.get("exit_code"))

        # ---- 6. 默认断网 ----
        print("\n[6] 默认断网")
        r = await tool.run(
            {"command": "python", "args": ["-c",
             "import socket; socket.setdefaulttimeout(3); socket.create_connection(('1.1.1.1', 53))"]},
            ctx,
        )
        check(not r.ok, "★★ 容器内无法建立外部 TCP 连接", r.data.get("exit_code"))

        # ---- 7. 超时杀容器 ----
        print("\n[7] 超时")
        r = await tool.run(
            {"command": "python", "args": ["-c", "import time; time.sleep(60)"], "timeout_s": 2}, ctx
        )
        check(r.data.get("timed_out") is True, "★★ 超时被标记", r.data.get("timed_out"))
        check(not mgr.active_containers(), "超时后登记表清空")

        # ---- 8. 输出上限 ----
        print("\n[8] 输出上限")
        r = await tool.run({"command": "python", "args": ["-c", "print('x'*100000)"]}, ctx)
        check(len(r.data["stdout"]) <= cfg.max_output_bytes, "★★ stdout 被截断到上限", len(r.data["stdout"]))
        check(r.data.get("truncated") is True, "标记 truncated")

        # ---- 9. 清理 ----
        print("\n[9] 容器清理")
        code, out, _ = await mgr._docker_quick(
            ["ps", "-a", "--filter", "name=loom-sbx-", "--format", "{{.Names}}"], 10
        )
        leftovers = [n for n in out.splitlines() if n.strip()]
        check(code == 0 and not leftovers, "★★ 没有残留的沙箱容器", leftovers)

        # ---- 10. 资源限制是否真的落到了 cgroup ----
        print("\n[10] 资源限制（读容器内 cgroup，证明参数生效）")
        r = await tool.run({"command": "sh", "args": ["-c",
            "cat /sys/fs/cgroup/pids.max; "
            "cat /sys/fs/cgroup/memory.max; "
            "cat /sys/fs/cgroup/cpu.max"]}, ctx)
        lines = [x.strip() for x in (r.data.get("stdout") or "").splitlines() if x.strip()]
        check(lines[0:1] == ["128"], "★★ pids 上限 128 生效", lines[0:1])
        check(lines[1:2] == ["536870912"], "★★ 内存上限 512MB 生效", lines[1:2])
        check(lines[2:3] == ["100000 100000"], "★★ CPU 上限 1 核生效", lines[2:3])

        # ---- 11. 凭据 / socket / 挂载面 ----
        print("\n[11] 凭据与挂载面")
        r = await tool.run({"command": "sh", "args": ["-c",
            "env; echo '---SOCK---'; ls /var/run/docker.sock 2>&1; "
            "echo '---ROOT---'; ls /workspace/.."]}, ctx)
        out = r.data.get("stdout") or ""
        env_part, _, rest = out.partition("---SOCK---")
        sock_part, _, root_part = rest.partition("---ROOT---")
        check("BAILIAN_API_KEY" not in out and "DEEPSEEK_API_KEY" not in out,
              "★★ 没有注入宿主 API Key")
        env_lines = [x for x in env_part.splitlines() if "=" in x]
        check(len(env_lines) < 20, f"★ 环境变量很少（{len(env_lines)} 个，无宿主 env 透传）")
        check("No such file" in sock_part or "cannot access" in sock_part,
              "★★ 容器内没有 docker.sock", sock_part.strip()[:60])
        check("bin" in root_part and "etc" in root_part and "Loom_agent" not in root_part,
              "★★ /workspace/.. 是容器根（看不到宿主目录）", root_part.strip().replace("\n", " ")[:80])

        # ---- 12. 任务取消 → 容器被清理 ----
        print("\n[12] 取消清理")
        cancel = asyncio.Event()
        ctx2 = make_context(
            TaskState(goal="取消测试", workspace=ws, task_id="task_docker_cancel"),
            authorized=True, cancel_event=cancel,
        )

        async def _fire() -> None:
            await asyncio.sleep(2.0)
            cancel.set()

        task = asyncio.create_task(_fire())
        r = await tool.run(
            {"command": "python", "args": ["-c", "import time; time.sleep(60)"], "timeout_s": 60},
            ctx2,
        )
        task.cancel()
        check(r.data.get("cancelled") is True, "★★ 取消后标记 cancelled", r.data.get("cancelled"))
        check(not mgr.active_containers(), "取消后登记表清空", mgr.active_containers())
        code, out3, _ = await mgr._docker_quick(
            ["ps", "-a", "--filter", "name=loom-sbx-", "--format", "{{.Names}}"], 10
        )
        check(not out3.strip(), "★★ 取消后没有残留容器", out3.strip())

    finally:
        await mgr.close()
        shutil.rmtree(ws, ignore_errors=True)

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 78)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print("\n失败项：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 78)
    return 0 if ok == total else 1


def main() -> int:
    global VERBOSE
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = p.parse_args().verbose
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
