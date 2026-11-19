"""
MCP Server 配置（M4.1）。

★ 配置文件长什么样
------------------
```json
{
  "servers": [
    {
      "id": "fs-demo",
      "command": "C:/Python313/python.exe",
      "args": ["D:/tools/fs_server.py"],
      "env": {"DEMO_MODE": "1"},
      "enabled": true,
      "trust": "prompt",
      "read_tools": ["read_note"]
    }
  ]
}
```

★ 安全边界（这些是硬规则，不是建议）
--------------------------------
1. **命令与参数分开传**：`command` + `args` 直接交给 `subprocess`，
   不经过 shell，也不接受 `"command": "python a.py && rm -rf /"` 这种字符串。
2. **环境变量是白名单**：默认只继承一组"跑进程必需"的键（PATH/SYSTEMROOT/TEMP…），
   再叠加配置里显式写的 `env`；名字里带 KEY/TOKEN/SECRET/PASSWORD 的**默认剔除**，
   除非配置里显式声明（显式声明=用户知情）。
3. **默认要审批**：`trust` 缺省是 `prompt`（所有工具都走审批）；
   只有配置里明确写 `trust: "read"` 或把工具列进 `read_tools`，才当作只读。
   工具自己的描述/schema 是**不可信输入**，不能用来降低风险等级。
4. **配置无效就拒绝启动**：缺 id / 缺 command / args 不是字符串列表 / id 非法，
   一律在加载阶段报错，错误信息里不回显 env 的值（只回显键名）。

★ 关于 command 能不能只写名字（`agent-desktop` 这种）
--------------------------------
**能写，但必须在 PATH 或 `LOOM_MCP_EXTRA_PATH` 里找得到真身。**
MCP 子进程是 `CreateProcess` 直起的（不过 shell），这意味着：
  · 它不会补 `PATHEXT`（写 `xxx` 不会自动试 `xxx.exe`）；
  · 它用的是**当前进程**的 PATH —— 服务由 .bat / Tauri / 桌面快捷方式拉起时，
    这个 PATH 常常比你的交互式 shell 短（实测：shell 里有 node 目录，服务里没有）。
两者的组合就是那个很难查的现象：**同一条命令 shell 里能跑、服务里报
`FileNotFoundError [WinError 2]`**，而且错误里不告诉你它在找什么。
所以这里加一层显式解析，把"找没找到、解析到哪"写进连接状态里。

★ 为什么解析到 `.cmd`/`.bat` 直接拒绝，而不套一层 `cmd /c`
--------------------------------
`cmd /c` 会把 shell 语义（`&` `|` `>` 变量展开）重新引进来，破掉上面第 1 条硬规则。
npm 全局包的 `.cmd` 只是个三行包装，真身通常是
`node_modules/<pkg>/bin/<pkg>-win32-x64.exe`；请直接把 command 指向它。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# 子进程默认继承的环境变量（只列"跑起来必需"的，其余一律不给）
INHERIT_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
})

# 名字像密钥的环境变量：默认不继承，也不允许从父进程带进来
SENSITIVE_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

# 配置里显式写这些键也会被拒绝（避免把主进程的凭据透给外部 Server）
_FORBIDDEN_ENV_KEYS = frozenset({"OPENAI_API_KEY", "XFYUN_API_KEY", "BAILIAN_API_KEY", "DEEPSEEK_API_KEY"})


class McpConfigError(ValueError):
    """配置无效（消息里只出现键名/结构问题，不回显值）。"""


@dataclass
class McpServerConfig:
    server_id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True
    # prompt = 所有工具都要审批；read = 该 Server 的工具都当只读（显式授权）
    trust: str = "prompt"
    # 细粒度授权：这些工具当只读（仅当 trust=prompt 时生效）
    read_tools: tuple[str, ...] = ()
    timeout_s: float = 30.0
    # 命令解析的诊断信息（空 = 没问题）。连接失败时拼进 error，
    # 让"找不到命令"这类问题一眼能看出是配置还是环境。
    resolve_note: str = ""

    def risk_for(self, tool_name: str):
        """按配置决定工具的风险等级（默认 WRITE = 需要审批）。"""
        from ..agent.state import RiskLevel

        if self.trust == "read":
            return RiskLevel.READ
        if tool_name in self.read_tools:
            return RiskLevel.READ
        return RiskLevel.WRITE


@dataclass
class McpConfig:
    enabled: bool = False
    config_path: str = ""
    call_timeout_s: float = 30.0
    startup_timeout_s: float = 15.0
    max_result_chars: int = 12_000
    servers: list[McpServerConfig] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.servers)

    @classmethod
    def from_env(cls) -> "McpConfig":
        from ..config import _env, _env_float  # 复用同一套 env 读取

        path = _env("LOOM_MCP_CONFIG", "")
        enabled = _env("LOOM_MCP", "0").lower() not in ("0", "false", "off", "no")
        cfg = cls(
            enabled=enabled,
            config_path=path,
            call_timeout_s=_env_float("LOOM_MCP_CALL_TIMEOUT", 30.0),
            startup_timeout_s=_env_float("LOOM_MCP_START_TIMEOUT", 15.0),
            max_result_chars=max(1000, int(_env_float("LOOM_MCP_MAX_RESULT_CHARS", 12_000))),
        )
        if enabled and path:
            cfg.servers = load_servers(path)
        return cfg


def _extra_search_dirs() -> list[str]:
    """`LOOM_MCP_EXTRA_PATH` 里额外要搜的目录（os.pathsep 分隔，空项忽略）。"""
    raw = os.environ.get("LOOM_MCP_EXTRA_PATH", "")
    return [p for p in raw.split(os.pathsep) if p.strip()]


def _looks_like_path(command: str) -> bool:
    """带目录分隔符就当路径看（含 ./x 与 C:/x 两种写法）。"""
    seps = [os.sep]
    if os.altsep:
        seps.append(os.altsep)
    seps.append("/")          # Windows 上 subprocess 也认正斜杠，这里一并当路径
    return any(s in command for s in seps)


def _reject_script_wrapper(requested: str, found: str) -> tuple[str, str]:
    """
    `.cmd` / `.bat` 不能直接 CreateProcess —— 与其让它失败成 WinError 2，
    不如在这里说清楚该改什么。（只在 Windows 上判，别的平台没有这个后缀约定。）
    """
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return requested, (
            f"命令 {requested!r} 指向 {found}，但 .cmd/.bat 必须经 shell 才能执行，"
            f"而 MCP 子进程刻意不经过 shell（模块 docstring 第 1 条硬规则）。"
            f"请改成该脚本内部真正的 .exe —— npm 包一般在 "
            f"node_modules/<包名>/bin/ 下有个原生可执行文件。"
        )
    return found, ""


def resolve_command(command: str) -> tuple[str, str]:
    """
    把配置里的 command 解析成**可直接 CreateProcess 的路径**。

    返回 `(要执行的命令, 诊断信息)`；诊断信息为空表示一切正常。
    ★ 不抛异常：解析失败也要让 Server 进"可诊断的 failed"，而不是拖垮整个配置文件。

    顺序：显式路径（只查存在性）→ 当前 PATH → `LOOM_MCP_EXTRA_PATH`。
    `.cmd`/`.bat` 一律拒绝（原因见模块 docstring）。
    """
    if _looks_like_path(command):
        if not os.path.isfile(command):
            return command, (
                f"command 指向的文件不存在：{command}。"
                f"（子进程不过 shell，不会做任何路径/后缀补全，必须写真实存在的绝对路径）"
            )
        return _reject_script_wrapper(command, command)

    found = shutil.which(command)
    if found is None:
        for d in _extra_search_dirs():
            found = shutil.which(command, path=d)
            if found:
                break
    if found is None:
        extra = _extra_search_dirs()
        where = f"PATH 与 LOOM_MCP_EXTRA_PATH（{len(extra)} 个目录）" if extra else "当前 PATH"
        return command, (
            f"在{where}里都找不到命令 {command!r}。"
            f"★ 注意：服务进程的 PATH 常常比你的 shell 短（.bat / Tauri 拉起时尤其明显）。"
            f"请把 command 改成绝对路径，或把该命令所在目录加进 LOOM_MCP_EXTRA_PATH。"
        )
    return _reject_script_wrapper(command, found)


def load_servers(path: str) -> list[McpServerConfig]:
    """读配置文件；任何结构问题都抛 McpConfigError（不回显 env 值）。"""
    p = Path(path)
    if not p.is_file():
        raise McpConfigError(f"MCP 配置文件不存在：{path}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpConfigError(f"MCP 配置文件读不出来：{type(exc).__name__}") from exc

    items = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise McpConfigError("MCP 配置缺少 servers 数组")

    out: list[McpServerConfig] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise McpConfigError(f"servers[{i}] 不是对象")
        sid = str(item.get("id") or "").strip()
        if not SERVER_ID_RE.match(sid):
            raise McpConfigError(
                f"servers[{i}] 的 id 非法（要求小写字母开头、只含 [a-z0-9_-]、≤32 字符）"
            )
        if sid in seen:
            raise McpConfigError(f"servers 里 id 重复：{sid}")
        seen.add(sid)

        command = str(item.get("command") or "").strip()
        if not command:
            raise McpConfigError(f"servers[{sid}] 缺少 command")
        if any(ch in command for ch in ("&&", "||", ";", "|", ">", "<", "$(")):
            raise McpConfigError(
                f"servers[{sid}] 的 command 含 shell 控制符；请把参数放进 args，不要拼命令行"
            )
        # 解析成可直接执行的路径（失败不抛，记进 resolve_note 供连接失败时展示）
        resolved_command, resolve_note = resolve_command(command)

        args = item.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpConfigError(f"servers[{sid}] 的 args 必须是字符串数组")

        env = item.get("env", {})
        if not isinstance(env, dict):
            raise McpConfigError(f"servers[{sid}] 的 env 必须是对象")
        clean_env: dict[str, str] = {}
        for k, v in env.items():
            key = str(k)
            if key in _FORBIDDEN_ENV_KEYS:
                raise McpConfigError(f"servers[{sid}] 的 env 不允许携带主进程凭据：{key}")
            if not isinstance(v, (str, int, float, bool)):
                raise McpConfigError(f"servers[{sid}] 的 env.{key} 只能是标量")
            clean_env[key] = str(v)

        trust = str(item.get("trust") or "prompt").lower()
        if trust not in ("prompt", "read"):
            raise McpConfigError(f"servers[{sid}] 的 trust 只能是 prompt / read")

        read_tools = item.get("read_tools", [])
        if not isinstance(read_tools, list) or not all(isinstance(t, str) for t in read_tools):
            raise McpConfigError(f"servers[{sid}] 的 read_tools 必须是字符串数组")

        cwd = str(item.get("cwd") or "")
        if cwd and not Path(cwd).is_dir():
            raise McpConfigError(f"servers[{sid}] 的 cwd 不存在或不是目录")

        out.append(McpServerConfig(
            server_id=sid,
            command=resolved_command,
            args=list(args),
            env=clean_env,
            cwd=cwd,
            enabled=bool(item.get("enabled", True)),
            trust=trust,
            read_tools=tuple(read_tools),
            timeout_s=float(item.get("timeout_s") or 0) or 0.0,
            resolve_note=resolve_note,
        ))
    return out


def build_child_env(server: McpServerConfig) -> dict[str, str]:
    """
    构造子进程环境：白名单继承 + 配置显式覆盖。

    ★ 为什么不是 `os.environ.copy()`：那会把模型 API Key 一起交给外部进程。
      白名单里没有的键，父进程有也不给。
    ★ 显式配置的键即使名字像密钥也放行（用户知情），但仍禁止主进程那几个凭据键。
    """
    env: dict[str, str] = {}
    for key in INHERIT_ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val:
            env[key] = val
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    for key, val in server.env.items():
        if key in _FORBIDDEN_ENV_KEYS:
            continue
        env[key] = val
    return env


def is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    return any(h in upper for h in SENSITIVE_ENV_HINTS)


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """给事件/健康检查用：只保留键名，值一律打码。"""
    return {k: ("***" if is_sensitive_env_name(k) else "<set>") for k in env}
