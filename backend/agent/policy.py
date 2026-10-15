"""
Loom Agent v0.1 · Policy Engine（执行前校验链）

★ 一句话原则：**模型的输出是「建议」，不是「权限」。**
  模型说「我要写这个文件」只是表达了意图；
  能不能真写，由 Policy 一条链走完才决定。

────────────────────────────────────────────────────────────────
校验链（顺序不能乱，每一步都可能短路）
────────────────────────────────────────────────────────────────
   ① 工具存在？          → 不存在直接失败（not_implemented）
   ② 参数 JSON 可解析？   → parse_error 直接失败（invalid_arguments）
   ③ schema 校验通过？    → 失败则回灌给模型改参数
   ④ 预算还有余量？       → 超了强制收敛，不再执行任何工具
   ⑤ 路径在工作区内？     → 越界 = 安全事件，失败且不可重试
   ⑥ 是否需审批？         → risk != read 时走审批，拿到授权才继续
   ⑦ 放行

为什么预算检查放在路径检查**之前**？
  → 因为预算耗尽意味着任务该结束了。此时即便路径合法也不该再执行 —— 
    否则一个已经超时的任务还能继续写文件，用户会以为它早停了。

为什么审批放在最后？
  → 因为「不值得执行的调用」不该打扰用户。参数都不合法，
    先让模型改参数，别拿一个注定失败的操作去问用户。
    （这直接决定了用户体验：审批弹窗越少越精准越好。）

────────────────────────────────────────────────────────────────
Policy 与 Tool 的分工
────────────────────────────────────────────────────────────────
    Policy  决定「能不能调」        —— 与具体工具无关的通用规则
    Tool    决定「怎么调、调了会怎样」—— 领域细节（写文件 / 跑命令）

所以 Policy 只依赖 Tool 暴露的元信息（risk / needs_approval / describe_action），
不 import 任何具体工具。加新工具时 Policy 一行都不用改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..tools.base import ErrorKind, Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from .state import RiskLevel, TaskState

log = logging.getLogger("loom.policy")


# --------------------------------------------------------------------------- #
# 判定结果
# --------------------------------------------------------------------------- #
@dataclass
class PolicyDecision:
    """
    Policy 的裁定。

    allow=True  → 可以执行（可能附带 approval_id 表示「已获授权」）
    allow=False → 不执行，result 里是要回灌给模型的错误
    """

    allow: bool
    result: ToolResult | None = None      # allow=False 时的失败结果
    needs_approval: bool = False          # 需要走审批流程
    approval_id: str = ""                 # 已获授权的凭据
    reason: str = ""                      # 给人看的裁定理由（进日志）
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def deny(result: ToolResult, reason: str = "") -> "PolicyDecision":
        return PolicyDecision(allow=False, result=result, reason=reason)

    @staticmethod
    def permit(approval_id: str = "", reason: str = "") -> "PolicyDecision":
        return PolicyDecision(allow=True, approval_id=approval_id, reason=reason)

    @staticmethod
    def require_approval(reason: str, meta: dict[str, Any] | None = None) -> "PolicyDecision":
        return PolicyDecision(allow=False, needs_approval=True, reason=reason, meta=meta or {})


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
class Policy:
    """
    执行前校验链的执行者。

    无状态（除了注入的 registry）—— 每次判定所需的状态都从传入的 TaskState 读。
    这样它天然可测试：造一个 TaskState 就能断言裁定结果。
    """

    def __init__(self, registry: ToolRegistry, *, allowed_tools: set[str] | None = None) -> None:
        self.registry = registry
        # 工具白名单（None = 全开）。用于「以只读模式运行」这类降权场景。
        self.allowed_tools = allowed_tools

    # ===================================================================== #
    # 主入口
    # ===================================================================== #
    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        state: TaskState,
        ctx: ToolContext,
        *,
        parse_error: str = "",
        already_approved: str = "",
    ) -> PolicyDecision:
        """
        走完整条校验链。

        参数：
            parse_error      模型给的参数 JSON 解析失败时的原因（来自 ModelAdapter）
            already_approved 已持有的 approval_id（恢复执行时用）
        """

        # ---- ① 工具存在 -------------------------------------------------
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.PERMISSION_DENIED,
                    f"工具 '{tool_name}' 不在本次任务允许的范围内",
                    hint=f"可用工具：{', '.join(sorted(self.allowed_tools))}",
                ),
                reason="工具不在白名单",
            )

        tool = self.registry.get(tool_name)
        if tool is None:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.NOT_IMPLEMENTED,
                    f"没有名为 '{tool_name}' 的工具",
                    hint=f"可用工具：{', '.join(sorted(self.registry.names))}",
                ),
                reason="工具不存在",
            )

        # ---- ② 参数 JSON 可解析 -----------------------------------------
        if parse_error:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"你提供的参数不是合法 JSON：{parse_error}",
                    hint="请重新输出合法的 JSON 对象作为参数。",
                ),
                reason="参数 JSON 解析失败",
            )

        # ---- ③ schema 校验 ----------------------------------------------
        schema_err = tool.validate_args(args)
        if schema_err:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.INVALID_ARGUMENTS,
                    schema_err,
                    hint=f"参数格式要求：{tool.parameters}",
                ),
                reason=f"schema 校验失败：{schema_err}",
            )

        # ---- ④ 预算 -----------------------------------------------------
        violation = state.budget_violation()
        if violation is not None:
            kind, limit, used = violation
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.EXECUTION_ERROR,
                    f"任务预算已耗尽（{kind}: {used}/{limit}），不再执行任何工具",
                    hint="请基于已有信息直接给出最终结论。",
                ),
                reason=f"预算耗尽：{kind} {used}/{limit}",
            )

        # ---- ⑤ 重复熔断（软：允许探索，超次数才拦）----------------------
        if state.should_break_repeat(tool_name, args):
            n = state.budgets.max_same_call_repeat
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.INVALID_ARGUMENTS,
                    f"同一调用已重复 {n} 次（工具={tool_name}，参数未变），判定为卡住",
                    hint=(
                        "换个思路：改参数、换工具，或基于已知信息给出结论。"
                        "重复同样的调用不会有新结果。"
                    ),
                ),
                reason="重复指纹熔断",
            )

        # ---- ⑤b 确定性失败熔断（硬：撞过一次就不许再撞）------------------
        # ★ 为什么在软熔断之后单独加一条？
        #   软熔断要数到 3 次才拦。但对「参数不变、结论必然不变」的失败
        #   （路径越界 / 敏感文件 / 用户已拒绝），第 2 次调用是纯浪费 ——
        #   实测真实模型会拿同样的参数连撞两次才换思路。
        #   这里让第二次就直接拦下，并把「不可重试」明确告诉模型，
        #   省下的是真金白银的 token 和一个轮次。
        prev_fail = state.failed_before(tool_name, args)
        if prev_fail is not None:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.PERMISSION_DENIED,
                    (
                        f"这次调用上次已经失败过，且属于**不可重试**的错误"
                        f"（{prev_fail.error_kind}）：{prev_fail.summary[:120]}"
                    ),
                    hint=(
                        "参数完全相同、错误原因相同，再调用一次结果不会变。"
                        "请换一个目标、换一种方法，或直接向用户说明这个限制。"
                    ),
                ),
                reason=f"确定性失败熔断（{prev_fail.error_kind}）",
            )

        # ---- ⑥ 审批 -----------------------------------------------------
        if tool.needs_approval:
            if already_approved:
                # 已授权 → 放行，但要把授权凭据带下去（工具自身还会再检查一次）
                return PolicyDecision.permit(
                    approval_id=already_approved, reason=f"{tool_name} 已获用户批准"
                )

            info = tool.describe_action(args, ctx)
            return PolicyDecision.require_approval(
                reason=f"{tool_name} 有副作用（risk={tool.risk.value}），需用户批准",
                meta={
                    "tool": tool_name,
                    "risk": tool.risk.value,
                    "action": info.get("action", ""),
                    "target": info.get("target", ""),
                    "impact": info.get("impact", ""),
                    "preview": info.get("preview", ""),
                },
            )

        # ---- ⑦ 放行 -----------------------------------------------------
        return PolicyDecision.permit(reason="只读工具，无需审批")

    # ===================================================================== #
    # 授权后的再校验（★ TOCTOU 防护）
    # ===================================================================== #
    def recheck_after_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        state: TaskState,
        ctx: ToolContext,
        pending: Any,
    ) -> PolicyDecision:
        """
        用户点「批准」之后、真正执行之前，再校验一次。

        ★ 为什么需要这一步？（TOCTOU = Time-of-Check to Time-of-Use）

        从「发起审批」到「用户批准」之间可能过了很久（默认 180 秒）。这期间：

          ① **文件可能被别人改了**。用户批准时看到的是旧内容，
             执行时磁盘上已经是新内容 —— 直接覆盖就丢了别人的改动。
             这个由 v0.1b 的 stale_content 检测处理，v0.1a 先用 args_hash 挡住。

          ② **参数可能被换掉**。如果程序有 bug 或事件流被篡改，
             批准时展示的是 read_file("a.py")，执行时却变成 write_file(".env")。
             这就是 args_hash 存在的意义 —— 比对指纹，不一致直接拒绝。

          ③ **工作区可能被换**。理论上不会，但检查一下成本极低。

        本方法就是这三道防线。
        """
        from ..tools.path_safety import resolve_in_workspace, PathViolation
        from .approval import args_fingerprint

        tool = self.registry.get(tool_name)
        if tool is None:
            return PolicyDecision.deny(
                ToolResult.failure(tool_name, ErrorKind.NOT_IMPLEMENTED, f"工具已不存在：{tool_name}"),
                reason="审批期间工具被移除",
            )

        # ---- 指纹比对 ---------------------------------------------------
        fresh_hash = args_fingerprint(tool_name, args)
        if pending is not None and getattr(pending, "args_hash", "") and fresh_hash != pending.args_hash:
            log.warning(
                "审批指纹不匹配：批准时=%s 执行时=%s（tool=%s）",
                pending.args_hash, fresh_hash, tool_name,
            )
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.PERMISSION_DENIED,
                    "参数在审批期间发生了变化，已拒绝执行",
                    hint="这是安全保护。请重新发起这次操作并再次确认。",
                ),
                reason="args_hash 不匹配（防「批准 A 执行 B」）",
            )

        # ---- 路径重新校验（TOCTOU）-------------------------------------
        if tool.risk in (RiskLevel.WRITE, RiskLevel.DELETE):
            candidate = args.get("path") or args.get("target")
            if candidate:
                try:
                    resolve_in_workspace(state.workspace, candidate, must_exist=False)
                except PathViolation as exc:
                    return PolicyDecision.deny(
                        ToolResult.failure(
                            tool_name,
                            ErrorKind.PATH_OUTSIDE_WORKSPACE,
                            f"执行前复检发现路径越界：{exc}",
                            hint="路径在审批期间变得不可信，操作已中止。",
                        ),
                        reason="审批后路径复检失败",
                    )

        # ---- 预算再查（审批可能等了很久，时长预算该重新算）-------------
        violation = state.budget_violation()
        if violation is not None:
            kind, limit, used = violation
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.EXECUTION_ERROR,
                    f"等待审批期间任务预算已耗尽（{kind}: {used}/{limit}）",
                    hint="请基于已有信息给出结论。",
                ),
                reason=f"审批后预算耗尽：{kind}",
            )

        return PolicyDecision.permit(approval_id=getattr(pending, "approval_id", ""), reason="审批后复检通过")


# --------------------------------------------------------------------------- #
# ToolContext 工厂
# --------------------------------------------------------------------------- #
def make_context(
    state: TaskState,
    *,
    approval_id: str = "",
    authorized: bool = False,
) -> ToolContext:
    """
    从 TaskState 造一个 ToolContext。

    ★ 关键：ToolContext 里的配置（忽略目录、敏感名单、上限）
      从 config 读一次，注入进 context。
      工具自己不 import config —— 保证工具是纯函数式的、可单测的。
    """
    from ..config import (
        BACKUP_DIR_NAME,
        IGNORED_DIR_NAMES,
        MAX_READ_CHARS,
        SENSITIVE_DIR_PARTS,
        SENSITIVE_NAMES,
    )

    return ToolContext(
        workspace=state.workspace,
        task_id=state.task_id,
        turn=state.turn,
        approval_id=approval_id,
        authorized=authorized,
        max_read_chars=MAX_READ_CHARS,
        ignored_dir_names=frozenset(IGNORED_DIR_NAMES),
        sensitive_names=frozenset(SENSITIVE_NAMES),
        sensitive_dir_parts=frozenset(SENSITIVE_DIR_PARTS),
        backup_dir_name=BACKUP_DIR_NAME,
    )
