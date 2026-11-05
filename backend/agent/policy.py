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
   ⑤ 重复熔断（软）      → 同参调用超限
   ⑤b 确定性失败熔断（硬）→ 撞过一次的确定性失败再撞
   ⑤b2 用户声明只读      → 一切写文件操作
   ⑤c 禁止路径          → 用户显式声明的禁止范围
   ⑤d 允许范围          → 用户显式声明的允许范围
   ⑥ 放行

────────────────────────────────────────────────────────────────
★ 步骤 5 变更（2026-11-05）：删除审批与白名单
────────────────────────────────────────────────────────────────
   已删除：
   - allowed_tools 白名单（① 的工具范围限制）
   - ⑥ 审批关卡（risk != read 时的人工审批挂起）
   - recheck_after_approval（TOCTOU 复检）
   - PolicyDecision.needs_approval / approval_id 字段

   保留（用户拍板）：
   - ②③ 参数格式、④ 预算、⑤ 熔断（软/硬）
   - ⑤b2 只读意图、⑤c 禁止路径、⑤d 允许范围（用户显式声明的约束）

   风险接受（用户明确拍板）：
   - 无审批 → Agent 完全自主执行
   - 路径越界靠沙箱物理拦（OS 级 ACL / Docker），不靠引擎拦

────────────────────────────────────────────────────────────────
Policy 与 Tool 的分工
────────────────────────────────────────────────────────────────
    Policy  决定「能不能调」        —— 与具体工具无关的通用规则
    Tool    决定「怎么调、调了会怎样」—— 领域细节（写文件 / 跑命令）

所以 Policy 只依赖 Tool 暴露的元信息（risk / describe_action），
不 import 任何具体工具。加新工具时 Policy 一行都不用改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..tools.base import ErrorKind, Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from .scope import hits as _scope_hits, rel_in_workspace, within_allowed
from .state import RiskLevel, TaskState

log = logging.getLogger("loom.policy")


def _hits_forbidden(workspace: str, target: str, forbidden: list[str]) -> bool:
    """
    `target`（可能是绝对路径或相对路径）是否落在禁止范围内。

    ★ 故意不解析符号链接：这里判的是"用户声明不许动哪些路径"，
      与安全边界（resolve_in_workspace）是两件事。传进来的 target 已经
      被 path_safety 校验过，不会越出工作区。
    """
    raw = (target or "").replace("\\", "/")
    if raw.lower().startswith(workspace.replace("\\", "/").lower().rstrip("/") + "/"):
        raw = raw[len(workspace.replace("\\", "/").rstrip("/")) + 1:]
    return _scope_hits(raw, forbidden)


# --------------------------------------------------------------------------- #
# 判定结果
# --------------------------------------------------------------------------- #
@dataclass
class PolicyDecision:
    """
    Policy 的裁定。

    allow=True  → 可以执行
    allow=False → 不执行，result 里是要回灌给模型的错误

    ★ 步骤 5 变更：删除 needs_approval / approval_id / require_approval。
      审批已整体移除，Policy 只做「放行 / 拒绝」。
    """

    allow: bool
    result: ToolResult | None = None      # allow=False 时的失败结果
    reason: str = ""                      # 给人看的裁定理由（进日志）
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def deny(result: ToolResult, reason: str = "") -> "PolicyDecision":
        return PolicyDecision(allow=False, result=result, reason=reason)

    @staticmethod
    def permit(reason: str = "") -> "PolicyDecision":
        return PolicyDecision(allow=True, reason=reason)


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
class Policy:
    """
    执行前校验链的执行者。

    无状态（除了注入的 registry）—— 每次判定所需的状态都从传入的 TaskState 读。
    这样它天然可测试：造一个 TaskState 就能断言裁定结果。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

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
    ) -> PolicyDecision:
        """
        走完整条校验链。

        参数：
            parse_error   模型给的参数 JSON 解析失败时的原因（来自 ModelAdapter）
        """

        # ---- ① 工具存在（简化：直接 registry.get()，None 就 fail）--------
        # ★ 步骤 5：删除 allowed_tools 白名单。工具范围不再由 Policy 限制，
        #   模型能调任何已注册工具。安全边界由沙箱（Docker / ACL）承担。
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

        # ---- ⑤b2 用户声明"只读" → 一切**写文件**的操作都不放行 ----------
        # ★ 为什么单独一条：read_only 是**意图级约束**，不是路径列表。
        #   它由 AgentCore 从任务原话里识别出来（intent.constraints）。
        # ★ 为什么只拦 WRITE 不拦 EXEC：真实 E2E 里诊断任务被这条规则卡死 ——
        #   "只运行 pytest 把结果告诉我，不要修改任何文件"里，命令执行是**只读**的，
        #   却被当成写操作拦下，模型连测试都跑不了。
        #   权衡：只拦写工具；命令的文件副作用由事后审计兜底（不假装命令绝对安全）。
        if state.constraints.get("read_only") and tool.risk is RiskLevel.WRITE:
            return PolicyDecision.deny(
                ToolResult.failure(
                    tool_name,
                    ErrorKind.PERMISSION_DENIED,
                    "本次任务被用户要求「只检查、不修改」，已拒绝任何写操作",
                    hint="如果确实需要修改，请让用户明确授权，或新建一个允许修改的任务。",
                ),
                reason="用户要求只读",
            )

        # ---- ⑤c 禁止路径（用户显式声明时才生效）--------------------------
        # ★ 为什么默认不拦：不传 forbidden_paths 时列表为空，这一步直接跳过，
        #   行为与之前完全一致。只有用户明确说了"这些路径不许动"才生效。
        # ★ 为什么在这里拦而不是等事后审计：禁止路径是**意图**，不是观察；
        #   事后标记只能告诉用户"已经被改了"，拦下来才能防止既成事实。
        if state.forbidden_paths and tool.risk in (RiskLevel.WRITE, RiskLevel.EXEC):
            # ★ EXEC 只看显式的 path（如有）：run_command 把 cwd 放进禁止目录
            #   并不等于"修改"—— 去那儿跑个测试是只读行为。真正的写入副作用
            #   由事后审计兜底；按 cwd 拦会把"进去跑个测试"也误杀
            #   （真实 E2E 里诊断任务的 pytest 被连拒两次，就是这么来的）。
            target = args.get("path") if tool.risk is RiskLevel.EXEC else (
                args.get("path") or args.get("cwd")
            )
            if target and _hits_forbidden(state.workspace, str(target), state.forbidden_paths):
                return PolicyDecision.deny(
                    ToolResult.failure(
                        tool_name,
                        ErrorKind.PERMISSION_DENIED,
                        f"路径 '{target}' 在本次任务明确禁止修改的范围内",
                        hint=(
                            f"禁止范围：{', '.join(state.forbidden_paths)}。"
                            "请换一个目标，或向用户说明为什么需要改这里。"
                        ),
                    ),
                    reason="命中禁止修改路径",
                )

        # ---- ⑤d 允许范围：范围外写入在**执行前**就被拦 --------------------
        # ★ 第五批：允许范围从"事后审计标记"升级为"执行前拦截"。
        #   前提：默认 allowed=["."]（= 整个工作区）时什么都不拦，行为不变。
        #   只有用户明确限定了范围（API 传入或从原话里抽取）才生效。
        # ★ 禁止范围永远优先：它在上一步（⑤c）已经拦掉了，这里只管"允许但超出"。
        if tool.risk is RiskLevel.WRITE and state.allowed_paths:
            target = args.get("path")
            if target:
                rel = rel_in_workspace(state.workspace, str(target))
                if rel and not within_allowed(rel, state.allowed_paths):
                    return PolicyDecision.deny(
                        ToolResult.failure(
                            tool_name,
                            ErrorKind.PERMISSION_DENIED,
                            f"路径 '{rel}' 不在允许修改的范围内（允许：{', '.join(state.allowed_paths)}）",
                            hint=(
                                "需要修改这个文件的话，得由用户明确授权。"
                                "可以在答复里说明这个需求，等授权后再改。"
                            ),
                        ),
                        reason="超出允许修改范围",
                    )

        # ---- ⑥ 放行 -----------------------------------------------------
        # ★ 步骤 5：删除审批关卡。所有通过前面检查的工具调用直接放行。
        #   风险边界由沙箱（Docker / ACL 受限令牌）承担，不再由人工审批承担。
        return PolicyDecision.permit(reason=f"{tool_name} 通过校验链，直接放行")


# --------------------------------------------------------------------------- #
# ToolContext 工厂
# --------------------------------------------------------------------------- #
def make_context(
    state: TaskState,
    *,
    cancel_event: Any = None,
) -> ToolContext:
    """
    从 TaskState 造一个 ToolContext。

    ★ 关键：ToolContext 里的配置（忽略目录、敏感名单、上限）
      从 config 读一次，注入进 context。
      工具自己不 import config —— 保证工具是纯函数式的、可单测的。

    ★ 步骤 5 变更：删除 approval_id / authorized 参数。
      审批已移除，ToolContext 不再携带授权凭据。
      写工具的 authorized 检查也一并移除（见 base.py / mutating 工具）。
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
        cancel_event=cancel_event,
        # M5：子任务要继承父任务的安全约束（不是子任务自己说了算）
        allowed_paths=list(state.allowed_paths) if state.allowed_paths else None,
        forbidden_paths=list(state.forbidden_paths) if state.forbidden_paths else None,
        max_read_chars=MAX_READ_CHARS,
        ignored_dir_names=frozenset(IGNORED_DIR_NAMES),
        sensitive_names=frozenset(SENSITIVE_NAMES),
        sensitive_dir_parts=frozenset(SENSITIVE_DIR_PARTS),
        backup_dir_name=BACKUP_DIR_NAME,
        # ★ 步骤 7：把任务级模式下发到工具（terminal 用它选 ACL 强度）
        session_mode=getattr(state, "session_mode", "workspace-write"),
    )
