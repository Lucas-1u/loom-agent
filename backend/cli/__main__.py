"""CLI 包入口。

支持两种等价调用方式：

    python -m backend.cli.main  "分析当前项目"
    python -m backend.cli       "分析当前项目"      ← 本文件提供的简写

为什么要这个文件：`python -m backend.cli` 需要包里有 `__main__.py`，
否则 Python 会报 "No module named backend.cli.__main__"。
一个文件解决，比让使用者记 `backend.cli.main` 更容易记。
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
