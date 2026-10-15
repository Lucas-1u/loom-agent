"""
================================================================
Loom Agent v0.1 · 后端包
================================================================

分层与依赖方向（单向，不可逆）：

    server/  ──→  agent/  ──→  tools/
       │            │
       │            └──→  models/
       └──→  events.py  ←── agent/ 也 import（双方依赖契约，不互相依赖）

铁律：
  · agent/ 不许 import server/     → 保证「Agent 逻辑不写死在 GUI/传输层」
  · events.py 是前后端唯一契约     → 改协议只改这里 + frontend/src/types/events.ts
"""

__version__ = "0.1.0a"
