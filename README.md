# Loom Agent

**一个跑在自己电脑上的 Coding Agent 运行时。**

给它一句话，它会自己规划、查项目、调工具、改文件、跑命令、验证结果，最后给一份**带证据**的答案。

- 手写 Agent 循环，不依赖编排框架（LangGraph 只作为可选引擎）
- 25 种事件类型贯穿全程，每一步都可回放、可审计
- 离线优先：没有 API Key 也能跑演示，没有网络不影响核心逻辑

---

## 它能做什么

### 执行

| 能力 | 说明 |
|---|---|
| **双引擎** | 默认 **Graph**（LangGraph 编译的状态图），**Legacy** 手写循环保留为显式兜底；切换引擎不改任何上层契约 |
| **三段式流程** | Plan（拆步骤）→ Act（调工具）→ Reflect（自评）；反思可触发重新规划 |
| **意图识别** | 区分 diagnostic / repair / implementation / test_authoring 等，不同意图走不同门禁 |
| **风险审查** | 执行前评估动作风险等级，越权操作在 Policy 层拦下 |
| **证据分级** | 模型声称 vs 实测输出分开记账，没验证过的结论不许写成已完成 |
| **计划真值** | 步骤状态必须与实际动作对应，防止"计划说做完了但什么都没改" |
| **预算** | 轮次 / 秒数 / 工具调用数 / 同调用重复次数四维限制；仍在推进时允许有界续期一次 |
| **反循环** | 零进展检测、重复调用熔断、软熔断与硬熔断两道闸 |

### 权限与隔离

四种会话权限模式，**服务端按模式切换工具注册表**，不是提示词里说一句：

| 模式 | 能做什么 |
|---|---|
| `read-only` | 只能看：写/执行类工具根本不注册 |
| `workspace-write` | 可改工作区内文件、可跑命令 |
| `full-access` | 不受工作区写限制 |
| `desktop` | 额外开放桌面操控（截屏、定位、点击、输入） |

配套的安全边界：

- `run_command` 的**唯一出口是 Docker 容器**，宿主机不直接执行
- 路径白名单 / 黑名单，越界在 Policy 层直接拒绝
- 删除文件强制先备份，备份失败则拒绝删除
- 可选 **Windows ACL 受限令牌沙箱**（`LOOM_ACL_MODE`）
- 所有写操作留 `ChangeRecord`（前后 hash、增删行数、备份位置）

### 工具

| 分类 | 工具 |
|---|---|
| 文件 | `read_file` `list_files` `write_file` `edit_file`（行号锚定）`delete_file`（删前备份） |
| 命令 | `run_command`（Docker 沙箱）、`terminal`（宿主机 shell，需相应权限模式） |
| 桌面 | `capture_screen` `vision_query` `desktop_locate` `desktop_click` `desktop_type`（经 MCP） |
| 检索 | `web_search`（DeepSeek Search）、`search_docs`（项目知识索引） |
| 协作 | `ask_user`（结构化提问，支持超时后补答）、`spawn_subtask`、`spawn_parallel`、`session_history` |
| 技能 | `load_skill`（三级渐进披露：目录 → 正文 → 附带文件） |
| MCP | 官方 SDK 接入，单 Server 常驻连接，实测挂载 **67** 个外部工具 |

### 上下文管理

- **压缩**：超阈值时把早期轮次摘要化，保留最近 N 轮完整内容
- **工具结果修剪**：只保留最近几条完整返回，更早的压成"开头 + 截断标记"
- **读取复用**：同文件同 hash 折叠成标记，不重复贴全文
- **图像引用**：截图以**引用**（路径 + 描述）进出上下文，不内联 base64；超出保留张数的自动卸载成文本占位符，原文件仍可按路径再读
- **Token 计量**：直接采用 Provider 返回的 `usage`（精确值，不估算），按调用与任务两级累计

### 持久化与恢复

| 能力 | 说明 |
|---|---|
| **三层落盘** | 消息历史 / 状态快照 / 事件流，各自独立、跨重启可读 |
| **事件回放** | 按任务、按类型、按轮次切片回放；导出导入格式稳定 |
| **会话投影** | 从事件流派生的一行汇总，可随时删表重建，重建后逐行一致 |
| **断点恢复** | Checkpoint 记录状态快照，配合消息历史把中断的任务接回来；恢复前做缺口检查，工具执行到一半的现场拒绝恢复（避免重复副作用） |
| **会话管理** | 删除会话两级可选：只清库 / 连"本会话创建且未被改动过"的文件一起删（删前自动备份） |

### 界面

| 入口 | 内容 |
|---|---|
| 主应用 | 聊天视图 + 执行流视图；顶部实时显示当前任务 token；侧栏按工作目录分组会话 |
| 任务树 | 父子任务可视化，含状态、耗时、阻断关系 |
| 统计页 | 任务量、成功率、token、LLM/工具墙钟分解、首 token 延迟、每日趋势（7/30/90 天） |
| 桌面壳 | Tauri 2 打包，可复用已运行的后端，原生目录选择 |

---

## 快速开始

### 环境

- Python 3.11+
- Node 18+
- （可选）Docker Desktop —— `run_command` 需要

### 安装

```bash
git clone https://github.com/Lucas-1u/loom-agent.git
cd loom-agent

python -m venv .venv
./.venv/Scripts/activate        # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

cd frontend && npm install && cd ..
```

### 配置

复制 `.env.example` 为 `.env`，填入模型凭据：

```ini
LOOM_MODEL=your-model
LOOM_BASE_URL=https://your-endpoint/v1
LOOM_API_KEY=sk-xxx
```

支持任何 OpenAI 兼容端点（讯飞 MaaS / 阿里百炼 / DeepSeek / 本地 vLLM 均可）。

> 不配也能跑：未设置 Key 时前端提供**演示模式**，用内置数据走完整个界面。

### 运行

```bash
# 后端（默认 8765）
python -m backend.server

# 前端（默认 5173）
cd frontend && npm run dev
```

打开 http://localhost:5173 ，新建会话 → 输入任务 → 看它干活。

### 自检

```bash
./.venv/Scripts/python.exe -m backend.selfcheck_agent    # Agent 全链路
./.venv/Scripts/python.exe -m backend.selfcheck_server   # 服务端 / 持久化 / 统计
./.venv/Scripts/python.exe -m backend.selfcheck_p1       # 编排与工具
./.venv/Scripts/python.exe -m backend.selfcheck_p2       # 模型层
./.venv/Scripts/python.exe -m backend.selfcheck_sandbox  # 沙箱
./.venv/Scripts/python.exe -m backend.selfcheck_mcp      # MCP
cd frontend && npm run test:events                       # 事件契约一致性
```

---

## 架构

```
frontend/           React + Vite + Tailwind；SSE 自行解析帧
backend/
  server/           FastAPI + 事件总线 + 调度器 + SQLite
    store.py          任务 / 会话 / 事件 / 投影
    runtime.py        单任务生命周期
    scheduler.py      并发调度、权限模式、预算覆盖
  agent/            Agent 内核（不依赖 server）
    core.py           Agent 循环（legacy）
    graph_engine.py   状态图引擎（默认）
    context.py        上下文构建、压缩、图像卸载
    policy.py         权限与风险关卡
    checkpoint/       快照保存与恢复判定
  tools/            工具实现（不依赖 agent）
  models/           模型适配（OpenAI 兼容 + 流式解析）
  events.py         事件契约（25 种类型）
  history/          历史查询与统计（只读叶子模块）
```

依赖方向单向：`server → agent → tools`。`agent` 不 import `server`，`events.py` 不 import 项目模块。

**State ≠ 聊天记录**：`TaskState` 是唯一真相，消息由 `ContextBuilder` 从 State + 历史单向派生。

---

## 文档

`docs/` 下按功能编号，每篇是完整的验收报告（问题 / 设计与取舍 / 改动清单 / 实测数据 / 已知局限）：

| 主题 | 文档 |
|---|---|
| 权限模式与沙箱 | `26` `27` `29` `30` |
| Computer Use / MCP | `31` |
| ask_user | `32` `37` |
| 联网搜索 | `34` |
| Checkpoint 自动恢复 | `35` |
| SSE 协议 | `36` |
| Skill 机制 | `38` |
| 持久化三层 | `39` |
| 会话历史查询 | `40` |
| Token 计量 | `41` |
| 删除会话 | `42` |
| 墙钟时间分解 | `43` |
| 图像块卸载 | `44` |

---

## 许可

MIT
