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
    # ★ 有序候选模型列表（第四批后的用户要求）：
    #   同一个 API Key，按顺序用；某个模型没额度/不可用就换下一个。
    #   models[0] 才是真正的主模型（model 字段与之保持一致）。
    models: list[str] = field(default_factory=list)
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
            # 讯飞 MaaS（2026-10-26 切换）：/v2/chat/completions 是标准 OpenAI 形状
            "xfyun": {
                "base_url": "https://maas-api.cn-huabei-1.xf-yun.com/v2",
                "model": "spark-x2.6",
                "key_env": "XFYUN_API_KEY",
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
            "xfyun": (
                "https://maas-api.cn-huabei-1.xf-yun.com/v2",
                "spark-x2.6",
                "XFYUN_API_KEY",
            ),
        }
        prov = (provider or _env("LOOM_DEFAULT_MODEL", "bailian")).lower()
        if prov not in presets:
            prov = "bailian"
        base_url, model, key_env = presets[prov]
        primary = _env("LOOM_MODEL", model)
        # LOOM_MODELS：逗号/空格分隔的有序候选列表；第一个是主模型
        raw = _env("LOOM_MODELS", "")
        models = [m.strip() for m in raw.replace(",", " ").split() if m.strip()]
        if not models:
            models = [primary]
        elif primary and primary not in models:
            models = [primary] + models
        return cls(
            provider=prov,
            model=models[0],
            models=models,
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

    # ---- 上下文压缩（第五批）-------------------------------------------
    # ★ 阈值用**字符数**表达：没有可靠 tokenizer，不假装精确 token 数。
    #   60k 字符 ≈ 40k token（按 1.5 字符/token 估算）。
    #   对 1M 上下文的模型（如星火 spark-x2.6）可调大，如 200_000。
    compact_max_chars: int = 60_000
    compact_keep_turns: int = 3
    # 单轮字符数超过此值就提前压一次（长输出场景，防止下一次请求撞墙）
    compact_long_turn_chars: int = 20_000

    # ---- 编排引擎（M1 / M4.3）-----------------------------------------
    # graph = LangGraph 引擎（M2 已实现，M4.3 起为**默认**）；
    # legacy = 原有 _loop 主循环，保留为可显式切换的兜底路径（暂不删除）。
    # ★ 未知值必须报错，绝不静默切到别的引擎。
    orchestrator: str = "graph"

    @classmethod
    def from_env(cls) -> "BudgetConfig":
        return cls(
            orchestrator=validate_orchestrator(_env("LOOM_ORCHESTRATOR", "graph")),
            compact_max_chars=_env_int("LOOM_COMPACT_CHARS", 60_000),
            compact_keep_turns=_env_int("LOOM_COMPACT_KEEP_TURNS", 3),
            compact_long_turn_chars=_env_int("LOOM_COMPACT_LONG_TURN", 20_000),
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
# Spill 溢出存储（对齐 DeepSeek Harness dsh-spill-policy）
# --------------------------------------------------------------------------- #

#: 工具结果超过此字符数 → 触发 spill（完整存盘 + head/tail 保留）
SPILL_MAX_INLINE_CHARS: int = 12_000

#: spill 存储根目录（相对于工作区根）
SPILL_DIR_NAME: str = ".loom_spill"

#: 是否启用 spill（默认开；关闭则退化为旧行为：纯截断）
SPILL_ENABLED: bool = True

#: 工具结果修剪器：超过此字符数的历史 tool 消息 → 修剪中间部分
PRUNE_THRESHOLD_CHARS: int = 8_192
#: 修剪时保留开头字符数
PRUNE_HEAD_CHARS: int = 4_096
#: 修剪时保留结尾字符数
PRUNE_TAIL_CHARS: int = 1_024


# --------------------------------------------------------------------------- #
# 命令执行沙箱（Docker）
# --------------------------------------------------------------------------- #
@dataclass
class SandboxConfig:
    """
    沙箱容器的限制条件。

    ★ 这些值决定「Agent 的任意命令最多能伤到哪儿」。
      模型无权修改其中任何一项 —— 它只能给 command / args / cwd / timeout。
    ★ network 刻意**不开放环境变量**：想联网必须改代码，
      这样"某次调试临时开个网"不会悄悄变成默认行为。
    """

    image: str = "loom-sandbox:py312"     # 由 dev/Dockerfile.sandbox 构建；也可填 python@sha256:...
    docker_bin: str = "docker"
    enabled: bool = True                  # False = 直接拒绝执行（不是改在宿主机跑）

    network: str = "none"
    user: str = "1000:1000"               # 非 root
    memory: str = "512m"
    cpus: str = "1.0"
    pids_limit: int = 128
    tmpfs_size: str = "64m"
    workspace_mount: str = "/workspace"   # 工作区在容器内的固定挂载点

    default_timeout_s: int = 60
    max_timeout_s: int = 300
    max_output_bytes: int = 16_384        # 单条流（stdout / stderr 各自）上限

    @classmethod
    def from_env(cls) -> "SandboxConfig":
        return cls(
            image=_env("LOOM_SANDBOX_IMAGE", "loom-sandbox:py312"),
            docker_bin=_env("LOOM_DOCKER_BIN", "docker") or "docker",
            enabled=_env("LOOM_SANDBOX_ENABLED", "1").lower() not in ("0", "false", "off", "no"),
            memory=_env("LOOM_SANDBOX_MEMORY", "512m"),
            cpus=_env("LOOM_SANDBOX_CPUS", "1.0"),
            pids_limit=_env_int("LOOM_SANDBOX_PIDS", 128),
            tmpfs_size=_env("LOOM_SANDBOX_TMPFS", "64m"),
            default_timeout_s=_env_int("LOOM_SANDBOX_TIMEOUT", 60),
            max_timeout_s=_env_int("LOOM_SANDBOX_MAX_TIMEOUT", 300),
            max_output_bytes=_env_int("LOOM_SANDBOX_MAX_OUTPUT", 16_384),
        )


# --------------------------------------------------------------------------- #
# 项目知识检索（M3）
# --------------------------------------------------------------------------- #
def default_index_path() -> str:
    """索引库默认位置：`sandbox/index/knowledge.db`（运行期产物，不进版本库）。"""
    return str(PROJECT_ROOT / "sandbox" / "index" / "knowledge.db")


@dataclass
class RagConfig:
    """
    检索配置。

    ★ enabled 与 db_path 是两件事：
      · enabled=False       → 整个能力关闭（不建库、不注册工具、不预检索）
      · db_path=""          → 视为不可用（多为测试/只读场景显式关掉）
    这两条都要能被环境变量控制，否则"临时关掉检索"就得改代码。

    ★ auto_prefetch 控制"任务开始时是否自动检索用户目标"：
      关掉后只剩 search_docs 工具（由模型按需调用）——这是 A/B 对比的开关。
    """

    enabled: bool = True
    db_path: str = field(default_factory=default_index_path)
    auto_prefetch: bool = True
    top_k: int = 4
    max_chars_per_hit: int = 700
    max_context_chars: int = 2_600
    chunk_chars: int = 1_200
    max_chunks_per_file: int = 60
    max_file_bytes: int = 262_144
    max_files: int = 400

    # ---- 混合检索（M6）--------------------------------------------------
    # embed: off = 只用 FTS5 关键词；onnx = 本地 fastembed（需装 fastembed）
    # retrieval: 默认检索模式（keyword | hybrid | semantic），工具可覆盖
    embed: str = "off"
    embed_model: str = "BAAI/bge-small-zh-v1.5"
    embed_cache: str = ""
    embed_batch: int = 32
    retrieval: str = "hybrid"
    hybrid_candidates: int = 20      # 每一路各取多少条候选再融合
    rrf_k: int = 60                  # RRF 的平滑常数（业界常用 60）

    @property
    def vectors_enabled(self) -> bool:
        return self.embed not in ("", "off", "none", "0", "false")

    @property
    def usable(self) -> bool:
        """是否真的可用：既要启用，也要有库路径（空路径视为关闭）。"""
        return self.enabled and bool(self.db_path)

    @classmethod
    def from_env(cls) -> "RagConfig":
        return cls(
            enabled=_env("LOOM_RAG", "1").lower() not in ("0", "false", "off", "no"),
            db_path=_env("LOOM_RAG_DB", default_index_path()),
            auto_prefetch=_env("LOOM_RAG_AUTO", "1").lower() not in ("0", "false", "off", "no"),
            top_k=max(1, _env_int("LOOM_RAG_TOP_K", 4)),
            max_chars_per_hit=max(120, _env_int("LOOM_RAG_HIT_CHARS", 700)),
            max_context_chars=max(300, _env_int("LOOM_RAG_CONTEXT_CHARS", 2_600)),
            chunk_chars=max(300, _env_int("LOOM_RAG_CHUNK_CHARS", 1_200)),
            max_chunks_per_file=max(1, _env_int("LOOM_RAG_CHUNKS_PER_FILE", 60)),
            max_file_bytes=max(4096, _env_int("LOOM_RAG_MAX_FILE_KB", 256) * 1024),
            max_files=max(1, _env_int("LOOM_RAG_MAX_FILES", 400)),
            embed=_env("LOOM_RAG_EMBED", "off").strip().lower(),
            embed_model=_env("LOOM_RAG_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"),
            embed_cache=_env("LOOM_RAG_EMBED_CACHE", ""),
            embed_batch=max(1, _env_int("LOOM_RAG_EMBED_BATCH", 32)),
            retrieval=_env("LOOM_RAG_MODE", "hybrid").strip().lower(),
            hybrid_candidates=max(4, _env_int("LOOM_RAG_CANDIDATES", 20)),
            rrf_k=max(1, _env_int("LOOM_RAG_RRF_K", 60)),
        )


@dataclass
class MultiAgentConfig:
    """
    父子任务（M5）配置。

    ★ 为什么要单独收口：层级、数量、子任务预算、超时都是**安全边界**，
      散落在代码里迟早会有的地方忘检查。集中在这里，测试只需盯一处。
    """

    enabled: bool = True
    max_depth: int = 2              # 允许派生的层级（父=0，子=1；子任务再派生需 depth<max_depth）
    max_children: int = 3           # 每个父任务最多派生几个子任务
    child_max_turns: int = 8
    child_max_seconds: float = 120.0
    child_max_tool_calls: int = 12
    approval_timeout_s: float = 60.0    # 子任务里等待人工审批的超时（超时=拒绝）
    timeout_s: float = 180.0            # 单次派生的总超时（父任务不会被无限拖住）

    @classmethod
    def from_env(cls) -> "MultiAgentConfig":
        return cls(
            enabled=_env("LOOM_AGENTS", "1").lower() not in ("0", "false", "off", "no"),
            max_depth=max(1, _env_int("LOOM_AGENT_MAX_DEPTH", 2)),
            max_children=max(1, _env_int("LOOM_AGENT_MAX_CHILDREN", 3)),
            child_max_turns=max(1, _env_int("LOOM_AGENT_CHILD_TURNS", 8)),
            child_max_seconds=_env_float("LOOM_AGENT_CHILD_SECONDS", 180.0),
            child_max_tool_calls=max(1, _env_int("LOOM_AGENT_CHILD_TOOLS", 12)),
            approval_timeout_s=_env_float("LOOM_AGENT_APPROVAL_TIMEOUT", 60.0),
            timeout_s=_env_float("LOOM_AGENT_TIMEOUT", 240.0),
        )


# --------------------------------------------------------------------------- #
# Spill 溢出存储（对齐 DeepSeek Harness dsh-spill-policy）
# --------------------------------------------------------------------------- #
@dataclass
class SpillConfig:
    """
    Spill 溢出存储配置。

    当工具结果超过 max_inline_chars 时，完整内容存盘，上下文只保留 head + tail。
    关闭时退化为旧行为（纯截断）。

    ★ 和 DeepSeek Harness 的关系：本配置对应 dsh-spill-policy 的 maxInlineTokens
      + dsh-compaction-tool-result-pruner 的 thresholdChars / headChars / tailChars。
    """

    enabled: bool = True
    max_inline_chars: int = SPILL_MAX_INLINE_CHARS
    dir_name: str = SPILL_DIR_NAME
    # 工具结果修剪器（compaction 时修剪历史 tool 消息）
    prune_threshold_chars: int = PRUNE_THRESHOLD_CHARS
    prune_head_chars: int = PRUNE_HEAD_CHARS
    prune_tail_chars: int = PRUNE_TAIL_CHARS

    @classmethod
    def from_env(cls) -> "SpillConfig":
        return cls(
            enabled=_env("LOOM_SPILL", "1").lower() not in ("0", "false", "off", "no"),
            max_inline_chars=max(1000, _env_int("LOOM_SPILL_MAX_INLINE", SPILL_MAX_INLINE_CHARS)),
            dir_name=_env("LOOM_SPILL_DIR", SPILL_DIR_NAME) or SPILL_DIR_NAME,
            prune_threshold_chars=max(1024, _env_int("LOOM_PRUNE_THRESHOLD", PRUNE_THRESHOLD_CHARS)),
            prune_head_chars=max(0, _env_int("LOOM_PRUNE_HEAD", PRUNE_HEAD_CHARS)),
            prune_tail_chars=max(0, _env_int("LOOM_PRUNE_TAIL", PRUNE_TAIL_CHARS)),
        )


# Checkpoint 目录名（对齐 DeepSeek Harness dsh-session-checkpoint-policy）
CHECKPOINT_DIR_NAME: str = ".loom_checkpoints"


@dataclass
class CheckpointConfig:
    """
    Checkpoint 跨进程恢复配置（P0，对齐 DeepSeek Harness）。

    在关键执行点（模型调用前 / 工具执行前 / 每轮开始前）保存 TaskState 快照
    到本地磁盘，进程崩溃后可从最近 checkpoint 恢复。

    ★ 和 DeepSeek Harness 的关系：本配置对应 dsh-session-checkpoint-policy 的
      checkpoint 频率策略。Loom 选择**可用性优先**（fail_closed=False）：
      checkpoint 保存失败只记日志，不阻止下游执行。

    ★ 与 Spill 的区别：Spill 是"工具结果太大→存盘+截断"（省上下文），
      Checkpoint 是"整份 TaskState→存盘"（防崩溃丢状态）。两者独立。
    """

    enabled: bool = True
    # 三个 checkpoint 点（对应 DSH 的 llm/stream、tools/execute、agent/pre-step）
    before_model_call: bool = True
    before_tool_exec: bool = True
    at_turn_start: bool = True
    # 每个任务最多保留多少个历史 checkpoint（超出按 seq 裁剪最旧的）
    max_history: int = 20
    # True = 保存失败阻止下游执行（DSH 默认）；False = 只记日志（Loom 默认）
    fail_closed: bool = False
    dir_name: str = CHECKPOINT_DIR_NAME

    @classmethod
    def from_env(cls) -> "CheckpointConfig":
        return cls(
            enabled=_env("LOOM_CHECKPOINT", "1").lower() not in ("0", "false", "off", "no"),
            before_model_call=_env("LOOM_CKPT_MODEL", "1").lower() not in ("0", "false", "off", "no"),
            before_tool_exec=_env("LOOM_CKPT_TOOL", "1").lower() not in ("0", "false", "off", "no"),
            at_turn_start=_env("LOOM_CKPT_TURN", "1").lower() not in ("0", "false", "off", "no"),
            max_history=max(1, _env_int("LOOM_CKPT_MAX_HISTORY", 20)),
            fail_closed=_env("LOOM_CKPT_FAIL_CLOSED", "0").lower() in ("1", "true", "on", "yes"),
            dir_name=_env("LOOM_CKPT_DIR", CHECKPOINT_DIR_NAME) or CHECKPOINT_DIR_NAME,
        )


def _load_mcp_config():
    """懒加载 MCP 配置（LOOM_MCP=0 时返回 None，不读文件）。"""
    from .mcp.config import McpConfig
    cfg = McpConfig.from_env()
    return cfg if cfg.enabled else None


# --------------------------------------------------------------------------- #
# 总配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig.from_env)
    model: ModelConfig = field(default_factory=ModelConfig.from_env)
    budgets: BudgetConfig = field(default_factory=BudgetConfig.from_env)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig.from_env)
    rag: RagConfig = field(default_factory=RagConfig.from_env)
    # MCP（M4.1）：类型是 backend.mcp.config.McpConfig；这里用 Any + 懒加载，
    # 避免 backend.config ↔ backend.mcp.config 的循环导入。
    mcp: Any = None
    # M5：父子任务边界（层级/数量/子任务预算/超时）
    multiagent: MultiAgentConfig = field(default_factory=MultiAgentConfig.from_env)
    # Spill：溢出存储 + 工具结果修剪（对齐 DeepSeek Harness）
    spill: SpillConfig = field(default_factory=SpillConfig.from_env)
    # Checkpoint：跨进程恢复快照（P0，对齐 DeepSeek Harness dsh-session-checkpoint-policy）
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig.from_env)
    log_level: str = "INFO"

    @classmethod
    def load(cls, provider: str = "") -> "Config":
        load_dotenv_if_present()
        return cls(
            server=ServerConfig.from_env(),
            model=ModelConfig.from_env(provider),
            budgets=BudgetConfig.from_env(),
            sandbox=SandboxConfig.from_env(),
            rag=RagConfig.from_env(),
            mcp=_load_mcp_config(),
            multiagent=MultiAgentConfig.from_env(),
            spill=SpillConfig.from_env(),
            checkpoint=CheckpointConfig.from_env(),
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


# --------------------------------------------------------------------------- #
# 编排引擎（M1）
# --------------------------------------------------------------------------- #
ORCHESTRATOR_LEGACY = "legacy"
ORCHESTRATOR_GRAPH = "graph"
ORCHESTRATORS = {ORCHESTRATOR_LEGACY, ORCHESTRATOR_GRAPH}


def validate_orchestrator(value: str) -> str:
    """
    校验编排器配置值。

    ★ 为什么 graph 也要在这里拦：M1 只交付"切换入口 + 历史持久化"，
      graph 引擎尚未实现。如果在这里放行，服务会"假装切换成功"——
      那正是 M1-A 明确禁止的。graph 已随 M2 实现，M4.3 起成为默认引擎；
      legacy 仍可用（显式设置 LOOM_ORCHESTRATOR=legacy）。
    """
    v = (value or "").strip().lower()
    if v not in ORCHESTRATORS:
        raise ValueError(
            f"编排器配置非法：{value!r}（合法值：{' | '.join(sorted(ORCHESTRATORS))}）"
        )
    # M2：graph 已实现。langgraph 未安装时在引擎构造处报可诊断错误
    #（不在这里拦 —— 配置合法性与可选依赖的可用性是两件事）。
    return v
