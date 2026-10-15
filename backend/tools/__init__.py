"""Loom Agent · 工具系统

    tools/base.py      工具契约（Tool 抽象 + 统一返回形状 + 11 种错误）
    tools/registry.py  注册表（名字 → Tool 实例 + 声明）
    tools/readonly/    只读工具（list_files / read_file）
    tools/mutating/    写入工具（write_file；v0.1b 加 edit_file / delete_file）

依赖方向：tools/ 是最底层，**不 import agent/ 也不 import server/**。
工具只知道「执行这件事」，不知道「Agent 为什么调用我」、也不知道「谁来审批」。
"""
