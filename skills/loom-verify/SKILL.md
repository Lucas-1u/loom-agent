---
name: loom-verify
description: Loom 仓库的改动验收套路：跑哪些自检套件、命令怎么写、期望数字是多少、哪些"失败"其实是故障注入。在本仓库改了后端/前端代码后、准备说"做完了"之前使用。
---

# Loom 改动验收（本仓库专用）

## 何时用

- 改了 `backend/` 下任何代码，想确认没把已有的 1600+ 项自检打挂。
- 改了前端 `frontend/src`，要跑事件契约 / SSE / 会话三组测试。
- 准备向用户交付"完成"，需要用可复现的数字说话。

## 硬约束

1. **不要凭印象报数字**。每次都以命令实际输出为准 —— 断言只增不减，历史数字会过期。
2. **不要用 `pytest` 跑这些套件**：它们是 `python -m backend.selfcheck_xxx` 形式的独立脚本，
   自带 `check()` 与结果汇总（`结果：N/N 通过`）。
3. 全绿之后再改文档里的数字；文档写错的代价比代码写错大（下一个人会照着文档验收）。

## 后端：17 个套件

```bash
cd $SKILL_DIR/../..        # 仓库根（loom/）；$SKILL_DIR 由 Loom 载入时替换成真实绝对路径
./.venv/Scripts/python.exe -m backend.selfcheck_agent
./.venv/Scripts/python.exe -m backend.selfcheck_p1
./.venv/Scripts/python.exe -m backend.selfcheck_p2
./.venv/Scripts/python.exe -m backend.selfcheck_cli
./.venv/Scripts/python.exe -m backend.selfcheck_server
./.venv/Scripts/python.exe -m backend.selfcheck_checkpoint
./.venv/Scripts/python.exe -m backend.selfcheck_subtask
./.venv/Scripts/python.exe -m backend.selfcheck_spawn_parallel
./.venv/Scripts/python.exe -m backend.selfcheck_workflow
./.venv/Scripts/python.exe -m backend.selfcheck_mcp
./.venv/Scripts/python.exe -m backend.selfcheck_terminal
./.venv/Scripts/python.exe -m backend.selfcheck_edit_file_line
./.venv/Scripts/python.exe -m backend.selfcheck_spill
./.venv/Scripts/python.exe -m backend.selfcheck_pruner_replay
./.venv/Scripts/python.exe -m backend.selfcheck_orchestrator_equiv
./.venv/Scripts/python.exe -m backend.selfcheck_rag
./.venv/Scripts/python.exe -m backend.selfcheck_sandbox
```

一次性跑完并只看结论：

```bash
for s in agent p1 p2 cli server checkpoint subtask spawn_parallel workflow mcp \
         terminal edit_file_line spill pruner_replay orchestrator_equiv rag sandbox; do
  printf "%-18s " "$s"
  ./.venv/Scripts/python.exe -m backend.selfcheck_$s 2>&1 | grep -E "^结果[:：]" | tail -1
done
```

**读结果时注意**：日志里出现 `Traceback`、`订阅者处理事件失败`、`落盘失败` 很可能是
**故意的故障注入**（自检在验证"订阅者异常被隔离""数据库只读时降级"）。
判定标准只有一条：最后一行是不是 `结果：N/N 通过`。

## 端到端（真实 HTTP + 真实 SSE，模型用脚本替身）

```bash
./.venv/Scripts/python.exe -m backend.server.test_live_e2e          # 全部场景
./.venv/Scripts/python.exe -m backend.server.test_live_e2e s6       # 只跑 ask_user 场景
```

`EXPECTED_COUNTS` 里有"断言条数必须等于预期"的保护：**加了断言就要同步改数字**，
否则会报"第 N 项断言未执行"。这是有意设计的，别绕。

## 前端

```bash
cd frontend
npm run test:events      # 事件契约 + SSE 解析器 + 权限模式枚举 + ask_user 自由输入
npm run test:sse         # 真起 HTTP 假服务器 + 真后端帧格式（需要 8765 端口空闲）
npm run test:session
npm run test:workspace
npm run test:tasktree
npm run build
npm run lint             # 只关心 error；既有 10 条 fast-refresh 警告不算回归
```

## 常见坑

- `test:sse` 要独占 8765 端口，先关掉在跑的后端服务。
- 前端 `test:events` 先经 esbuild 转译到 `.tmp/`，**改了 `src/types/events.ts` 必须重跑**。
- 后端自检全程离线：不许发真实网络请求（联网搜索的测试全部 mock `urlopen`）。
- 在 Windows + Git Bash 下用 `./.venv/Scripts/python.exe`，不要直接 `python`（可能指向别的解释器）。
