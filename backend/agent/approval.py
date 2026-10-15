"""
Loom Agent v0.1 · 审批管理器（人机协作的挂起 / 恢复）

★ 这是 v0.1a 的技术核心。也是老项目没做到、而真实 Coding Agent 必须有的东西。

────────────────────────────────────────────────────────────────
问题：Agent 要写文件，得先问人。这期间它该干什么？
────────────────────────────────────────────────────────────────
三种可能的设计：

  设计 A（错）「阻塞轮询」
       while not decided: await asyncio.sleep(0.5); check_flag()
       → 浪费 CPU、延迟高、多个审批会互相干扰、取消很难做干净。

  设计 B（老项目的做法）「事后审计」
       Agent 直接执行，执行完告诉用户「我改了 X」。
       → 这不是审批，是通知。用户没有拦截机会，安全上不成立。

  设计 C（本项目采用）「Future 挂起 / 恢复」★
       Agent 创建一个 asyncio.Future，把它注册进 _pending 表，然后 await 它。
       此刻 Agent 协程**真的被挂起**（不占 CPU），控制权回到事件循环，
       于是 CLI 可以读用户输入、HTTP 服务可以收 POST 请求。
       用户做出决定后，resolver 调 future.set_result(...)，
       Agent 协程从 await 处**原地苏醒**，继续往下走。

   这就是「真正的挂起-恢复」，而不是「循环检查」。代价是要理解 asyncio.Future。

────────────────────────────────────────────────────────────────
四个安全细节（都不是可选项）
────────────────────────────────────────────────────────────────
① **超时**：asyncio.wait_for(fut, timeout=180)
   用户离开电脑了怎么办？不能让 Agent 永远挂着。
   超时视同拒绝（保守选择），但单独发 APPROVAL_EXPIRED 事件，
   让用户回来时知道「刚才有过一个请求，我错过了」。

② **一次性**：resolve() 用 pop 而不是 get
   同一个 approval_id 只能被决策一次。防止重放攻击 / 前端 bug 导致重复 set_result。

③ **args_hash 指纹**：防止「批准 A，执行 B」
   发起审批时算一次，执行前再算一次。见 policy.recheck_after_approval。

④ **路由**：resolve 返回 bool，让调用方知道「这个 id 是否有效」
   CLI 打错 id、或 id 已超时被清理，都能得到明确的 false 而不是静默成功。

────────────────────────────────────────────────────────────────
与事件系统的关系
────────────────────────────────────────────────────────────────
本模块只负责「挂起-恢复」这个纯机制，**不负责发事件**。
发事件是 AgentCore 的事 —— 保持职责单一：
    AgentCore：emit(approval.request) → await approval.request(...) → emit(approval.decision)
    ApprovalManager：只管 future 表

这样 ApprovalManager 可以脱离事件总线单测（见 selfcheck_p5）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .state import ApprovalRecord, ApprovalStatus, PendingApproval, RiskLevel

log = logging.getLogger("loom.approval")


# --------------------------------------------------------------------------- #
# 参数指纹
# --------------------------------------------------------------------------- #
def args_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """
    参数指纹：工具名 + 归一化参数 的 sha256 前 16 位。

    ★ 归一化要点：
      · sort_keys=True   → {"a":1,"b":2} 与 {"b":2,"a":1} 指纹一致
      · default=str      → 遇到不可序列化对象不炸
      · 中文不转义       → 保证跨平台一致（同一份参数在不同机器指纹相同）

    为什么把工具名也纳入指纹？
      → 因为要防的是「批准 read_file(a.py)，执行 write_file(a.py)」这种情况。
        只比参数不够，名字也得进指纹。
    """
    try:
        norm = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        norm = repr(args)
    raw = f"{tool}\x00{norm}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    """一次审批的决策结果。"""

    approved: bool
    approval_id: str
    status: ApprovalStatus
    reason: str = ""
    decided_at: float = 0.0
    waited_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "approval_id": self.approval_id,
            "status": self.status.value,
            "reason": self.reason,
            "waited_s": round(self.waited_s, 2),
        }


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class ApprovalManager:
    """
    审批管理器。

    典型用法（AgentCore 内部）：

        pending = PendingApproval(
            approval_id=PendingApproval.new_id(),
            tool="write_file",
            args=args,
            args_hash=args_fingerprint("write_file", args),
            risk=RiskLevel.WRITE,
            action="覆盖文件",
            target="src/main.py",
            impact="……",
        )
        emit(approval.request, pending.to_dict())
        decision = await approvals.request(pending)      # ← 这里挂起
        emit(approval.decision, decision.to_dict())
        if decision.approved:
            ...继续执行...

    外部（CLI / HTTP 接口）在另一个协程里：

        approvals.resolve(approval_id, "approved", reason="没问题")
    """

    def __init__(self, *, timeout_s: float = 180.0, on_resolve: Callable[[Decision], None] | None = None) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._created: dict[str, float] = {}          # approval_id -> 发起时刻
        self._reasons: dict[str, str] = {}            # approval_id -> 决策理由（用户填的）
        self._on_resolve = on_resolve
        self.resolved_count = 0
        self.expired_count = 0

    # ===================================================================== #
    # 发起（Agent 侧，会挂起）
    # ===================================================================== #
    async def request(self, pending: PendingApproval) -> Decision:
        """
        发起审批并挂起等待。

        ★ 这里是整个审批机制的心脏。三行关键代码：
             fut = create_future()
             self._pending[aid] = fut
             decision = await asyncio.wait_for(fut, timeout)

        第一行：造一个「未来会有结果」的占位。
        第二行：把它登记进路由表，让外部能按 id 找到它。
        第三行：挂起本协程 —— 控制权交回事件循环，CLI/HTTP 得以运行。
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        aid = pending.approval_id
        self._pending[aid] = fut
        self._created[aid] = time.time()
        t0 = time.time()

        log.info("请求审批 %s：tool=%s target=%s（等待 %.0fs）", aid, pending.tool, pending.target, self.timeout_s)

        try:
            verdict = await asyncio.wait_for(fut, timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(aid, None)
            self._created.pop(aid, None)
            self._reasons.pop(aid, None)
            self.expired_count += 1
            log.warning("审批 %s 超时（%.0fs），视同拒绝", aid, self.timeout_s)
            return Decision(
                approved=False,
                approval_id=aid,
                status=ApprovalStatus.EXPIRED,
                reason=f"等待 {self.timeout_s:.0f} 秒未获得响应，视同拒绝",
                decided_at=time.time(),
                waited_s=time.time() - t0,
            )
        except asyncio.CancelledError:
            # 任务被外部取消（用户 Ctrl+C 或超时保护）
            self._pending.pop(aid, None)
            self._created.pop(aid, None)
            self._reasons.pop(aid, None)
            log.info("审批 %s 被取消", aid)
            raise

        waited = time.time() - t0
        approved = verdict == "approved"
        # 理由来自 resolve() 时用户填的内容（可能为空）
        reason = self._reasons.pop(aid, "")
        return Decision(
            approved=approved,
            approval_id=aid,
            status=ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
            reason=reason,
            decided_at=time.time(),
            waited_s=waited,
        )

    # ===================================================================== #
    # 决策（外部侧，不阻塞）
    # ===================================================================== #
    def resolve(self, approval_id: str, decision: str, reason: str = "") -> bool:
        """
        做出决策，唤醒挂起的 Agent 协程。

        参数：
            decision  "approved" | "rejected"

        返回：
            True  —— 决策被接受（找到了对应的挂起请求且尚未被决策）
            False —— id 无效 / 已决策过 / 已超时

        ★ 用 pop 而不是 get：保证一次性。
          这是防重放的关键 —— 第二次调用同一个 id 一定返回 False。
        """
        fut = self._pending.pop(approval_id, None)
        self._created.pop(approval_id, None)

        if fut is None:
            log.warning("审批 %s 不在等待中（可能已决策或已超时）", approval_id)
            return False

        if fut.done():
            log.warning("审批 %s 的结果已被设置过，忽略重复决策", approval_id)
            return False

        # 把理由记下来，让 request() 能把它放进 Decision
        self._reasons[approval_id] = reason
        fut.set_result("approved" if decision == "approved" else "rejected")
        self.resolved_count += 1
        log.info("审批 %s → %s%s", approval_id, decision, f"（{reason}）" if reason else "")
        return True

    def approve(self, approval_id: str, reason: str = "") -> bool:
        return self.resolve(approval_id, "approved", reason)

    def reject(self, approval_id: str, reason: str = "用户拒绝了该操作") -> bool:
        return self.resolve(approval_id, "rejected", reason)

    # ===================================================================== #
    # 查询
    # ===================================================================== #
    @property
    def waiting_ids(self) -> list[str]:
        return [aid for aid, f in self._pending.items() if not f.done()]

    @property
    def has_pending(self) -> bool:
        return bool(self.waiting_ids)

    def current(self) -> str | None:
        """当前唯一等待中的 id（v0.1 单任务，实际上最多一个）。"""
        ids = self.waiting_ids
        return ids[0] if ids else None

    def elapsed_of(self, approval_id: str) -> float:
        t0 = self._created.get(approval_id)
        return (time.time() - t0) if t0 else 0.0

    def remaining_of(self, approval_id: str) -> float:
        """还剩多少秒超时。CLI 用来显示倒计时。"""
        el = self.elapsed_of(approval_id)
        return max(0.0, self.timeout_s - el)

    def cancel_all(self, reason: str = "任务中止") -> int:
        """
        取消所有等待中的审批（任务被强制中止时调用）。
        返回被取消的数量。
        """
        ids = self.waiting_ids
        for aid in ids:
            self.resolve(aid, "rejected", reason)
        return len(ids)

    def stats(self) -> dict[str, Any]:
        return {
            "waiting": len(self.waiting_ids),
            "resolved": self.resolved_count,
            "expired": self.expired_count,
            "timeout_s": self.timeout_s,
        }


# --------------------------------------------------------------------------- #
# 记录构造辅助
# --------------------------------------------------------------------------- #
def make_record(pending: PendingApproval, decision: Decision) -> ApprovalRecord:
    """把 (待审批, 决策) 合成一条历史记录，供审计。"""
    return ApprovalRecord(
        approval_id=pending.approval_id,
        tool=pending.tool,
        target=pending.target,
        status=decision.status,
        args_hash=pending.args_hash,
        decided_at=decision.decided_at or time.time(),
        reason=decision.reason,
    )


def build_pending(
    *,
    tool: str,
    args: dict[str, Any],
    risk: RiskLevel,
    action: str,
    target: str,
    impact: str = "",
    preview: str = "",
    turn: int = 0,
) -> PendingApproval:
    """
    构造 PendingApproval —— 顺手把 args_hash 算好。

    ★ 强制在这里算指纹（而不是让调用方各自算），
      是为了保证「发起审批」和「执行前复检」用的是同一个算法。
      只要两边都走这个函数，就不可能不一致。
    """
    return PendingApproval(
        approval_id=PendingApproval.new_id(),
        tool=tool,
        args=args,
        args_hash=args_fingerprint(tool, args),
        risk=risk,
        action=action,
        target=target,
        impact=impact,
        preview=preview,
        turn=turn,
    )
