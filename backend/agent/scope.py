"""
改动范围：路径匹配 + 工作区快照/对比

★ 为什么单独一个模块？
  Policy（执行前拦截）和 AgentCore（结束后审计）都需要判断"这个路径在不在允许范围内"。
  两处各写一套，迟早会出现"审计说越界、拦截没拦"的分裂。

★ 职责边界
  这里只做**字符串层面的路径归一与包含判断**，不做安全边界判定 ——
  真正的路径安全（符号链接、越界拒绝）在 `tools/path_safety.py`。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

# 快照时跳过的目录：版本库/依赖/缓存，以及 Loom 自己产生的备份
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".loom_backup",
}
# 只有小于这个体积的文件才算哈希；更大的只用 size+mtime 推断，并标记为不确定
MAX_HASH_BYTES = 512 * 1024


def norm_rel(raw: str) -> str:
    """
    把用户/模型给的路径归一成 `a/b/c` 形式（小写、正斜杠、无首尾斜杠）。

    ★ "." 必须归一成空串：默认 `allowed_paths=["."]` 表示"整个工作区"，
      如果留下 "."，`within_allowed` 会把它当成一个名叫 "." 的目录，
      于是**每一个**改动都会被误判成"超出允许范围"（压力测试跑出来的真 bug）。
    """
    s = (raw or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    s = s.strip("/").lower()
    return "" if s == "." else s


def hits(rel: str, patterns: list[str]) -> bool:
    """`rel` 是否命中 patterns 里的任意一项（文件精确匹配或目录前缀匹配）。"""
    r = norm_rel(rel)
    if not r:
        return False
    for p in patterns or []:
        q = norm_rel(p)
        if not q:
            continue
        if r == q or r.startswith(q + "/"):
            return True
    return False


def within_allowed(rel: str, allowed: list[str]) -> bool:
    """
    是否落在允许范围内。

    ★ 空列表 / ["."] / [""] 都表示"整个工作区" —— 默认值，行为与以前一致。
    """
    cleaned = [norm_rel(a) for a in (allowed or []) if norm_rel(a)]
    if not cleaned:
        return True
    return hits(rel, cleaned)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(root: str) -> dict[str, dict[str, Any]]:
    """
    遍历工作区，返回 `{相对路径: {size, mtime_ns, sha}}`。

    ★ sha 只对足够小的文件计算；更大或读不了的文件退化成 size+mtime，
      并在对比时标记 `uncertain` —— 不假装能精确归因。
    """
    out: dict[str, dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                st = os.stat(full)
                entry: dict[str, Any] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha": ""}
                if st.st_size <= MAX_HASH_BYTES:
                    entry["sha"] = _sha256(full)
                else:
                    entry["uncertain"] = True
            except OSError as exc:
                entry = {"size": -1, "mtime_ns": 0, "sha": "", "unreadable": str(exc)[:80]}
            out[rel] = entry
    return out


def diff_tree(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """
    对比两次快照，返回 `{"added": [...], "modified": [...], "deleted": [...]}`。

    ★ modified 的判断分两档：
      两边都有 sha → 比 sha（精确）
      只有 size/mtime（大文件）→ 认为"可能变了"，仍算 modified 但在调用方
      会被标成 uncertain —— 宁可多说一句"不确定"，也不要漏报。
    """
    added = [rel for rel in after if rel not in before]
    deleted = [rel for rel in before if rel not in after]
    modified: list[str] = []
    for rel, cur in after.items():
        prev = before.get(rel)
        if prev is None:
            continue
        if prev.get("sha") and cur.get("sha"):
            if prev["sha"] != cur["sha"]:
                modified.append(rel)
        elif (prev.get("size"), prev.get("mtime_ns")) != (cur.get("size"), cur.get("mtime_ns")):
            modified.append(rel)
    return {"added": sorted(added), "modified": sorted(modified), "deleted": sorted(deleted)}


def rel_in_workspace(workspace: str, target: str) -> str:
    """
    把（可能是绝对路径的）目标路径转成工作区相对路径。

    ★ 用途：Policy 在**执行前**判断"这次写入在不在允许范围内"。
      转换失败（越界/非法）时返回空串 —— 调用方应按"不在范围内"处理，
      绝不能因为"算不出相对路径"就放行。
    """
    try:
        from ..tools.path_safety import resolve_in_workspace

        rp = resolve_in_workspace(workspace, target, must_exist=False)
    except Exception:  # noqa: BLE001 —— 越界就是越界
        return ""
    # ResolvedPath.relative 已经是「相对工作区根」的路径，直接用；
    # ★ 不要再对它做一次 relpath —— 那是把相对路径当成相对 CWD 解析，
    #   会得到 "��/Loom_agent/loom/docs/x" 这种完全错的结果（实测踩到）。
    rel = (getattr(rp, "relative", "") or "").strip()
    if rel:
        return rel.replace("\\", "/")
    try:
        return os.path.relpath(str(getattr(rp, "absolute", rp)), str(workspace)).replace("\\", "/")
    except (ValueError, TypeError):
        return ""
