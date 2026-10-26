# Loom Agent

> **把 Agent 的内部机制摊开给你看。**

目标形态是 Codex / Cursor 式的个人 Coding Agent。但现阶段的目标不是"功能做得多"，
而是 **"每一步都能讲清楚为什么"** —— 这是一个用来**理解现代 AI Agent 内部架构**的实验平台。

**纯 Python · 手写 Agent Loop · 自检 1022 项（离线，全部通过）· 真实模型 7/7 场景通过**

---

## 这是什么

Loom 是一个跑在本地的 Coding Agent：

```
你：分析一下这个项目的结构

Loom：[PLAN   ] 计划 3 步
       1. 列出根目录文件
       2. 读取入口文件与配置
       3. 归纳项目结构与关键发现
      [TOOL   ] list_files(path=".")
      [RESULT ] 12 个文件，3 个目录
      [THINK  ] 有 main.py 和 requirements.txt，先读这两个
      [TOOL   ] read_file(path="main.py")
      ...
      [ANSWER ] ## 关键发现
                1. README 描述的结构与实际不符
                2. main.py 引用了 services/，但该目录不存在
```

跟"调一个 API 让模型聊天"的区别在于：**Agent Loop、状态机、权限判定、事件流全部是手写的显式代码**，
不藏在任何编排框架里。

### 它现在能做什么（截至 M2.2）

| 能力 | 状态 |
|---|---|
| 五阶段 Agent Loop（PLAN / ACT / OBSERVE / REFLECT / FINAL） | ✅ |
| 工具调用（列目录 / 读文件 / 写文件 / 局部修改 / 沙箱执行命令） | ✅ 共 5 个 |
| **局部修改** `edit_file`（唯一匹配 / 冲突检测 / stale 检测 / diff 预览） | ✅ |
| 写操作**人工审批**（真挂起-恢复，不是轮询） | ✅ |
| 工作区越界防护 / 允许-禁止范围 / 改动审计 | ✅ |
| 结构化事件流（**17** 种事件类型） | ✅ |
| 上下文压缩 / 文件读取复用 / 预算续期 | ✅ |
| 消息历史持久化（`task_history`，append-only） | ✅ |
| 编排引擎（`LOOM_ORCHESTRATOR=legacy\|graph`，默认 legacy） | ✅ 双引擎 |
| Windows 一键启动器 | ✅ |
| `delete_file` | ⬜ **未实现**（从未写过，不在当前 M1–M6 计划内） |
| 浏览器 GUI（React + Vite） | ✅ 已接真实后端 |
| 桌面外壳（Tauri） | ⬜ 未实现（`frontend/src-tauri` 不存在） |

---

## 命令执行（`run_command` + Docker 沙箱）

Agent 可以跑 Python 脚本、pytest、构建命令，但**只能在容器里跑**。
审批与隔离是两层：批准只代表"这条命令可以执行"，**不会**放宽任何一条隔离限制。

### 一次性准备镜像

沙箱默认断网，所以依赖必须打进镜像（运行期 `pip install` 一定失败）：

```bash
docker build -f dev/Dockerfile.sandbox -t loom-sandbox:py312 .
```

需要更多依赖就改 `dev/Dockerfile.sandbox` 里的 `pip install` 再 build。
镜像默认锁到 `python:3.12.7-slim`；要更严格可锁 digest（见该文件注释）。
运行时带 `--pull never`，本地没有镜像时直接报错，**不会偷偷联网拉取**。

### 每次执行的固定限制（写死在代码里，模型改不了）

| 限制 | 值 |
|---|---|
| 网络 | `--network none`（要联网必须改代码） |
| 用户 | `--user 1000:1000`（非 root） |
| 根文件系统 | `--read-only`，只有 `/tmp` 是 tmpfs |
| capabilities | `--cap-drop ALL` + `no-new-privileges` |
| 资源 | 1 CPU / 512MB 内存（含 swap）/ 128 进程 / 默认 60s、上限 300s |
| 挂载 | 只挂工作区到 `/workspace`（读写），不挂 docker.sock / 家目录 / 系统目录 |
| 环境变量 | 只注入 `HOME` / `TMPDIR` / 三个 Python 变量，不透传宿主机 env |
| 输出 | stdout / stderr 各 16KB，超出截断（不静默） |

### Docker 不可用时

`run_command` 返回 `sandbox_unavailable` 并说明原因，**不会退回到宿主机执行**。
服务启动时会在横幅和 `/api/health` 的 `sandbox` 字段里显示状态。

### 测试

```bash
python -m backend.selfcheck_sandbox        # 96 项，离线（不需要 Docker）
python -m backend.test_sandbox_docker      # 25 项真实容器；没有 Docker 会 [SKIP]
```

离线用例验证的是**参数与控制流**，不能替代真实隔离验证。

> ⚠️ 这不是"绝对安全"的沙箱：它与 Docker 守护进程同权限级别，
> 容器逃逸/内核漏洞仍可能影响宿主机。它降低的是"Agent 跑错命令把系统搞坏"的风险，
> 不是对抗恶意代码的强隔离。

---

## 为什么又造一个 Agent

市面上的 Agent 框架很多，用起来很方便 —— 但**关键机制被封装起来了**，
想看清"它到底怎么跑的"就很难。遇到问题时你只能猜：是模型没选对工具？
是上下文拼错了？还是权限判定把请求拦了？

Loom 反过来做：不用 LangChain 之类的编排层，核心机制全部手写，
**每一处都留了注释说明"为什么这么设计""不这么做会怎样"**。

代价是功能少、跑得慢。收益是 —— **打开任何一个文件都能读懂**。

三条优先级铁律（冲突时按这个顺序让步）：

1. **架构清晰 > 功能数量**
2. **可理解性 > 代码量**
3. **可扩展性 > 快速堆功能**

界面美观度不是当前目标。

---

## 快速开始

### 先看它跑（不需要 API Key）

```bash
git clone https://github.com/Lucas-1u/loom-agent.git
cd loom-agent

python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

# 三个离线演示，各自暴露一条关键路径
python -m backend.demo            # 正常：规划 → 观测 → 写文件（含审批）
python -m backend.demo --reject   # 拒绝：写操作被拒后 Agent 如何收场
python -m backend.demo --budget   # 熔断：重复调用 + 轮数耗尽
```

`demo` 会自建临时工作区、跑完整套 Agent Loop，然后打印 **调试面板**（轮次 / 证据 /
审批 / 文件变更 / 计划打勾）、**事件直方图**、**磁盘实际状态**。
这是理解整套机制最快的入口 —— 两个终端并排跑一次，胜读十页文档。

### 接真实模型

```bash
cp .env.example .env
# 填入 BAILIAN_API_KEY（阿里云百炼）或 DEEPSEEK_API_KEY
```

两个供应商都走 OpenAI 兼容协议，**同一个 Adapter 实现**（`models/openai_compat.py`），
换供应商只改一个配置项。

```bash
python -m backend.cli -w D:/some/project "分析这个项目的结构"
```

常用参数：

```bash
-m deepseek               # 换供应商（bailian | deepseek）
--model-name qwen3.7-plus # 换具体模型
--readonly                # 只读模式：卸载所有写入工具，零副作用
--no-plan / --no-reflect  # 关掉阶段，做对比实验
--compact --no-think      # 精简输出
--dry-run                 # 只看环境信息，不跑任务
```

交互模式（不传任务描述）里：`:t` 看工具清单、`:s` 看预算、`:q` 退出。

### Windows 一键启动

双击 `loom.bat` 即可，它替你处理了四件容易忘、忘了就报错的事：

| 它做了什么 | 不做会怎样 |
|---|---|
| 切到项目根目录 | `python -m backend.cli` 找不到包 |
| 清除系统代理 | 请求被本机代理拦住，卡在「正在调用模型…」直到超时 |
| 用 `.venv` 里的 Python | 系统 Python 缺依赖，直接崩 |
| 清掉 `PYTHONUTF8` / `PYTHONIOENCODING` | 输入中文任务时行为不可预测 |

工作目录支持**拖文件夹进窗口**，也支持**直接回车复用上次的**（记忆在 `.loom_last_workspace`）。

**想让 GUI 也能一键启动？**

`loom.bat` 是命令行版。GUI 版是根目录的 `loom-gui.bat`（会装依赖、查端口、起 Vite、自动开浏览器）。

如果不想每次都先进到项目目录，可以把启动器装到桌面：

```bash
python dev/install_desktop.py            # 装到桌面（生成 Loom.bat）
python dev/install_desktop.py --where    # 只看会装到哪，不写文件
python dev/install_desktop.py --remove   # 卸载
```

桌面上的 `Loom.bat` 只是**跳板**：切到项目目录后 `call loom-gui.bat`。
真正的启动逻辑只在项目里维护一份，不会出现「桌面版和项目版行为不一致」。

> 安装时会把 `__LOOM_HOME__` 占位符替换成当前项目路径 —— 不用手改。
> 项目挪了位置，重跑一次安装脚本即可。

### GUI 界面（Phase 3c 起已接真实后端）

前端是**真的在跟后端说话**：会话/任务来自 `GET /api/sessions`、`POST /api/tasks`，
执行过程走 `GET /api/stream`（SSE，断线用 `?since=<lastId>` 补拉），审批走
`POST /api/approvals/{id}/decision`。演示数据只在后端不可达时兜底（右上角会显示 `DEMO` /
`OFFLINE` 徽章，不会假装连上了）。

```bash
# 先起后端（默认 127.0.0.1:8765），再起前端
python -m backend.server
cd frontend && npm install && npm run dev      # http://localhost:5173

npm run test:workspace   # 路径规范化 / 分组（26 项）
npm run test:session     # 会话隔离（16 项）
npm run test:events      # 事件契约：后端 EventType ↔ 前端清单 ↔ 样式（31 项）
```

> `test:events` 不是可选的：`eventStream.ts` 是**按 `EVENT_TYPES` 逐个 addEventListener**
> 的，清单少一种，那种事件的 SSE 帧就会被浏览器静默丢弃（`context.compacted` 就这么丢过一次）。

**三栏布局：左侧按工作目录分组的会话列表 + 中央双视图 + 底部工作区与输入框。**

| 视图 | 看什么 |
|---|---|
| 聊天视图 | 日常对话：用户消息、Markdown 答复、运行状态 |
| 执行流 | Agent 的每一步：PLAN / TOOL_CALL / TOOL_RESULT / 审批 / 最终回答 |

**四层数据模型**（详细说明见 `frontend/src/features/session/`）：

```
Session（有自己的工作目录）
  └── Task（一次具体执行，有自己的 task_id 和事件流）
        ├── Message（可读对话，不是每条事件都算）
        └── AgentEvent（执行过程）
```

**几条硬规则：**

- **历史会话按工作目录分组**，不按时间。分组 key 是**完整规范化路径**，
  所以 `D:\Projects\demo-app` 和 `E:\Experiments\demo-app` 是两组（界面会自动加长显示名区分）。
- **切换视图只换渲染**，`SET_VIEW` 只改一个字段，不碰任何数据。
- **切换会话不重新执行任务**，`SET_ACTIVE_SESSION` 只改选中项。
- **同一会话多个 Task 时**，执行流顶部会出现任务选择器，事件不会串台。

**调试参数**：

| 参数 | 作用 |
|---|---|
| `?view=execution` | 直接打开执行流视图 |
| `?view=execution&scroll=top` | 从时间线开头看，而不是贴底 |

**当前进度**：布局 / 会话分组 / 双视图 / 事件配色 / 折叠 / 自动滚动 / 响应式都已就绪，
并且 Phase 3c 已把演示数据换成真实事件（含审批卡、取消、断线补拉）。
**下一步**：M3（RAG 检索）‖ M4（MCP 接入），之后 M5 → M6；M6 才做四项技术的展示增强
（子任务树 / mcp / rag 标记），本轮不涉及。

---

## 它是怎么跑的

```
User Task → PLAN → ACT(Tool Calling) → OBSERVE(Tool Result) → REFLECT → FINAL ANSWER
```

| 阶段 | 做什么 | 带 `tools` 吗 |
|---|---|---|
| **PLAN** | 让模型输出 JSON 计划（2-5 步）。格式不符**降级**，不崩溃、不设计划、留痕 | ❌ |
| **ACT** | 决定：调工具，还是直接作答 | ✅ **只有这里带** |
| **OBSERVE** | 工具结果进 `Evidence` 列表 | ❌ |
| **REFLECT** | 判断是否收敛；顺便推进计划步骤 | ❌ |
| **FINAL** | 基于已观测的证据，**真正回答用户的问题** | ❌ |

**阶段工具门控**是被测试钉死的：`PLAN=False, ACT=True, REFLECT=False, FINAL=False`
（`FakeAdapter.tools_gate` 断言）。只有 ACT 阶段给模型工具 —— 否则它会在该收口的时候又去调工具。

### FINAL 为什么必须是一次独立的模型调用

这一条是踩过坑才明白的。最初 REFLECT 判定 `done=true` 就直接用程序生成的模板收尾：

```
任务已完成（第 4 轮，共观测 6 条事实）。
计划共 4 步，已完成 4 步。
已执行的动作：
  ✓ list_files：. 下有 0 个目录、5 个文件
  ...
```

**这没有回答用户的问题。** 用户问「分析当前项目结构」，拿到的却是一份工具调用清单 ——
而模型其实早在思维链里得出了真结论（"main.py 引用了 `services/`，但这个目录不存在"），
只因为收尾走了模板而被丢弃。

区别在于：

| | 回答的问题 | 谁生成 |
|---|---|---|
| 进度清单 | 「你做了什么」 | **程序**（已由 `task.end` / 运行摘要承担） |
| 最终答复 | 「你发现了什么」 | **模型**（基于证据作答） |

所以 FINAL 是独立的一次调用，且**不带 tools** —— 强制它收口成答案。
调用失败时降级回记账式答复，但会**如实说明原因**。

### 终止条件都有明确的原因

`agent_done` / `no_more_actions` / `empty_reply` / `budget_turns` / `budget_seconds` /
`budget_tool_calls` / `too_many_errors` / `model_error` / `cancelled` / `internal_error` /
`verification_failed` / **`no_progress`**

`no_progress` 是 M2.2 新增的：模型反复把 REFLECT 格式的进展汇报当答复提交、而期间
**没有任何工具执行 / 新证据 / 计划推进**时，先给一次受控恢复提示，仍无进展就以这个原因终止
（判据是"零进展"，不是"反思了几次"，见 `_handle_reflect_answer`）。

中断时兜底答复会**诚实声明**"以上为中断时的实际进展，不是完整结论" ——
只在真正中断时才说，正常完成时不出现。

---

## 几个值得讲的设计

### 1. 事件是第一公民 —— `events.py`

Agent 做任何事都产生**结构化事件**，而不是 `print` 日志：

```python
{"seq": 7, "ts": 1759..., "task_id": "task_ab12", "turn": 2,
 "type": "tool.call",
 "data": {"id": "call_1", "tool": "read_file",
          "args": {"path": "main.py"}, "risk": "read"}}
```

17 种类型：`task.start` / `task.end` / `plan` / `thinking` / `act` / `observe` /
`tool.call` / `tool.result` / `approval.request` / `approval.decision` / `approval.expired` /
`reflect` / `final_answer` / `error` / `budget.exceeded` / `context.compacted` / `state.snapshot`

两个关键设计：

- **`emit()` 同步分配 `seq`，投递走 `asyncio.create_task`** —— 订阅者永远拖不慢 Agent
- **订阅者抛异常被吞掉并单独记日志** —— 一个 UI 组件崩了不该搞死整个任务

`to_sse()` 直接产出 SSE 帧，将来接 GUI 零改造。

### 2. State ≠ 聊天记录 —— `state.py` / `context.py`

`TaskState` 是**结构化的程序状态**：轮次、预算用量、计划步骤、证据列表、
工具调用指纹、待审批项、文件变更记录。

`messages[]` 是从 TaskState **派生**出来的视图（`context.py` 单向生成）。
反过来（从消息数组反推状态）不好使 —— 这是从上一个项目里学到的。

还有一个协议不变量由 `TurnBuilder` 强制守：**带 `tool_calls` 的 assistant 消息后面，
必须紧跟每个 call_id 对应的 `tool` 消息，且顺序一致**。缺失的自动补占位符，
在 `build()` 里统一执行 —— 只有一处，不会漏。（漏了会直接吃 API 400。）

### 3. 模型的输出是建议，不是权限 —— `policy.py`

每次工具调用前过一条 7 步校验链，顺序是设计过的：

```
① 工具存在且在白名单
② 参数 JSON 可解析           ← parse_error 优先于 schema 校验
③ 参数符合 schema
④ 预算还有余额               ← 在路径检查之前：已超时的任务不该继续写文件
⑤ 重复熔断（软）
⑤b 确定性失败熔断（硬）
⑥ 是否需要审批               ← 放最后：别为注定失败的调用打扰用户
⑦ 放行
```

**双层熔断**的分工：

| 层 | 触发条件 | 行为 |
|---|---|---|
| 软 | 同一「工具+参数」出现 ≥ 3 次 | 拒绝，提示换思路 |
| 硬 | 同一「工具+参数」**失败过且错误不可重试** | **第 2 次就拒** |

硬熔断针对的是"参数不变则结论必然不变"的失败：`path_outside_workspace` /
`sensitive_file` / `permission_denied`。可重试类（`not_found` 等）不受影响。

> 加这条是因为真实模型实测：对越界路径它会拿**完全相同的参数**连撞两次才换思路。
> 软熔断要数到 3 才拦，中间那次是纯浪费的 token 和一个轮次。

### 4. 审批是真挂起，不是轮询 —— `approval.py`

三种做法里只有第三种是对的：

- ❌ **轮询**：Agent 循环里查标志位 —— 不是真挂起，浪费 CPU，语义含糊
- ❌ **事后审计**：执行完再记录 —— 那不叫审批
- ✅ **`asyncio.Future` 挂起/恢复** —— 本项目的做法

```python
fut = loop.create_future()
self._pending[aid] = fut
verdict = await asyncio.wait_for(fut, timeout=self.timeout_s)   # ← 真的挂在这里
```

三道安全防线：

| 问题 | 防线 |
|---|---|
| "批的是 A，执行的是 B" | `args_hash` = sha256[:16]（工具名 + 排序后参数），执行前重新比对 |
| "同一个审批重放两次" | `pop()` 一次性消费，审批 ID 用完即焚 |
| "等待审批期间文件被换了" | `recheck_after_approval()` 重新校验路径 + 预算（TOCTOU） |

> **踩坑**：`input()` 是同步阻塞的，直接调会把事件循环卡死，
> 连 `asyncio.wait_for` 的超时都不会生效。必须用
> `loop.run_in_executor(None, sys.stdin.readline)` 包一层。

### 5. 路径安全 —— `path_safety.py`

真实攻击面有三个：`../` 穿越、**符号链接逃逸**、Windows 8.3 短名。

```python
t = os.path.normcase(os.path.normpath(target))
r = os.path.normcase(os.path.normpath(root))
if not r.endswith(os.sep):
    r += os.sep          # ← 关键
return t.startswith(r)
```

朴素 `startswith` 会把 `D:/proj_evil` 误判成在 `D:/proj` 里面 —— 补上分隔符才安全。
用 `realpath` 而不是 `abspath`，是因为只有前者会解析符号链接。

---

## 项目结构

```
loom/
├── loom.bat                     一键启动器（Windows）
│
├── backend/                     Python Agent Runtime
│   ├── events.py          ★★★  事件契约的唯一来源（17 种事件）
│   ├── state.py           ★★★  TaskState：结构化程序状态（≠ 聊天记录）
│   ├── config.py                路径 / 模型 / 预算 / 安全常量
│   ├── demo.py                  离线演示（不需要 API Key）
│   │
│   ├── agent/                   ── Agent Core ──
│   │   ├── core.py        ★★★  五阶段循环主控（legacy 引擎）
│   │   ├── graph_engine.py ★★  LangGraph 引擎（复用 core 的同一组检查点）
│   │   ├── policy.py      ★★   执行前校验链 + 软/硬双层熔断
│   │   ├── approval.py    ★★   审批管理器（asyncio.Future 真挂起）
│   │   ├── context.py     ★★   State → messages 单向派生
│   │   ├── compaction.py       上下文压缩（状态块 + 摘要，可降级）
│   │   ├── intent.py           意图分类 / 约束抽取 / 证据分级
│   │   ├── scope.py            改动范围审计（基线 diff + 归因）
│   │   ├── history_log.py      消息历史持久化（append-only + 缺口检测）
│   │   └── state.py            （见上，契约层）
│   │
│   ├── tools/                   ── 工具层 ──
│   │   ├── base.py        ★★   统一工具契约（15 种错误类型）
│   │   ├── path_safety.py ★★★  路径越界防护
│   │   ├── registry.py    ★★   注册表 + 统一执行入口
│   │   ├── readonly/           list_files / read_file（自动执行）
│   │   ├── mutating/           write_file / edit_file（需审批）
│   │   │   └── _shared.py      备份 / 哈希 / 文本读取（两个写工具共用）
│   │   └── exec/               run_command（需审批 + Docker 沙箱）
│   │
│   ├── models/                  ── 模型层 ──
│   │   ├── adapter.py           ModelAdapter 抽象 + FakeAdapter
│   │   └── openai_compat.py     一套实现服务百炼 / DeepSeek / 讯飞（含候选轮换）
│   │
│   ├── cli/                     CLI 交互层
│   │   ├── render.py            Agent 运行日志区（按事件类型渲染）
│   │   ├── console.py           终端输入解码（UTF-8 / GBK 宽容处理）
│   │   ├── approve.py           审批交互
│   │   └── main.py              入口 + 参数 + 交互 REPL
│   │
│   └── selfcheck_*.py           自检套件（1022 项，全离线）
│
├── dev/                         开发辅助
│   ├── loom.bat.src             启动器源文件（UTF-8）
│   ├── build_bat.py             构建：UTF-8 源 → GBK 产物
│   ├── simulate_keys.py         模拟真人按键，端到端验证启动器
│   └── test_workspace_reuse.py  工作目录选择器回归测试
│
└── sandbox/demo_project/        测试夹具（故意埋了矛盾供模型发现）
```

### 依赖方向是单向的

```
server/  →  agent/  →  tools/
             ↓
          events.py   ←（server 与 agent 都 import，但它不 import 任何项目内模块）
```

**铁律：`agent/` 里禁止 import `server/`。**
这是"Agent 逻辑不写死在 GUI"在代码层的落地 —— 哪天有人在 Agent 里直接推 GUI 状态，import 就会失败。

---

## 工具边界

| 工具 | 风险 | 需审批 | 说明 |
|---|---|---|---|
| `list_files` | read | ❌ 自动 | 只列一层，跳过噪音目录，上限 300 条 |
| `read_file` | read | ❌ 自动 | 40k 字符上限，多编码探测，带行号，返回内容哈希 |
| `write_file` | write | ✅ **需确认** | 写前自动备份，`overwrite=false` 保护 |
| `edit_file` | write | ✅ **需确认** | 只改一小段。**唯一匹配**；匹配到多处时拒绝而不是猜 |
| `run_command` | exec | ✅ **需确认** | **只能在 Docker 沙箱里跑**（断网 / 只读根 / 非 root / 限额） |
| `delete_file` | — | — | ⬜ **未实现**（`tools/mutating/` 里只有 write/edit） |

判据是 **"有没有副作用"**，不是工具名。

统一返回形状：

```python
{"ok": bool, "tool": str, "summary": str, "data": {...},
 "error": {"kind": ..., "message": ..., "hint": ...} | None,
 "side_effects": [...], "retryable": bool, "security": bool}
```

15 种错误类型（`tools/base.py` 的 `ErrorKind`，本次实测 `len(ErrorKind)==15`）：
`not_found` / `not_a_file` / `not_a_dir` / `permission_denied` / `invalid_arguments` /
`path_outside_workspace` / `sensitive_file` / `too_large` / `timeout` / `execution_error` /
`not_implemented` / `internal_error` / `patch_conflict` / `stale_content` / `sandbox_unavailable`
（审批被拒 / 超时不单列错误类型，走 `ToolResult` 的失败摘要回灌）

`summary` 进 Evidence（给模型看结论），`data` 进消息体（给模型看细节）—— 分两个粒度是有意的。

---

## 测试

```bash
# 离线自检（不需要 API Key）—— 本次全部实跑，合计 1022 项
python -m backend.selfcheck_p1      #  54 —— 契约层（含事件类型 17 的断言）
python -m backend.selfcheck_p2      #  45 —— 模型层（含候选轮换）
python -m backend.selfcheck_agent   # 575 —— Agent 全链路（重点）
python -m backend.selfcheck_cli     #  67 —— CLI 渲染 + 审批 + 终端输入解码
python -m backend.selfcheck_server  # 185 —— 存储 / 事件广播 / 运行时 / 调度
python -m backend.selfcheck_sandbox #  96 —— 沙箱参数与控制流（假进程）
python dev/test_workspace_reuse.py  #  17 —— 启动器的工作目录选择器

# 集成 / 真实环境
python -m backend.server.test_live_e2e     # 51 —— 真 HTTP/SSE/调度/审批（模型用替身）
python -m backend.test_sandbox_docker      # 25 —— 真实容器（无 Docker 会 SKIP）
python -m backend.test_live_model          #  7 个真实模型场景（联网、烧 token）
python -m backend.test_approval_live       # 真实交互式审批（管道喂 stdin）

# 前端（cd frontend）
npm run test:workspace   # 26 —— 路径规范化 / 分组
npm run test:session     # 16 —— 会话隔离
npm run test:events      # 31 —— 事件契约：后端 EventType ↔ 前端清单 ↔ 样式
```

> 数字口径：以上是**代码里实际注册的断言数**（本次运行核对）。`selfcheck_*` 与前端
> 三个脚本本次都实跑并通过；E2E / 真实容器 / 真实模型本次**未运行**。

**为什么自检写这么多？** 因为 Agent 的行为有太多"看起来对但实际不对"的地方：

- 计划步骤没推进 → 却报告"计划已完成"
- 正常完成 → 却说"这不是完整结论"
- 审批批了 A → 实际执行的是 B
- `/proj_evil` 被当成 `/proj` 的子目录

这些都不会崩，只会静默地做错事。**1022 项自检就是用来钉死这些的。**

---

## 路线图

已完成的阶段：

- ✅ **v0.1a** — 五阶段 Loop + 4 工具 + Policy + 审批（CLI）
- ✅ **v0.1b** — `edit_file`（唯一匹配 / `patch_conflict` / `stale_content` / CRLF+BOM 保真）
- ✅ **v0.1c** — `run_command` + Docker 沙箱（唯一执行出口，无宿主机分支）
- ✅ **Phase 3a–3d** — SQLite 持久化 / 调度 / FastAPI+SSE / 前端接真后端 / E2E 51 项
- ✅ **第三阶段 1–6 批** — 验证闸门 / 计划状态 / 范围审计 / 意图与证据分级 / 风险收尾确认 /
  约束一致性 / 读取复用 / 上下文压缩 / 预算续期
- ✅ **M1** — 编排器开关 + 消息历史持久化（`task_history`）
- ✅ **M2** — LangGraph 引擎（`LOOM_ORCHESTRATOR=graph`，与 legacy 共用同一组检查点）
- ✅ **M2.1** — 工具链修复（`command` 含空格一律拒 / exit 127 专属提示 / task_id 孤儿）
- ✅ **M2.2** — 前端事件契约（`context.compacted`）+ 反思答复的无进展终止（`no_progress`）

未实现 / 后续（原始计划的 M1–M6）：

- ⬜ **M3** — RAG 检索（`search_docs` + FTS5，只索引文档不索引源码）
- ⬜ **M4** — MCP 接入（`MCPToolAdapter` → 现有 Registry，默认需审批）
- ⬜ **M5** — Multi-Agent（`spawn_subtask` + 父子任务 + 证据分级验收）
- ⬜ **M6** — 前端展示与前后对比
- ⬜ `delete_file`（不在 M1–M6 计划内）、Tauri 桌面外壳、跨进程续跑

---

## 一些真实踩过的坑

这些坑的共同点是：**不报错，只是静默地做错事** —— 所以值得记下来。

**编码不能猜。** Windows 上终端送进 stdin 的字节可能是 UTF-8（现代终端 / Git Bash），
也可能是 GBK（传统 cmd）—— **取决于终端，不取决于代码**。
一开始按"控制台代码页"去猜，在 cmd 里对了，在 MSYS 终端下必错。
最终方案是**宽容解码**：先按 UTF-8 解，失败回退 GBK。
可靠性来自字节结构 —— UTF-8 的续字节一律 `10xxxxxx`，而 GBK 双字节是 `81-FE + 40-FE`，
几乎不可能碰巧构成合法 UTF-8。关键是**从 `sys.stdin.buffer` 拿原始字节** ——
走 TextIOWrapper 那一层的话，字节早被解完了，到手的乱码救不回来。

**批处理文件不能用 UTF-8 存。** cmd 读 `.bat` 时按系统 ANSI 代码页逐字节解释。
存成 UTF-8 不只是中文乱码 —— cmd 定位文件位置时按字节偏移 seek，
一个汉字占 3 字节会让指针落在字符中间，整行被切碎。报错长这样：
`'ps_proxy' 不是内部或外部命令`（其实是 `set "https_proxy="` 被切了一半），
**完全看不出是编码问题**。所以：源用 UTF-8（好编辑），产物用 GBK（cmd 能读）。

**`set /p` 分不清「空行」和「EOF」。** 两者表现完全一样：变量保持原值 + errorlevel=1。
用它判断"读不到输入"，就等于把"用户敲回车复用上次目录"直接判死。

**`echo` 里落单的 `!` 会静默吃掉后面一大段文字**（在 `enabledelayedexpansion` 下）。
`echo [!] 提示...（例如 D:\` 输出成 `[\` —— 不报错，就是断半截。

---

## 环境要求

- Python 3.11+（开发用的是 3.13）
- 一个 OpenAI 兼容的模型 API（百炼 / DeepSeek，或任何兼容端点）

---

## License

[MIT](LICENSE) —— 随意使用、修改、分发，保留版权声明即可。
