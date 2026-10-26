"""
文件发现：决定"哪些文件该进项目知识库"。

三条边界（顺序不可调换，越靠前越便宜）：

    ① **目录级排除** —— 先剪枝，别等 os.walk 把 node_modules 走一遍
    ② **文件名级排除** —— 密钥/凭据类文件根本不该进索引（不是"进了再过滤"）
    ③ **内容级检查** —— 二进制、超大、读不出来 → 记录原因并跳过

★ 为什么排除规则要写在这里而不是散在调用方
--------------------------------------------
索引是**长期存在**的东西：今天漏排一个 `.env`，它会一直躺在库里被检索到。
把"什么不该进"收敛到一个模块，测试只需要盯这一处。

★ 为什么不用 glob 而不是 os.walk
----------------------------
需要在遍历时剪枝（dirs[:] = ...），glob 做不到；工作区再小也不该无脑递归。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# 目录名级排除（出现在路径任意一段都跳过）
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    ".venv", "venv", "env", ".env.d",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
    "coverage", "htmlcov", ".coverage",
    ".idea", ".vscode", ".vs",
    "site-packages", ".eggs", "*.egg-info",
    # Loom 自己的运行期产物：索引库、任务库、备份
    ".loom_backup", "index",
})

# 文件名/后缀级排除（敏感或明显无文本价值）
EXCLUDED_FILE_NAMES: frozenset[str] = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
})
EXCLUDED_SUFFIXES: frozenset[str] = frozenset({
    ".lock", ".min.js", ".min.css", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svgz",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".whl", ".exe", ".dll", ".so", ".dylib",
    ".db", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".class", ".jar", ".bin",
    ".key", ".pem", ".p12", ".pfx", ".crt", ".cer",
    ".mp3", ".mp4", ".wav", ".mov", ".avi",
})

# 敏感文件名（与 config.SENSITIVE_NAMES 同源，这里保留一份最小集做兜底）
_SENSITIVE_NAMES: frozenset[str] = frozenset({
    ".env", ".env.local", ".env.production", ".env.development",
    "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd",
})
_SENSITIVE_PREFIXES: tuple[str, ...] = (".env.", "secrets.", "credentials.")

# 可索引的文本类型（第一版范围）。不在表里的文件不进索引 ——
# 宁可少索引，也不要让二进制/未知格式混进来污染检索结果。
INDEXABLE_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".pyi",
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".sh", ".bat", ".ps1",
})


@dataclass
class DiscoveredFile:
    """一个待索引文件（还没读内容）。"""

    path: str            # 相对工作区、统一用 / 分隔
    abs_path: str
    size: int
    mtime: float
    suffix: str


@dataclass
class SkippedFile:
    path: str
    reason: str          # too_large / binary / sensitive / unreadable / unsupported


@dataclass
class Discovery:
    files: list[DiscoveredFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    truncated: bool = False          # 文件数超过上限被截断

    def skipped_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            out[s.reason] = out.get(s.reason, 0) + 1
        return out


def is_sensitive_name(name: str) -> bool:
    """文件名是否敏感（不允许进索引）。"""
    low = name.lower()
    if low in _SENSITIVE_NAMES:
        return True
    if any(low.startswith(p) for p in _SENSITIVE_PREFIXES):
        return True
    return False


def is_indexable(path: Path) -> bool:
    """后缀是否在可索引范围内（大小写不敏感）。"""
    return path.suffix.lower() in INDEXABLE_SUFFIXES


def looks_binary(head: bytes) -> bool:
    """粗略二进制判定：出现 NUL 就认为不是文本。

    ★ 为什么只看 NUL：文本文件（含 UTF-8 中文）不会出现 0x00；
      而常见的二进制格式在前 4KB 内几乎必有 NUL。比"按后缀猜"可靠。
    """
    return b"\x00" in head


def discover(
    workspace: str,
    *,
    max_files: int = 400,
    max_file_bytes: int = 262_144,
    extra_excludes: Iterable[str] = (),
) -> Discovery:
    """
    遍历工作区，产出可索引文件与跳过原因。

    参数 max_files / max_file_bytes 是**硬上限**：宁可只索引一部分，
    也不能让一次任务启动扫描几十万文件或读一个 200MB 的日志。
    """
    root = Path(workspace).resolve()
    extra = {e.replace("\\", "/").strip("/").lower() for e in extra_excludes if e.strip()}
    out = Discovery()

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir

        # ---- ① 目录剪枝（含隐藏目录；但工作区根本身不剪）--------------
        kept: list[str] = []
        for d in dirnames:
            low = d.lower()
            if low in EXCLUDED_DIR_NAMES or low.startswith("."):
                continue
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if rel.lower() in extra:
                continue
            kept.append(d)
        dirnames[:] = sorted(kept)

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel.lower() in extra:
                continue
            # ---- ② 文件名级排除 -------------------------------------
            if name.lower() in EXCLUDED_FILE_NAMES:
                out.skipped.append(SkippedFile(rel, "excluded_name"))
                continue
            if is_sensitive_name(name):
                out.skipped.append(SkippedFile(rel, "sensitive"))
                continue
            if Path(name).suffix.lower() in EXCLUDED_SUFFIXES:
                out.skipped.append(SkippedFile(rel, "unsupported"))
                continue
            if not is_indexable(Path(name)):
                out.skipped.append(SkippedFile(rel, "unsupported"))
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                out.skipped.append(SkippedFile(rel, "unreadable"))
                continue
            # ---- ③ 内容级检查（先看大小，再试读 4KB 判二进制）---------
            if st.st_size > max_file_bytes:
                out.skipped.append(SkippedFile(rel, "too_large"))
                continue
            if st.st_size == 0:
                out.skipped.append(SkippedFile(rel, "empty"))
                continue
            try:
                with open(os.path.join(dirpath, name), "rb") as f:
                    head = f.read(4096)
            except OSError:
                out.skipped.append(SkippedFile(rel, "unreadable"))
                continue
            if looks_binary(head):
                out.skipped.append(SkippedFile(rel, "binary"))
                continue

            if len(out.files) >= max_files:
                out.truncated = True
                return out
            out.files.append(DiscoveredFile(
                path=rel, abs_path=os.path.join(dirpath, name),
                size=st.st_size, mtime=st.st_mtime, suffix=Path(name).suffix.lower(),
            ))
    return out


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """
    把原始字节解码成文本，返回 (内容, 实际编码)。

    ★ 编码不能猜单一值：项目里 UTF-8 / GBK 混排很常见（Windows）。
      顺序：UTF-8（含 BOM）→ GB18030（GBK 的超集）→ latin-1（最后兜底，绝不抛）。
      latin-1 兜底的意义是"宁可索引到乱码，也不要让一个坏文件搞崩整次索引"。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def read_text(path: str) -> tuple[str, str]:
    """读文件并解码（返回内容与实际编码）。"""
    return decode_bytes(Path(path).read_bytes())
