"""pytest 全局 fixtures。

为 SQLAlchemy ORM 单测准备隔离 in-memory SQLite engine。
不污染项目默认 sqlite 文件。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

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


@pytest.fixture
def root_log_records():
    """Collect every record that reaches the root logger, as formatted text.

    pytest's logging plugin swaps ``sys.stdout`` and attaches its own handler, so
    a test that redirects file descriptors 1/2 never sees log output.  Records do
    always pass through the root logger's handlers, so collect them there.
    """
    import logging

    class _Sink(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(self.format(record))

    sink = _Sink()
    sink.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root = logging.getLogger()
    root.addHandler(sink)
    try:
        yield sink.lines
    finally:
        root.removeHandler(sink)
        sink.close()


@pytest.fixture
def httpx_logger_unconfigured():
    """Start from a process that never configured the ``httpx`` logger.

    ``setup_logging`` pins it at WARNING, and other tests in the same session may
    have run it.  A test about "the adapter protects itself without any logging
    setup" must begin from ``NOTSET`` or it passes for the wrong reason.
    """
    import logging

    logger = logging.getLogger("httpx")
    previous = logger.level
    root_previous = logging.getLogger().level
    root_handlers = list(logging.getLogger().handlers)
    logger.setLevel(logging.NOTSET)
    try:
        yield logger
    finally:
        logger.setLevel(previous)
        logging.getLogger().setLevel(root_previous)
        for handler in list(logging.getLogger().handlers):
            if handler not in root_handlers:
                logging.getLogger().removeHandler(handler)
                handler.close()
