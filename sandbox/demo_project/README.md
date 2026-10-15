# 小智 AI

个人 AI 聊天机器人，跑在 `chat.lycwsss.top`。

## 技术栈

- 后端：FastAPI + uvicorn，SSE 流式输出
- 前端：原生 HTML / CSS / JS，零构建
- 数据：SQLite（aiosqlite）
- 模型：阿里云百炼 `qwen3.7-max-preview`（推理模型）

## 目录结构

```
backend/
├── main.py                 服务入口
├── db/database.py          SQLite 封装
├── services/
│   ├── llm_client.py       模型调用
│   ├── stream_handler.py   Agent Loop（裸 while + messages）
│   └── tool_registry.py    工具注册表
└── tools/
    ├── python_executor.py  子进程沙箱执行
    └── plot_chart.py       ECharts 图表工具
frontend/                   静态页面
```

## 本地运行

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

## 部署

阿里云 ECS 青岛节点，systemd + Nginx 反代，脚本 `deploy.sh`。
