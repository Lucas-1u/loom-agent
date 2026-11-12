"""
UserInputManager —— ask_user 的挂起-恢复机制（2026-11-11 对齐 DSH 后重写）

★ 它解决什么问题
----------------
Agent 遇到歧义时需要问用户（"你要改哪个文件？"、"这个 API 用 GET 还是 POST？"）。
工具调用后 Agent 协程必须**真的挂起**等用户回答，而不是忙轮询或事后通知。

★ 一次调用可以问多个问题
------------------------
对齐 DSH `dsh-tool-ask-user`：`questions[]` 每项带 `id` / `question` / `header?` /
`options?` / `multi_select?`。答案是一批 `{id, selected[], custom?}`。

★ 三个状态（这是与 DSH timed 模式对齐的关键）
--------------------------------------------
| 状态 | 含义 | 谁在等 |
|---|---|---|
| `waiting` | 已发出提问，前台还在等 | 工具协程 await 中 |
| `pending` | **前台等待超时**，但问题没作废，仍然可以回答 | 没有人在 await，答案靠回调送达 |
| `answered` / `closed` | 已有答案（或已清理） | — |

`pending` 是这次改造的核心：旧实现超时就把 future 丢掉、把工具变成 failure，
用户再想回答已经没门了。现在超时只是"Agent 不再等"，问题仍挂在 `_open` 里，
用户在 `pending_ttl_s` 内回答，仍会被接收并送进 Agent 的下一步。

★ 与审批的区别
--------------
| | 审批（已移除） | ask_user |
|---|---|---|
| 触发 | 策略层拦截风险动作 | 模型主动调用工具 |
| 语义 | "允许/拒绝这个动作" | "回答问题" |
| 答案 | bool | 结构化答案（选项 + 自由输入） |
| 机制 | Future 挂起 | Future 挂起（相同） |

★ 四个安全细节
--------------
① 超时：`wait_for(shield(fut), t)`。**用 shield** ——普通的 wait_for 会把 future
   cancel 掉，那样超时之后就不可能再回答了。shield 让 future 活下来等 late 答案。
② 一次性：第一次 resolve 就把状态改掉，同一个 ask_id 不会被消费两次。
③ 不涉及权限：ask_user 是澄清，不是授权。不需要 args_hash 指纹。
④ 泄漏防护：`_sweep()` 清掉超过 `pending_ttl_s` 的 pending 状态。

★ 职责边界
----------
本模块只管"挂起-恢复"这个纯机制，不发事件、不碰工具。
发事件是 ask_user 工具的事（通过回调注入）；把 late 答案塞进对话历史是 AgentCore 的事
（通过 `on_late_answer` 回调）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("loom.user_input")

# ---- 请求状态 -------------------------------------------------------------- #
WAITING = "waiting"
PENDING = "pending"
ANSWERED = "answered"
CLOSED = "closed"

#: pending 状态保留多久（秒）。超时后问题仍然可答的时间窗口。
DEFAULT_PENDING_TTL_S = 1800.0

#: resolve 的三种结果
RESOLVE_ANSWERED = "answered"   # 有人正在等，直接唤醒
RESOLVE_LATE = "late"           # 前台已超时，答案以"补充回答"送达
RESOLVE_INVALID = "invalid"     # 不存在 / 已回答 / 已过期


@dataclass
class AskRequest:
    """一次提问（可含多个问题）。"""

    ask_id: str
    questions: list[dict[str, Any]]
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    task_id: str = ""
    timeout_s: float | None = None
    state: str = WAITING
    expires_at: float = 0.0
    on_response: Callable[[list[dict[str, Any]], bool], None] | None = None

    @property
    def first_question(self) -> str:
        return str(self.questions[0].get("question") or "") if self.questions else ""

    def to_payload(self) -> dict[str, Any]:
        """给事件 / API 用的提问载荷（不含 future 等内部字段）。"""
        return {
            "ask_id": self.ask_id,
            "task_id": self.task_id,
            "question": self.first_question,
            "questions": list(self.questions),
            "timeout_s": self.timeout_s,
        }


def render_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> str:
    """把一批答案渲染成人（和模型）都好读的几行文字。

    工具回给模型的 `data` 另给结构化 JSON；这段文字用于 summary / 迟到回答的消息正文。
    """
    by_id = {str(q.get("id") or ""): q for q in questions}
    lines: list[str] = []
    for ans in answers or []:
        qid = str(ans.get("id") or "")
        q = by_id.get(qid, {})
        title = str(q.get("header") or q.get("question") or qid)
        selected = [str(s) for s in (ans.get("selected") or [])]
        custom = str(ans.get("custom") or "").strip()
        if selected and custom:
            value = "、".join(selected) + f"；补充：{custom}"
        elif selected:
            value = "、".join(selected)
        elif custom:
            value = f"（自定义）{custom}"
        else:
            value = "（跳过，未作答）"
        lines.append(f"- [{qid}] {title} → {value}")
    return "\n".join(lines) if lines else "（没有答案）"


class UserInputManager:
    """ask_user 的挂起-恢复管理器。

    典型用法（ask_user 工具内部）：

        answers = await manager.request(
            questions,
            task_id=tid,
            on_request=lambda aid, req: bus.emit(USER_INPUT_REQUEST, req.to_payload()),
            on_pending=lambda aid, req: bus.emit(USER_INPUT_EXPIRED, {...}),
            on_response=lambda answers, late: bus.emit(USER_INPUT_RESPONSE, {...}),
        )
        # answers is None → 前台等待超时（进入 pending，问题仍可答）

    外部（API 端点）在另一个协程里：

        status = manager.resolve(ask_id, [{"id": "q1", "selected": ["GET"]}])
    """

    def __init__(
        self,
        *,
        timeout_s: float = 300.0,
        pending_ttl_s: float = DEFAULT_PENDING_TTL_S,
    ) -> None:
        self.timeout_s = timeout_s
        self.pending_ttl_s = pending_ttl_s
        self._open: dict[str, AskRequest] = {}
        self.resolved_count = 0
        self.late_count = 0
        self.expired_count = 0
        #: late 答案的接收方（由 AgentCore 注入：把答案塞进对话历史）
        self.on_late_answer: Callable[[AskRequest, list[dict[str, Any]]], None] | None = None

    # ---- 基础 --------------------------------------------------------------
    @staticmethod
    def new_ask_id() -> str:
        return uuid.uuid4().hex[:12]

    def open_requests(self) -> list[AskRequest]:
        """当前还挂在系统里的提问（waiting + pending），用于诊断/自检。"""
        return list(self._open.values())

    # ---- 提问 --------------------------------------------------------------
    async def request(
        self,
        questions: list[dict[str, Any]],
        *,
        task_id: str = "",
        timeout_s: float | None = None,
        on_request: Callable[[str, AskRequest], None] | None = None,
        on_pending: Callable[[str, AskRequest], None] | None = None,
        on_response: Callable[[list[dict[str, Any]], bool], None] | None = None,
    ) -> list[dict[str, Any]] | None:
        """创建挂起点，等用户回答。

        返回答案数组；**超时返回 None**（此时问题转入 pending，仍然可以回答，
        答案会通过 `on_response(answers, late=True)` 与 `on_late_answer` 送达）。

        `timeout_s`：负数或 0 表示无限等待；None 表示用管理器默认值。
        """
        ask_id = self.new_ask_id()
        timeout = self.timeout_s if timeout_s is None else timeout_s
        if timeout is not None and timeout <= 0:
            timeout = None  # -1 / 0 → 无限等

        loop = asyncio.get_running_loop()
        req = AskRequest(
            ask_id=ask_id,
            questions=list(questions),
            future=loop.create_future(),
            task_id=task_id,
            timeout_s=timeout,
            on_response=on_response,
        )
        self._open[ask_id] = req
        self._sweep()

        if on_request:
            on_request(ask_id, req)

        if timeout is None:
            answers = await req.future
        else:
            try:
                # ★ shield：超时只取消"等待"，不取消 future 本身 —— 否则答案永远进不来
                answers = await asyncio.wait_for(asyncio.shield(req.future), timeout=timeout)
            except asyncio.TimeoutError:
                # 竞态：用户恰好在超时瞬间答了 → 答案已经到了，按正常路径返回
                if req.future.done() and not req.future.cancelled():
                    return req.future.result()
                if req.state == WAITING:
                    req.state = PENDING
                    req.expires_at = time.time() + self.pending_ttl_s
                    self.expired_count += 1
                    # late 答案靠这个回调收尾（future 完成时触发）
                    req.future.add_done_callback(lambda _f, r=req: self._on_late(r))
                    if on_pending:
                        on_pending(ask_id, req)
                return None

        return answers

    # ---- 回答 --------------------------------------------------------------
    def resolve(self, ask_id: str, answers: list[dict[str, Any]]) -> str:
        """用户提交答案。返回 `answered` / `late` / `invalid`。

        · answered —— 有人正在 await，直接唤醒（工具马上返回）
        · late     —— 前台已超时，答案作为"补充回答"送进 Agent 下一步
        · invalid  —— 不存在 / 已回答过 / pending 也过期了
        """
        self._sweep()
        req = self._open.get(ask_id)
        if req is None:
            return RESOLVE_INVALID
        if req.future.done():
            self._open.pop(ask_id, None)
            return RESOLVE_INVALID

        if req.state == WAITING:
            req.state = ANSWERED
            self._open.pop(ask_id, None)
            self.resolved_count += 1
            req.future.set_result(answers)
            if req.on_response:
                _safe_call(req.on_response, answers, False)
            return RESOLVE_ANSWERED

        if req.state == PENDING:
            # 不在这里 pop —— 交给 done_callback 统一收尾（它会通知 core）
            self.resolved_count += 1
            req.future.set_result(answers)
            return RESOLVE_LATE

        return RESOLVE_INVALID

    # ---- 内部 --------------------------------------------------------------
    def _on_late(self, req: AskRequest) -> None:
        """pending 的 future 完成 → 把答案分发给两个接收方。"""
        if req.state != PENDING:
            return
        req.state = CLOSED
        self._open.pop(req.ask_id, None)
        self.late_count += 1

        answers = req.future.result() if not req.future.cancelled() else []
        if req.on_response:
            _safe_call(req.on_response, answers, True)
        if self.on_late_answer:
            _safe_call(self.on_late_answer, req, answers)

    def _sweep(self) -> None:
        """清掉过期/已完成的条目（防泄漏）。"""
        now = time.time()
        for ask_id, req in list(self._open.items()):
            if req.future.done():
                self._open.pop(ask_id, None)
                continue
            if req.state == PENDING and req.expires_at and now > req.expires_at:
                req.state = CLOSED
                self._open.pop(ask_id, None)
                req.future.cancel()


def _safe_call(fn: Callable, *args: Any) -> None:
    """回调异常不能拖垮主流程（事件发不出去比 Agent 挂掉轻得多）。"""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.warning("user_input 回调异常", exc_info=True)
