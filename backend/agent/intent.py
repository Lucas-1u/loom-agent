"""
任务意图、用户约束与验证可信度风险（第三批）

★ 为什么单独一个模块？
  前两批留下三个缺口，本质上是同一个问题：**系统不知道用户到底想要什么**。
    · 验证闸门靠"改没改文件"猜任务类型 → 诊断任务被误拦
    · exit_code=0 就被当成"问题已解决" → 绕过验证的修改照样算成功
    · 用户说"别改测试"只体现在 forbidden_paths 上，没有语义

  这个模块把这三件事变成**可判定的规则**，全部是纯函数、可单测。

★ 三条设计纪律
  ① 分类规则优先读**用户明确声明的约束**，再读关键词，最后才看运行时信号。
  ② 不让模型自己发明任务 DSL —— 它生成的类型没法被程序验证。
  ③ 分不出来就标 unknown，**不擅自解释成最宽松的类型**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# 任务意图
# --------------------------------------------------------------------------- #
INTENTS = ("diagnostic", "repair", "test_authoring", "verification", "implementation", "unknown")

# 每类意图的关键词。顺序即优先级描述里的"任务描述分类"层。
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "diagnostic": (
        "分析", "排查", "诊断", "定位", "看看", "是什么", "为什么失败", "为啥",
        "怎么回事", "解释", "review", "审计一下", "查一下",
    ),
    "verification": (
        "跑一下", "运行测试", "执行测试", "跑测试", "报告结果", "验证现状",
        "确认是否", "检查是否通过", "只运行", "看一下测试结果",
    ),
    "repair": (
        "修复", "修一下", "修好", "改正", "报错", "失败", "bug", "崩溃",
        "fix", "broken", "不工作", "异常",
        # ★ 交付物措辞：说"让测试全部通过"就是在要求修复，
        #   这类信号应该比"为什么"这类弱信号更能定意图。
        "全部通过", "都通过", "变成绿", "变为绿",
    ),
    "test_authoring": (
        "写测试", "补测试", "加测试", "添加测试", "单元测试", "编写测试",
        "测试用例", "add tests", "write tests",
    ),
    "implementation": (
        "实现", "新增", "加一个", "写一个", "开发", "支持", "增加",
        "implement", "add a", "create a",
    ),
}

# 用户明确约束 → 正则。命中的约束会**改变验证策略**，甚至自动收紧权限。
_CONSTRAINT_PATTERNS: dict[str, tuple[str, ...]] = {
    # 不许动测试/测试配置
    "no_test_modification": (
        "不要改测试", "不许修改测试", "禁止改测试", "别改测试", "不要动测试",
        "不要修改测试", "不准改测试", "don't modify test", "don't change test",
        "不要改测试配置", "不许动 conftest",
    ),
    # 只读：什么都不许改
    "read_only": (
        "只检查", "只读", "仅查看", "不要修改", "不许修改", "禁止修改", "别改",
        "不要动", "不准改", "don't modify", "read-only", "read only", "不要写入",
    ),
    # 只报告，不修
    "no_fix": (
        "不要修复", "别修", "不用修", "不需要修", "只报告", "报告即可",
        "don't fix", "report only", "不要改代码",
    ),
}

# 命中这些路径的改动，可能改变测试的执行语义
TEST_CONFIG_NAMES = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
TEST_PATH_HINTS = ("tests/", "test_", "_test.py", "spec/", "conftest.py", "pytest.ini", "tox.ini")

# 内容里出现这些，说明可能在改变测试行为
_CONTENT_PATTERNS: dict[str, str] = {
    "skip": r"@pytest\.mark\.skip|pytest\.skip\(|@unittest\.skip|skipIf|skipUnless",
    "xfail": r"@pytest\.mark\.xfail|pytest\.xfail\(|expectedFailure",
    "mock": r"monkeypatch|unittest\.mock|mock\.patch|MagicMock|Mock\(",
    "collection_hook": r"def (pytest_|collect_|setup_module|teardown_module)",
}


@dataclass
class IntentResult:
    intent: str = "unknown"
    confidence: str = "unknown"        # high / medium / low / unknown
    reasons: list[str] = field(default_factory=list)
    constraints: dict[str, bool] = field(default_factory=dict)
    scoped_forbidden: list[str] = field(default_factory=list)
    scoped_allowed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "constraints": self.constraints,
            "scoped_forbidden": self.scoped_forbidden,
            "scoped_allowed": self.scoped_allowed,
        }


def _norm(text: str) -> str:
    return (text or "").lower()


def classify_intent(goal: str, *, forbidden_paths: list[str] | None = None) -> IntentResult:
    """
    按优先级判定任务意图：

      1. 用户明确约束（no_test_modification / read_only / no_fix）
      2. 用户明确要求的验证方式（"跑一下…报告结果" → verification）
      3. 关键词分类
      4. 都不命中 → unknown（**不**默认成最宽松的类型）

    ★ 为什么关键词命中数取最大而不是按列表顺序？
      "跑一下测试看看为什么失败" 同时含 verification 和 repair 的词。
      这种情况下按用户原意应是"先看再决定"，所以谁的词多听谁的；
      持平就认为信息不足 → unknown，让后续流程保守处理。
    """
    text = _norm(goal)
    out = IntentResult()
    out.constraints = extract_constraints(goal)
    out.scoped_forbidden = extract_scoped_forbidden(goal)
    out.scoped_allowed = extract_scoped_allowed(goal)

    # ---- 1. 显式约束优先 ----
    if out.constraints.get("read_only"):
        out.intent = "diagnostic"
        out.confidence = "high"
        out.reasons.append("用户声明了只读/不许修改 → 按诊断任务处理")
    if out.constraints.get("no_fix"):
        out.intent = "verification"
        out.confidence = "high"
        out.reasons.append("用户声明了不要修复、只报告 → 按验证任务处理")
    if out.intent != "unknown":
        # forbidden_paths 里带了 tests/ 也算一条佐证
        for p in forbidden_paths or []:
            if any(h in _norm(p) for h in ("test", "conftest", "pytest")):
                out.reasons.append(f"调用方同时禁止了 {p}")
                break
        return out

    # ---- 2. 验证方式 ----
    if out.constraints.get("no_test_modification") and any(
        k in text for k in ("跑", "运行", "执行", "pytest", "测试结果")
    ):
        out.intent = "verification"
        out.confidence = "high"
        out.reasons.append("用户要求跑测试且禁止改测试 → 验证任务")
        return out

    # ---- 3. 关键词 ----
    # ★ M5：先把**否定式约束**从句摘掉再打分。
    #   真实事故：子任务目标里写着"读取并汇报，不要修改任何文件"，
    #   结果命中 repair 的关键词「修改」→ 判定成修复意图 →
    #   M3 的"零改动零验证"闸门把它判成未通过（verification_failed）。
    #   "不要修改"是**约束**，不是**意图** —— 不能让它把任务变成修复任务。
    keywords_text = _NEGATIVE_CLAUSE_RE.sub(" ", text)
    scores: dict[str, list[str]] = {}
    for intent, words in _INTENT_KEYWORDS.items():
        hits = [w for w in words if w in keywords_text]
        if hits:
            scores[intent] = hits
    if not scores:
        out.intent = "unknown"
        out.confidence = "unknown"
        out.reasons.append("没有命中任何意图关键词，保持 unknown")
        return out

    best = max(scores.items(), key=lambda kv: len(kv[1]))
    tied = [k for k, v in scores.items() if len(v) == len(best[1])]
    if len(tied) > 1:
        out.intent = "unknown"
        out.confidence = "low"
        out.reasons.append(f"多个意图打平（{tied}），信息不足，保持 unknown")
        return out

    out.intent = best[0]
    out.confidence = "high" if len(best[1]) >= 2 else "medium"
    out.reasons.append(f"命中 {best[0]} 关键词：{best[1]}")
    return out


# 否定式约束从句："不要修改…" / "请勿删除…" / "禁止改动…" 等
_NEGATIVE_CLAUSE_RE = re.compile(
    r"(?:不要|请勿|别|不能|不得|禁止|无需|不需要)[^。；\n]{0,20}?"
    r"(?:修改|改动|改写|删除|移除|改变|写入|新增|添加|创建|提交|推送)[^。；\n]*"
)

_BAN_RE = re.compile(
    r"(不要修改|不许修改|禁止修改|不准修改|不要改|不许改|禁止改|别改|不要动|不要写入)"
)
_PATH_TOKEN = re.compile(r"[\w./\\-]+", re.IGNORECASE)
_PATHY_EXT = (".py", ".md", ".csv", ".json", ".txt", ".ini", ".toml", ".cfg")


def extract_scoped_forbidden(goal: str) -> list[str]:
    """
    抽取「禁止修改 <具体路径>」里的路径。

    ★ 为什么必须和 read_only 分开（真实扩样跑出来的 bug）：
      "不许修改 tests/test_pipeline.py 和 data/input.csv" 限定了目标，
      被误判成"什么都不许改"（read_only）后，整个实现任务连一行都写不了，
      3 次运行全部 model_error。**限定了目标的是路径约束，没限定才是全局只读。**
    """
    text = _norm(goal)
    out: list[str] = []
    for m in _BAN_RE.finditer(text):
        # ★ 只在**当前子句**里找路径：遇到"，" "；" "。" 换行就停。
        #   否则"不许修改 A 和 B，其余只许改 pipeline/"会把 pipeline/
        #   （那是**允许**范围）也当成禁止范围 —— 真实扩样里就是这么把
        #   实现任务整个堵死的（Agent 被正确挡住并如实报告了冲突）。
        tail = text[m.end():]
        for sep in ("，", ";", "。", "\n"):
            i = tail.find(sep)
            if i >= 0:
                tail = tail[:i]
        # "不要修改**任何**文件（包括 A、B）" 是**全局**只读：
        #   括号里列路径只是举例，不能据此降级成"只禁这几个路径"。
        if any(k in tail[:20] for k in ("任何", "所有", "一切", "任意", "全部")):
            continue
        for tok in _PATH_TOKEN.findall(tail):
            t = tok.strip("./")
            if "/" in tok or t.endswith(_PATHY_EXT) or t in ("tests", "data", "pipeline"):
                if t and t not in out:
                    out.append(t)
            if len(out) >= 6:
                return out
    return out


_ALLOW_RE = re.compile(
    # ★ "只允许修改" 里有"修"字，不能只写"只允许改"（那匹配不上）
    r"(只许改|只能改|只允许改|只允许修改|仅允许改|仅允许修改|仅修改|只改|仅改)"
)


def extract_scoped_allowed(goal: str) -> list[str]:
    """
    抽取「只许改 <路径>」里的路径 → 允许范围。

    ★ 为什么也要抽：
      "不许修改 A 和 B，其余只许改 pipeline/" 里，pipeline/ 是**允许**范围。
      只抽禁止不抽允许，会把允许范围误当禁止（见 extract_scoped_forbidden 的坑）。
    """
    text = _norm(goal)
    out: list[str] = []
    for m in _ALLOW_RE.finditer(text):
        tail = text[m.end():]
        for sep in ("，", ";", "。", "\n"):
            i = tail.find(sep)
            if i >= 0:
                tail = tail[:i]
        for tok in _PATH_TOKEN.findall(tail):
            t = tok.strip("./")
            if "/" in tok or t.endswith(_PATHY_EXT) or t in ("tests", "data", "pipeline"):
                if t and t not in out:
                    out.append(t)
            if len(out) >= 6:
                return out
    return out


def extract_constraints(goal: str) -> dict[str, bool]:
    """
    从任务文本里抽取用户明确约束（只认明确说法，不做委婉推断）。

    ★ 约束之间会互相吞并，必须排序处理：
      "不要修改测试" 里含子串 "不要修改" —— 如果先判 read_only，
      就会把"只禁测试"误放大成"什么都不许改"，任务直接没法做。
      所以：先判最具体的（no_test_modification），再判笼统的（read_only），
      前者命中时后者置 False。
    """
    text = _norm(goal)
    out: dict[str, bool] = {}
    for name, pats in _CONSTRAINT_PATTERNS.items():
        out[name] = any(p in text for p in pats)
    if out.get("no_test_modification"):
        out["read_only"] = False
    # ★ 禁令后面跟了具体路径 → 是路径约束，不是全局只读（真实扩样抓到的 bug）
    if out.get("read_only") and extract_scoped_forbidden(goal):
        out["read_only"] = False
    return out


# --------------------------------------------------------------------------- #
# 验证可信度风险
# --------------------------------------------------------------------------- #
def is_test_related_path(path: str) -> bool:
    p = _norm(path).replace("\\", "/")
    return any(h in p for h in TEST_PATH_HINTS)


def is_test_config_path(path: str) -> bool:
    name = _norm(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name in TEST_CONFIG_NAMES


def scan_content_risks(content: str) -> list[str]:
    """内容里可能改变测试行为的模式。"""
    out: list[str] = []
    for name, pat in _CONTENT_PATTERNS.items():
        if re.search(pat, content or "", re.IGNORECASE):
            out.append(name)
    return out


def detect_risks(
    changed: list[dict[str, Any]],
    *,
    constraints: dict[str, bool] | None = None,
    forbidden_paths: list[str] | None = None,
    read_text: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """
    给审计出来的文件变动标注"验证可信度风险"。

    分级：
      high   → 明确违反用户约束（说了别改测试还是改了）
      medium → 可能改变测试语义（新增/改 conftest、引入 skip/xfail/mock、
               改 pytest 配置），需要 Agent 解释
      low    → 普通改动

    ★ 为什么"出现 mock"不直接算违规？
      用户要求写单元测试时，mock 外部依赖是完全正当的做法。
      这里只标风险，判不判违规由约束 + 任务意图决定。

    ★ read_text 可以为 None（读不了就只按路径判断，并标 uncertain），
      不假装看到了内容。
    """
    constraints = constraints or {}
    forbidden = [_norm(p) for p in (forbidden_paths or [])]
    risks: list[dict[str, Any]] = []

    for item in changed:
        path = item.get("path", "")
        if not path:
            continue
        kind = item.get("kind", "modified")
        via = item.get("via", "unattributed")
        is_cfg = is_test_config_path(path)
        is_test = is_test_related_path(path)
        in_forbidden = any(_norm(path).startswith(f) or _norm(path) == f for f in forbidden)

        content = ""
        uncertain = bool(item.get("uncertain"))
        if read_text is not None and not uncertain:
            try:
                content = read_text(path) or ""
            except Exception:  # noqa: BLE001 —— 读不了就标不确定，不猜
                uncertain = True
        patterns = scan_content_risks(content) if content else []

        level = "low"
        why: list[str] = []
        if in_forbidden:
            level = "high"
            why.append("该路径在调用方声明的禁止修改范围内")
        if constraints.get("no_test_modification") and (is_test or is_cfg):
            level = "high"
            why.append("用户明确要求不要修改测试/测试配置")
        if level != "high":
            if is_cfg:
                level = "medium"
                why.append(f"改动了测试运行配置 {path.rsplit('/', 1)[-1]}，可能改变测试执行语义")
            if "skip" in patterns or "xfail" in patterns:
                level = "medium"
                why.append("引入了 skip/xfail，可能让原本失败的用例不再执行")
            if "mock" in patterns and not is_test:
                level = "medium"
                why.append("在非测试代码里引入 mock/monkeypatch，可能改变被测行为")
            if "collection_hook" in patterns:
                level = "medium"
                why.append("引入 pytest 收集/模块级钩子，可能影响测试收集顺序")

        risks.append({
            "path": path,
            "kind": kind,
            "via": via,
            "level": level,
            "reasons": why,
            "patterns": patterns,
            "uncertain": uncertain,
        })

    # 去重：同一路径只保留最高等级那条（避免事件流重复告警）
    best: dict[str, dict[str, Any]] = {}
    order = {"low": 0, "medium": 1, "high": 2}
    for r in risks:
        cur = best.get(r["path"])
        if cur is None or order[r["level"]] > order[cur["level"]]:
            best[r["path"]] = r
    return [best[k] for k in sorted(best)]


# --------------------------------------------------------------------------- #
# 证据分级
# --------------------------------------------------------------------------- #
def grade_evidence(
    *,
    command_history: list[dict[str, Any]],
    change_turns: list[int],
    risks: list[dict[str, Any]],
    intent: str,
) -> dict[str, Any]:
    """
    把"执行记录"折算成"与用户目标相关的证据等级"。

      A → 观察到失败 → 改动 → 同一条命令转绿，且没有可信度风险
      B → 有成功执行，但没覆盖到失败场景，或存在需要解释的可疑改动
      C → 只有无关的执行记录 / 最后一次仍失败 / 风险明确

    ★ 这不是形式化验证，只是把"证据到底指不指向用户的问题"显式记下来，
      好在最终答复里区分"做完"和"验证过"。
    """
    cmds = list(command_history or [])
    failed = [c for c in cmds if not c.get("ok")]
    ok = [c for c in cmds if c.get("ok")]
    high = [r for r in (risks or []) if r.get("level") == "high"]
    medium = [r for r in (risks or []) if r.get("level") == "medium"]

    # 诊断/验证任务：失败本身就是交付物，不按"修复证据"打分
    if intent in ("diagnostic", "verification"):
        if not cmds:
            return {"grade": "C", "reasons": ["没有执行过任何命令，只有代码阅读"],
                    "unverified": ["没有任何执行证据"]}
        return {
            "grade": "B",
            "reasons": [f"{intent} 任务：执行了 {len(cmds)} 条命令，结果已如实记录"],
            "unverified": [],
        }

    if not cmds:
        return {"grade": "C", "reasons": ["没有执行过任何命令"],
                "unverified": ["缺少任何执行证据"]}

    if failed and ok:
        last_fail = failed[-1]
        last_ok = ok[-1]
        same_cmd = (last_ok.get("command") or "") == (last_fail.get("command") or "")
        # ★ 只要"成功的那次之前发生过改动"就算数，不要求改动夹在失败和成功之间：
        #   真实顺序常常是 改 → 跑（红）→ 再跑（绿），改动发生在第一次失败之前。
        changed_before = any(t <= last_ok.get("turn", 0) for t in change_turns)
        if same_cmd and changed_before:
            if high:
                return {"grade": "C",
                        "reasons": ["失败→通过之间发生了高风险改动，证据可信度不足"],
                        "unverified": ["需要人工确认改动是否只是让测试变绿"]}
            if medium:
                return {"grade": "B",
                        "reasons": ["失败→通过之间存在可疑改动，通过结果需要解释"],
                        "unverified": ["改动是否改变了原本的验证条件"]}
            return {"grade": "A",
                    "reasons": [f"同一命令 `{last_fail.get('command')}` 由失败转为成功，且无可信度风险"],
                    "unverified": []}
        return {"grade": "B",
                "reasons": ["失败与成功不是同一条命令，或改动发生在失败之前"],
                "unverified": ["原始失败场景没有被同一命令复验"]}

    if ok and not failed:
        return {"grade": "B",
                "reasons": ["所有命令都成功，但没有观察到失败场景"],
                "unverified": ["没有证据表明原始问题已被复现并修复"]}

    return {"grade": "C",
            "reasons": [f"最后一次执行仍失败（退出码 {failed[-1].get('exit_code')}）"],
            "unverified": ["修复尚未通过执行验证"]}


# --------------------------------------------------------------------------- #
# 任务目标与权限范围的冲突检测（第五批）
# --------------------------------------------------------------------------- #
_MENTIONED = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}|\b(?:tests?|docs?|src|pipeline|data)/[\w./\\-]*")


def extract_mentioned_paths(goal: str) -> list[str]:
    """
    抽取任务原文中**提到的具体文件/目录**。

    ★ 为什么需要它：用户经常在同一句话里既限定范围又提新要求 ——
      "只允许修改 pipeline/，同时新增 tests/test_breakdown.py"。
      这两件事互相矛盾。系统必须能指出"你要我改的东西在允许范围之外"，
      而不是等 Agent 撞墙才发现（更不能默默把范围放开）。
    """
    text = _norm(goal)
    out: list[str] = []
    for m in _MENTIONED.finditer(text):
        tok = m.group(0).strip("/")
        if not tok or len(tok) < 3:
            continue
        if tok not in out:
            out.append(tok)
        if len(out) >= 12:
            break
    return out


def detect_scope_conflicts(
    goal: str,
    *,
    allowed_paths: list[str] | None,
    forbidden_paths: list[str] | None,
) -> list[dict[str, Any]]:
    """
    检测「用户要求」与「权限范围」之间的冲突。

    返回每条冲突：{path, kind, reason}
      forbidden_but_requested  目标里点名要改，但那个路径被明确禁止
      outside_allowed         目标里点名要改，但不在允许范围内

    ★ 只检测、只披露，**不自动放开权限**：擅自扩大权限比完不成任务更危险。
    """
    from .scope import hits as scope_hits, within_allowed

    allowed = list(allowed_paths or [])
    forbidden = list(forbidden_paths or [])
    conflicts: list[dict[str, Any]] = []

    for path in extract_mentioned_paths(goal):
        low = path.lower()
        # 目标提到的是不是"要改的文件"？只把像路径的东西算进来
        if not ("/" in low or low.endswith((".py", ".md", ".csv", ".json", ".txt", ".ini", ".toml"))):
            continue
        if scope_hits(path, forbidden):
            conflicts.append({
                "path": path,
                "kind": "forbidden_but_requested",
                "reason": f"任务要求涉及 {path}，但该路径在明确禁止修改的范围内",
            })
        elif allowed and not within_allowed(path, allowed):
            conflicts.append({
                "path": path,
                "kind": "outside_allowed",
                "reason": (f"任务要求涉及 {path}，但允许修改的范围是 "
                           f"{', '.join(allowed)}；需要用户明确授权才能改"),
            })
    return conflicts
