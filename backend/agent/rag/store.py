"""
索引存储：SQLite（普通表 + FTS5 虚表）。

★ 表结构
--------
    rag_files   一个文件一行：workspace + path 唯一，存 sha256 / 大小 / mtime / 片段数
                作用：增量更新的判据 + "哪些文件被删了"
    rag_chunks  一个片段一行：workspace / path / ord / title / 行号 / 正文
                作用：检索命中后取回正文与定位信息
    rag_fts     FTS5 虚表，rowid = rag_chunks.id，只存**分词后的 token 串**
                作用：bm25 关键词检索

★ 为什么 FTS 单独存一份 token 串，而不是对正文建索引
--------------------------------------------------
FTS5 的 unicode61 分词器把**连续中文当成一个词**：
"这个项目的权限审批怎么实现" 会变成一个巨型 token，按子串根本搜不到。
所以入库前先自己做分词（中文二元组 + ASCII 词），把 token 串存进 FTS，
正文留在 rag_chunks 里做展示。代价是索引体积变大（约 1.5~2 倍），
换来的是中文/标识符都能命中，且零新依赖。

★ 增量更新的判据是 **内容哈希**，不是 mtime
------------------------------------------
mtime 会因为 checkout / 复制 / 时区而不靠谱（改了内容但 mtime 没变，
或者 mtime 变了但内容没变）。哈希多读一次文件，换来"到底变没变"的确定答案。
mtime 只作为统计信息保存，不参与判断。

★ 工作区隔离
----------
所有查询都带 `workspace = ?`；不同工作区共用一个库文件但互不可见。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import RagConfig          # 唯一一份配置定义（backend/config.py）
from .chunk import Chunk, chunk_text
from .discover import Discovery, decode_bytes

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{1,63}")
_NUM = re.compile(r"\b\d{1,12}\b")

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_MAX_CHUNKS_PER_FILE = 60


def workspace_key(workspace: str) -> str:
    """
    工作区标识：realpath + normcase（Windows 大小写不敏感）。

    ★ 不用哈希：库里存明文路径更利于排查（"这条是哪来的"），
      长度也不是问题；用 normcase 保证 D:\\Proj 与 d:\\proj 是同一个工作区。
    """
    p = os.path.realpath(workspace)
    return os.path.normcase(os.path.normpath(p)).replace("\\", "/")


def tokenize(text: str) -> list[str]:
    """
    入库/查询共用的分词器（纯规则，可预测、可测试）。

        英文/标识符：连续的 [A-Za-z0-9_.] 词（小写，长度 ≥2）
        数字：整段
        中文：二元组（bigram）—— 单字成词时保留单字

    ★ 为什么要 bigram 而不是单字：单字召回太脏（"的"/"是"命中一切），
      bigram 在中文上是最省事又能用的折中（无需词典、无新依赖）。
    ★ 为什么中英分开：代码里的 `run_command` 必须整体成一个 token，
      不能被拆成中文二元组那套规则。
    """
    out: list[str] = []
    for m in _WORD.finditer(text):
        w = m.group(0).lower()
        if len(w) >= 2:
            out.append(w)
    for m in _NUM.finditer(text):
        out.append(m.group(0))
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def token_text(text: str) -> str:
    return " ".join(tokenize(text))


@dataclass
class Hit:
    """一条检索命中。score 越高越相关（是 -bm25，**不是**语义相似度）。"""

    path: str
    title: str
    start_line: int
    end_line: int
    text: str
    score: float
    ord: int = 0
    # M6：这条命中是关键词、向量还是两路都命中（便于排障与前端展示）
    matched_by: str = ""

    @property
    def locator(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "locator": self.locator,
            "score": round(self.score, 4),
            "chars": len(self.text),
            "matched_by": self.matched_by,
        }


@dataclass
class IndexStats:
    workspace: str = ""
    files: int = 0
    chunks: int = 0
    chars: int = 0
    indexed_at: float = 0.0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    # M6：本轮新算的向量数 / 库里已有的向量总数 / 本次是否降级
    embedded: int = 0
    vectors: int = 0
    degraded: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "files": self.files,
            "chunks": self.chunks,
            "chars": self.chars,
            "indexed_at": round(self.indexed_at, 3),
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unchanged": self.unchanged,
            "skipped": dict(self.skipped),
            "errors": self.errors[:10],
            "truncated": self.truncated,
            "embedded": self.embedded,
            "vectors": self.vectors,
            "degraded": self.degraded,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_files (
  workspace   TEXT NOT NULL,
  path        TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  size        INTEGER NOT NULL DEFAULT 0,
  mtime       REAL NOT NULL DEFAULT 0,
  chunks      INTEGER NOT NULL DEFAULT 0,
  indexed_at  REAL NOT NULL,
  PRIMARY KEY (workspace, path)
);

CREATE TABLE IF NOT EXISTS rag_chunks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace   TEXT NOT NULL,
  path        TEXT NOT NULL,
  ord         INTEGER NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  start_line  INTEGER NOT NULL DEFAULT 1,
  end_line    INTEGER NOT NULL DEFAULT 1,
  text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_ws ON rag_chunks(workspace, path);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(tokens, tokenize='unicode61');

-- M6：向量（float32 BLOB，已归一化 → 点积即余弦）
-- model 记进主键的一部分：换模型/维度时必须整库重算，不能混用
CREATE TABLE IF NOT EXISTS rag_vectors (
  chunk_id   INTEGER PRIMARY KEY,
  workspace  TEXT NOT NULL,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rag_vectors_ws ON rag_vectors(workspace, model);
"""


class RagStore:
    """索引库（进程内复用连接 + 线程锁；同步接口，调用方用 to_thread 包）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 生命周期 ----------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---- 查询统计 ----------------------------------------------------------
    def stats(self, workspace: str) -> IndexStats:
        ws = workspace_key(workspace)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(chunks),0), MAX(indexed_at) "
                "FROM rag_files WHERE workspace = ?", (ws,)
            ).fetchone()
            chunks, chars = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)),0) "
                "FROM rag_chunks WHERE workspace = ?", (ws,)
            ).fetchone()
        return IndexStats(
            workspace=ws, files=int(row[0] or 0), chunks=int(chunks or 0),
            chars=int(chars or 0), indexed_at=float(row[2] or 0.0),
        )

    # ---- 增量同步 ----------------------------------------------------------
    def sync(
        self,
        workspace: str,
        discovery: Discovery,
        *,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_chunks_per_file: int = DEFAULT_MAX_CHUNKS_PER_FILE,
    ) -> IndexStats:
        """
        把发现结果同步进索引：新增/更新/删除，内容没变的文件跳过。

        整个过程一个事务：中途异常 → 回滚，不会留下"文件行更新了但片段没换"的半成品。
        """
        ws = workspace_key(workspace)
        stats = IndexStats(workspace=ws, skipped=discovery.skipped_summary(),
                           truncated=discovery.truncated)
        now = time.time()

        with self._lock:
            existing = {
                path: sha for path, sha in self._conn.execute(
                    "SELECT path, sha256 FROM rag_files WHERE workspace = ?", (ws,))
            }
            current: set[str] = set()
            try:
                for f in discovery.files:
                    current.add(f.path)
                    try:
                        raw = Path(f.abs_path).read_bytes()
                    except OSError as exc:
                        stats.errors.append({"path": f.path, "reason": f"unreadable: {exc}"})
                        self._delete_file(ws, f.path)
                        existing.pop(f.path, None)
                        continue
                    sha = hashlib.sha256(raw).hexdigest()
                    if existing.get(f.path) == sha:
                        stats.unchanged += 1
                        continue
                    text, enc = decode_bytes(raw)
                    chunks = chunk_text(f.path, text, max_chars=chunk_chars)[:max_chunks_per_file]
                    self._replace_file(ws, f.path, sha, f.size, f.mtime, chunks, now)
                    if f.path in existing:
                        stats.updated += 1
                    else:
                        stats.added += 1

                # 删掉"上次有、这次没有"的文件（真的被删了，或被排除规则覆盖了）
                for path in sorted(set(existing) - current):
                    self._delete_file(ws, path)
                    stats.removed += 1
                self._conn.commit()
            except sqlite3.Error as exc:  # noqa: BLE001 —— 回滚后如实上报
                self._conn.rollback()
                stats.errors.append({"path": "*", "reason": f"db_error: {exc}"})

        after = self.stats(workspace)
        stats.files, stats.chunks, stats.chars = after.files, after.chunks, after.chars
        stats.indexed_at = after.indexed_at or now
        return stats

    def _replace_file(self, ws: str, path: str, sha: str, size: int, mtime: float,
                      chunks: list[Chunk], now: float) -> None:
        self._delete_file(ws, path)
        for c in chunks:
            cur = self._conn.execute(
                "INSERT INTO rag_chunks(workspace, path, ord, title, start_line, end_line, text) "
                "VALUES (?,?,?,?,?,?,?)",
                (ws, c.path, c.ord, c.title, c.start_line, c.end_line, c.text),
            )
            self._conn.execute(
                "INSERT INTO rag_fts(rowid, tokens) VALUES (?, ?)",
                (int(cur.lastrowid), token_text(f"{c.title}\n{c.text}")),
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO rag_files(workspace, path, sha256, size, mtime, chunks, indexed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ws, path, sha, size, mtime, len(chunks), now),
        )

    def _delete_file(self, ws: str, path: str) -> None:
        ids = [r[0] for r in self._conn.execute(
            "SELECT id FROM rag_chunks WHERE workspace = ? AND path = ?", (ws, path))]
        if ids:
            self._conn.execute(
                f"DELETE FROM rag_vectors WHERE chunk_id IN ({','.join('?' for _ in ids)})",
                tuple(ids),
            )
        for cid in ids:
            self._conn.execute("DELETE FROM rag_fts WHERE rowid = ?", (int(cid),))
        self._conn.execute("DELETE FROM rag_chunks WHERE workspace = ? AND path = ?", (ws, path))
        self._conn.execute("DELETE FROM rag_files WHERE workspace = ? AND path = ?", (ws, path))

    # ---- 检索 --------------------------------------------------------------
    # ===================================================================== #
    # M6：向量（暴力余弦；语料在几百~几千片段量级，够用且零额外依赖）
    # ===================================================================== #
    def pending_chunks(self, ws: str, model: str, *, limit: int = 512) -> list[tuple[int, str]]:
        """还没有该模型向量的片段（按 id 升序，保证分批稳定）。"""
        rows = self._conn.execute(
            "SELECT c.id, c.text FROM rag_chunks c "
            "LEFT JOIN rag_vectors v ON v.chunk_id = c.id AND v.model = ? "
            "WHERE c.workspace = ? AND v.chunk_id IS NULL ORDER BY c.id LIMIT ?",
            (model, ws, limit),
        ).fetchall()
        return [(int(cid), text or "") for cid, text in rows]

    def save_vectors(self, ws: str, model: str, rows: list[tuple[int, list[float]]]) -> int:
        """
        写入向量（已归一化）。返回写入条数。

        ★ 用 float32 + BLOB：512 维 ≈ 2KB/条，1000 条 ≈ 2MB。
        """
        if not rows:
            return 0
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO rag_vectors"
                "(chunk_id, workspace, model, dim, vec, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (cid, ws, model, len(vec), _pack(vec), now)
                    for cid, vec in rows if vec
                ],
            )
            self._conn.commit()
        return len(rows)

    def vector_count(self, ws: str, model: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM rag_vectors WHERE workspace = ? AND model = ?",
            (ws, model),
        ).fetchone()
        return int(row[0]) if row else 0

    def vector_search(self, ws: str, qvec: list[float], model: str, *,
                      limit: int = 20) -> list[tuple[int, float]]:
        """
        暴力余弦：读出该工作区该模型的全部向量，点积排序。

        ★ 为什么不做 ANN：几百~几千条的规模下，纯 Python 点积 <50ms，
          引入 faiss/chroma 只是多一个依赖和一个需要同步的索引。
        """
        if not qvec:
            return []
        qn = _normalize(qvec)
        rows = self._conn.execute(
            "SELECT chunk_id, vec FROM rag_vectors WHERE workspace = ? AND model = ?",
            (ws, model),
        ).fetchall()
        scored: list[tuple[int, float]] = []
        for cid, blob in rows:
            vec = _unpack(blob)
            if not vec:
                continue
            scored.append((int(cid), _dot(qn, vec)))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return scored[:limit]

    def chunks_by_ids(self, ws: str, ids: list[int]) -> list[tuple[int, Hit]]:
        """按 id 取回片段（保持传入顺序）→ [(id, Hit)]；score 由调用方填。"""
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, path, ord, title, start_line, end_line, text FROM rag_chunks "
                f"WHERE workspace = ? AND id IN ({marks})", (ws, *ids),
            )
            found = {
                int(cid): Hit(path=path, title=title, start_line=int(sl), end_line=int(el),
                              text=text, score=0.0, ord=int(od))
                for cid, path, od, title, sl, el, text in cur
            }
        return [(i, found[i]) for i in ids if i in found]

    def search(self, workspace: str, query: str, *, limit: int = 20) -> list[Hit]:
        """
        关键词检索（FTS5 MATCH + bm25）。

        ★ 多词之间是 **OR**：中文长句会被切成十几个 bigram，用 AND 会把
          "只差一个词"的文档全排除（第一版宁可多召回，再靠条数/预算筛）。
        ★ 排序用 bm25（SQLite 实现，越小越相关），这里取负号变成"越大越好"。
          它衡量的是"词频 + 稀有词权重"，**不是**语义相似度。
        """
        ws = workspace_key(workspace)
        toks = tokenize(query)[:32]
        if not toks:
            return []
        match = " OR ".join(f'"{t}"' for t in toks)
        rows = self._conn.execute(
            "SELECT f.rowid, bm25(rag_fts) AS s FROM rag_fts f "
            "WHERE rag_fts MATCH ? ORDER BY s LIMIT ?",
            (match, max(limit * 6, 30)),
        ).fetchall()
        if not rows:
            return []
        order = {int(rid): float(score) for rid, score in rows}
        ids = list(order)
        placeholders = ",".join("?" for _ in ids)
        hits: list[Hit] = []
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, path, ord, title, start_line, end_line, text FROM rag_chunks "
                f"WHERE workspace = ? AND id IN ({placeholders})", (ws, *ids),
            )
            for cid, path, ordinal, title, s, e, text in cur:
                hits.append(Hit(path=path, title=title, start_line=int(s), end_line=int(e),
                                text=text, score=-order[int(cid)], ord=int(ordinal)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]


# --------------------------------------------------------------------------- #
# M6：向量编解码（float32 BLOB；归一化后点积 = 余弦）
# --------------------------------------------------------------------------- #
def _normalize(vec: list[float]) -> list[float]:
    norm = 0.0
    for v in vec:
        norm += v * v
    if norm <= 0:
        return []
    inv = 1.0 / (norm ** 0.5)
    return [v * inv for v in vec]


def _pack(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"<{len(vec)}f", *_normalize(vec))


def _unpack(blob: bytes) -> list[float]:
    import struct

    n = len(blob) // 4
    if n <= 0:
        return []
    return list(struct.unpack(f"<{n}f", blob))


def _dot(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
