"""
Skill 的文件系统提供者（对应 DSH `dsh-skill-filesystem`）

只做四件事：
  ① 找目录里的技能（`<root>/<名字>/SKILL.md`）
  ② 解析 YAML frontmatter（有 PyYAML 就用它，没有就用内置的粗略解析）
  ③ 校验（名字合法 / description 非空 / 体积上限）
  ④ 列出附带文件（references / scripts / assets），供模型决定要不要读

★ 为什么校验要这么严
------------------
技能名会被拼进路径。`../../etc/passwd` 这种名字如果不挡，`load_skill` 就变成
任意文件读取。所以：名字只允许 `[A-Za-z0-9._-]`、不允许以点开头、长度 ≤ 64；
附带文件的相对路径必须 realpath 之后仍在技能目录内。

★ 为什么坏技能是"跳过并记录"，不是"整个加载失败"
--------------------------------------------
一个目录里 10 个技能，坏 1 个不该让另外 9 个也用不了。但**必须能看见**——
跳过的原因会进 `report`，`/api/health` 与自检都能读到，避免"装了但没生效"。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

#: 技能正文文件名（大小写敏感：DSH / Claude / WorkBuddy 都叫 SKILL.md）
SKILL_FILE = "SKILL.md"

#: frontmatter 分隔符
DELIM = "---"

#: 技能名白名单：首字符必须是字母数字（挡掉 "." ".." 开头），其余允许 . _ -
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 默认正文上限（字符）。技能是"做法"，不该是一本小说。
MAX_BODY_CHARS = 24_000

#: 附带文件最多列多少个（再多也不会有模型去逐个读）
MAX_FILES = 40

#: 目录里每个描述截断到多少字符（目录要便宜）
MAX_DESC_CHARS = 220


@dataclass
class Skill:
    """一个已解析的技能。"""

    name: str
    description: str
    root: str                      # 技能目录绝对路径（realpath）
    source: str = ""               # 层标签：项目级 / 用户级 / 内置 / 自定义
    body: str = ""                 # SKILL.md 正文（已截断到上限）
    files: list[str] = field(default_factory=list)   # 附带文件相对路径（posix 风格）
    meta: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False        # 正文是否被截断
    path: str = ""                 # SKILL.md 的绝对路径

    @property
    def chars(self) -> int:
        return len(self.body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "root": self.root,
            "source": self.source,
            "chars": self.chars,
            "files": list(self.files),
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------- #
# frontmatter 解析
# --------------------------------------------------------------------------- #
def _strip_quotes(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        quote = s[0]
        inner = s[1:-1]
        if quote == '"':
            # 只处理最常见的转义；YAML 的全部转义规则交给 PyYAML
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    return s


def minimal_yaml(text: str) -> tuple[dict[str, Any], str]:
    """不依赖 PyYAML 的粗略解析（只在装了 PyYAML 的环境之外兜底）。

    支持：`key: value`（含引号）、`key:` + 缩进 `- item` 列表、
    `key: |` / `key: >` 块标量、一层嵌套（`metadata:\\n  tags:\\n    - x`）。

    ★ 不做的事：锚点/别名、多行 flow 集合、复杂类型推断。真需要那些写法的技能
      应该要求环境装 PyYAML —— 与其猜错，不如只支持能确定的部分。
    """
    root: dict[str, Any] = {}
    if not text.strip():
        return root, ""

    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent > 0:
            # 顶层以外的缩进在没有父键时无处安放 —— 跳过（父键分支会消费掉正常嵌套）
            continue
        if ":" not in raw:
            continue
        key, _, rest = raw.partition(":")
        key = key.strip().strip("'\"")
        rest = rest.strip()
        if not key:
            continue

        if rest in ("|", ">"):
            block: list[str] = []
            block_indent = -1
            while i < n:
                nxt = lines[i]
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt.strip() and nxt_indent <= indent + 1 and block:
                    break
                if nxt.strip() and block_indent < 0:
                    block_indent = nxt_indent
                if block_indent >= 0:
                    block.append(nxt[block_indent:])
                else:
                    block.append("")
                i += 1
            join = "\n" if rest == "|" else " "
            root[key] = join.join(block).strip()
            continue

        if rest:
            root[key] = _strip_quotes(rest)
            continue

        # `key:` 后面可能是列表，也可能是嵌套 map
        items: list[Any] = []
        child: dict[str, Any] = {}
        while i < n:
            nxt = lines[i]
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            if not nxt.strip():
                i += 1
                continue
            if nxt_indent <= indent:
                break
            body = nxt.strip()
            if body.startswith("- "):
                item = body[2:].strip()
                items.append(_strip_quotes(item) if item else "")
            elif ":" in body:
                k2, _, v2 = body.partition(":")
                k2 = k2.strip().strip("'\"")
                v2 = v2.strip()
                if v2:
                    child[k2] = _strip_quotes(v2)
                else:
                    # 孙层：`metadata: { tags: [a, b] }` 这类，收成列表
                    sub: list[Any] = []
                    j = i + 1
                    while j < n:
                        deeper = lines[j]
                        deeper_indent = len(deeper) - len(deeper.lstrip(" "))
                        if not deeper.strip():
                            j += 1
                            continue
                        if deeper_indent <= nxt_indent:
                            break
                        if deeper.strip().startswith("- "):
                            sub.append(_strip_quotes(deeper.strip()[2:]))
                        j += 1
                    child[k2] = sub
                    i = j
                    continue
            i += 1
        root[key] = items if items else child
    return root, ""


def _loads_yaml(text: str) -> tuple[dict[str, Any], str]:
    """优先 PyYAML（正确），没有就退回 minimal_yaml（够用）。"""
    try:
        import yaml  # noqa: PLC0415 —— 可选依赖，按需导入
    except ImportError:
        return minimal_yaml(text)
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 —— YAML 的异常类型很杂
        # ★ 坏 frontmatter 不静默降级成 minimal：那样会把语法错误当成"字段缺失"，
        #   用户看到的是"缺少 description"，而真实原因是缩进写错了。
        return {}, f"frontmatter 不是合法 YAML：{type(exc).__name__}: {exc}"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        return {}, "frontmatter 必须是 key: value 映射"
    return data, ""


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str]:
    """把 SKILL.md 拆成 (meta, body, 错误说明)。"""
    if not text:
        return {}, "", "文件为空"
    src = text.lstrip("\ufeff")
    lines = src.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIM:
        return {}, src, "缺少 YAML frontmatter（文件第一行必须是 ---）"

    end = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() in (DELIM, "..."):
            end = idx
            break
    if end < 0:
        return {}, src, "frontmatter 没有结束的 ---"

    head = "".join(lines[1:end])
    body = "".join(lines[end + 1:])
    meta, err = _loads_yaml(head)
    if err:
        return {}, src, err
    return meta, body, ""


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def _inside(root: str, target: str) -> bool:
    """target 是否在 root 内（比 startswith 可靠：要求分隔符边界）。"""
    r = os.path.normcase(os.path.realpath(root))
    t = os.path.normcase(os.path.realpath(target))
    return t == r or t.startswith(r.rstrip(os.sep) + os.sep)


def list_bundled_files(root: str, *, max_files: int = MAX_FILES) -> list[str]:
    """列出技能目录里的附带文件（相对路径，posix 风格，不含 SKILL.md）。"""
    out: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(base, name), root).replace("\\", "/")
            if rel == SKILL_FILE:
                continue
            out.append(rel)
            if len(out) >= max_files:
                return out
    return out


def scan_root(
    root: str,
    *,
    source: str = "",
    max_body_chars: int = MAX_BODY_CHARS,
    max_files: int = MAX_FILES,
    max_skills: int = 64,
) -> tuple[list[Skill], list[dict[str, str]]]:
    """扫描一个技能根目录。返回 (技能列表, 跳过记录)。

    跳过记录长这样：`{"dir": "...", "reason": "缺少 description"}` ——
    宁可让用户看到"为什么没生效"，也不要静默吞掉。
    """
    skills: list[Skill] = []
    skipped: list[dict[str, str]] = []
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        return skills, skipped

    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        return skills, [{"dir": root, "reason": f"目录读不了：{exc}"}]

    for entry in entries:
        if entry.startswith(".") or entry.startswith("_"):
            continue  # 隐藏目录 / 约定用 _ 前缀放公共素材，不当技能
        skill_dir = os.path.join(root, entry)
        if not os.path.isdir(skill_dir):
            continue
        if len(skills) >= max_skills:
            skipped.append({"dir": skill_dir, "reason": f"超过单目录上限 {max_skills}，未加载"})
            continue

        path = os.path.join(skill_dir, SKILL_FILE)
        if not os.path.isfile(path):
            # 没有 SKILL.md 的目录很常见（references 里放了个子目录就会被扫到），
            # 这里不算错误，也不必记录。
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(max_body_chars * 4)
        except OSError as exc:
            skipped.append({"dir": skill_dir, "reason": f"读不了 SKILL.md：{exc}"})
            continue

        meta, body, err = parse_frontmatter(text)
        if err:
            skipped.append({"dir": skill_dir, "reason": err})
            continue

        name = str(meta.get("name") or entry).strip()
        if not NAME_RE.match(name):
            skipped.append({
                "dir": skill_dir,
                "reason": f"技能名不合法：{name!r}（只允许字母数字开头的 [A-Za-z0-9._-]，≤64 字符）",
            })
            continue
        description = str(meta.get("description") or "").strip()
        if not description:
            skipped.append({"dir": skill_dir, "reason": "缺少 description（模型靠它决定要不要载入）"})
            continue

        body = body.lstrip("\n")
        truncated = len(body) > max_body_chars
        if truncated:
            body = body[:max_body_chars] + f"\n\n…（正文超过 {max_body_chars} 字符，已截断）"

        skills.append(Skill(
            name=name,
            description=description,
            root=skill_dir,
            source=source,
            body=body,
            files=list_bundled_files(skill_dir, max_files=max_files),
            meta=meta,
            truncated=truncated,
            path=path,
        ))
    return skills, skipped
