"""
Checkpoint 存储层

★ 设计原则
----------------
  · 每个 checkpoint 是一个 JSON 文件，放在 `<workspace>/.loom_checkpoint/` 下
  · 文件名 = `<task_id>_<seq>.json`，seq 从 1 递增
  · 每个文件包含完整 TaskState 的 to_dict() 快照 + 元数据
  · 读取时从最新开始找，找到第一个完整文件就返回

★ 与 SQLite 的关系
----------------
TaskStore 已经把 state_json 落库了，但它只在审批事件和终态时写。
Checkpoint 提供**更细粒度**的快照：每个执行点之前都写一次，
使得崩溃后可以恢复到最近的一致状态。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class CheckpointRef:
    """指向一个 checkpoint 的引用。"""

    task_id: str
    seq: int
    path: str
    created_at: float
    phase: str = ""  # checkpoint 点标识


@runtime_checkable
class CheckpointStore(Protocol):
    """Checkpoint 存储抽象。"""

    def save(self, payload: dict[str, Any]) -> CheckpointRef: ...
    def latest(self, task_id: str) -> CheckpointRef | None: ...
    def load(self, ref: CheckpointRef) -> dict[str, Any] | None: ...
    def list(self, task_id: str) -> list[CheckpointRef]: ...
    def delete(self, ref: CheckpointRef) -> bool: ...


class LocalDiskCheckpointStore:
    """
    本地磁盘实现。

    每个 checkpoint 一个 JSON 文件。目录结构：
        <base_dir>/
            <task_id>_0001.json
            <task_id>_0002.json
            ...
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = str(base_dir)
        Path(self.base_dir).mkdir(parents=True, exist_ok=True)

    def _path_for(self, task_id: str, seq: int) -> str:
        return os.path.join(self.base_dir, f"{task_id}_{seq:04d}.json")

    def save(self, payload: dict[str, Any]) -> CheckpointRef:
        task_id = str(payload.get("task_id", "unknown"))
        phase = str(payload.get("phase", ""))
        # 找下一个 seq
        existing = self.list(task_id)
        seq = max((r.seq for r in existing), default=0) + 1
        path = self._path_for(task_id, seq)
        now = time.time()
        full = {
            "meta": {
                "task_id": task_id,
                "seq": seq,
                "phase": phase,
                "created_at": now,
            },
            "state": payload.get("state", {}),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, default=str)
        return CheckpointRef(task_id=task_id, seq=seq, path=path, created_at=now, phase=phase)

    def latest(self, task_id: str) -> CheckpointRef | None:
        refs = self.list(task_id)
        return refs[-1] if refs else None

    def load(self, ref: CheckpointRef) -> dict[str, Any] | None:
        if not os.path.exists(ref.path):
            return None
        try:
            with open(ref.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def list(self, task_id: str) -> list[CheckpointRef]:
        refs: list[CheckpointRef] = []
        if not os.path.isdir(self.base_dir):
            return refs
        for name in sorted(os.listdir(self.base_dir)):
            if not name.endswith(".json"):
                continue
            if task_id not in name:
                continue
            # 解析 seq
            try:
                stem = name[:-5]  # 去掉 .json
                # 格式: <task_id>_<seq>.json
                _, seq_str = stem.rsplit("_", 1)
                seq = int(seq_str)
            except (ValueError, AttributeError):
                continue
            full_path = os.path.join(self.base_dir, name)
            try:
                stat = os.stat(full_path)
                with open(full_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                phase = str(data.get("meta", {}).get("phase", ""))
            except (OSError, json.JSONDecodeError):
                continue
            refs.append(CheckpointRef(
                task_id=task_id,
                seq=seq,
                path=full_path,
                created_at=stat.st_mtime,
                phase=phase,
            ))
        refs.sort(key=lambda r: r.seq)
        return refs

    def delete(self, ref: CheckpointRef) -> bool:
        try:
            os.remove(ref.path)
            return True
        except OSError:
            return False


def load_checkpoint(store: CheckpointStore, ref: CheckpointRef) -> dict[str, Any] | None:
    """从存储中加载一个 checkpoint。"""
    data = store.load(ref)
    if data is None:
        return None
    # 返回 state 部分（调用方负责重建 TaskState）
    state = data.get("state", {})
    if not isinstance(state, dict):
        return None
    # 注入 checkpoint 元数据
    state["_checkpoint"] = {
        "seq": data.get("meta", {}).get("seq"),
        "phase": data.get("meta", {}).get("phase"),
        "created_at": data.get("meta", {}).get("created_at"),
    }
    return state
