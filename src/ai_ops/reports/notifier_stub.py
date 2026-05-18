"""通知 stub — Task B 完成后将被 `from ai_ops.notify import report_ready` 替换。

接口签名锁死：`report_ready(kind: str, path: str) -> None`
- kind: "daily" | "weekly"
- path: 报告文件绝对路径

之所以独立 stub：解耦本 sprint Task A / Task B 的交付依赖。
B 完成后只需把 cli_commands.py / cron.py 的 import 从 `notifier_stub` 切到 `notify` 即可。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def report_ready(kind: str, path: str) -> None:
    """报告生成完成的通知钩子（stub 版）。

    当前实现：打 INFO 日志 + stderr 提示。
    真实实现（Task B）：飞书/企业微信 webhook 推送。
    """
    msg = f"[report_ready][stub] kind={kind} path={path}"
    logger.info(msg)
    # stderr 输出便于 cron / docker 日志中可见
    print(msg, file=sys.stderr, flush=True)
