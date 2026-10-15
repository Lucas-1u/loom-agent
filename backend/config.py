"""
Loom Agent v0.1 · 配置

集中所有可调参数。优先级：环境变量 > .env 文件 > 这里的默认值。

★ 为什么不用 pydantic-settings 的 BaseSettings 自动读环境变量？
  可用，但会让「配置从哪来」变得不透明（对学习者不友好）。
  这里显式列出每个字段 + 显式写明环境变量名，读代码时就看得见全貌。
  等配置项超过 30 个再考虑换。（可理解性 > 代码量）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
# backend/config.py → backend/ → loom/ → Loom_agent/
BACKEND_DIR = Path(__file__).resolve().parent
LOOM_DIR = BACKEND_DIR.parent
PROJECT_ROOT = LOOM_DIR.parent
DOCS_DIR = PROJECT_ROOT / "docs"


def load_dotenv_if_present() -> None:
    """
    加载 loom/.env（若存在）。

    为什么手写而不直接 import dotenv 就在模块顶层调用？
      → 因为要让 import 顺序可控：先 load_dotenv，再读 os.environ。
        而且 python-dotenv 缺失时也不该让整个程序崩掉（降级为只读环境变量）。
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        env_file = LOOM_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
    except ImportError:
        pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# 服务端点
# --------------------------------------------------------------------------- #
@dataclass
class ServerConfig:
    """本地 HTTP 服务（P5 之后用；v0.1a CLI 阶段也会起，方便边跑边看）"""

    host: str = "127.0.0.1"   # ★ 绝不改成 0.0.0.0 —— 这是本机 Agent，不该对外暴露
    port: int = 8765

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            host=_env("LOOM_HOST", "127.0.0.1"),
            port=_env_int("LOOM_PORT", 8765),
        )


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
@dataclass
class ModelConfig:
    """
    ModelAdapter 的配置。

    ★ 关键设计：一个 Adapter 实现服务两个供应商。
      百炼与 DeepSeek 都是 OpenAI 兼容接口，唯一区别是 base_url / api_key / model 名。
      所以不需要写两套代码 —— 见 models/openai_compat.py。
    """

    provider: str = "bailian"     # bailian | deepseek
    model: str = "qwen3.7-plus"
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2      # Agent 任务要稳定，低温
    timeout_s: float = 35.0       # 百炼 strict JSON 实测 14.6s，留足余量
    max_retries: int = 2

    # ---- 供应商预设 --------------------------------------------------------
    PRESETS: dict[str, dict[str, str]] = field(
        default_factory=lambda: {
            "bailian": {
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3.7-plus",
                "key_env": "BAILIAN_API_KEY",
            },
            "deepseek": {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "key_env": "DEEPSEEK_API_KEY",
            },
        },
        repr=False,
    )

    @classmethod
    def from_env(cls, provider: str = "") -> "ModelConfig":
        llm = cls()
        presets = {
            "bailian": (
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "qwen3.7-plus",
                "BAILIAN_API_KEY",
            ),
            "deepseek": (
                "https://api.deepseek.com/v1",
                "deepseek-chat",
                "DEEPSEEK_API_KEY",
            ),
        }
        prov = (provider or _env("LOOM_DEFAULT_MODEL", "bailian")).lower()
        if prov not in presets:
            prov = "bailian"
        base_url, model, key_env = presets[prov]
        return cls(
            provider=prov,
            model=_env("LOOM_MODEL", model),
            base_url=_env("LOOM_BASE_URL", base_url),
            api_key=_env(key_env, ""),
            temperature=_env_float("LOOM_TEMPERATURE", 0.2),
            timeout_s=_env_float("LOOM_TIMEOUT", 35.0),
            max_retries=_env_int("LOOM_MAX_RETRIES", 2),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


# --------------------------------------------------------------------------- #
# 预算（与 state.Budgets 的默认值同源）
# --------------------------------------------------------------------------- #
@dataclass
class BudgetConfig:
    max_turns: int = 20
    max_seconds: float = 300.0
    max_tool_calls: int = 40
    max_same_call_repeat: int = 3
    max_errors: int = 5
    approval_timeout_s: float = 180.0
    min_evidence: int = 1

    @classmethod
    def from_env(cls) -> "BudgetConfig":
        return cls(
            max_turns=_env_int("LOOM_MAX_TURNS", 20),
            max_seconds=_env_float("LOOM_MAX_SECONDS", 300.0),
            max_tool_calls=_env_int("LOOM_MAX_TOOL_CALLS", 40),
            max_same_call_repeat=_env_int("LOOM_MAX_SAME_CALL_REPEAT", 3),
            max_errors=_env_int("LOOM_MAX_ERRORS", 5),
            approval_timeout_s=_env_float("LOOM_APPROVAL_TIMEOUT", 180.0),
            min_evidence=_env_int("LOOM_MIN_EVIDENCE", 1),
        )


# --------------------------------------------------------------------------- #
# 工具 / 安全
# --------------------------------------------------------------------------- #
# 敏感文件名（审批时高亮警示；v0.1 仍允许（需批准），未来可升级为硬拒绝）
SENSITIVE_NAMES: set[str] = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
}

# 敏感目录（在其中的文件一律视为敏感）
SENSITIVE_DIR_PARTS: set[str] = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".git",          # .git 内部文件不该被 Agent 随手改
}

# 读取时直接跳过的大目录 / 重目录（list_files 用）
IGNORED_DIR_NAMES: set[str] = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

# 单文件读取上限（字符）。超过则截断 —— 防止一个巨大文件吃掉全部上下文预算
MAX_READ_CHARS: int = 40_000

# 单次工具结果写入上下文的上限（字符）
MAX_TOOL_RESULT_CHARS: int = 8_000

# 覆盖写入前的备份目录（相对于工作区根）
BACKUP_DIR_NAME: str = ".loom_backup"


# --------------------------------------------------------------------------- #
# 总配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig.from_env)
    model: ModelConfig = field(default_factory=ModelConfig.from_env)
    budgets: BudgetConfig = field(default_factory=BudgetConfig.from_env)
    log_level: str = "INFO"

    @classmethod
    def load(cls, provider: str = "") -> "Config":
        load_dotenv_if_present()
        return cls(
            server=ServerConfig.from_env(),
            model=ModelConfig.from_env(provider),
            budgets=BudgetConfig.from_env(),
            log_level=_env("LOOM_LOG_LEVEL", "INFO"),
        )


# --------------------------------------------------------------------------- #
# 日志（统一入口，避免各处 logging.basicConfig 互相打架）
# --------------------------------------------------------------------------- #
_configured = False


def setup_logging(level: str = "INFO") -> None:
    import logging

    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
