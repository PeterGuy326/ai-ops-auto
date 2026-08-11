from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ai_ops.scheduler.queue import TaskQueue


async def test_date_job_runs_inside_scheduler_event_loop():
    """Regression: callbacks must not be dispatched to a thread without a loop."""
    queue = TaskQueue()
    scheduler_loop = asyncio.get_running_loop()
    completed = asyncio.Event()

    async def callback():
        assert asyncio.get_running_loop() is scheduler_loop
        completed.set()

    queue.start()
    try:
        queue.schedule_once(
            datetime.utcnow() + timedelta(milliseconds=20),
            callback,
            job_id="event-loop-regression",
        )
        await asyncio.wait_for(completed.wait(), timeout=2)
    finally:
        queue.shutdown()


def test_naive_project_datetime_is_normalized_to_aware_utc():
    queue = TaskQueue()
    captured = {}

    class Job:
        id = "utc-normalization"

    def add_job(*args, **kwargs):
        captured.update(kwargs)
        return Job()

    queue._scheduler.add_job = add_job

    async def callback():
        return None

    naive_utc = datetime(2026, 8, 10, 2, 30)
    queue.schedule_once(naive_utc, callback)

    assert captured["run_date"] == naive_utc.replace(tzinfo=timezone.utc)
