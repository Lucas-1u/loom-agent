"""
ask_user —— Agent 主动向用户提问，拿到答案后继续。

★ 它解决什么问题
----------------
Agent 遇到歧义时不能猜——猜错了就一路错下去。ask_user 让 Agent 在
需要用户决策时**真的挂起**等回答，拿到答案后继续执行。

★ 契约（2026-11-11 对齐 DSH `dsh-tool-ask-user`）
------------------------------------------------
请求：
    questions: [
      {id: "target",            # 必填，稳定 id，答案里原样回显
       question: "改哪个文件？", # 必填
       header: "确认",          # 可选，短标题
       options: [               # 可选，纯字符串数组也接受
         {label: "a.py", description: "主入口"},
         {label: "b.py"}],
       multi_select: false}     # 可选，能否多选
    ]
    timeout_s: 120              # 可选，-1 = 无限等

    旧写法 {question: "...", options: ["A","B"]} 仍然兼容（转成单问题）。

答案（回给模型的是结构化 JSON）：
    {"kind": "answered",
     "answers": [{"id": "target", "selected": ["a.py"], "custom": ""}]}

    · selected 为空且没有 custom = 用户明确跳过了这个问题
    · custom 非空 = 用户选了「其他（自己填写）」，内容在 custom 里
      ★ 这是本地增强：每个问题除了 AI 给的选项，永远多一个自由输入口，
        否则选项穷举不完时用户只能被迫选个最接近的。

超时（对齐 DSH timed 模式的 pending）：
    返回 ok=True、data={"kind": "pending", ...}，**不是 failure**。
    前台不再等，但问题仍然可回答 —— 用户稍后作答会作为一条用户消息
    送进 Agent 的下一步。描述里明确告诉模型"这不是拒绝、也不是跳过"。

★ 安全边界
----------
· risk=READ（无副作用——只是问用户，不碰文件/桌面/命令）；
· 因此 read-only 模式下 ask_user 也可用；
· 挂起是 Agent 协程级别的，不创建子进程，不涉及沙箱。

★ 工厂式注册
------------
不注入 user_input 时工具不注册（工具清单与之前一致）。
正常部署下 AgentCore 总会创建 UserInputManager。
"""

from __future__ import annotations

from typing import Any

from ..base import ErrorKind, Tool, ToolContext, ToolResult, timed
from ...agent.state import RiskLevel
from ...agent.user_input import render_answers as _render_answers
from ...events import Event, EventType

#: 单次提问最多几个问题（防止模型一次甩 50 个问题给用户）
MAX_QUESTIONS = 8
#: 单题最多几个选项
MAX_OPTIONS = 12


def _normalize_options(raw: Any) -> tuple[list[dict[str, str]], str]:
    """把 options 归一成 [{label, description?}]。

    接受两种写法：["A", "B"] 与 [{"label": "A", "description": "..."}]。
    """
    if not isinstance(raw, list):
        return [], "options 必须是数组"
    out: list[dict[str, str]] = []
    for i, item in enumerate(raw[:MAX_OPTIONS]):
        if isinstance(item, str):
            label = item.strip()
            desc = ""
        elif isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            desc = str(item.get("description") or "").strip()
        else:
            return [], f"options[{i}] 必须是字符串或对象"
        if not label:
            return [], f"options[{i}] 缺少 label"
        opt = {"label": label}
        if desc:
            opt["description"] = desc
        out.append(opt)
    return out, ""


def parse_timeout(args: dict[str, Any]) -> tuple[float | None, str]:
    """归一 `timeout_s`。返回 (秒数, 错误说明)。

    三种取值严格区分（混了就出 bug）：
    · 不传   → None（交由 UserInputManager 用它的默认值）
    · -1     → -1.0（无限等待；manager 约定"<=0 即无限"）
    · 正整数 → 该秒数
    ★ 校验放在这里而不是 validate_args：`run()` 直接调本模块的归一函数，
      校验点只有一处，才不会有"绕过 validate 就漏检"的口子。
    """
    raw = (args or {}).get("timeout_s")
    if raw is None:
        return None, ""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None, "timeout_s 必须是整数"
    if raw == -1:
        return -1.0, ""
    if raw < 1:
        return None, "timeout_s 必须是 -1（无限）或正整数"
    return float(raw), ""


def normalize_questions(args: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """把工具参数归一成 questions 数组。返回 (questions, 错误说明)。

    兼容旧写法（单个 question），因为历史任务、旧提示词、自检里都用过它。
    """
    args = args or {}
    _, timeout_err = parse_timeout(args)
    if timeout_err:
        return [], timeout_err
    raw = args.get("questions")

    if isinstance(raw, list) and raw:
        if len(raw) > MAX_QUESTIONS:
            return [], f"一次最多问 {MAX_QUESTIONS} 个问题（收到 {len(raw)} 个）"
        questions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                return [], f"questions[{i}] 必须是对象"
            text = str(item.get("question") or "").strip()
            if not text:
                return [], f"questions[{i}].question 不能为空"
            qid = str(item.get("id") or f"q{i + 1}").strip() or f"q{i + 1}"
            if qid in seen:
                return [], f"questions[{i}].id 重复：{qid}（答案靠 id 对应，必须唯一）"
            seen.add(qid)

            q: dict[str, Any] = {"id": qid, "question": text}
            header = str(item.get("header") or "").strip()
            if header:
                q["header"] = header
            if item.get("options"):
                opts, err = _normalize_options(item["options"])
                if err:
                    return [], f"questions[{i}].{err}"
                if opts:
                    q["options"] = opts
            if item.get("multi_select"):
                q["multi_select"] = True
            questions.append(q)
        return questions, ""

    # ---- 旧写法：单问题 ----
    text = str(args.get("question") or "").strip()
    if not text:
        return [], "缺少 questions（或旧版 question）参数"
    q = {"id": "q1", "question": text}
    header = str(args.get("header") or "").strip()
    if header:
        q["header"] = header
    if args.get("options"):
        opts, err = _normalize_options(args["options"])
        if err:
            return [], err
        if opts:
            q["options"] = opts
    if args.get("multi_select"):
        q["multi_select"] = True
    return [q], ""


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "向用户提问并等回答。一次可以问多个问题，每个问题可以带选项（单选/多选）。"
        "适用于：需要用户在几个方案里选一个、需要用户提供缺失信息（路径、参数、偏好）、"
        "做有副作用或不可逆的决定前确认。"
        "★ 用户除了你给的选项，还可以自己填写「其他」——所以不要为了穷举选项而纠结，"
        "选项给最常见的两三个就够。"
        "★ 用户可能跳过某个问题，也可能超时未答；超时会返回 kind=pending，"
        "这既不是拒绝也不是跳过，你可以继续做不依赖它的部分。"
        "注意：① 只在真正需要用户决策时用，能自己判断的就别问；"
        "② 问题要具体、自包含，别让用户猜你的上下文；③ 不要问 trivial 的问题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": (
                    "要问的问题列表。每个问题：id（必填，稳定且唯一，答案里原样回显）、"
                    "question（必填）、header（可选短标题）、"
                    "options（可选，[{\"label\":\"A\",\"description\":\"说明\"}] 或 [\"A\",\"B\"]）、"
                    "multi_select（可选，能否多选）。"
                ),
            },
            "timeout_s": {
                "type": "integer",
                "description": "整批问题的等待上限（秒）。-1 表示无限等待；不传用默认值。",
            },
            "question": {
                "type": "string",
                "description": "（旧版单问题写法，新代码请用 questions）要问的问题。",
            },
            "options": {
                "type": "array",
                "description": "（旧版，配合 question 使用）选项列表。",
            },
            "multi_select": {
                "type": "boolean",
                "description": "（旧版）是否允许多选。",
            },
        },
        "required": [],
    }
    risk = RiskLevel.READ

    def validate_args(self, args: dict[str, Any]) -> str:
        err = super().validate_args(args)
        if err:
            return err
        questions, err = normalize_questions(args)
        if err:
            return err
        if not questions:
            return "至少要有一个问题"
        return ""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with timed() as t:
            questions, err = normalize_questions(args or {})
            if err:
                return ToolResult.failure(
                    self.name, ErrorKind.INVALID_ARGUMENTS, err,
                    hint="检查 questions 的结构：id/question 必填，options 是数组。",
                )

            manager = getattr(ctx, "user_input", None)
            if manager is None:
                return ToolResult.failure(
                    self.name, ErrorKind.EXECUTION_ERROR,
                    "ask_user 需要 user_input 管理器，但未注入",
                    hint="检查 AgentCore 是否正确创建了 UserInputManager。",
                )

            bus = getattr(ctx, "bus", None)
            task_id = getattr(ctx, "task_id", "") or ""
            turn = getattr(ctx, "turn", 0)
            timeout_s, _ = parse_timeout(args or {})

            # ask_id 在 on_request 里才知道，但 on_response 要用它 → 用一个可变的槽
            req_holder: dict[str, str] = {"id": ""}

            def on_request(ask_id: str, req: Any) -> None:
                req_holder["id"] = ask_id
                if bus:
                    bus.emit(Event.make(
                        EventType.USER_INPUT_REQUEST,
                        {**req.to_payload(), "task_id": task_id},
                        turn=turn,
                    ))

            def on_pending(ask_id: str, req: Any) -> None:
                # ★ 复用 expired 事件（新增事件类型要动前端订阅清单，代价大）：
                #   语义 = "前台等待超时"，data.pending=True 表示问题仍然可回答。
                if bus:
                    bus.emit(Event.make(
                        EventType.USER_INPUT_EXPIRED,
                        {
                            "ask_id": ask_id,
                            "task_id": task_id,
                            "question": req.first_question,
                            "questions": list(req.questions),
                            "pending": True,
                            "timeout_s": req.timeout_s,
                            "note": "Agent 已不再等待，但你仍可以回答，答案会送进它的下一步。",
                        },
                        turn=turn,
                    ))

            req_holder: dict[str, str] = {"id": ""}

            def on_request_wrapped(ask_id: str, req: Any) -> None:
                req_holder["id"] = ask_id
                on_request(ask_id, req)

            def on_response(answers: list[dict[str, Any]], late: bool) -> None:
                if bus:
                    bus.emit(Event.make(
                        EventType.USER_INPUT_RESPONSE,
                        {
                            "ask_id": req_holder["id"],
                            "task_id": task_id,
                            "answers": answers,
                            "late": bool(late),
                            # 兼容字段：老的渲染/日志只读 answer
                            "answer": _render_answers(questions, answers),
                        },
                        turn=turn,
                    ))

            answers = await manager.request(
                questions,
                task_id=task_id,
                timeout_s=timeout_s,
                on_request=on_request_wrapped,
                on_pending=on_pending,
                on_response=on_response,
            )

            if answers is None:
                # 超时 → pending（不是 failure）。模型要能区分"用户没答"和"用户拒绝"。
                return ToolResult.success(
                    self.name,
                    "等待用户回答超时（pending）：问题仍然有效，用户可以稍后回答，"
                    "届时答案会作为一条用户消息送进来。这不是拒绝、也不是跳过，"
                    "继续做不依赖它的部分即可。",
                    data={
                        "kind": "pending",
                        "ask_id": req_holder["id"],
                        "answers": [],
                        "questions": questions,
                    },
                    duration_ms=t.ms,
                )

            summary = _render_answers(questions, answers)
            return ToolResult.success(
                self.name,
                f"用户回答：\n{summary}",
                data={"kind": "answered", "answers": answers, "questions": questions},
                duration_ms=t.ms,
            )


def make():
    """工厂：ask_user 是通用能力，总是注册（read-only 也能问用户）。
    运行时从 ctx.user_input 拿管理器（AgentCore 保证注入）。"""
    return AskUserTool()
