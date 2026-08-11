"""发布时间打散（反风控的最便宜也最关键的一刀）。

为什么需要：
  - 风控会盯"机器规律"——固定整点、固定间隔、凌晨 0-6 点发布都是死签名
  - 我们的调度系统默认按 PublishJob.scheduled_at 触发，需要在触发前加 jitter
  - 同时把凌晨 0-6 这段"人少 + 算法降权"时间整体推到 7 点之后

设计取舍：
  - 本函数只计算时间，是否持久化由调用方决定
  - Phase 0 调度器会将实际触发时间写回 PublishJob.scheduled_at，
    使数据库恢复与内存 callback 遵守同一时间点
  - 默认窗口 0-600 秒（10 分钟），可配
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import settings


# 凌晨保护时段：[0:00, 7:00) 内的计划时间整体推到 7:00 之后
_NIGHT_START = time(0, 0)
_NIGHT_END = time(7, 0)


def _to_business_time(dt: datetime) -> datetime:
    """Interpret project-naive datetimes as UTC, then convert to business time."""
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(
        ZoneInfo(getattr(settings, "scheduler_timezone", "Asia/Shanghai"))
    )


def _from_business_time(local_dt: datetime, original: datetime) -> datetime:
    """Return a local adjustment in the same representation as the input."""
    utc_dt = local_dt.astimezone(timezone.utc)
    if original.tzinfo is None:
        return utc_dt.replace(tzinfo=None)
    return utc_dt.astimezone(original.tzinfo)


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
    local_dt = _to_business_time(dt)
    t = local_dt.time()
    if _NIGHT_START <= t < _NIGHT_END:
        target = local_dt.replace(hour=7, minute=0, second=0, microsecond=0)
        target += timedelta(seconds=random.randint(0, 30 * 60))
        return _from_business_time(target, dt)
    return dt


def is_safe_publish_window(dt: datetime) -> bool:
    """快速判断是否落在小红书算法友好窗口。

    友好窗口：早 7-9 / 午 12-14 / 晚 19-22。其它时段算正常但非高峰。
    不友好（返回 False）：凌晨 0-6。
    """
    t = _to_business_time(dt).time()
    return not (_NIGHT_START <= t < _NIGHT_END)
