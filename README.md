# Loom Agent

> **把 Agent 的内部机制摊开给你看。**

目标形态是 Codex / Cursor 式的个人 Coding Agent。但现阶段的目标不是"功能做得多"，
而是 **"每一步都能讲清楚为什么"** —— 这是一个用来**理解现代 AI Agent 内部架构**的实验平台。

**纯 Python · 不用编排框架 · 自检 397/397 · 真实模型 7/7 场景通过**

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

### 它现在能做什么（v0.1a）

| 能力 | 状态 |
|---|---|
| 五阶段 Agent Loop（PLAN / ACT / OBSERVE / REFLECT / FINAL） | ✅ |
| 工具调用（列目录 / 读文件 / 写文件） | ✅ |
| **局部修改** `edit_file`（唯一匹配 / 冲突检测 / stale 检测 / diff 预览） | ✅ |
| 写操作**人工审批**（真挂起-恢复，不是轮询） | ✅ |
| 工作区越界防护 | ✅ |
| 结构化事件流（16 种事件类型） | ✅ |
| Windows 一键启动器 | ✅ |
| `delete_file` | ⬜ 下一步 |
| `run_command`（allowlist + 审批） | ⬜ 待定 |
| 桌面 GUI（Tauri + React） | ⬜ 规划中 |

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
`budget_tool_calls` / `too_many_errors` / `model_error` / `cancelled` / `internal_error`

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

16 种类型：`task.start` / `task.end` / `plan` / `thinking` / `act` / `observe` /
`tool.call` / `tool.result` / `approval.request` / `approval.decision` / `approval.expired` /
`reflect` / `final_answer` / `error` / `budget.exceeded` / `state.snapshot`

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
│   ├── events.py          ★★★  事件契约的唯一来源（16 种事件）
│   ├── state.py           ★★★  TaskState：结构化程序状态（≠ 聊天记录）
│   ├── config.py                路径 / 模型 / 预算 / 安全常量
│   ├── demo.py                  离线演示（不需要 API Key）
│   │
│   ├── agent/                   ── Agent Core ──
│   │   ├── core.py        ★★★  五阶段循环主控
│   │   ├── policy.py      ★★   执行前校验链 + 软/硬双层熔断
│   │   ├── approval.py    ★★   审批管理器（asyncio.Future 真挂起）
│   │   └── context.py     ★★   State → messages 单向派生
│   │
│   ├── tools/                   ── 工具层 ──
│   │   ├── base.py        ★★   统一工具契约（11 种错误类型）
│   │   ├── path_safety.py ★★★  路径越界防护
│   │   ├── registry.py    ★★   注册表 + 统一执行入口
│   │   ├── readonly/           list_files / read_file（自动执行）
│   │   └── mutating/           write_file / edit_file（需审批）
│   │       └── _shared.py      备份 / 哈希 / 文本读取（两个写工具共用）
│   │
│   ├── models/                  ── 模型层 ──
│   │   ├── adapter.py           ModelAdapter 抽象 + FakeAdapter
│   │   └── openai_compat.py     一套实现同时服务百炼 / DeepSeek
│   │
│   ├── cli/                     CLI 交互层
│   │   ├── render.py            Agent 运行日志区（16 种标签）
│   │   ├── console.py           终端输入解码（UTF-8 / GBK 宽容处理）
│   │   ├── approve.py           审批交互
│   │   └── main.py              入口 + 参数 + 交互 REPL
│   │
│   └── selfcheck_*.py           自检套件（397 项，全离线）
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
| `read_file` | read | ❌ 自动 | 40k 字符上限，多编码探测，带行号 |
| `write_file` | write | ✅ **需确认** | 写前自动备份，`overwrite=false` 保护 |
| `edit_file` | write | ✅ **需确认** | 只改一小段。**唯一匹配**；匹配到多处时拒绝而不是猜 |
| `delete_file` | delete | ✅ | 下一步 |
| `run_command` | exec | ✅ | 待定 |

判据是 **"有没有副作用"**，不是工具名。

统一返回形状：

```python
{"ok": bool, "tool": str, "summary": str, "data": {...},
 "error": {"kind": ..., "message": ..., "hint": ...} | None,
 "side_effects": [...], "retryable": bool, "security": bool}
```

11 种错误类型：`not_found` / `not_a_file` / `not_a_dir` / `permission_denied` /
`invalid_arguments` / `path_outside_workspace` / `sensitive_file` / `too_large` /
`timeout` / `execution_error` / `not_implemented` / `internal_error`

`summary` 进 Evidence（给模型看结论），`data` 进消息体（给模型看细节）—— 分两个粒度是有意的。

---

## 测试

```bash
# 离线自检（不需要 API Key）
python -m backend.selfcheck_p1      # 46  —— 契约层
python -m backend.selfcheck_p2      # 32  —— 模型层
python -m backend.selfcheck_agent   # 208 —— Agent 全链路（重点）
python -m backend.selfcheck_cli     # 67  —— CLI 渲染 + 审批 + 终端输入解码
python dev/test_workspace_reuse.py  # 17  —— 启动器的工作目录选择器

# 联网（需要 .env 里的 Key）
python -m backend.test_live_model          # 5 个真实模型场景
python -m backend.test_approval_live       # 真实交互式审批（管道喂 stdin）
```

**为什么自检写这么多？** 因为 Agent 的行为有太多"看起来对但实际不对"的地方：

- 计划步骤没推进 → 却报告"计划已完成"
- 正常完成 → 却说"这不是完整结论"
- 审批批了 A → 实际执行的是 B
- `/proj_evil` 被当成 `/proj` 的子目录

这些都不会崩，只会静默地做错事。**397 项自检就是用来钉死这些的。**

---

## 路线图

- ~~**v0.1b**~~ — ✅ `edit_file`（唯一匹配 / `patch_conflict` / `stale_content` /
  CRLF 与 BOM 保真 / diff 审批预览）
- **下一步** — `delete_file`（复用同一套备份与 stale 检测）
- **v0.1c** — `run_command`（allowlist + 审批）
- **v0.2** — 本地 HTTP + SSE 服务层（`POST /api/task` + `GET /api/stream`）
- **v0.3** — Tauri + React 桌面端（Agent Core 零改动，只消费事件流）

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
