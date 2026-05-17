"""发布时间打散（反风控的最便宜也最关键的一刀）。

为什么需要：
  - 风控会盯"机器规律"——固定整点、固定间隔、凌晨 0-6 点发布都是死签名
  - 我们的调度系统默认按 PublishJob.scheduled_at 触发，需要在触发前加 jitter
  - 同时把凌晨 0-6 这段"人少 + 算法降权"时间整体推到 7 点之后

设计取舍：
  - 不修改 PublishJob.scheduled_at 落库值（保留"计划时间"）
  - 只对"实际触发时间"做位移
  - 默认窗口 0-600 秒（10 分钟），可配
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from ..config import settings


# 凌晨保护时段：[0:00, 7:00) 内的计划时间整体推到 7:00 之后
_NIGHT_START = time(0, 0)
_NIGHT_END = time(7, 0)


def jitter_publish_time(
    planned: datetime,
    *,
    max_jitter_seconds: int | None = None,
    avoid_night: bool = True,
) -> datetime:
    """对计划发布时间做 jitter + 凌晨保护。

    Args:
        planned: 计划发布时间（PublishJob.scheduled_at）
        max_jitter_seconds: 上抖动窗口。None 时取 settings.publish_jitter_seconds
        avoid_night: 是否把 [0:00, 7:00) 计划时间推到 7:00+

    Returns:
        实际应该触发的时间。
    """
    if planned is None:
        return planned  # caller 决定怎么处理

    window = (
        max_jitter_seconds
        if max_jitter_seconds is not None
        else getattr(settings, "publish_jitter_seconds", 600)
    )
    window = max(0, int(window))
    offset = random.randint(0, window) if window > 0 else 0
    actual = planned + timedelta(seconds=offset)

    if avoid_night:
        actual = _push_out_of_night(actual)

    return actual


def _push_out_of_night(dt: datetime) -> datetime:
    """如果落在凌晨 0:00-7:00 这段，整体推到 7:00 之后再加 0-30 分钟 jitter。"""
    t = dt.time()
    if _NIGHT_START <= t < _NIGHT_END:
        target = dt.replace(hour=7, minute=0, second=0, microsecond=0)
        target += timedelta(seconds=random.randint(0, 30 * 60))
        return target
    return dt


def is_safe_publish_window(dt: datetime) -> bool:
    """快速判断是否落在小红书算法友好窗口。

    友好窗口：早 7-9 / 午 12-14 / 晚 19-22。其它时段算正常但非高峰。
    不友好（返回 False）：凌晨 0-6。
    """
    t = dt.time()
    return not (_NIGHT_START <= t < _NIGHT_END)
