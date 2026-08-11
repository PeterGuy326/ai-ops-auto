"""Long-running scheduler process.

The API only serves control-plane requests. This service owns APScheduler and
the durable PublishJob scanner so Gunicorn/Uvicorn web worker counts cannot
multiply scheduled side effects.
"""
from __future__ import annotations

import asyncio

from ..core.db import require_database_at_head
from ..observability import get_logger, init_observability
from ..observability.sentry import capture_exception
from .queue import queue
from .runtime import run_worker_loop

logger = get_logger(__name__)


def _register_periodic_jobs() -> None:
    """Register non-publish cron jobs on the single scheduler owner."""
    registrations = (
        ("account health", "ai_ops.scheduler.health", "schedule_daily_health_check"),
        ("reports", "ai_ops.reports.cron", "schedule_report_crons"),
    )
    for label, module_name, function_name in registrations:
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
        except Exception as exc:
            logger.exception("scheduler service: %s registration failed", label)
            capture_exception(
                exc,
                scope="scheduler.service.register",
                registration=label,
            )


async def run_scheduler_service(
    *,
    poll_seconds: float | None = None,
    stop_event: asyncio.Event | None = None,
    limit: int = 100,
) -> None:
    """Own APScheduler and continuously recover due database jobs."""
    init_observability()
    # Worker 与 API 使用同一 schema 启动闸门：默认只接受 Alembic head；
    # dev 显式 AUTO_UPGRADE_DB=true 时才允许走安全自动升级。
    require_database_at_head()
    queue.start()
    _register_periodic_jobs()
    try:
        await run_worker_loop(
            poll_seconds=poll_seconds,
            stop_event=stop_event,
            limit=limit,
        )
    finally:
        queue.shutdown()
