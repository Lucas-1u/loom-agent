"""
Skill 机制本体（对应 DSH `dsh-skill`）

★ 这里只有三件事
--------------
  ① `catalog_text()`   —— 渲染系统提示里的"技能目录"（第 1 级披露）
  ② `load(name)`       —— 载入某个技能的正文（第 2 级披露）
  ③ `read_file(...)`   —— 读技能里的附带文件（第 3 级披露）

★ 为什么目录要"分组 + 截断"
------------------------
分组（项目级 / 用户级 / 内置）是为了让模型知道"这个技能是谁定的、有多可信"：
项目级的约定优先于用户级，内置的是 Loom 自带示例。截断是因为目录会跟着
系统提示每轮发送 —— 一个技能一行可以，一行 500 字不行。

★ 为什么正文里要替换 `$SKILL_DIR`
-------------------------------
从别的 agent 搬来的技能，正文里常写死 `<SKILL_ROOT>` 之类的占位符（宿主要负责
把它换掉）。Loom 在这里统一替换成真实绝对路径，于是"搬过来就能用"成为契约本身，
而不是每次都要人工改文本。

★ 路径安全
--------
`read_file` 的相对路径必须落在技能目录内（realpath + 分隔符边界双重判断）。
技能目录是用户可控内容，不能让 `../` 把任意文件读进上下文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .loader import MAX_BODY_CHARS, MAX_DESC_CHARS, Skill, _inside, scan_root

#: 系统提示里技能目录的总预算（字符）。超了就截断，不让它挤占工作上下文。
CATALOG_MAX_CHARS = 4_000

#: 附带文件读入上限
MAX_FILE_CHARS = 12_000

#: 正文里的路径占位符。从别的宿主搬来的技能基本跑不出这几种写法。
ROOT_TOKENS = ("${SKILL_DIR}", "$SKILL_DIR", "<SKILL_ROOT>", "{{SKILL_DIR}}")


@dataclass
class ResolvedRoot:
    """一个待扫描的技能根目录。"""

    path: str
    label: str


def expand_root_tokens(text: str, root: str) -> str:
    """把正文里的路径占位符换成真实技能目录。"""
    if not text:
        return text
    norm = root.replace("\\", "/")
    for token in ROOT_TOKENS:
        if token in text:
            text = text.replace(token, norm)
    return text


@dataclass
class SkillLibrary:
    """一个任务可见的全部技能。"""

    roots: list[ResolvedRoot] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    max_body_chars: int = MAX_BODY_CHARS
    max_file_chars: int = MAX_FILE_CHARS

    # ---- 基本 --------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.skills)

    @property
    def names(self) -> list[str]:
        return sorted(self.skills)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name.strip())

    def describe(self) -> dict[str, Any]:
        """给 `GET /api/health` 之类的诊断接口用。"""
        return {
            "count": self.count,
            "names": self.names,
            "roots": [{"path": r.path, "label": r.label} for r in self.roots],
            "skipped": list(self.skipped),
        }

    # ---- 构建 --------------------------------------------------------------
    @classmethod
    def from_roots(
        cls,
        roots: list[ResolvedRoot],
        *,
        max_body_chars: int = MAX_BODY_CHARS,
        max_file_chars: int = MAX_FILE_CHARS,
        max_skills: int = 64,
    ) -> "SkillLibrary":
        """扫描多个根目录。**先扫到的优先**（同名技能后者不覆盖前者）。"""
        lib = cls(max_body_chars=max_body_chars, max_file_chars=max_file_chars)
        seen_roots: set[str] = set()
        for r in roots:
            real = os.path.realpath(r.path)
            if real in seen_roots:
                continue
            seen_roots.add(real)
            found, skipped = scan_root(
                real,
                source=r.label,
                max_body_chars=max_body_chars,
                max_skills=max_skills,
            )
            lib.roots.append(ResolvedRoot(path=real, label=r.label))
            for s in found:
                if s.name in lib.skills:
                    lib.skipped.append({
                        "dir": s.root,
                        "reason": f"技能名 {s.name} 已被 "
                                  f"{lib.skills[s.name].source or '更早的目录'} 覆盖，本份未加载",
                    })
                    continue
                lib.skills[s.name] = s
            lib.skipped.extend(skipped)
        return lib

    @classmethod
    def from_config(cls, cfg: Any = None, *, workspace: str = "") -> "SkillLibrary":
        """按配置 + 工作区构建技能库。

        `cfg` 为空时用环境变量的默认值（与 config.SkillsConfig.from_env 一致），
        这样单独 new 一个 AgentCore 也能拿到技能，不会出现"没接线就永远为空"。
        """
        if cfg is None:
            from ..config import SkillsConfig  # noqa: PLC0415 —— 避免模块级循环

            cfg = SkillsConfig.from_env()

        roots: list[ResolvedRoot] = []
        project_dir = str(getattr(cfg, "project_dir", "") or "")
        if workspace and project_dir:
            roots.append(ResolvedRoot(os.path.join(workspace, project_dir), "项目级"))
        user_dir = str(getattr(cfg, "user_dir", "") or "")
        if user_dir:
            roots.append(ResolvedRoot(os.path.expanduser(user_dir), "用户级"))
        for extra in list(getattr(cfg, "dirs", []) or []):
            if str(extra).strip():
                roots.append(ResolvedRoot(os.path.expanduser(str(extra).strip()), "自定义"))
        builtin = str(getattr(cfg, "builtin_dir", "") or "")
        # ★ 内置技能只在"工作区就在这个仓库里"时提供。
        #   理由：内置目录（<仓库>/skills）装的是 Loom 自己的开发套路（跑哪些自检、
        #   哪些 Traceback 是故意的）。别的项目里根本没有这些命令，让模型看到它
        #   只会浪费轮次。这不是特例 —— 判据是"工作区是否在该目录的所属仓库内"，
        #   换成任何内置目录都成立。
        if builtin and workspace and _inside(os.path.dirname(os.path.realpath(builtin)),
                                             os.path.realpath(workspace)):
            roots.append(ResolvedRoot(builtin, "内置"))

        return cls.from_roots(
            roots,
            max_body_chars=int(getattr(cfg, "max_body_chars", MAX_BODY_CHARS)),
            max_file_chars=int(getattr(cfg, "max_file_chars", MAX_FILE_CHARS)),
            max_skills=int(getattr(cfg, "max_skills", 64)),
        )

    # ---- 第 1 级：目录 -----------------------------------------------------
    def catalog_text(
        self,
        *,
        max_chars: int = CATALOG_MAX_CHARS,
        max_desc: int = MAX_DESC_CHARS,
    ) -> str:
        """渲染系统提示里的技能目录。没有技能就返回空串（调用方据此不加这一节）。"""
        if not self.skills:
            return ""

        by_label: dict[str, list[Skill]] = {}
        for label in [r.label for r in self.roots]:
            by_label.setdefault(label, [])
        for s in self.skills.values():
            by_label.setdefault(s.source or "其他", []).append(s)

        lines: list[str] = []
        budget = max_chars
        omitted = 0
        for label, items in by_label.items():
            if not items:
                continue
            lines.append(f"### {label}")
            for s in sorted(items, key=lambda x: x.name):
                desc = s.description.replace("\n", " ").strip()
                if len(desc) > max_desc:
                    desc = desc[:max_desc] + "…"
                line = f"- `{s.name}`：{desc}"
                if budget - len(line) < 0:
                    omitted += 1
                    continue
                budget -= len(line) + 1
                lines.append(line)
        if omitted:
            lines.append(f"（还有 {omitted} 个技能因篇幅未列出；需要时可直接用名字试 load_skill）")
        return "\n".join(lines)

    # ---- 第 2 级：正文 -----------------------------------------------------
    def load(self, name: str) -> tuple[Skill | None, str]:
        skill = self.get(name)
        if skill is None:
            avail = "、".join(self.names) if self.skills else "无"
            return None, f"没有名为 {name!r} 的技能（可用：{avail}）"
        return skill, ""

    # ---- 第 3 级：附带文件 -------------------------------------------------
    def read_file(self, name: str, rel: str) -> tuple[str, str]:
        """读技能目录内的附带文件。返回 (内容, 错误说明)。"""
        skill, err = self.load(name)
        if skill is None:
            return "", err

        rel = (rel or "").strip().replace("\\", "/").lstrip("/")
        if not rel:
            return "", "要读哪个文件？给一个相对路径，例如 references/api.md"
        target = os.path.realpath(os.path.join(skill.root, rel))
        if not _inside(skill.root, target):
            return "", f"路径越界：{rel} 不在技能 {name} 的目录内"
        if os.path.isdir(target):
            return "", f"{rel} 是目录；用 load_skill(name) 的返回里可以看到附带文件清单"
        if not os.path.isfile(target):
            # 相近候选：先按子串，再用 difflib 兜（"references/none.md" 找不到时
            # 真正常见的是名字记错，而不是斜杠写错）
            near = [f for f in skill.files if rel.lower() in f.lower()]
            if not near:
                import difflib

                near = difflib.get_close_matches(rel, skill.files, n=3, cutoff=0.4)
            hint = f"；相近的有：{'、'.join(near)}" if near else ""
            return "", f"技能 {name} 里没有 {rel}{hint}"

        try:
            with open(target, "rb") as fh:
                raw = fh.read(self.max_file_chars * 4 + 4096)
        except OSError as exc:
            return "", f"读不了 {rel}：{exc}"

        if b"\x00" in raw[:2048]:
            return "", (f"{rel} 看起来是二进制文件，不能读进上下文；"
                        f"它的绝对路径是 {target}，需要处理时交给终端或命令")
        text = raw.decode("utf-8", errors="replace")
        if len(text) > self.max_file_chars:
            text = text[: self.max_file_chars] + (
                f"\n\n…（超过 {self.max_file_chars} 字符，已截断；"
                f"完整文件：{target}）"
            )
        return expand_root_tokens(text, skill.root), ""


# --------------------------------------------------------------------------- #
# 渲染（工具返回给模型的那段文本）
# --------------------------------------------------------------------------- #
def render_skill(skill: Skill) -> str:
    """把技能正文渲染成模型看到的样子：先给位置与附带件，再给正文。"""
    head = [f"技能：{skill.name}（{skill.source or '未标注来源'}）"]
    head.append(f"目录：{skill.root.replace(chr(92), '/')}")
    if skill.files:
        shown = "、".join(skill.files[:20])
        more = f" 等 {len(skill.files)} 个" if len(skill.files) > 20 else ""
        head.append(f"附带文件：{shown}{more}")
        head.append("（需要时用 load_skill(name=…, file=\"相对路径\") 读；"
                    "脚本可直接用上面的绝对路径调用）")
    else:
        head.append("附带文件：无")
    body = expand_root_tokens(skill.body, skill.root)
    return "\n".join(head) + "\n\n──── 正文 ────\n" + body


def render_skill_file(skill: Skill, rel: str, text: str) -> str:
    head = (f"技能 {skill.name} 的文件：{rel}\n"
            f"绝对路径：{os.path.join(skill.root, rel).replace(chr(92), '/')}")
    return head + "\n\n──── 内容 ────\n" + text
