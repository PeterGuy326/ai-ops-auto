"""关键模块 import 防回归（TD-A2 根治验证）。

历史教训（上轮 sprint）：
  api/main.py 走 fastapi.templating → 拉 jinja2，但 pyproject.toml 没声明依赖。
  上轮靠 `pip install jinja2` 环境补丁通过验收，新环境装机会爆。
  本测试守护：在干净环境跑一次 pytest，import 任意关键路径都不应该 ImportError。

策略：只测 import 不测语义——语义有专门的测试文件覆盖；本文件只防"依赖漏声明"。
"""
from __future__ import annotations


def test_api_main_imports():
    """api/main.py 链路完整可 import，FastAPI app 实例存在。"""
    from ai_ops.api import main
    assert main.app is not None
    assert main.app.title == "ai-ops-auto"


def test_notify_imports():
    """ai_ops.notify 公共 API（B 模块）import 完整。"""
    from ai_ops import notify
    for name in (
        "publish_success", "publish_failed", "account_expired",
        "report_ready", "content_taint", "fanout_done",
    ):
        assert hasattr(notify, name), f"notify 缺导出：{name}"


def test_reports_imports():
    """ai_ops.reports 公共 API（A 模块）import 完整。"""
    from ai_ops import reports
    for name in (
        "build_daily_report", "write_daily_report", "run_daily_report_job",
        "build_weekly_report", "write_weekly_report", "run_weekly_report_job",
        "report_ready",
    ):
        assert hasattr(reports, name), f"reports 缺导出：{name}"


def test_scheduler_queue_imports():
    """ai_ops.scheduler.queue 模块级 queue 单例存在（TD-A1 改造后）。"""
    from ai_ops.scheduler.queue import queue, TaskQueue
    assert queue is not None
    assert isinstance(queue, TaskQueue)
    # TD-A1 helper 函数也要可用
    assert hasattr(TaskQueue, "_translate_linux_dow")
