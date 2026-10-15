"""
Loom Agent v0.1 · 路径安全（工作区边界）

★ 这是整个项目最重要的安全组件。它只做一件事：
    给定「工作区根目录」和「用户/模型给的路径」，判断这个路径是否越界。

────────────────────────────────────────────────────────────────
为什么不能简单用 os.path.abspath 加 startswith？
────────────────────────────────────────────────────────────────
三个真实攻击面：

  ① **`../` 穿越**
       workspace = D:/proj
       输入 "../../Windows/System32/config/SAM"
       abspath 会把它规范化成 D:/Windows/System32/config/SAM
       → 用 realpath + 前缀比较能挡住。

  ② **符号链接逃逸（symlink escape）**
       攻击者在工作区里放一个链接 evil.txt -> C:/Users/me/.ssh/id_rsa
       输入 "evil.txt"
       abspath 得到 D:/proj/evil.txt（前缀匹配通过！）
       → **必须用 realpath 解掉链接**，得到 C:/Users/me/.ssh/id_rsa，才发现越界。
       这是 abspath 与 realpath 的本质区别，也是最容易被忘掉的一步。

  ③ **Windows 短名（8.3）绕过**
       "PROGRA~1" 是 "Program Files" 的短名。
       需要用 os.path.realpath，Windows 上它会做规范化。

────────────────────────────────────────────────────────────────
另外一个陷阱：前缀比较不能只比字符串
────────────────────────────────────────────────────────────────
   root  = D:/proj
   target = D:/proj_evil/x.txt
   
   字符串 startswith("D:/proj") 会**误判为通过**！
   → 必须比较「带分隔符的前缀」：D:/proj/ 或 target == D:/proj

────────────────────────────────────────────────────────────────
还有一个：不存在的路径
────────────────────────────────────────────────────────────────
   写新文件时，目标文件还不存在，realpath 对不存在的路径也能工作
   （它会规范化已存在的部分 + 保留不存在的尾部），
   但要注意必须对**父目录**做 realpath 检查，否则
   D:/proj/newdir_which_is_symlink/x.txt 这种会漏。
   本模块的 resolve_in_workspace 显式处理了这个情况。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class PathViolation(Exception):
    """路径越界。这是安全事件，不是普通错误。"""

    def __init__(self, message: str, *, given: str = "", resolved: str = "", root: str = ""):
        super().__init__(message)
        self.given = given
        self.resolved = resolved
        self.root = root


@dataclass
class ResolvedPath:
    """路径解析结果。"""

    given: str          # 用户/模型原始输入
    absolute: str       # 规范化后的绝对路径（已 realpath）
    relative: str       # 相对于工作区根的路径（用于展示，不含盘符）
    exists: bool

    def __str__(self) -> str:
        return self.relative or self.absolute


# --------------------------------------------------------------------------- #
# 根目录规范化
# --------------------------------------------------------------------------- #
def normalize_root(root: str) -> str:
    """
    规范化工作区根。

    必须做两件事：
      1. realpath（解掉链接、规范化大小写与短名）
      2. 校验它真的是个目录

    一次做，到处复用 —— 在 TaskState.workspace 里存的就是这个值，
    此后所有路径校验都基于它，不允许中途换根。
    """
    if not root:
        raise PathViolation("工作区根目录未设置")
    abs_root = os.path.realpath(os.path.abspath(root))
    if not os.path.isdir(abs_root):
        raise PathViolation(f"工作区根目录不存在或不是目录：{abs_root}", root=abs_root)
    return abs_root


# --------------------------------------------------------------------------- #
# 核心：解析并校验
# --------------------------------------------------------------------------- #
def resolve_in_workspace(root: str, given: str, *, must_exist: bool = False) -> ResolvedPath:
    """
    把 given 解析为工作区内的绝对路径，越界抛 PathViolation。

    参数：
        root        已 normalize_root 的工作区根
        given       模型/用户给的路径（可为相对路径、可为 "."）
        must_exist  是否要求目标已存在

    返回：
        ResolvedPath

    抛：
        PathViolation —— 越界 / 非法 / 不存在（must_exist 时）
    """
    if given is None:
        raise PathViolation("路径参数为空", root=root)

    raw = str(given).strip()
    if not raw:
        raw = "."
    if "\x00" in raw:
        raise PathViolation("路径含空字节，拒绝", given=given, root=root)

    # ---- 第一步：拼成绝对路径 ---------------------------------------------
    # 注意：Windows 上绝对路径可能是 "D:/x" 或 "\\server\share" 形式。
    # os.path.isabs 在 Windows 上能正确处理盘符。
    if os.path.isabs(raw):
        candidate = os.path.abspath(raw)
    else:
        candidate = os.path.abspath(os.path.join(root, raw))

    # ---- 第二步：realpath 解掉符号链接（★ 关键步骤）------------------------
    resolved = os.path.realpath(candidate)
    root_real = os.path.realpath(root)

    # ---- 第三步：带分隔符的前缀比较（避免 D:/proj vs D:/proj_evil 误判）----
    if not _is_within(resolved, root_real):
        raise PathViolation(
            f"路径越出工作区：{given} → {resolved}（工作区：{root_real}）",
            given=given,
            resolved=resolved,
            root=root_real,
        )

    # ---- 第四步：不存在的中间目录也要校验（防 symlink 中间层逃逸）----------
    # 例：工作区里有 evil_dir -> C:/Windows，模型传 "evil_dir/notepad.exe"
    # 上面的 realpath 已经解开了 evil_dir，所以 resolved 会变成 C:/Windows/notepad.exe，
    # 第三步就挡住了。但若 evil_dir 本身不存在（模型拼错），realpath 不会报错，
    # 这时 resolved 仍在工作区内，是安全的 —— 交给 must_exist 处理即可。

    exists = os.path.exists(resolved)

    if must_exist and not exists:
        raise PathViolation(f"路径不存在：{resolved}", given=given, resolved=resolved, root=root_real)

    rel = os.path.relpath(resolved, root_real)
    rel = "" if rel == "." else rel.replace(os.sep, "/")

    return ResolvedPath(given=raw, absolute=resolved, relative=rel, exists=exists)


def _is_within(target: str, root: str) -> bool:
    """
    判断 target 是否在 root 之内（已 realpath 的两个绝对路径）。

    ★ 必须比较「带分隔符的前缀」，否则 D:/proj 会匹配到 D:/proj_evil。
    Windows 上还要做大小写归一（NTFS 不区分大小写）。
    """
    t = os.path.normcase(os.path.normpath(target))
    r = os.path.normcase(os.path.normpath(root))

    if t == r:
        return True

    sep = os.sep
    if not r.endswith(sep):
        r = r + sep
    return t.startswith(r)


# --------------------------------------------------------------------------- #
# 敏感文件判定
# --------------------------------------------------------------------------- #
def is_sensitive(abs_path: str, relative: str, *, sensitive_names: set[str], sensitive_dir_parts: set[str]) -> tuple[bool, str]:
    """
    判断是否命中敏感文件名单。

    返回 (是否敏感, 原因)。命中不是「拒绝」，而是「显著提醒」——
    v0.1 的策略是让用户知情后决定，而不是替用户做主拒绝。
    （写 .env 有时确实是正当需求，比如让 Agent 生成配置模板。）
    """
    abspath = os.path.normcase(os.path.abspath(abs_path))
    name = os.path.basename(abspath)

    # 直接命中文件名
    for s in sensitive_names:
        if name == s.lower():
            return True, f"文件名 '{s}' 属于常见密钥/凭证文件"

    # 或命中敏感目录
    parts = [p for p in os.path.normcase(relative).replace("\\", "/").split("/") if p]
    for part in parts:
        if part in {p.lower() for p in sensitive_dir_parts}:
            return True, f"位于敏感目录 '{part}' 内"

    return False, ""


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def display_path(root: str, abs_path: str) -> str:
    """
    把绝对路径转成「工作区相对路径」，用于展示与日志。

    为什么日志要脱敏成相对路径？
      → 因为绝对路径会暴露用户的真实目录结构（用户名、盘符布局）。
        展示成 "./src/main.py" 既达意又不泄露。
    """
    if not abs_path:
        return ""
    try:
        rel = os.path.relpath(os.path.realpath(abs_path), os.path.realpath(root))
    except ValueError:
        return abs_path
    if rel.startswith(".."):
        return abs_path   # 越界时如实显示（用于告警）
    return "./" + rel.replace(os.sep, "/") if rel != "." else "."


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def safe_join(root: str, *parts: str) -> str:
    """受限拼接：拼完仍必须在 root 内，否则抛异常。"""
    joined = os.path.join(root, *parts)
    resolved = os.path.realpath(os.path.abspath(joined))
    if not _is_within(resolved, os.path.realpath(root)):
        raise PathViolation(f"拼接后越界：{joined}", resolved=resolved, root=root)
    return resolved
