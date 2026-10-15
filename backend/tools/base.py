"""
Loom Agent v0.1 · 工具契约

★ 核心命题：**统一的返回形状**
────────────────────────────────────────────────────────────────
所有工具，无论成功失败，一律返回同一个结构：

    {
      "ok": bool,                  # 成功与否（唯一判定字段）
      "tool": str,                 # 工具名
      "summary": str,              # 一句话摘要（给模型看 + 给 UI 显示）
      "data": Any,                 # 结构化数据（给程序用，可空）
      "error": {...} | None,       # 失败时的错误详情
      "side_effects": [...]        # 本次调用造成的外部影响
    }

为什么必须统一？
  ① 模型侧：OBSERVE 阶段要把它序列化成 tool 消息。形状不一致就得写各种 if。
  ② 程序侧：State.record_tool_call() 要统一留痕，算 Evidence、算熔断指纹。
  ③ UI 侧：TOOL_RESULT 事件要统一渲染。形状不一致前端就得写各种分支。

代价是「每个工具都要包装一下」，收益是整个链路只有一条通路。
老项目每个工具自己 return 字符串 / dict 混用，是后来很多麻烦的根源。

────────────────────────────────────────────────────────────────
★ 11 种错误类型（ErrorKind）
────────────────────────────────────────────────────────────────
错误分类不是为了好看，而是为了**决定下一步做什么**：

    not_found            → 模型路径写错了，让它换一个路径重试
    not_a_file / not_a_dir → 类型不对，让模型纠正
    permission_denied    → 越界或系统权限，**不该重试**，要让模型明白边界
    invalid_arguments    → 参数不合 schema，让模型改参数
    path_outside_workspace → 越界（安全事件），必须上报且不重试
    sensitive_file       → 命中敏感名单，需要特别提醒
    too_large            → 内容过大，已截断（ok=True 但带提示）
    timeout              → 可重试
    execution_error      → 工具内部异常
    not_implemented      → 保证 Agent 不会调到不存在的功能
    internal_error       → 兜底

把「可重试性」也编码进错误里（retryable 字段），Agent 的 REFLECT 阶段
就能据此决定「换个参数再试」还是「如实告诉用户做不到」。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..agent.state import RiskLevel


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class ErrorKind(str, Enum):
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    NOT_A_DIR = "not_a_dir"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ARGUMENTS = "invalid_arguments"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    SENSITIVE_FILE = "sensitive_file"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


# 哪些错误值得让模型换个姿势再试一次
RETRYABLE_KINDS: set[ErrorKind] = {
    ErrorKind.NOT_FOUND,
    ErrorKind.NOT_A_FILE,
    ErrorKind.NOT_A_DIR,
    ErrorKind.INVALID_ARGUMENTS,
    ErrorKind.TIMEOUT,
}

# 哪些错误属于「安全事件」，必须显著上报（前端可高亮）
SECURITY_KINDS: set[ErrorKind] = {
    ErrorKind.PATH_OUTSIDE_WORKSPACE,
    ErrorKind.SENSITIVE_FILE,
    ErrorKind.PERMISSION_DENIED,
}


# --------------------------------------------------------------------------- #
# 统一返回
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    """工具执行结果（唯一的返回形状）。"""

    ok: bool
    tool: str
    summary: str
    data: Any = None
    error: dict[str, Any] | None = None
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    # ---- 构造 --------------------------------------------------------------
    @staticmethod
    def success(
        tool: str,
        summary: str,
        data: Any = None,
        side_effects: list[dict[str, Any]] | None = None,
        duration_ms: int = 0,
    ) -> "ToolResult":
        return ToolResult(
            ok=True,
            tool=tool,
            summary=summary,
            data=data,
            side_effects=side_effects or [],
            duration_ms=duration_ms,
        )

    @staticmethod
    def failure(
        tool: str,
        kind: ErrorKind,
        message: str,
        *,
        hint: str = "",
        data: Any = None,
        duration_ms: int = 0,
    ) -> "ToolResult":
        """
        hint 是给模型的「怎么改」提示。经验：带 hint 的错误能让模型
        自我修正的成功率大幅提高（"路径不存在，请用 list_files 先确认"）。
        """
        return ToolResult(
            ok=False,
            tool=tool,
            summary=message,
            data=data,
            error={
                "kind": kind.value,
                "message": message,
                "hint": hint,
                "retryable": kind in RETRYABLE_KINDS,
                "security": kind in SECURITY_KINDS,
            },
            duration_ms=duration_ms,
        )

    # ---- 序列化 ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "summary": self.summary,
            "data": self.data,
            "error": self.error,
            "side_effects": self.side_effects,
            "duration_ms": self.duration_ms,
        }

    def to_message_content(self, max_chars: int = 8000) -> str:
        """
        转成喂给模型的 tool 消息文本。

        注意：这里喂的是 **完整内容**（截断到 max_chars），
        因为模型需要看到实际文件内容才能工作。
        而 summary 只是「一句话摘要」，另作 Evidence 用。
        """
        import json

        if not self.ok and self.error:
            parts = [f"[失败] {self.summary}"]
            if self.error.get("hint"):
                parts.append(f"建议：{self.error['hint']}")
            parts.append(f"错误类型：{self.error.get('kind')}")
            return "\n".join(parts)[:max_chars]

        # 成功：优先喂 data（内容），没有 data 就喂 summary
        if self.data is None:
            return self.summary[:max_chars]

        if isinstance(self.data, str):
            body = self.data
        else:
            body = json.dumps(self.data, ensure_ascii=False, indent=2, default=str)

        text = f"{self.summary}\n\n{body}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n…（内容被截断，原长 {len(text)} 字符）"
        return text


# --------------------------------------------------------------------------- #
# 工具上下文
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    """
    工具执行时的环境信息。

    为什么要把 workspace 通过 context 传，而不是让工具自己去读全局配置？
      → 因为工具必须是「无全局状态」的纯函数式组件。传给它的 workspace 是什么，
        它就只能在那个范围内活动。这样测试时可以随时换工作区，
        也杜绝了「工具偷偷访问工作区外」的可能。
    """

    workspace: str                    # 工作区根目录（已 realpath 的绝对路径）
    task_id: str = ""
    turn: int = 0
    # 由 Policy 填入本次调用的授权凭据（v0.1a 用）
    approval_id: str = ""
    # 是否已获授权（Policy 判定后置 True；写工具检查它）
    authorized: bool = False

    # 可注入的配置（避免工具直接 import config，保证可测试）
    max_read_chars: int = 40_000
    ignored_dir_names: frozenset[str] = field(default_factory=frozenset)
    sensitive_names: frozenset[str] = field(default_factory=frozenset)
    sensitive_dir_parts: frozenset[str] = field(default_factory=frozenset)
    backup_dir_name: str = ".loom_backup"


# --------------------------------------------------------------------------- #
# 工具基类
# --------------------------------------------------------------------------- #
class Tool(ABC):
    """
    工具抽象基类。

    子类只需要：
      ① 定义元信息（name / description / parameters / risk）
      ② 实现 run(args, ctx) -> ToolResult
    其余（schema 暴露、审批判定、留痕）由框架统一处理。
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    # ★ 权限边界由「是否有副作用」决定，而不是由工具名决定
    risk: RiskLevel = RiskLevel.READ

    @property
    def needs_approval(self) -> bool:
        """
        是否需要用户批准。

        默认规则：只读不需要，其余都需要。
        ★ 注意这里是 **工具级默认**。真正判定在 policy.py 里 ——
          因为像 run_command 这种工具，同一条命令可能是只读（git status）
          也可能有副作用（rm），必须按「完整命令 + 参数」逐次判定。
        """
        return self.risk != RiskLevel.READ

    # ---- 参数校验 ----------------------------------------------------------
    def validate_args(self, args: dict[str, Any]) -> str:
        """
        轻量 schema 校验（不引入 jsonschema 依赖）。

        只查：必填项存在 + 类型大致正确。返回 "" 表示通过，否则返回错误说明。
        为什么不用 jsonschema？—— 参数结构都很浅（一层平铺），
        手写 30 行比引依赖更容易看懂，也少一层黑箱。
        """
        props = self.parameters.get("properties", {}) or {}
        required = self.parameters.get("required", []) or []

        for key in required:
            if key not in args or args[key] is None:
                return f"缺少必填参数 '{key}'"

        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in args.items():
            spec = props.get(key)
            if not spec:
                continue  # 未声明的额外参数先放过（模型有时会加解释字段）
            want = spec.get("type")
            py = type_map.get(want)
            if py is None or value is None:
                continue
            # bool 是 int 的子类，得单独挡一下
            if want == "integer" and isinstance(value, bool):
                return f"参数 '{key}' 应为整数，收到布尔值"
            if not isinstance(value, py):
                return f"参数 '{key}' 应为 {want}，收到 {type(value).__name__}"
        return ""

    # ---- 执行 --------------------------------------------------------------
    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """真正执行。子类实现。异常请自行捕获并转成 ToolResult.failure。"""

    # ---- 给模型看的声明 ----------------------------------------------------
    def to_spec(self) -> Any:
        from ..models.adapter import ToolSpec

        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters or {"type": "object", "properties": {}},
        )

    # ---- 审批展示信息 ------------------------------------------------------
    def describe_action(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, str]:
        """
        生成审批界面要展示的三要素：要做什么 / 对谁做 / 有什么影响。

        ★ 关键：这些文案由**工具自己**提供，不是让模型自由发挥。
          否则模型可以写一段「这很安全」来骗用户点批准。
        """
        return {
            "action": self.description.split("。")[0] if self.description else self.name,
            "target": str(args.get("path", "") or args.get("command", "")),
            "impact": "",
            "preview": "",
        }


# --------------------------------------------------------------------------- #
# 计时辅助
# --------------------------------------------------------------------------- #
class timed:
    """with timed() as t: ... → t.ms 拿到毫秒。省得每个工具都手写 time.time()。"""

    def __enter__(self) -> "timed":
        self._t0 = time.time()
        self.ms = 0
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.ms = int((time.time() - self._t0) * 1000)
        return False
