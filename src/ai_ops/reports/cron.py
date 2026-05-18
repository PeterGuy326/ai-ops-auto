"""APScheduler 注册 — 被 api/main.py lifespan 调用。

调度策略：
- daily : 每日 18:00 → cron(hour=18, minute=0)
- weekly: 每周一 09:00 → cron(day_of_week='mon', hour=9, minute=0)

实现说明（避坑）：
  不走 scheduler.queue.TaskQueue.schedule_cron 的 5 段字符串路径。
  原因：queue.schedule_cron 把 "0 9 * * 1" 透传给 APScheduler 时，
  APScheduler 的 day_of_week 语义是 mon=0..sun=6，与 Linux cron 的 sun=0..sat=6 错位，
  会把 "* * 1" 解析为周二而非周一。
  → 用 APScheduler 原生 day_of_week='mon' 字面量最稳。
  详见 P7-COMPLETION 中的"技术债"段。
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
