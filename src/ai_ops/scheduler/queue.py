"""任务调度后端。第一版 APScheduler，可平滑切 Celery。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .jitter import jitter_publish_time


class TaskQueue:
    """对调度后端的薄壳，业务代码不直接依赖 APScheduler。"""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def schedule_once(
        self,
        when: datetime,
        coro_factory: Callable[[], Coroutine],
        job_id: str | None = None,
    ) -> str:
        job = self._scheduler.add_job(
            lambda: asyncio.create_task(coro_factory()),
            trigger="date",
            run_date=when,
            id=job_id,
            replace_existing=True,
        )
        return job.id

    def schedule_cron(
        self,
        cron: str,
        coro_factory: Callable[[], Coroutine],
        job_id: str | None = None,
    ) -> str:
        # cron 字符串："分 时 日 月 周"，例如 "0 9 * * *"
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(f"非法 cron：{cron}")
        kwargs = dict(zip(("minute", "hour", "day", "month", "day_of_week"), parts))
        job = self._scheduler.add_job(
            lambda: asyncio.create_task(coro_factory()),
            trigger="cron",
            id=job_id,
            replace_existing=True,
            **kwargs,
        )
        return job.id

    def cancel(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def schedule_publish(
        self,
        planned: datetime,
        coro_factory: Callable[[], Coroutine],
        job_id: str | None = None,
        *,
        avoid_night: bool = True,
    ) -> tuple[str, datetime]:
        """带 jitter + 凌晨保护的发布调度。返回 (调度ID, 实际触发时间)。

        与 schedule_once 区别：本方法专为 PublishJob 设计，规避风控对"机器规律"的检测。
        """
        actual = jitter_publish_time(planned, avoid_night=avoid_night)
        sid = self.schedule_once(actual, coro_factory, job_id=job_id)
        return sid, actual


queue = TaskQueue()
