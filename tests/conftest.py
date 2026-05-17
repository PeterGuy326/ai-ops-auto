"""pytest 全局 fixtures。

为 SQLAlchemy ORM 单测准备隔离 in-memory SQLite engine。
不污染项目默认 sqlite 文件。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 让 tests 也能用 settings.data_dir（不污染真实 data/）
_tmp_data = Path(tempfile.gettempdir()) / "ai_ops_test_data"
_tmp_data.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AI_OPS_TEST_DATA_DIR", str(_tmp_data))

# 确保 src 在 path 上（pyproject 配了 packages.find = src，pip install -e 后正常；
# 但仓库直接 pytest 时也要可跑）
_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
if _src.exists() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
