"""
工作流持久化（V1）：workflows / steps / jobs / outbox 四张表。

★ 为什么复用同一个 SQLite 文件
--------------------------
不引入 Redis/新服务；表前缀 `workflow_` 明确分区，和 sessions/tasks 共用一个库文件。
连接是独立的（不与 TaskStore 共享），因为 workflow 会被工具层调用，而
`agent/` 不允许 import `server/`——这个约束在 RAG 里已经踩过一次。

★ outbox 是"可靠"的关键
--------------------
事件先落库（`workflow_outbox`）再投递；投递成功后写 `dispatched_at`。
进程如果在"落库后、投递前"挂掉，重启扫描时还能补发——纯内存 EventBus 做不到这一点。

★ 去重
-----
`claim_step()` 用 `UPDATE ... WHERE status IN ('pending','ready')` 原子抢占，
返回 rowcount 判断是否抢到 —— 同一个 step 被重复调度时，第二次会拿到 0。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import (Job, JobStatus, Step, StepStatus, StepSpec, Workflow, WorkflowStatus,
                     WorkflowView)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
  workflow_id   TEXT PRIMARY KEY,
  goal          TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL,
  task_id       TEXT NOT NULL DEFAULT '',
  workspace     TEXT NOT NULL DEFAULT '',
  created_at    REAL NOT NULL,
  deadline_at   REAL NOT NULL DEFAULT 0,
  cancel        INTEGER NOT NULL DEFAULT 0,
  plan_version  INTEGER NOT NULL DEFAULT 1,
  error         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workflow_steps (
  workflow_id  TEXT NOT NULL,
  step_id      TEXT NOT NULL,
  title        TEXT NOT NULL DEFAULT '',
  kind         TEXT NOT NULL DEFAULT '',
  spec         TEXT NOT NULL DEFAULT '{}',
  depends      TEXT NOT NULL DEFAULT '[]',
  status       TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  timeout_s    REAL NOT NULL DEFAULT 0,
  result       TEXT,
  error        TEXT NOT NULL DEFAULT '',
  started_at   REAL NOT NULL DEFAULT 0,
  ended_at     REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (workflow_id, step_id)
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
  job_id       TEXT PRIMARY KEY,
  workflow_id  TEXT NOT NULL,
  step_id      TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL,
  resumable    INTEGER NOT NULL DEFAULT 0,
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 1,
  timeout_s    REAL NOT NULL DEFAULT 60,
  created_at   REAL NOT NULL,
  started_at   REAL NOT NULL DEFAULT 0,
  ended_at     REAL NOT NULL DEFAULT 0,
  result       TEXT,
  error        TEXT NOT NULL DEFAULT '',
  external_id  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wf_jobs_run ON workflow_jobs(workflow_id, status);

CREATE TABLE IF NOT EXISTS workflow_outbox (
  event_id    TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  dispatched_at REAL,
  attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wf_outbox_pending ON workflow_outbox(dispatched_at);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class WorkflowStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ===================================================================== #
    # 创建
    # ===================================================================== #
    def create(self, *, goal: str, steps: list[StepSpec], task_id: str = "",
               workspace: str = "", deadline_s: float = 0.0,
               plan_version: int = 1) -> Workflow:
        wid = new_id("wf")
        now = time.time()
        deadline = (now + deadline_s) if deadline_s > 0 else 0.0
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_runs(workflow_id, goal, status, task_id, workspace,"
                " created_at, deadline_at, cancel, plan_version, error)"
                " VALUES (?,?,?,?,?,?,?,0,?, '')",
                (wid, goal, WorkflowStatus.PENDING.value, task_id, workspace, now, deadline,
                 plan_version),
            )
            for s in steps:
                self._conn.execute(
                    "INSERT INTO workflow_steps(workflow_id, step_id, title, kind, spec,"
                    " depends, status, attempts, timeout_s, result, error, started_at, ended_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?,NULL,'',0,0)",
                    (wid, s.id, s.title or s.id, s.kind,
                     json.dumps(s.spec, ensure_ascii=False),
                     json.dumps(list(s.depends_on), ensure_ascii=False),
                     StepStatus.PENDING.value, float(s.timeout_s or 0)),
                )
            self._conn.commit()
        return Workflow(id=wid, goal=goal, status=WorkflowStatus.PENDING, task_id=task_id,
                        workspace=workspace, created_at=now, deadline_at=deadline,
                        plan_version=plan_version)

    # ===================================================================== #
    # 读取
    # ===================================================================== #
    def get(self, workflow_id: str) -> WorkflowView | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if not row:
                return None
            srows = self._conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
        wf = Workflow(
            id=row["workflow_id"], goal=row["goal"],
            status=WorkflowStatus(row["status"]), task_id=row["task_id"],
            workspace=row["workspace"] if "workspace" in row.keys() else "",
            created_at=row["created_at"], deadline_at=row["deadline_at"],
            cancel_requested=bool(row["cancel"]), plan_version=row["plan_version"],
            error=row["error"],
        )
        steps = [
            Step(workflow_id=r["workflow_id"], step_id=r["step_id"], title=r["title"],
                 kind=r["kind"], spec=_loads(r["spec"]) or {},
                 depends_on=_loads(r["depends"]) or [], status=StepStatus(r["status"]),
                 attempts=r["attempts"], timeout_s=r["timeout_s"],
                 result=_loads(r["result"]), error=r["error"],
                 started_at=r["started_at"], ended_at=r["ended_at"])
            for r in srows
        ]
        return WorkflowView(workflow=wf, steps=steps)

    def list_unfinished(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT workflow_id FROM workflow_runs WHERE status IN (?,?,?)",
                (WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value,
                 WorkflowStatus.PAUSED.value),
            ).fetchall()
        return [r["workflow_id"] for r in rows]

    # ===================================================================== #
    # 状态更新
    # ===================================================================== #
    def set_workflow_status(self, workflow_id: str, status: WorkflowStatus,
                            *, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_runs SET status = ?, error = COALESCE(NULLIF(?,''), error)"
                " WHERE workflow_id = ?",
                (status.value, error, workflow_id),
            )
            self._conn.commit()

    def request_cancel(self, workflow_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE workflow_runs SET cancel = 1 WHERE workflow_id = ?",
                               (workflow_id,))
            self._conn.commit()

    def cancelled(self, workflow_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        return bool(row["cancel"]) if row else True

    def claim_step(self, workflow_id: str, step_id: str) -> bool:
        """
        原子抢占：只有 pending/ready 能被抢到。

        ★ 这是"重复事件不导致重复执行"的第一道闸：第二次调度 rowcount = 0。
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE workflow_steps SET status = ?, started_at = ?, attempts = attempts + 1"
                " WHERE workflow_id = ? AND step_id = ? AND status IN (?,?)",
                (StepStatus.RUNNING.value, now, workflow_id, step_id,
                 StepStatus.PENDING.value, StepStatus.READY.value),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def finish_step(self, workflow_id: str, step_id: str, status: StepStatus,
                    *, result: dict[str, Any] | None = None, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_steps SET status = ?, result = ?, error = ?, ended_at = ?"
                " WHERE workflow_id = ? AND step_id = ?",
                (status.value, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error, time.time(), workflow_id, step_id),
            )
            self._conn.commit()

    def reset_step(self, workflow_id: str, step_id: str) -> None:
        """重启恢复：把 running 的步骤退回 pending（仅对可安全重跑的场景调用）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_steps SET status = ?, started_at = 0 WHERE workflow_id = ?"
                " AND step_id = ? AND status = ?",
                (StepStatus.PENDING.value, workflow_id, step_id, StepStatus.RUNNING.value),
            )
            self._conn.commit()

    # ===================================================================== #
    # Job
    # ===================================================================== #
    def create_job(self, *, workflow_id: str, step_id: str, kind: str, timeout_s: float,
                   resumable: bool, max_attempts: int = 1) -> Job:
        job = Job(id=new_id("job"), workflow_id=workflow_id, step_id=step_id, kind=kind,
                  timeout_s=timeout_s, resumable=resumable, max_attempts=max_attempts,
                  created_at=time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_jobs(job_id, workflow_id, step_id, kind, status, resumable,"
                " attempts, max_attempts, timeout_s, created_at, started_at, ended_at, result,"
                " error, external_id) VALUES (?,?,?,?,?,?,0,?,?,?,0,0,NULL,'','')",
                (job.id, workflow_id, step_id, kind, JobStatus.PENDING.value,
                 1 if resumable else 0, max_attempts, timeout_s, job.created_at),
            )
            self._conn.commit()
        return job

    def job_started(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_jobs SET status = ?, started_at = ?, attempts = attempts + 1"
                " WHERE job_id = ?",
                (JobStatus.RUNNING.value, time.time(), job_id),
            )
            self._conn.commit()

    def job_finished(self, job_id: str, status: JobStatus, *,
                     result: dict[str, Any] | None = None, error: str = "",
                     external_id: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_jobs SET status = ?, result = ?, error = ?, ended_at = ?,"
                " external_id = COALESCE(NULLIF(?,''), external_id) WHERE job_id = ?",
                (status.value,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error, time.time(), external_id, job_id),
            )
            self._conn.commit()

    def jobs_of(self, workflow_id: str) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_jobs WHERE workflow_id = ? ORDER BY rowid",
                (workflow_id,),
            ).fetchall()
        return [
            Job(id=r["job_id"], workflow_id=r["workflow_id"], step_id=r["step_id"],
                kind=r["kind"], status=JobStatus(r["status"]), resumable=bool(r["resumable"]),
                attempts=r["attempts"], max_attempts=r["max_attempts"],
                timeout_s=r["timeout_s"], created_at=r["created_at"],
                started_at=r["started_at"], ended_at=r["ended_at"],
                result=_loads(r["result"]), error=r["error"], external_id=r["external_id"])
            for r in rows
        ]

    def running_jobs(self, workflow_id: str) -> list[Job]:
        return [j for j in self.jobs_of(workflow_id) if j.status is JobStatus.RUNNING]

    # ===================================================================== #
    # Outbox
    # ===================================================================== #
    def enqueue_event(self, workflow_id: str, kind: str, payload: dict[str, Any]) -> str:
        eid = new_id("wfe")
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_outbox(event_id, workflow_id, kind, payload, created_at,"
                " dispatched_at, attempts) VALUES (?,?,?,?,?,NULL,0)",
                (eid, workflow_id, kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
        return eid

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_outbox WHERE dispatched_at IS NULL"
                " ORDER BY created_at LIMIT ?", (limit,),
            ).fetchall()
        return [
            {"event_id": r["event_id"], "workflow_id": r["workflow_id"], "kind": r["kind"],
             "payload": _loads(r["payload"]) or {}, "attempts": r["attempts"]}
            for r in rows
        ]

    def mark_dispatched(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_outbox SET dispatched_at = ?, attempts = attempts + 1"
                " WHERE event_id = ? AND dispatched_at IS NULL",
                (time.time(), event_id),
            )
            self._conn.commit()

    def event_seen(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT dispatched_at FROM workflow_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
        return bool(row and row["dispatched_at"])


def _loads(text: Any) -> Any:
    if text is None or text == "":
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
