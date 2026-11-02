"""
Spill 存储后端 —— 对齐 DeepSeek Harness 的 dsh-spill / dsh-spill-local。

抽象服务定义 + 本地文件系统实现。

设计要点
--------
- **保存 FULL content，逐字不改**：不截断、不改写，原样写盘
- **按 owner（task_id）建子目录**：会话级隔离，不跨任务串味
- **locator 是不透明字符串**：调用方不解析路径，只用它做引用
- **真实存储失败抛异常**：调用方（spill_policy）决定降级策略
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SpillRef:
    """Spill 存储引用。"""

    locator: str           # 完整内容的绝对路径
    total_bytes: int       # 完整内容的 UTF-8 字节数
    retrieval_hint: str    # 给模型的检索指引


class SpillStore(ABC):
    """
    抽象 Spill 存储服务。

    子类只需实现 {@link save_text}，注册为 `spillStore` 服务。
    语义要求（所有实现必须遵守）：
      · save_text 保存 FULL content，逐字不改，返回引用
      · owner 是任务隔离键，按它建子目录
      · locator 是不透明字符串，不与 suggestedName 相同
      · 真实存储失败时抛异常（调用方决定降级策略）
    """

    @abstractmethod
    async def save_text(
        self,
        *,
        owner: str,
        tool_name: str,
        content: str,
    ) -> SpillRef:
        ...


class LocalSpillStore(SpillStore):
    """本地文件系统实现。"""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    async def save_text(
        self,
        *,
        owner: str,
        tool_name: str,
        content: str,
    ) -> SpillRef:
        owner_dir = os.path.join(self.base_dir, owner)
        os.makedirs(owner_dir, exist_ok=True)

        # 时间戳 + 工具名，避免碰撞
        ts = int(time.time() * 1000)
        fname = f"{tool_name}_{ts}.txt"
        abs_path = os.path.join(owner_dir, fname)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        size = os.path.getsize(abs_path)

        return SpillRef(
            locator=abs_path,
            total_bytes=size,
            retrieval_hint=(
                "完整内容已存盘。可用 read_file 读取该路径的指定部分。"
            ),
        )
