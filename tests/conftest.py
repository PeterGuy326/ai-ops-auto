"""pytest 全局 fixtures。

为 SQLAlchemy ORM 单测准备隔离 in-memory SQLite engine。
不污染项目默认 sqlite 文件。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 让所有运行时 sidecar/lock 都进入本轮独立临时目录，不污染仓库 data/。
# Settings 对应的真实环境变量名是 DATA_DIR；旧的 AI_OPS_TEST_DATA_DIR
# 不会被 pydantic-settings 读取，测试仍会误写 ./data。
_tmp_data = Path(tempfile.mkdtemp(prefix="ai_ops_test_data_"))
os.environ.setdefault("DATA_DIR", str(_tmp_data))

# 确保 src 在 path 上（pyproject 配了 packages.find = src，pip install -e 后正常；
# 但仓库直接 pytest 时也要可跑）
_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
if _src.exists() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
