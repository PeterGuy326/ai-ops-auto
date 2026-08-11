"""Database-backed PublishJob recovery and worker loop.

APScheduler callbacks are an optimization only: this module treats the database
as the durable source of truth.  Multiple processes may scan the same due rows;
``worker.execute_job`` performs the conditional claim that guarantees at-most-one
publisher entry for each attempt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from ..config import settings
from ..core.db import session_scope
from ..core.enums import JobStatus
from ..core.models import PublishJob
from ..core.schemas import PublishResult
from ..core.time import as_utc_naive
from ..observability import get_logger
from ..observability.sentry import capture_exception

logger = get_logger(__name__)

DUE_JOB_STATUSES: tuple[JobStatus, ...] = (
    JobStatus.PENDING,
    JobStatus.RETRYING,
)


def get_due_job_ids(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[int]:
    """Return runnable job ids whose durable schedule is due.

    ``scheduled_at=None`` is the legacy representation for "publish as soon as
    possible", so it is intentionally treated as due. Exact jobs additionally
    require their immutable approved not-before timestamp to be present and due.
    The query does not claim rows; every caller still goes through
    execute_job's cross-database CAS.
    """
    cutoff = as_utc_naive(now) or datetime.utcnow()
    safe_limit = max(1, min(int(limit), 1000))
    with session_scope() as session:
        return list(
            session.scalars(
                select(PublishJob.id)
                .where(PublishJob.status.in_(DUE_JOB_STATUSES))
                .where(
                    or_(
                        PublishJob.plan_id.is_(None),
                        and_(
                            PublishJob.approved_planned_for.is_not(None),
                            PublishJob.approved_planned_for <= cutoff,
                        ),
                    )
                )
                .where(
                    or_(
                        PublishJob.scheduled_at.is_(None),
                        PublishJob.scheduled_at <= cutoff,
                    )
                )
                .order_by(
                    PublishJob.scheduled_at.asc().nullsfirst(),
                    PublishJob.id.asc(),
                )
                .limit(safe_limit)
            ).all()
        )


def get_stale_running_job_ids(
    *,
    now: datetime | None = None,
    stale_after_seconds: float | None = None,
    limit: int = 100,
) -> tuple[list[int], datetime]:
    """Return abandoned RUNNING rows and the cutoff used for their CAS."""
    cutoff_now = as_utc_naive(now) or datetime.utcnow()
    timeout = (
        float(stale_after_seconds)
        if stale_after_seconds is not None
        else float(getattr(settings, "job_running_timeout_seconds", 7200))
    )
    cutoff = cutoff_now - timedelta(seconds=max(1.0, timeout))
    safe_limit = max(1, min(int(limit), 1000))
    with session_scope() as session:
        ids = session.scalars(
            select(PublishJob.id)
            .where(PublishJob.status == JobStatus.RUNNING)
            .where(
                or_(
                    PublishJob.started_at.is_(None),
                    PublishJob.started_at <= cutoff,
                )
            )
            .order_by(
                PublishJob.started_at.asc().nullsfirst(),
                PublishJob.id.asc(),
            )
            .limit(safe_limit)
        ).all()
    return list(ids), cutoff


def reconcile_stale_running_jobs(
    *,
    now: datetime | None = None,
    stale_after_seconds: float | None = None,
    limit: int = 100,
) -> list[int]:
    """Move abandoned RUNNING jobs to explicit, operator-reviewable failure.

    We deliberately do not put these rows back into RETRYING: the platform may
    already have accepted the post before the process died.
    """
    from .worker import INTERRUPTED_EXECUTION_ERROR, mark_running_job_uncertain

    job_ids, cutoff = get_stale_running_job_ids(
        now=now,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
    )
    reconciled: list[int] = []
    for job_id in job_ids:
        if mark_running_job_uncertain(
            job_id,
            INTERRUPTED_EXECUTION_ERROR,
            now=now,
            started_before=cutoff,
        ):
            reconciled.append(job_id)
    if reconciled:
        logger.error(
            "scheduler reconciled interrupted publishing jobs",
            extra={"job_ids": reconciled, "count": len(reconciled)},
        )
    return reconciled


async def scan_due_jobs(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[int, PublishResult]:
    """Recover and execute one bounded batch of due jobs.

    Automatic publishing is deny-by-default.  Explicit ``execute_job`` calls are
    separate and remain available to authenticated/manual callers.
    """
    # State repair is safe even when external publishing is disabled. It keeps
    # a hard-crashed execution from remaining RUNNING forever.
    reconcile_stale_running_jobs(now=now, limit=limit)

    if not bool(getattr(settings, "auto_publish_enabled", False)):
        logger.debug("scheduler.scan_due_jobs: auto publish disabled")
        return {}

    job_ids = get_due_job_ids(now=now, limit=limit)
    if not job_ids:
        return {}

    from .worker import execute_job

    concurrency = max(
        1,
        int(getattr(settings, "scheduler_max_concurrency", 4)),
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def execute_one(job_id: int) -> tuple[int, PublishResult]:
        async with semaphore:
            try:
                return job_id, await execute_job(job_id)
            except Exception as exc:  # one corrupt job must not abort the whole scan
                logger.exception(
                    "scheduler.scan_due_jobs: job execution crashed",
                    extra={"job_id": job_id},
                )
                capture_exception(exc, scope="scheduler.scan_due_jobs", job_id=job_id)
                return job_id, PublishResult(success=False, error=f"worker 异常: {exc}")

    pairs = await asyncio.gather(*(execute_one(job_id) for job_id in job_ids))
    return dict(pairs)


async def run_worker_loop(
    *,
    poll_seconds: float | None = None,
    stop_event: asyncio.Event | None = None,
    limit: int = 100,
) -> None:
    """Run immediate startup recovery followed by periodic database scans.

    ``stop_event`` makes the loop usable by both an API lifespan and a dedicated
    CLI worker without signal-handling policy leaking into this module.
    """
    interval = (
        float(poll_seconds)
        if poll_seconds is not None
        else float(getattr(settings, "scheduler_poll_seconds", 15))
    )
    interval = max(0.1, interval)
    stopper = stop_event or asyncio.Event()

    if not bool(getattr(settings, "auto_publish_enabled", False)):
        # Keep the process/lifespan task alive.  The dedicated worker may also
        # host non-publishing periodic work, and a disabled safety gate should
        # mean "no external action", not "worker crashed/exited".
        logger.warning("scheduler worker running with auto publish disabled")

    consecutive_failures = 0
    while not stopper.is_set():
        try:
            await scan_due_jobs(limit=limit)
            consecutive_failures = 0
            delay = interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            delay = min(interval * (2 ** min(consecutive_failures - 1, 6)), 60.0)
            logger.exception(
                "scheduler worker scan failed; retrying",
                extra={
                    "consecutive_failures": consecutive_failures,
                    "retry_in_seconds": delay,
                },
            )
            capture_exception(
                exc,
                scope="scheduler.run_worker_loop",
                consecutive_failures=consecutive_failures,
            )
        try:
            await asyncio.wait_for(stopper.wait(), timeout=delay)
        except TimeoutError:
            continue
