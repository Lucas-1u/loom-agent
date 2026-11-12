"""
Loom · Skill 机制（对齐 DSH `dsh-skill` + `dsh-skill-filesystem`）
====================================================================

★ 什么是 Skill
--------------
一个**目录**，里面有一份 `SKILL.md`：

    skills/<名字>/
    ├── SKILL.md          必填。YAML frontmatter（name / description）+ Markdown 正文
    ├── references/       可选。需要时才读进上下文的资料（长文档、schema、规范）
    ├── scripts/          可选。可执行脚本（不读进上下文，直接跑）
    └── assets/           可选。产出用的模板/图片/字体（不读进上下文）

它和"提示词""工具""子 Agent"的区别：
  · 提示词  → 对所有任务都生效（全局）
  · 技能    → 只在该场景下生效（按需载入），平时只占目录里一行字
  · 工具    → 提供**能力**（能做什么）
  · 技能    → 提供**做法**（该怎么做，按什么顺序，注意哪些坑）

★ 三级渐进披露（本机制存在的全部理由）
------------------------------------
| 级别 | 内容 | 何时进上下文 | 成本 |
|---|---|---|---|
| 1 | name + description | **总是**（系统提示的技能目录） | 每个技能约一行 |
| 2 | SKILL.md 正文 | 模型调 `load_skill(name)` 时 | 几百 ~ 几千字 |
| 3 | references / scripts / assets | 模型显式要，或按正文自己去读/执行 | 用多少算多少 |

如果只有 1 没有 2/3，就退化成"往系统提示里塞一大堆说明书"——上下文必炸；
如果只有 2/3 没有 1，模型根本不知道该载入哪个——技能等于不存在。

★ 模块分工（照搬 DSH 的包边界，便于对照）
---------------------------------------
    loader.py   ← dsh-skill-filesystem：扫目录、解析 frontmatter、校验、列附带文件
    library.py  ← dsh-skill：技能目录怎么渲染、正文怎么载入、附带文件怎么读

loader 只认文件系统，不认识 Agent / 工具 / 提示词；library 也只依赖 loader。
两个模块都不 import agent/、tools/、server/ —— 保持叶子地位。
"""

from .library import SkillLibrary, render_skill, render_skill_file
from .loader import Skill, minimal_yaml, parse_frontmatter, scan_root

__all__ = [
    "Skill",
    "SkillLibrary",
    "scan_root",
    "parse_frontmatter",
    "minimal_yaml",
    "render_skill",
    "render_skill_file",
]
