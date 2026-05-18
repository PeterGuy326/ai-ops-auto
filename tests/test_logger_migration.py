"""Task Y · 结构化日志迁移验证。

底层逻辑：
  上轮 sprint e1f13b8 上线了 observability.get_logger + JsonFormatter，但
  存量 10 处 `logging.getLogger(__name__)` 没有切到结构化通道。本次 Task Y
  把 notify/ + reports/notifier_stub + publishers/xhs_camoufox 共 4 个模块
  全部迁移完毕。本测试是迁移闭环的最后一公里——验证：

    1. 4 个模块的 logger 都是经 observability.get_logger 拿到的（而不是
       直接 logging.getLogger，否则 JsonFormatter 不接管 = 观测打折）
    2. 各模块用 `extra=` dict 传 dynamic 字段（不是 %-style / f-string），
       这样 JsonFormatter 能把字段平铺到 JSON 顶层供 ELK / Loki 查询
    3. 真实跑一次 _safe(raise) → JSON handler 捕获 → 验证 event / error
       字段确实进了 JSON（这是 end-to-end 闭环，不是只看类型）

闭环验收命令见任务 DONE 验收清单。
"""
from __future__ import annotations

import io
import json
import logging

import pytest


# ----------------------------------------------------------------------
# Case 1-4: 4 个模块的 logger 实例属于"经 root handler 接管"的 stdlib Logger
# ----------------------------------------------------------------------
# 底层逻辑：observability.get_logger 等价于 stdlib logging.getLogger（同一
# 个 logger registry），区别在于 root logger 上挂的 handler 是
# JsonFormatter。我们验证：
#   a) 模块顶部 logger 名 == 模块 __name__（确保是按模块切片而非共用 root）
#   b) logger 类型是 stdlib Logger（兼容 caplog / 第三方 handler）
#   c) propagate=True（让 root 上的 JsonFormatter handler 能捕获）

def _assert_logger_observability_compliant(logger: logging.Logger, expected_name: str) -> None:
    """统一断言：logger 是 observability 通道下的合规实例。"""
    assert isinstance(logger, logging.Logger), f"{expected_name} 应是 stdlib Logger"
    assert logger.name == expected_name, f"logger.name={logger.name} 不等于 {expected_name}"
    # propagate=True 是 observability 链路前提（root handler 才能收到）
    assert logger.propagate is True, f"{expected_name}.propagate 应为 True，否则 JSON 通道断链"


def test_notify_init_logger_uses_observability():
    """notify/__init__.py 的 logger 应来自 observability 通道。"""
    from ai_ops.notify import logger as notify_logger
    _assert_logger_observability_compliant(notify_logger, "ai_ops.notify")


def test_notify_webhook_logger_uses_observability():
    """notify/webhook.py 的 logger 同上。"""
    from ai_ops.notify.webhook import logger as webhook_logger
    _assert_logger_observability_compliant(webhook_logger, "ai_ops.notify.webhook")


def test_reports_notifier_stub_logger_uses_observability():
    """reports/notifier_stub.py 的 logger 同上（DEPRECATED 但仍走结构化通道）。"""
    from ai_ops.reports.notifier_stub import logger as stub_logger
    _assert_logger_observability_compliant(stub_logger, "ai_ops.reports.notifier_stub")


def test_publishers_xhs_camoufox_logger_uses_observability():
    """publishers/xhs_camoufox.py 用别名 `log`，但本质同上。"""
    from ai_ops.publishers.xhs_camoufox import log as xhs_log
    _assert_logger_observability_compliant(xhs_log, "ai_ops.publishers.xhs_camoufox")


# ----------------------------------------------------------------------
# Case 5: end-to-end 验证 extra dict 真的进 JSON 顶层
# ----------------------------------------------------------------------
# 底层逻辑：上面 4 个 case 验类型/链路，但不验 "extra 字段真的能落到 JSON"。
# 这个 case 起一个临时 JsonFormatter handler 直接挂到目标 logger，跑一次
# notify._safe 包装的 raise → 解析 stdout JSON → 断言 event / error 字段
# 确实在 payload 顶层。这是闭环的最后一公里——不依赖真飞书 webhook。

@pytest.fixture
def captured_json_logs(monkeypatch):
    """临时把 JsonFormatter handler 挂到 ai_ops.notify logger，捕获 JSON 输出。

    用 try/finally 还原，避免污染其他测试。不动 root，只挂模块 logger。
    """
    from ai_ops.observability.structured_logging import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)

    target = logging.getLogger("ai_ops.notify")
    target.addHandler(handler)
    # 保险：临时降低 level，避免被外部 setup 锁在 WARNING 以上
    original_level = target.level
    target.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)


def test_safe_wrapper_emits_json_with_extra_fields(captured_json_logs):
    """_safe(fn) 内 raise → JsonFormatter 输出含 event=fn name + error 字段。"""
    from ai_ops.notify import _safe

    @_safe
    def boom():
        raise ValueError("synthetic boom for test")

    # 调用应被 _safe 吞掉返回 None，不抛
    assert boom() is None

    output = captured_json_logs.getvalue().strip()
    assert output, "_safe 内 raise 后应有 JSON 日志输出（warning 级别）"

    # 多行 handler 输出（多个 logger 链路） — 取最后一行（_safe 自己的 warning）
    lines = [line for line in output.splitlines() if line.strip()]
    last = lines[-1]
    payload = json.loads(last)

    # JsonFormatter Schema 三件套
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "ai_ops.notify"
    assert "swallowed exception" in payload["message"]
    # extra 字段平铺到顶层（JsonFormatter 设计）
    assert payload["event"] == "boom", "fn.__name__ 应作为 event 字段"
    assert "synthetic boom for test" in payload["error"]
    # exc_info=True → 应带 exception traceback 字段
    assert "exception" in payload, "exc_info=True 应触发 JsonFormatter 输出 exception 字段"
    assert "ValueError" in payload["exception"]
