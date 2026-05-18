"""DEPRECATED — 已切 ai_ops.notify.report_ready，本文件保留作为兜底。

历史背景：
  上轮 sprint 拆分 A↔B 交付时本 stub 解耦了任务依赖。B 模块的真函数
  上线后，A 的所有 import（cli_commands / daily / weekly / reports/__init__.py）
  已在 TD-A3 收口中切换到 `from ai_ops.notify import report_ready`。

保留原因（不直接删）：
  - 防止存量外部脚本仍引用 `ai_ops.reports.notifier_stub.report_ready` 时炸 ImportError
  - 保留 tests/test_reports.py / tests/test_notify.py 的签名兼容性回归用例

下一步：
  下个清理 sprint 确认全网无引用后删除本文件，同时移除
  tests/test_reports.py 中 `test_report_ready_stub_no_raise` 和
  tests/test_notify.py 中的 stub 签名对比用例。

接口签名（与 ai_ops.notify.report_ready 锁死）：
  report_ready(kind: Literal["daily", "weekly"], path: str | Path) -> None
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def report_ready(kind: str, path: str) -> None:
    """[DEPRECATED] 通知钩子的 stub 版本。

    新代码请用 `from ai_ops.notify import report_ready`。
    本函数仅为兼容性兜底，行为：打 INFO 日志 + stderr 提示，不发飞书。
    """
    msg = f"[report_ready][stub-deprecated] kind={kind} path={path}"
    logger.info(msg)
    # stderr 输出便于 cron / docker 日志中可见
    print(msg, file=sys.stderr, flush=True)
