"""
沙箱 / run_command 自检（S 组，**全离线，不需要 Docker**）

跑法：
    ./.venv/Scripts/python.exe -m backend.selfcheck_sandbox
    ./.venv/Scripts/python.exe -m backend.selfcheck_sandbox -v

★ 这里能验证什么、不能验证什么
────────────────────────────────────────────────────────────────
  能：参数校验、容器参数构造（安全 flag 是否齐全）、路径边界、
      Docker 不可用时的行为、超时/取消/输出截断的控制流、
      Policy 审批与「审批后参数变化」、审批展示内容。

  不能：Docker 是否**真的**按这些 flag 隔离。
      这里用假进程（FakeProc）替代 docker CLI，验证的是控制流。
      真实隔离效果必须跑 backend/test_sandbox_docker.py（需要 Docker）。
      ★ 本文件全绿 ≠ 沙箱已经过安全验收。

★ 为什么要假进程
    没有 Docker 的机器上，超时/取消/输出截断这些逻辑一行都测不到。
    假进程只实现 manager 用到的那几个方法（stdout/stderr/wait/kill），
    足够把「什么时候杀容器、杀完返回什么」跑到。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.agent.approval import args_fingerprint, build_pending
from backend.agent.policy import Policy, make_context
from backend.agent.state import RiskLevel, TaskState
from backend.config import SandboxConfig
from backend.sandbox import SandboxManager
from backend.tools.exec.run_command import RunCommandTool
from backend.tools.registry import ToolRegistry, build_default_registry

VERBOSE = False
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, actual: object = ...) -> None:
    text = label if actual is ... else f"{label}｜实际：{actual!r}"
    results.append((bool(cond), text))
    if cond:
        if VERBOSE:
            print(f"  [ok] {text}")
    else:
        print(f"  [X]  {text}")


# --------------------------------------------------------------------------- #
# 假 docker（进程 + CLI）
# --------------------------------------------------------------------------- #
class FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        await asyncio.sleep(0)
        return chunk


class FakeProc:
    """模拟 `docker run` 的子进程。hang=True 时只有 kill() 才能结束。"""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", code: int = 0, hang: bool = False):
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode: int | None = None
        self._code = code
        self._hang = hang
        self._killed = asyncio.Event()
        self.killed = False

    async def wait(self) -> int:
        if self._hang:
            await self._killed.wait()
            self.returncode = 137
        else:
            await asyncio.sleep(0)
            self.returncode = self._code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._killed.set()


class FakeDocker:
    """替代 docker CLI：探测成功，run 返回预设的假进程。"""

    def __init__(
        self,
        proc: FakeProc | None = None,
        *,
        version_ok: bool = True,
        image_present: bool = True,
    ) -> None:
        self.proc = proc
        self.version_ok = version_ok
        self.image_present = image_present
        self.calls: list[tuple[str, ...]] = []
        self.spawned: list[tuple[str, ...]] = []

    async def quick(self, args, timeout: float = 10.0) -> tuple[int, str, str]:
        self.calls.append(tuple(args))
        head = args[0] if args else ""
        if head == "version":
            return (0, "24.0.9\n", "") if self.version_ok else (1, "", "Cannot connect to the Docker daemon")
        if head == "image":
            return (0, "[]", "") if self.image_present else (1, "", "No such image")
        if head == "kill" and self.proc is not None:
            self.proc.kill()
        return 0, "", ""

    async def spawn(self, *argv, **kwargs):
        self.spawned.append(tuple(str(a) for a in argv))
        if self.proc is None:
            raise FileNotFoundError("docker: not found")
        return self.proc


def cfg(**kw) -> SandboxConfig:
    base = dict(
        image="loom-sandbox:py312",
        docker_bin="docker",
        network="none",
        user="1000:1000",
        memory="512m",
        cpus="1.0",
        pids_limit=128,
        tmpfs_size="64m",
        workspace_mount="/workspace",
        default_timeout_s=60,
        max_timeout_s=300,
        max_output_bytes=16_384,
    )
    base.update(kw)
    return SandboxConfig(**base)


def build_tool(docker: FakeDocker, **cfg_kw) -> tuple[RunCommandTool, SandboxManager]:
    mgr = SandboxManager(cfg(**cfg_kw), spawn=docker.spawn)
    mgr._docker_quick = docker.quick  # type: ignore[method-assign]
    return RunCommandTool(mgr), mgr


def make_ws() -> str:
    ws = tempfile.mkdtemp(prefix="loom_sbx_")
    Path(ws, "tests").mkdir()
    Path(ws, "tests", "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    return ws


# --------------------------------------------------------------------------- #
# [S1] 参数与路径
# --------------------------------------------------------------------------- #
async def test_args(ws: str) -> None:
    print("\n[S1] 参数校验 / 工作目录边界 / 超时钳制")

    docker = FakeDocker(FakeProc(code=0))
    tool, mgr = build_tool(docker)
    ctx = make_context(TaskState(goal="g", workspace=ws), authorized=True)

    for bad, why in [
        ({}, "缺 command"),
        ({"command": ""}, "空 command"),
        ({"command": "   "}, "全空格 command"),
        ({"command": "python", "args": "print(1)"}, "args 传成字符串"),
        ({"command": "python", "args": [{"x": 1}]}, "args 含对象"),
        ({"command": "python", "args": [None]}, "args 含 null"),
        ({"command": "x" * 600}, "command 过长"),
        ({"command": "python", "timeout_s": True}, "timeout 传布尔"),
    ]:
        err = tool.validate_args(bad)
        check(bool(err), f"拒绝非法参数：{why}")

    # 数字参数会被转成字符串（不经过 shell，没有注入问题）
    cmd, argv = tool._normalize_argv({"command": "python", "args": ["-c", 1, True]})
    check(cmd == "python" and argv == ["-c", "1", "True"], f"标量参数转字符串：{argv}")

    # ---- 工作目录越界 ----
    r = await tool.run({"command": "python", "cwd": "../"}, ctx)
    check(not r.ok and r.error["kind"] in ("path_outside_workspace", "invalid_arguments"),
          "★ cwd 越界被拒绝", r.error and r.error["kind"])

    r = await tool.run({"command": "python", "cwd": "/etc"}, ctx)
    check(not r.ok, "★ 绝对路径 cwd 被拒绝", r.error and r.error["kind"])

    r = await tool.run({"command": "python", "cwd": "no_such_dir"}, ctx)
    check(not r.ok and r.error["kind"] in ("invalid_arguments", "not_found"),
          "不存在的 cwd 被拒绝", r.error and r.error["kind"])

    Path(ws, "afile.txt").write_text("x", encoding="utf-8")
    r = await tool.run({"command": "python", "cwd": "afile.txt"}, ctx)
    check(not r.ok and r.error["kind"] == "not_a_dir", "cwd 指向文件被拒绝", r.error and r.error["kind"])

    # ---- 未授权：必须在碰 docker 之前就拒绝 ----
    docker2 = FakeDocker(FakeProc(code=0))
    tool2, _ = build_tool(docker2)
    ctx_unauthed = make_context(TaskState(goal="g", workspace=ws), authorized=False)
    r = await tool2.run({"command": "python", "args": ["-V"]}, ctx_unauthed)
    check(not r.ok and r.error["kind"] == "permission_denied", "★ 未授权直接拒绝")
    check(not docker2.spawned and not docker2.calls, "★★ 未授权时没有碰 docker", docker2.calls)

    # ---- 超时钳制 ----
    captured: dict[str, Any] = {}
    real_run = mgr.run

    async def spy(**kw):
        captured.update(kw)
        return await real_run(**kw)

    mgr.run = spy  # type: ignore[method-assign]
    await tool.run({"command": "python", "args": ["-V"], "timeout_s": 9999}, ctx)
    check(captured.get("timeout_s") == 300.0, f"超时上限钳制（{captured.get('timeout_s')}）")
    await tool.run({"command": "python", "args": ["-V"], "timeout_s": 0}, ctx)
    check(captured.get("timeout_s") == 1.0, f"超时下限钳制（{captured.get('timeout_s')}）")
    await tool.run({"command": "python", "args": ["-V"]}, ctx)
    check(captured.get("timeout_s") == 60.0, f"默认超时（{captured.get('timeout_s')}）")
    mgr.run = real_run  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# [S2] 容器参数（安全边界）
# --------------------------------------------------------------------------- #
def test_argv(ws: str) -> None:
    print("\n[S2] docker run 参数（安全边界）")

    tool, mgr = build_tool(FakeDocker())
    argv = mgr.build_run_argv(
        container_name="loom-sbx-test",
        command="python",
        args=["-m", "pytest", "-q"],
        cwd_rel="tests",
        workspace_root=ws,
    )
    def has(*parts: str) -> bool:
        """参数必须**连续出现**（而不是散落在各处）。"""
        n = len(parts)
        return any(tuple(argv[i:i + n]) == parts for i in range(len(argv) - n + 1))

    check(has("--network", "none"), "★ 默认禁网（--network none）")
    check("--read-only" in argv, "★ 根文件系统只读")
    check(has("--cap-drop", "ALL"), "★ 丢弃全部 capabilities")
    check(has("--security-opt", "no-new-privileges:true"), "★ no-new-privileges")
    check(has("--user", "1000:1000"), "★ 非 root 用户")
    check(has("--pids-limit", "128"), "★ 进程数上限")
    check(has("--memory", "512m") and has("--memory-swap", "512m"), "★ 内存上限（且禁用 swap 绕过）")
    check(has("--cpus", "1.0"), "★ CPU 上限")
    check("--tmpfs" in argv and any("/tmp" in a for a in argv), "★ /tmp 用 tmpfs")
    check(has("--pull", "never"), "★ 绝不隐式拉镜像（--pull never）")
    check("--privileged" not in argv, "★★ 没有 --privileged")
    check(not any("docker.sock" in a for a in argv), "★★ 没有挂载 docker.sock")
    check(not any("--env-file" in a for a in argv), "★ 没有 --env-file（不透传宿主 env）")
    check(not any(a in ("--pid", "--ipc", "--uts") for a in argv), "★ 没有共享宿主命名空间")

    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    check(len(mounts) == 1, f"★★ 只挂载一个目录（实际 {len(mounts)}）", mounts)
    check(mounts and mounts[0].endswith(":/workspace:rw"), "★ 挂载点是 /workspace", mounts)
    check(mounts and str(Path(ws).resolve()) in mounts[0], "★ 挂载源就是工作区", mounts)

    workdir = argv[argv.index("-w") + 1] if "-w" in argv else ""
    check(workdir == "/workspace/tests", f"★ cwd 映射到容器内 /workspace/tests（{workdir}）")

    # 命令必须是「镜像名之后的独立参数」，不能是拼好的字符串
    idx = argv.index("loom-sandbox:py312")
    check(argv[idx + 1:] == ["python", "-m", "pytest", "-q"], "★★ 命令逐参数传递（无 shell 拼接）",
          argv[idx + 1:])

    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    allowed = {"HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1",
               "PYTHONIOENCODING=utf-8", "TMPDIR=/tmp"}
    check(set(envs) <= allowed, "★★ 环境变量只有白名单，没有宿主凭据", [e for e in envs if e not in allowed])

    # cwd 防御性校验
    for bad in ("..", "../..", "/etc", "a/../../b"):
        try:
            mgr.build_run_argv(container_name="c", command="x", args=[], cwd_rel=bad, workspace_root=ws)
            check(False, f"build_run_argv 应拒绝非法 cwd：{bad}")
        except ValueError:
            check(True, f"build_run_argv 拒绝非法 cwd：{bad}")

    # describe_action 不能执行 docker
    d = tool.describe_action({"command": "python", "args": ["-V"]}, make_context(TaskState(goal="g", workspace=ws)))
    check("python -V" in d["target"], f"审批展示含完整命令（{d['target']}）")
    check("不会" in d["impact"] and "无网络" in d["impact"], "★ 影响说明写明「批准不放宽隔离」")
    check("argv" in d["preview"].lower(), "★ 预览给出真实 argv")

    d2 = tool.describe_action({"command": "bash", "args": ["-c", "echo hi"]},
                              make_context(TaskState(goal="g", workspace=ws)))
    check("shell" in d2["impact"].lower(), "★ 使用 shell 时有额外警示")


# --------------------------------------------------------------------------- #
# [S3] 执行控制流（假进程）
# --------------------------------------------------------------------------- #
async def test_run_flow(ws: str) -> None:
    print("\n[S3] 执行控制流：正常 / 非零 / 超时 / 取消 / 输出截断")

    ctx = make_context(TaskState(goal="g", workspace=ws, task_id="task_abc12345"), authorized=True)

    # ---- 正常 ----
    docker = FakeDocker(FakeProc(stdout=b"2\n", code=0))
    tool, mgr = build_tool(docker)
    r = await tool.run({"command": "python", "args": ["-c", "print(2)"]}, ctx)
    check(r.ok, "正常执行 ok=True", r.summary)
    check(r.data["exit_code"] == 0 and r.data["stdout"] == "2\n", "★ stdout 被捕获", r.data)
    check(r.data["stderr"] == "", "stderr 为空")
    check(docker.spawned and docker.spawned[0][1] == "run", "走的是 docker run", docker.spawned[:1])
    check(not mgr.active_containers(), "★ 正常结束后没有残留容器")

    # ---- 非零退出 ----
    docker = FakeDocker(FakeProc(stdout=b"partial\n", stderr=b"AssertionError: boom\n", code=1))
    tool, _mgr = build_tool(docker)
    r = await tool.run({"command": "python", "args": ["-m", "pytest"]}, ctx)
    check(not r.ok and r.data["exit_code"] == 1, "★★ 非零退出码判为失败")
    check("AssertionError" in r.data["stderr"], "★★ stderr 原样带回", r.data["stderr"][:40])
    check("AssertionError" in r.to_message_content(), "★★ 失败时 stderr 也喂给模型（可见报错）")
    check("stderr" in r.data and "stdout" in r.data, "data 同时含 stdout / stderr")

    # ---- 超时 → 杀死容器 ----
    proc = FakeProc(hang=True)
    docker = FakeDocker(proc)
    tool, mgr = build_tool(docker)
    r = await tool.run({"command": "python", "args": ["-c", "import time; time.sleep(99)"], "timeout_s": 1}, ctx)
    check(not r.ok and r.data["timed_out"] is True, "★★ 超时被标记")
    check(r.error["kind"] == "timeout", "超时错误类型 timeout", r.error["kind"])
    check(proc.killed, "★★ 超时后进程/容器被终止")
    check(any(c and c[0] == "kill" for c in docker.calls),
          "★★ 调用了 docker kill", docker.calls[-2:])
    check(not mgr.active_containers(), "超时后登记表已清空")

    # ---- 取消 ----
    proc = FakeProc(hang=True)
    docker = FakeDocker(proc)
    tool, mgr = build_tool(docker)
    cancel = asyncio.Event()
    ctx2 = make_context(TaskState(goal="g", workspace=ws, task_id="task_cancel01"),
                        authorized=True, cancel_event=cancel)

    async def _trigger():
        await asyncio.sleep(0.05)
        cancel.set()

    asyncio.create_task(_trigger())
    r = await tool.run({"command": "python", "args": ["-c", "while 1: pass"], "timeout_s": 30}, ctx2)
    check(not r.ok and r.data["cancelled"] is True, "★★ 任务取消 → 命令被标记 cancelled")
    check(proc.killed, "★★ 取消后容器被终止")
    check("取消" in r.summary, f"提示文案说明是取消（{r.summary}）")

    # ---- 输出截断 ----
    big = b"x" * 50_000
    docker = FakeDocker(FakeProc(stdout=big, code=0))
    tool, _ = build_tool(docker)
    r = await tool.run({"command": "python", "args": ["-c", "print('x'*50000)"]}, ctx)
    check(len(r.data["stdout"]) == 16_384, f"★★ stdout 截断到上限（{len(r.data['stdout'])}）")
    check(r.data["truncated"] is True, "★ 标记 truncated")
    check("截断" in r.summary, f"摘要里提示截断（{r.summary}）")

    # ---- 带 cancel_event 但命令正常结束：不能被误判成取消 ----
    # ★ 回归：`finally` 里 `cancel_task.cancel()` 后 `await` 它，抛的是
    #   CancelledError（BaseException），`suppress(Exception)` 抓不住 →
    #   命令明明成功了，整个任务却被判成 "cancelled"。
    #   只有「带了 cancel_event 且命令正常结束」才会走到，离线用例之前漏了。
    docker_idle = FakeDocker(FakeProc(stdout=b"ok\n", code=0))
    tool_idle, _ = build_tool(docker_idle)
    idle = asyncio.Event()          # 永不 set
    ctx_idle = make_context(
        TaskState(goal="g", workspace=ws, task_id="task_ok01"),
        authorized=True, cancel_event=idle,
    )
    r = await tool_idle.run({"command": "python", "args": ["-c", "print(1)"]}, ctx_idle)
    check(r.ok and r.data["exit_code"] == 0 and r.data["cancelled"] is False,
          "★★ 带 cancel_event 的正常执行不会被误判为取消",
          {"ok": r.ok, "cancelled": r.data.get("cancelled")})

    # ---- spawn 失败 ----
    docker = FakeDocker(proc=None)
    tool, _ = build_tool(docker)
    r = await tool.run({"command": "python"}, ctx)
    check(not r.ok and r.error["kind"] == "sandbox_unavailable", "★ docker CLI 起不来 → sandbox_unavailable",
          r.error and r.error["kind"])


# --------------------------------------------------------------------------- #
# [S4] Docker / 镜像不可用：必须明确失败，绝不回退宿主机
# --------------------------------------------------------------------------- #
async def test_no_docker(ws: str) -> None:
    print("\n[S4] Docker / 镜像不可用")

    ctx = make_context(TaskState(goal="g", workspace=ws), authorized=True)

    docker = FakeDocker(version_ok=False)
    tool, _mgr = build_tool(docker)
    r = await tool.run({"command": "python", "args": ["-c", "print(1)"]}, ctx)
    check(not r.ok and r.error["kind"] == "sandbox_unavailable", "★★ 守护进程不可用 → 明确失败",
          r.error and r.error["kind"])
    check("没有" in r.error["message"] or "不会" in r.error["hint"], "★★ 明确说明「没有在宿主机执行」")
    check(not docker.spawned, "★★ 没有尝试 docker run")

    docker = FakeDocker(image_present=False)
    tool, _ = build_tool(docker)
    r = await tool.run({"command": "python"}, ctx)
    check(not r.ok and r.error["kind"] == "sandbox_unavailable", "★ 镜像不存在 → 明确失败")
    check("docker build" in r.error["hint"], "★ hint 里给出构建命令", r.error["hint"][:60])
    check(not docker.spawned, "镜像缺失时没有执行任何容器")

    # ---- 显式禁用：连探测都不该发生 ----
    docker = FakeDocker(FakeProc(code=0))
    tool, _mgr = build_tool(docker, enabled=False)
    r = await tool.run({"command": "python"}, ctx)
    check(not r.ok and r.error["kind"] == "sandbox_unavailable",
          "★★ LOOM_SANDBOX_ENABLED=0 → 明确拒绝（不是悄悄放行）")
    check(not docker.calls and not docker.spawned,
          "★★ 禁用时连 docker 都不探测", docker.calls)

    # 请求异常也不能被当成成功
    bad = FakeDocker(FakeProc(code=0))
    tool, mgr = build_tool(bad)

    async def boom(**kw):
        raise RuntimeError("unexpected")

    mgr.run = boom  # type: ignore[method-assign]
    r = await tool.run({"command": "python"}, ctx)
    check(not r.ok and r.error["kind"] == "execution_error", "★ 沙箱内部异常 → 失败（不是成功）")


# --------------------------------------------------------------------------- #
# [S5] Policy / 审批 / 审批后参数变化
# --------------------------------------------------------------------------- #
async def test_policy(ws: str) -> None:
    print("\n[S5] Policy 审批 / TOCTOU")

    reg = ToolRegistry()
    tool, _mgr = build_tool(FakeDocker())
    reg.register(tool, group="exec")
    policy = Policy(reg)
    state = TaskState(goal="跑测试", workspace=ws)
    state.task_id = "task_policy1"
    ctx = make_context(state)

    args = {"command": "python", "args": ["-m", "pytest", "-q"], "cwd": "tests"}
    d = policy.check("run_command", args, state, ctx)
    check(d.needs_approval is True, "★★ run_command 必须审批（不能被自动放行）")
    check(d.meta.get("risk") == "exec", f"审批风险等级 exec（{d.meta.get('risk')}）")
    check("pytest" in d.meta.get("target", ""), f"审批目标里是真实命令（{d.meta.get('target')}）")
    check("不会" in d.meta.get("impact", ""), "审批影响说明写明不放宽隔离")

    # ---- 审批后参数被换掉 → 拒绝 ----
    pending = build_pending(
        tool="run_command", args=args, risk=RiskLevel.EXEC,
        action=d.meta["action"], target=d.meta["target"], impact=d.meta["impact"],
    )
    changed = {"command": "python", "args": ["-m", "pytest", "--maxfail=1"], "cwd": "tests"}
    d2 = policy.recheck_after_approval("run_command", changed, state, ctx, pending)
    check(not d2.allow, "★★ 审批后参数变化 → 拒绝执行")
    check("参数" in d2.result.summary, f"提示说明参数变了（{d2.result.summary}）")

    # ---- 参数没变 → 放行 ----
    d3 = policy.recheck_after_approval("run_command", args, state, ctx, pending)
    check(d3.allow, "参数一致 → 复检通过")
    check(args_fingerprint("run_command", args) == pending.args_hash, "指纹同口径")

    # ---- 拒绝之后不能偷偷执行 ----
    check(policy.check("run_command", args, state, ctx, already_approved="").needs_approval, "无审批凭据仍需审批")

    # ---- 只读模式必须卸载 run_command ----
    reg_all = build_default_registry(include_mutating=False)
    check(not reg_all.has("run_command"), "★★ 只读模式没有 run_command")
    reg_ro = build_default_registry().filter(groups=["readonly"])
    check(not reg_ro.has("run_command"), "★★ 只读子集没有 run_command")

    # ---- 默认注册表里确实有，且分组正确 ----
    reg_default = build_default_registry(sandbox=SandboxManager(cfg()))
    check(reg_default.has("run_command"), "默认注册表包含 run_command")
    check("run_command" in reg_default.group("exec"), "分组为 exec", reg_default.group("exec"))
    check(reg_default.require("run_command").risk is RiskLevel.EXEC, "风险等级 EXEC")


# --------------------------------------------------------------------------- #
# [S6] 端到端：Agent 提命令 → 审批 → 沙箱不可用 → 明确失败（不伪造成功）
# --------------------------------------------------------------------------- #
async def test_agent_flow(ws: str) -> None:
    print("\n[S6] Agent 端到端：审批通过但沙箱不可用")

    from backend.agent.core import AgentConfig, AgentCore
    from backend.events import EventBus
    from backend.models.adapter import FakeAdapter, ModelReply, ToolCall

    class NoDocker(SandboxManager):
        async def probe(self, *, force: bool = False):
            from backend.sandbox.manager import DockerStatus

            return DockerStatus(available=False, image="loom-sandbox:py312",
                                detail="模拟：Docker 没有安装")

    reg = ToolRegistry()
    tool, _ = build_tool(FakeDocker())
    tool.sandbox = NoDocker(cfg())
    reg.register(tool, group="exec")
    from backend.tools.readonly import list_files, read_file

    reg.register(list_files.make(), group="readonly")
    reg.register(read_file.make(), group="readonly")

    script = [
        ModelReply(content="先跑一下测试。"),
        ModelReply(tool_calls=[ToolCall(id="c1", name="run_command",
                                        arguments={"command": "python", "args": ["-m", "pytest"]},
                                        arguments_raw='{"command":"python","args":["-m","pytest"]}')]),
        ModelReply(content="命令没能执行，Docker 不可用。"),
    ]
    bus = EventBus()
    results_ev: list[Any] = []

    async def collect(ev: Any) -> None:
        results_ev.append(ev)

    bus.subscribe(collect)

    core = AgentCore(
        FakeAdapter(script),
        reg,
        config=AgentConfig(max_turns=4, max_seconds=20, approval_timeout_s=5, min_evidence=0),
        bus=bus,
    )
    task = asyncio.create_task(core.run("跑测试", workspace=ws))
    approved = False
    for _ in range(500):
        ids = core.approvals.waiting_ids
        if ids:
            approved = core.approvals.approve(ids[0], "同意")
            break
        if task.done():
            break
        await asyncio.sleep(0.01)

    # ★ core.run() 内部会自己造 TaskState，所以要接住它的返回值
    state = await asyncio.wait_for(task, timeout=20)
    await asyncio.sleep(0.05)

    check(approved, "★★ Agent 确实停下来等审批了（不是直接执行）")

    tool_results = [e for e in results_ev if e.type == "tool.result"]
    check(tool_results and tool_results[-1].data.get("ok") is False,
          "★★ 审批通过后仍然被沙箱拦下（ok=False）")
    check(tool_results and tool_results[-1].data.get("error", {}).get("kind") == "sandbox_unavailable",
          "★★ 错误类型是 sandbox_unavailable",
          tool_results[-1].data.get("error", {}).get("kind") if tool_results else None)
    check(state.status.value in ("done", "failed", "aborted"), f"任务收敛（{state.status.value}）")
    check(not state.evidence_summary, "★ 失败的执行没有产生证据（不伪造成功）",
          len(state.evidence_summary))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def amain() -> int:
    ws = make_ws()
    try:
        await test_args(ws)
        test_argv(ws)
        await test_run_flow(ws)
        await test_no_docker(ws)
        await test_policy(ws)
        await test_agent_flow(ws)
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    ok = sum(1 for c, _ in results if c)
    total = len(results)
    print("\n" + "=" * 78)
    print(f"结果：{ok}/{total} 通过")
    if ok != total:
        print(f"\n失败项（{total - ok} 条）：")
        for c, label in results:
            if not c:
                print(f"  ✗ {label}")
    print("=" * 78)
    print("注：本文件全绿只说明控制流正确；真实隔离效果需 Docker，见 test_sandbox_docker.py")
    return 0 if ok == total else 1


def main() -> int:
    global VERBOSE
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    VERBOSE = a.verbose
    print("=" * 78)
    print("Loom 沙箱自检（离线，无需 Docker）")
    print("=" * 78)
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
