"""
删除会话（含可选的「会话产物」）—— 对比表外的新功能。

★ 两种删除：
    ① 仅删会话：DB 里的会话 + 任务 + 事件 + 投影 + 历史，**工作区文件一个不动**
    ② 连产物一起删：额外删除「本会话创建、且内容至今未变」的文件

★ ★ 产物怎么定位（这是整个功能最需要保守的地方）：
  从各任务 `state_json.change_records` 推导，按 path 分组取**最后一次**操作，
  同时满足三条才算"本会话的产物"：

    ① 最后一条操作是 create / overwrite
    ② 该条的 `before_hash == ""` —— 操作前文件不存在，确实是本会话新建的
    ③ 当前文件的 sha256 仍等于 `after_hash` —— 内容没被用户或后续会话改过

  任何一条不满足 → 归入 `skipped` 并写明原因，**绝不删**。

  ★ 为什么不用文件 mtime 猜："今天改过的文件"既可能是本会话建的，也可能是
    用户手改的、IDE 保存的、另一个会话动了同一个文件。mtime 只能说明"动过"，
    说不出"谁动的"。删除不可逆（即便有备份），宁可漏删，不能错删用户的原有文件。

★ 备份：走和 `delete_file` 工具同一套 `make_backup`，落到
  `<workspace>/.loom_backup/<时间戳>/<相对路径>`，路径可预期、用户能自己还原。
  **备份失败就中止**（一个文件都不删），和 delete_file 的约定一致。
"""

from __future__ import annotations

import os
from typing import Any

from ..tools.base import ToolContext
from ..tools.hashing import sha256_file
from ..tools.mutating._shared import make_backup

# 只有这两个动作会"让文件出现/被整体替换"；edit 是在既有文件上改，不能算新建
GENERATED_ACTIONS = ("create", "overwrite")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


def _inside(root: str, target: str) -> bool:
    """target 是否在 root 之内（都用绝对路径比较，防 ../ 逃逸）。"""
    if not root:
        return False
    r, t = _norm(root), _norm(target)
    return t == r or t.startswith(r + os.sep)


def _abs_of(workspace: str, path: str) -> str:
    """change_records 里存的是相对工作区的路径（display_path 的结果）。"""
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(workspace, path))


def collect_artifacts(workspace: str, states: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """
    从任务状态里推导"本会话产出的文件"。纯函数，不删任何东西。

    返回 `{files, skipped, total}`：
        files   —— 可以安全删除的（会被备份）
        skipped —— 看着像但**不能删**的，带原因（前端要显示给用户）
    """
    last: dict[str, dict[str, Any]] = {}
    for task_id, st in states:
        for rec in (st.get("change_records") or []):
            path = str(rec.get("path") or "").strip()
            if not path:
                continue
            ts = float(rec.get("ts") or 0)
            prev = last.get(path)
            # 同一路径可能被多个任务、多轮操作过 —— 只认最后一次
            if prev is None or ts >= float(prev.get("ts") or 0):
                last[path] = {**rec, "_task": task_id, "ts": ts}

    files: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for path, rec in last.items():
        action = str(rec.get("action") or "")
        before = str(rec.get("before_hash") or "")
        after = str(rec.get("after_hash") or "")

        if action == "delete":
            skipped.append({"path": path, "reason": "会话结束时已被删除"})
            continue
        if action not in GENERATED_ACTIONS:
            skipped.append({"path": path, "reason": f"只是编辑（{action}），不是新建"})
            continue
        if before:
            # 有 before_hash = 写之前文件就存在 —— 那是用户的文件，不能删
            skipped.append({"path": path, "reason": "会话前已存在，只是被改写/覆盖"})
            continue

        abs_path = _abs_of(workspace, path)
        if not _inside(workspace, abs_path):
            skipped.append({"path": path, "reason": "不在当前工作区内"})
            continue
        if not os.path.isfile(abs_path):
            skipped.append({"path": path, "reason": "文件已不存在"})
            continue
        try:
            now_hash = sha256_file(abs_path)
            size = os.path.getsize(abs_path)
        except OSError as exc:
            skipped.append({"path": path, "reason": f"读取失败：{type(exc).__name__}"})
            continue
        if after and now_hash != after:
            skipped.append({"path": path, "reason": "内容已被后续修改，不再等同于会话产物"})
            continue

        files.append({
            "path": path,
            "abs_path": abs_path,
            "bytes": size,
            "task_id": rec.get("_task", ""),
            "ts": rec.get("ts") or 0.0,
        })

    return {"files": files, "skipped": skipped, "total": len(files)}


def build_plan(store: Any, session_id: str) -> dict[str, Any] | None:
    """
    生成删除预案（只读）。返回 None 表示会话不存在。

    ★ 为什么 preview 和 execute 共用同一个 build_plan：
      "给用户看的清单"和"实际要删的清单"必须是同一份计算结果 ——
      分成两套逻辑，迟早出现"预览说删 3 个，实际删了 5 个"。
    """
    sess = store.get_session_sync(session_id)
    if not sess:
        return None

    workspace = str(sess.get("workspace") or "")
    tasks = store.list_tasks_sync(session_id)
    states: list[tuple[str, dict[str, Any]]] = []
    for t in tasks:
        full = store.get_task_sync(t["task_id"], with_state=True)
        if full and isinstance(full.get("state"), dict):
            states.append((t["task_id"], full["state"]))

    artifacts = collect_artifacts(workspace, states)
    # 投影/事件数从 DB 数，不数 state（state 只是快照，可能有缺口）
    rows = store.list_projections_sync(session_id=session_id, limit=1000)
    event_total = sum(int(r.get("event_count") or 0) for r in rows)

    total_bytes = sum(int(f["bytes"]) for f in artifacts["files"])
    return {
        "session": {
            "session_id": sess["session_id"],
            "title": sess.get("title") or "",
            "workspace": workspace,
            "created_at": sess.get("created_at"),
            "updated_at": sess.get("updated_at"),
        },
        "tasks": len(tasks),
        "events": event_total,
        "artifacts": {
            "total": artifacts["total"],
            "total_bytes": total_bytes,
            "files": [
                {"path": f["path"], "bytes": f["bytes"], "task_id": f["task_id"]}
                for f in artifacts["files"]
            ],
            "skipped": artifacts["skipped"],
        },
        # 前端要用来做"输入名字确认"
        "confirm_text": (sess.get("title") or "").strip() or session_id,
    }


def execute_plan(store: Any, session_id: str, *, with_artifacts: bool) -> dict[str, Any]:
    """
    执行删除。返回结果（含备份目录与逐文件结果）。

    ★ 顺序：备份 → 删文件 → 删 DB。
      文件先删：万一 DB 删除失败，至少不会出现"库里没了、文件还在"——
      那种情况用户会以为文件也删了，实际没有，更难收拾。
      每删一个文件都先备份，备份失败立即抛错（一个都不删）。
    """
    plan = build_plan(store, session_id)
    if plan is None:
        raise KeyError(session_id)

    workspace = plan["session"]["workspace"]
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    backup_dir = ""

    if with_artifacts and plan["artifacts"]["files"]:
        ctx = ToolContext(workspace=workspace)
        for item in plan["artifacts"]["files"]:
            abs_path = _abs_of(workspace, item["path"])
            if not _inside(workspace, abs_path):
                failed.append({"path": item["path"], "reason": "不在工作区内"})
                continue
            backup_path, err = make_backup(abs_path, ctx)
            if err:
                failed.append({"path": item["path"], "reason": f"备份失败：{err}"})
                continue      # 备份失败 → 这个文件不删
            backup_dir = os.path.dirname(backup_path)
            try:
                os.remove(abs_path)
                removed.append({"path": item["path"], "backup": backup_path})
            except OSError as exc:
                failed.append({"path": item["path"], "reason": f"删除失败：{type(exc).__name__}"})

    counts = store.delete_session_sync(session_id)
    return {
        "session_id": session_id,
        "with_artifacts": bool(with_artifacts),
        "db": counts,
        "removed_files": removed,
        "failed_files": failed,
        "backup_dir": backup_dir,
        "skipped": plan["artifacts"]["skipped"],
    }
