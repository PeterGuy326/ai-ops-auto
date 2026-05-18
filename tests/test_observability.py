"""Task G · 可观测性单测。

覆盖：
  - structured_logging：JSON formatter 输出含 timestamp/level/logger/message/extra
  - structured_logging：text formatter 不破坏现有阅读体验
  - sentry：空 dsn 静默跳过返回 False
  - sentry：dsn 但 sentry-sdk 未装时返回 False 且不报错
  - init_observability：幂等（多次调用只生效一次）
  - get_logger：返回 stdlib Logger 实例（保证 API 兼容）
"""
from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from ai_ops import observability as obs_mod
from ai_ops.observability import get_logger, init_observability
from ai_ops.observability.sentry import init_sentry
from ai_ops.observability.structured_logging import JsonFormatter, setup_logging


@pytest.fixture(autouse=True)
def _reset_obs():
    """每个 test 前后重置 _initialized 标志，避免幂等导致后续 test 跑不上。"""
    obs_mod._reset_for_test()
    yield
    obs_mod._reset_for_test()


class TestJsonFormatter:
    def test_basic_fields(self):
        """LogRecord 序列化含 timestamp/level/logger/message 四基础字段。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="x.py",
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        out = formatter.format(record)
        data = json.loads(out)
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "hello world"
        assert "timestamp" in data
        # ISO 8601 + Z 后缀
        assert data["timestamp"].endswith("Z")

    def test_extra_fields_flattened(self):
        """extra 字段平铺到顶层 JSON。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="x.py", lineno=1,
            msg="event", args=(), exc_info=None,
        )
        # 模拟 logger.info("...", extra={"job_id": 42, "account_id": 7})
        record.job_id = 42
        record.account_id = 7
        out = formatter.format(record)
        data = json.loads(out)
        assert data["job_id"] == 42
        assert data["account_id"] == 7

    def test_exception_included(self):
        """exc_info 时追加 exception 字段。"""
        formatter = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="x.py", lineno=1,
            msg="failed", args=(), exc_info=exc_info,
        )
        out = formatter.format(record)
        data = json.loads(out)
        assert "exception" in data
        assert "ValueError" in data["exception"]
        assert "boom" in data["exception"]

    def test_non_jsonable_extra_falls_back_to_str(self):
        """extra 含不可 JSON 序列化对象（如自定义类）时退化为 str，不抛。"""
        formatter = JsonFormatter()

        class Weird:
            def __repr__(self):
                return "<Weird>"

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="x.py", lineno=1,
            msg="m", args=(), exc_info=None,
        )
        record.obj = Weird()
        out = formatter.format(record)
        data = json.loads(out)
        assert data["obj"] == "<Weird>"


class TestSetupLogging:
    def test_json_mode_outputs_json(self, capsys):
        """log_format=json 时，stdout 是 JSON 单行。"""
        setup_logging(log_format="json", log_level="INFO")
        log = logging.getLogger("ai_ops.test.json")
        log.info("hello", extra={"foo": "bar"})

        captured = capsys.readouterr().out.strip().splitlines()
        # 找包含 "hello" 的那行
        json_line = next((line for line in captured if "hello" in line), None)
        assert json_line is not None, f"no 'hello' line found in: {captured}"
        data = json.loads(json_line)
        assert data["message"] == "hello"
        assert data["foo"] == "bar"
        assert data["level"] == "INFO"

    def test_text_mode_human_readable(self, capsys):
        """log_format=text 时，输出是人类可读格式（不是 JSON）。"""
        setup_logging(log_format="text", log_level="INFO")
        log = logging.getLogger("ai_ops.test.text")
        log.info("hello text mode")
        captured = capsys.readouterr().out
        assert "hello text mode" in captured
        # text 模式应该不是合法 JSON
        line = next((l for l in captured.strip().splitlines() if "hello text mode" in l), "")
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_log_level_filtering(self, capsys):
        """log_level=WARNING 时，INFO 日志被过滤掉。"""
        setup_logging(log_format="text", log_level="WARNING")
        log = logging.getLogger("ai_ops.test.level")
        log.info("should be filtered")
        log.warning("should appear")
        captured = capsys.readouterr().out
        assert "should be filtered" not in captured
        assert "should appear" in captured


class TestInitSentry:
    def test_empty_dsn_returns_false(self):
        """空 dsn → 静默跳过返回 False。"""
        assert init_sentry(dsn="", environment="dev", release="test") is False

    def test_dsn_without_sdk_returns_false(self, monkeypatch):
        """配了 dsn 但 sentry-sdk 未装 → False + warning，不抛。"""
        from ai_ops.observability import sentry as sentry_mod
        # mock _sentry_sdk_available 返回 False，模拟未装
        monkeypatch.setattr(sentry_mod, "_sentry_sdk_available", lambda: False)
        result = sentry_mod.init_sentry(
            dsn="https://fake@sentry.io/1", environment="dev", release="test"
        )
        assert result is False

    def test_dsn_with_sdk_init_failure_swallowed(self, monkeypatch):
        """sentry-sdk 装了但 init 抛异常 → 吞掉返回 False。"""
        from ai_ops.observability import sentry as sentry_mod

        monkeypatch.setattr(sentry_mod, "_sentry_sdk_available", lambda: True)

        class FakeSentry:
            @staticmethod
            def init(**kw):
                raise RuntimeError("network down")

        import sys
        monkeypatch.setitem(sys.modules, "sentry_sdk", FakeSentry)
        result = sentry_mod.init_sentry(
            dsn="https://fake@sentry.io/1", environment="dev", release="test"
        )
        assert result is False


class TestInitObservability:
    def test_idempotent(self, capsys):
        """多次调用 init_observability 只生效一次（避免 handler 重复挂）。"""
        init_observability(log_format="text", log_level="INFO")
        first = capsys.readouterr().out
        init_observability(log_format="text", log_level="INFO")
        init_observability(log_format="text", log_level="INFO")
        # 第二次开始不应再输出 "observability initialized"
        after = capsys.readouterr().out
        assert after == "", f"expected no output on re-init, got: {after!r}"

    def test_get_logger_returns_stdlib_logger(self):
        """get_logger 必须返回 stdlib Logger 实例（API 兼容）。"""
        log = get_logger("ai_ops.test.compat")
        assert isinstance(log, logging.Logger)
        # 现有代码用 logger.warning(...) 这种 stdlib API 必须可用
        assert callable(log.warning)
        assert callable(log.info)
        assert callable(log.error)

    def test_init_with_json_format(self, capsys):
        """init_observability(log_format='json') → 日志出 JSON。"""
        init_observability(log_format="json", log_level="INFO")
        log = get_logger("ai_ops.test.init_json")
        log.info("init_test", extra={"task": "G"})
        captured = capsys.readouterr().out
        # 找 init_test 那行
        line = next((l for l in captured.strip().splitlines() if "init_test" in l), None)
        assert line is not None
        data = json.loads(line)
        assert data["message"] == "init_test"
        assert data["task"] == "G"
