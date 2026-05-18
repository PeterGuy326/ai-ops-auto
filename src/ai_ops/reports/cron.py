"""APScheduler 注册 — 被 api/main.py lifespan 调用。

调度策略：
- daily : 每日 18:00 → cron(hour=18, minute=0)
- weekly: 每周一 09:00 → cron(day_of_week='mon', hour=9, minute=0)

实现说明：
  使用显式 day_of_week='mon' 字面量，比 5 段 cron 字符串更明确，
  阅读时一眼能看出"周一"语义。TaskQueue.schedule_cron 自身已根治
  Linux cron→APScheduler dow 语义错位（TD-A1 已收口，见 tests/test_queue_cron.py），
  本文件保留显式字面量是出于可读性，不是 workaround。
"""
from __future__ import annotations

import asyncio

from ..scheduler.queue import queue
from .daily import run_daily_report_job
from .weekly import run_weekly_report_job


def schedule_report_crons(
    daily_hour: int = 18,
    daily_minute: int = 0,
    weekly_dow: str = "mon",
    weekly_hour: int = 9,
    weekly_minute: int = 0,
) -> tuple[str, str]:
    """注册 daily / weekly 两个 cron job，返回 (daily_id, weekly_id)。

    job_id 固定（replace_existing=True 保证重启幂等）。
    """
    scheduler = queue._scheduler  # 直接用底层 AsyncIOScheduler 避开 queue 的 cron 字符串歧义

    did_job = scheduler.add_job(
        lambda: asyncio.create_task(run_daily_report_job()),
        trigger="cron",
        hour=daily_hour,
        minute=daily_minute,
        id="report-daily",
        replace_existing=True,
    )
    wid_job = scheduler.add_job(
        lambda: asyncio.create_task(run_weekly_report_job()),
        trigger="cron",
        day_of_week=weekly_dow,
        hour=weekly_hour,
        minute=weekly_minute,
        id="report-weekly",
        replace_existing=True,
    )
    return did_job.id, wid_job.id
