"""
本地 ONNX embedding（M6 · 混合检索）。

★ 为什么是"可选、默认关"
------------------
Loom 的定位是离线优先：默认只有 FTS5 关键词检索，不下载模型、不联网。
`LOOM_RAG_EMBED=onnx` 才启用本地向量；模型权重放在项目内的 `sandbox/models`，
不进仓库（`.gitignore` 已忽略 sandbox/），换机器需要重新下载。

★ 为什么不走云端 embedding
------------------
云端要联网、要把工作区文本发出去——对一个"本地代码 Agent"来说代价太高，
而且离线时整条检索会退化。这里只做本地 ONNX（fastembed）。

★ 失败时的语义
------------
`encode()` 失败一律抛 `EmbedUnavailable`（带原因），由 `service.py` 决定
"降级成关键词 + 在结果里标注 degraded"，**不允许**静默返回空列表——
否则用户会以为"没有相关资料"，而真相是"向量这条路挂了"。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("loom.rag.embed")

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class EmbedUnavailable(RuntimeError):
    """embedding 不可用（未安装 / 未启用 / 模型加载失败 / 编码失败）。"""


class Embedder:
    """embedding 的统一接口（同步；调用方在线程里跑）。"""

    name: str = "null"
    model: str = ""
    _dim: int = 0

    @property
    def available(self) -> bool:
        return False

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        raise EmbedUnavailable("embedding 未启用")


class NullEmbedder(Embedder):
    """未启用：调用即报错，由上层降级。"""

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    def __str__(self) -> str:
        return f"NullEmbedder({self.reason})"


class OnnxEmbedder(Embedder):
    """
    fastembed（ONNX Runtime）本地模型。

    ★ 线程安全：fastembed 的 session 不保证并发安全，这里用一把锁串行化。
      索引是批量的、查询是单条的，锁不会成为瓶颈。
    """

    name = "onnx"

    def __init__(self, model_name: str, *, cache_dir: str = "", threads: int = 0) -> None:
        self.model = model_name
        self._cache_dir = cache_dir
        self._threads = threads
        self._lock = threading.Lock()
        self._model: Any = None
        self._load_error = ""

    # ---- 懒加载 ------------------------------------------------------------
    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding  # 延迟导入：没装也能跑关键词检索
        except Exception as exc:  # noqa: BLE001
            raise EmbedUnavailable(f"fastembed 未安装：{type(exc).__name__}") from exc

        opts: dict[str, Any] = {"model_name": self.model}
        if self._cache_dir:
            opts["cache_dir"] = self._cache_dir
        if self._threads:
            opts["threads"] = self._threads
        started = time.time()
        try:
            self._model = TextEmbedding(**opts)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"{type(exc).__name__}: {exc}"
            raise EmbedUnavailable(f"加载模型失败（{self.model}）：{self._load_error}") from exc
        log.info("embedding 模型已加载：%s（%.1fs）", self.model, time.time() - started)
        return self._model

    @property
    def available(self) -> bool:
        return not self._load_error

    # ---- 编码 --------------------------------------------------------------
    def encode(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        texts = [t for t in (texts or []) if isinstance(t, str) and t.strip()]
        if not texts:
            return []
        model = self._ensure_model()
        with self._lock:
            try:
                gen = model.query_embed(texts) if is_query else model.embed(texts)
                vecs = [v.tolist() if hasattr(v, "tolist") else list(v) for v in gen]
            except Exception as exc:  # noqa: BLE001
                raise EmbedUnavailable(f"编码失败：{type(exc).__name__}: {exc}") from exc
        if vecs and not self._dim:
            self._dim = len(vecs[0])
        # ★ 归一化在调用方做（余弦 = 点积），这里只保证维度一致
        return vecs


_EMBEDDERS: dict[tuple[str, str, str], Embedder] = {}
_LOCK = threading.Lock()


def get_embedder(*, provider: str = "off", model: str = "", cache_dir: str = "") -> Embedder:
    """按 (provider, model, cache_dir) 复用实例（模型加载很贵，只做一次）。"""
    provider = (provider or "off").strip().lower()
    if provider in ("", "off", "none", "0", "false"):
        return NullEmbedder("disabled")
    if provider != "onnx":
        return NullEmbedder(f"unknown_provider:{provider}")

    model = (model or DEFAULT_MODEL).strip()
    cache_dir = cache_dir or default_cache_dir()
    key = (provider, model, cache_dir)
    with _LOCK:
        inst = _EMBEDDERS.get(key)
        if inst is None:
            inst = OnnxEmbedder(model, cache_dir=cache_dir)
            _EMBEDDERS[key] = inst
        return inst


def default_cache_dir() -> str:
    """
    模型缓存目录：默认放项目内 `sandbox/models/fastembed`。

    ★ 为什么不用系统临时目录：Windows 的 Temp 会被清理，模型 100MB+，
      清掉之后下一次检索要重下（实测下载 4 分钟）。
    """
    env = os.environ.get("LOOM_RAG_EMBED_CACHE", "").strip()
    if env:
        return env
    root = Path(__file__).resolve().parents[3]   # loom/
    p = root / "sandbox" / "models" / "fastembed"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)
