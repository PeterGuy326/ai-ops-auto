"""Database-backed publishing runtime reliability tests.

The cases focus on state transitions and conflict paths: duplicate claims,
terminal-state rejection, retry boundaries, restart recovery, and Article fan-out
aggregation.  Every publisher is an in-process fake; no platform is contacted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import (
    Account,
    Article,
    Base,
    Metrics,
    MetricsCollectionTask,
    PublishJob,
    Topic,
)
from ai_ops.core.schemas import PublishResult
from ai_ops.scheduler import runtime as runtime_mod
from ai_ops.scheduler import worker as worker_mod
from ai_ops.runtime import receipts as receipt_mod


@pytest.fixture
def runtime_db(tmp_path, monkeypatch):
    """Bind production SessionLocal to an isolated file SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)

    from ai_ops.accounts.manager import RateCheckResult

    monkeypatch.setattr(worker_mod, "get_credential", lambda session, account_id: {"fake": True})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda session, account_id, **kwargs: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "is_paused", lambda account: False)
    monkeypatch.setattr(worker_mod, "mark_published", lambda session, account_id: None)
    monkeypatch.setattr(worker_mod, "_pre_publish_check", lambda *args, **kwargs: (True, None))

    from ai_ops.scheduler import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda job_id: [])
    import ai_ops.notify as notify_mod

    monkeypatch.setattr(notify_mod, "publish_success", lambda snapshot: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda snapshot: None)

    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _create_article_and_jobs(
    SessionLocal,
    *,
    statuses: tuple[JobStatus, ...] = (JobStatus.PENDING,),
    attempts: int = 0,
    max_attempts: int = 3,
    scheduled_at: datetime | None = None,
    plan_id: int | None = None,
    approved_planned_for: datetime | None = None,
) -> tuple[int, list[int]]:
    with SessionLocal() as session:
        topic = Topic(
            name=f"runtime-{datetime.utcnow().timestamp()}",
            keywords=[],
            persona={},
            target_platforms=[],
        )
        session.add(topic)
        session.flush()
        article = Article(
            topic_id=topic.id,
            title="runtime reliability",
            body="safe test body",
            content_type=ContentType.LONG_ARTICLE,
            status=ArticleStatus.SCHEDULED,
            target_platforms=[],
            target_account_ids=[],
            extra={},
        )
        session.add(article)
        session.flush()

        job_ids: list[int] = []
        for index, status in enumerate(statuses):
            account = Account(
                platform=Platform.XIAOHONGSHU,
                nickname=f"runtime-account-{article.id}-{index}",
                health=AccountHealth.HEALTHY,
                profile={},
                encrypted_credential=b"",
            )
            session.add(account)
            session.flush()
            job = PublishJob(
                article_id=article.id,
                account_id=account.id,
                platform=Platform.XIAOHONGSHU,
                status=status,
                publisher_kind="fake",
                attempts=attempts,
                max_attempts=max_attempts,
                plan_id=plan_id,
                approved_planned_for=approved_planned_for,
                scheduled_at=scheduled_at,
                raw_response={},
            )
            session.add(job)
            session.flush()
            job_ids.append(job.id)
        session.commit()
        return article.id, job_ids


def test_manual_execute_cannot_run_exact_job_before_approved_time(
    runtime_db,
    monkeypatch,
):
    approved_time = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        plan_id=999,
        approved_planned_for=approved_time,
        scheduled_at=approved_time,
    )
    publisher_calls = 0

    async def should_not_publish(*args, **kwargs):
        nonlocal publisher_calls
        publisher_calls += 1
        return PublishResult(success=True)

    monkeypatch.setattr(worker_mod, "_try_publishers", should_not_publish)
    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert "尚未到审批计划执行时间" in (result.error or "")
    assert publisher_calls == 0
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.PENDING
        assert job.attempts == 0
        assert job.started_at is None


def test_due_scanner_cannot_bypass_exact_approved_time_with_mutable_schedule(
    runtime_db,
):
    now = datetime.now(UTC).replace(tzinfo=None)
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        plan_id=1000,
        approved_planned_for=now + timedelta(hours=1),
        scheduled_at=now - timedelta(minutes=1),
    )

    assert job_id not in runtime_mod.get_due_job_ids(now=now)
    assert job_id in runtime_mod.get_due_job_ids(now=now + timedelta(hours=1))


def test_exact_retry_moves_only_mutable_schedule(runtime_db, monkeypatch):
    approved_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.RUNNING,),
        attempts=1,
        plan_id=1002,
        approved_planned_for=approved_time,
        scheduled_at=approved_time,
    )
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_retry_base_seconds=10),
    )
    finished_at = datetime.now(UTC).replace(tzinfo=None)

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        worker_mod._finish_failed_attempt(
            session,
            job,
            "temporary exact failure",
            now=finished_at,
        )
        session.commit()

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.RETRYING
        assert job.approved_planned_for == approved_time
        assert job.scheduled_at == finished_at + timedelta(seconds=10)


def test_manual_execute_keeps_legacy_future_job_compatibility(runtime_db, monkeypatch):
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        scheduled_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
    )

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="manual-legacy")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    with runtime_db() as session:
        assert session.get(PublishJob, job_id).status == JobStatus.SUCCESS


def test_concurrent_execute_only_one_call_enters_publisher(runtime_db, monkeypatch):
    _, (job_id,) = _create_article_and_jobs(runtime_db)
    entered = asyncio.Event()
    release = asyncio.Event()
    publisher_calls = 0

    async def slow_success(*args, **kwargs):
        nonlocal publisher_calls
        publisher_calls += 1
        entered.set()
        await release.wait()
        return PublishResult(success=True, platform_post_id="only-once")

    monkeypatch.setattr(worker_mod, "_try_publishers", slow_success)

    async def run_both():
        first = asyncio.create_task(worker_mod.execute_job(job_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        duplicate = await worker_mod.execute_job(job_id)
        release.set()
        winner = await first
        return winner, duplicate

    winner, duplicate = asyncio.run(run_both())
    assert winner.success is True
    assert duplicate.success is False
    assert "不可执行" in (duplicate.error or "")
    assert publisher_calls == 1

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.SUCCESS
        assert job.attempts == 1


def test_same_account_jobs_are_serialized_before_publisher(runtime_db, monkeypatch):
    """Different jobs for one account must not bypass interval/quota admission."""
    _, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.PENDING, JobStatus.PENDING),
    )
    first_id, second_id = job_ids
    with runtime_db() as session:
        first = session.get(PublishJob, first_id)
        second = session.get(PublishJob, second_id)
        second.account_id = first.account_id
        session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    publisher_calls = 0

    async def slow_success(*args, **kwargs):
        nonlocal publisher_calls
        publisher_calls += 1
        entered.set()
        await release.wait()
        return PublishResult(success=True, platform_post_id="serialized")

    monkeypatch.setattr(worker_mod, "_try_publishers", slow_success)

    async def exercise():
        first_task = asyncio.create_task(worker_mod.execute_job(first_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        blocked = await worker_mod.execute_job(second_id)
        release.set()
        completed = await first_task
        return completed, blocked

    completed, blocked = asyncio.run(exercise())

    assert completed.success is True
    assert blocked.success is False
    assert publisher_calls == 1
    with runtime_db() as session:
        assert session.get(PublishJob, first_id).status == JobStatus.SUCCESS
        assert session.get(PublishJob, second_id).status == JobStatus.PENDING


@pytest.mark.parametrize(
    "status",
    [JobStatus.RUNNING, JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.DEAD],
)
def test_terminal_or_running_job_is_rejected(runtime_db, monkeypatch, status):
    _, (job_id,) = _create_article_and_jobs(runtime_db, statuses=(status,))
    calls = 0

    async def should_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return PublishResult(success=True)

    monkeypatch.setattr(worker_mod, "_try_publishers", should_not_run)
    result = asyncio.run(worker_mod.execute_job(job_id))
    assert result.success is False
    assert calls == 0
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == status
        assert job.attempts == 0


def test_failure_sets_exponential_retry_schedule_and_scanner_recovers(runtime_db, monkeypatch):
    _, (job_id,) = _create_article_and_jobs(runtime_db)
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_retry_base_seconds=10, auto_publish_enabled=True),
    )

    async def fail(*args, **kwargs):
        return PublishResult(success=False, error="temporary outage")

    monkeypatch.setattr(worker_mod, "_try_publishers", fail)
    asyncio.run(worker_mod.execute_job(job_id))

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.RETRYING
        assert job.attempts == 1
        assert job.finished_at is not None
        assert job.scheduled_at == job.finished_at + timedelta(seconds=10)
        retry_at = job.scheduled_at

    assert job_id not in runtime_mod.get_due_job_ids(now=retry_at - timedelta(microseconds=1))
    assert job_id in runtime_mod.get_due_job_ids(now=retry_at)

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="recovered")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(auto_publish_enabled=True, scheduler_poll_seconds=15),
    )
    results = asyncio.run(runtime_mod.scan_due_jobs(now=retry_at))
    assert results[job_id].success is True
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        article = session.get(Article, job.article_id)
        assert job.status == JobStatus.SUCCESS
        assert job.attempts == 2
        assert article.status == ArticleStatus.PUBLISHED


def test_retry_exhaustion_marks_job_and_article_dead(runtime_db, monkeypatch):
    article_id, (job_id,) = _create_article_and_jobs(
        runtime_db,
        attempts=1,
        max_attempts=2,
    )

    async def fail(*args, **kwargs):
        return PublishResult(success=False, error="still broken")

    monkeypatch.setattr(worker_mod, "_try_publishers", fail)
    asyncio.run(worker_mod.execute_job(job_id))
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        article = session.get(Article, article_id)
        assert job.status == JobStatus.DEAD
        assert job.attempts == 2
        assert job.scheduled_at is None
        assert article.status == ArticleStatus.DEAD


def test_second_failure_uses_doubled_backoff(runtime_db, monkeypatch):
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        attempts=1,
        max_attempts=3,
    )
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_retry_base_seconds=10, auto_publish_enabled=True),
    )

    async def fail(*args, **kwargs):
        return PublishResult(success=False, error="second temporary outage")

    monkeypatch.setattr(worker_mod, "_try_publishers", fail)
    asyncio.run(worker_mod.execute_job(job_id))
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.RETRYING
        assert job.attempts == 2
        assert job.scheduled_at == job.finished_at + timedelta(seconds=20)


def test_article_published_only_after_all_fanout_jobs_succeed(runtime_db, monkeypatch):
    article_id, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.PENDING, JobStatus.PENDING),
    )

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="ok")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    asyncio.run(worker_mod.execute_job(job_ids[0]))
    with runtime_db() as session:
        assert session.get(Article, article_id).status == ArticleStatus.PUBLISHING

    asyncio.run(worker_mod.execute_job(job_ids[1]))
    with runtime_db() as session:
        assert session.get(Article, article_id).status == ArticleStatus.PUBLISHED


def test_article_waits_for_fanout_then_reflects_dead_sibling(runtime_db, monkeypatch):
    article_id, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.PENDING, JobStatus.PENDING),
        max_attempts=1,
    )

    async def fail(*args, **kwargs):
        return PublishResult(success=False, error="terminal publisher failure")

    monkeypatch.setattr(worker_mod, "_try_publishers", fail)
    asyncio.run(worker_mod.execute_job(job_ids[0]))
    with runtime_db() as session:
        assert session.get(PublishJob, job_ids[0]).status == JobStatus.DEAD
        assert session.get(Article, article_id).status == ArticleStatus.PUBLISHING

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="sibling-ok")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    asyncio.run(worker_mod.execute_job(job_ids[1]))
    with runtime_db() as session:
        assert session.get(Article, article_id).status == ArticleStatus.DEAD


def test_permanent_preflight_failure_marks_article_failed(runtime_db, monkeypatch):
    article_id, (job_id,) = _create_article_and_jobs(runtime_db)
    monkeypatch.setattr(
        worker_mod,
        "_pre_publish_check",
        lambda *args, **kwargs: (False, "污点拦截"),
    )
    publisher_calls = 0

    async def should_not_publish(*args, **kwargs):
        nonlocal publisher_calls
        publisher_calls += 1
        return PublishResult(success=True)

    monkeypatch.setattr(worker_mod, "_try_publishers", should_not_publish)
    result = asyncio.run(worker_mod.execute_job(job_id))
    assert result.success is False
    assert publisher_calls == 0
    with runtime_db() as session:
        assert session.get(PublishJob, job_id).status == JobStatus.FAILED
        assert session.get(Article, article_id).status == ArticleStatus.FAILED


def test_successful_republish_ignores_superseded_dead_job(runtime_db, monkeypatch):
    article_id, (dead_job_id,) = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.DEAD,),
        attempts=3,
        max_attempts=3,
    )
    with runtime_db() as session:
        replacement = worker_mod.republish_job(session, dead_job_id, reason="test")
        session.commit()
        replacement_id = replacement.id

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="replacement-ok")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    asyncio.run(worker_mod.execute_job(replacement_id))
    with runtime_db() as session:
        old_job = session.get(PublishJob, dead_job_id)
        replacement = session.get(PublishJob, replacement_id)
        assert old_job.superseded_by_job_id == replacement_id
        assert replacement.status == JobStatus.SUCCESS
        assert session.get(Article, article_id).status == ArticleStatus.PUBLISHED


def test_due_scanner_recovers_pending_jobs_but_not_future_jobs(runtime_db, monkeypatch):
    now = datetime.utcnow()
    _, (due_id,) = _create_article_and_jobs(
        runtime_db,
        scheduled_at=now - timedelta(minutes=1),
    )
    _, (future_id,) = _create_article_and_jobs(
        runtime_db,
        scheduled_at=now + timedelta(hours=1),
    )
    calls: list[int] = []

    async def succeed(platform, account_id, credential, content):
        calls.append(account_id)
        return PublishResult(success=True, platform_post_id="restart-recovered")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)
    monkeypatch.setattr(runtime_mod, "settings", SimpleNamespace(auto_publish_enabled=True))
    results = asyncio.run(runtime_mod.scan_due_jobs(now=now))
    assert set(results) == {due_id}
    assert len(calls) == 1
    with runtime_db() as session:
        assert session.get(PublishJob, due_id).status == JobStatus.SUCCESS
        assert session.get(PublishJob, future_id).status == JobStatus.PENDING


def test_publish_success_commits_exact_metrics_windows_with_the_job(runtime_db, monkeypatch):
    _, (job_id,) = _create_article_and_jobs(runtime_db)

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="durable-feedback")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        tasks = list(
            session.scalars(
                select(MetricsCollectionTask)
                .where(MetricsCollectionTask.job_id == job_id)
                .order_by(MetricsCollectionTask.interval_index.asc())
            )
        )
        assert job.status == JobStatus.SUCCESS
        assert len(tasks) == 3
        assert [task.window_seconds for task in tasks] == [3600, 86400, 604800]
        assert [task.due_at for task in tasks] == [
            job.finished_at + timedelta(seconds=window) for window in (3600, 86400, 604800)
        ]
        assert all(task.status == "queued" for task in tasks)


def test_disk_state_publisher_can_run_without_database_credential(runtime_db, monkeypatch):
    """Account-name/profile based publishers must reach the registry with an empty dict."""
    _, (job_id,) = _create_article_and_jobs(runtime_db)

    def missing_credential(*args, **kwargs):
        raise ValueError("account has no database credential")

    async def disk_state_success(platform, account_id, credential, content):
        assert credential == {}
        return PublishResult(success=True, platform_post_id="disk-state")

    monkeypatch.setattr(worker_mod, "get_credential", missing_credential)
    monkeypatch.setattr(worker_mod, "_try_publishers", disk_state_success)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    with runtime_db() as session:
        assert session.get(PublishJob, job_id).status == JobStatus.SUCCESS


def test_claimed_job_does_not_count_against_its_own_daily_quota(runtime_db, monkeypatch):
    """With cap=1, the first CAS-claimed job must still be publishable."""
    from ai_ops.accounts import manager as account_manager

    _, (job_id,) = _create_article_and_jobs(runtime_db)
    monkeypatch.setattr(worker_mod, "check_rate_limit", account_manager.check_rate_limit)
    monkeypatch.setattr(account_manager.settings, "nurture_days", 0)
    monkeypatch.setattr(account_manager.settings, "publish_min_interval_seconds", 0)
    monkeypatch.setattr(account_manager.settings, "publish_max_per_day", 1)

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="quota-one")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    with runtime_db() as session:
        assert session.get(PublishJob, job_id).status == JobStatus.SUCCESS


def test_retrying_job_does_not_block_another_job_daily_quota(runtime_db, monkeypatch):
    """A failed attempt waiting to retry must not reserve a daily publish slot."""
    from ai_ops.accounts import manager as account_manager

    _, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.RETRYING, JobStatus.PENDING),
        attempts=1,
    )
    retrying_id, pending_id = job_ids
    with runtime_db() as session:
        retrying = session.get(PublishJob, retrying_id)
        pending = session.get(PublishJob, pending_id)
        pending.account_id = retrying.account_id
        retrying.started_at = datetime.utcnow()
        session.commit()

    monkeypatch.setattr(worker_mod, "check_rate_limit", account_manager.check_rate_limit)
    monkeypatch.setattr(account_manager.settings, "nurture_days", 0)
    monkeypatch.setattr(account_manager.settings, "publish_min_interval_seconds", 0)
    monkeypatch.setattr(account_manager.settings, "publish_max_per_day", 1)

    async def succeed(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="quota-not-deadlocked")

    monkeypatch.setattr(worker_mod, "_try_publishers", succeed)

    result = asyncio.run(worker_mod.execute_job(pending_id))

    assert result.success is True
    with runtime_db() as session:
        assert session.get(PublishJob, retrying_id).status == JobStatus.RETRYING
        assert session.get(PublishJob, pending_id).status == JobStatus.SUCCESS


def test_policy_deferral_does_not_consume_publisher_attempt(runtime_db, monkeypatch):
    """Nurture/quota/time gates defer until a legal time without exhausting retries."""
    from ai_ops.accounts.manager import RateCheckResult

    _, (job_id,) = _create_article_and_jobs(runtime_db)
    retry_at = datetime.utcnow() + timedelta(hours=3)
    publisher_calls = 0

    def defer(*args, **kwargs):
        return RateCheckResult(False, "daily quota", retry_at=retry_at)

    async def should_not_publish(*args, **kwargs):
        nonlocal publisher_calls
        publisher_calls += 1
        return PublishResult(success=True)

    monkeypatch.setattr(worker_mod, "check_rate_limit", defer)
    monkeypatch.setattr(worker_mod, "_try_publishers", should_not_publish)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert publisher_calls == 0
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.RETRYING
        assert job.attempts == 0
        assert job.scheduled_at == retry_at


def test_cancelled_publish_fails_closed_for_manual_verification(runtime_db, monkeypatch):
    """Cancellation must not leave RUNNING or blindly retry an uncertain post."""
    article_id, (job_id,) = _create_article_and_jobs(runtime_db)
    entered = asyncio.Event()

    async def never_finishes(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_mod, "_try_publishers", never_finishes)
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_execution_timeout_seconds=60),
    )

    async def cancel_in_flight():
        task = asyncio.create_task(worker_mod.execute_job(job_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_in_flight())

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.finished_at is not None
        assert job.scheduled_at is None
        assert "平台结果未知" in job.error
        assert job.raw_response["outcome_uncertain"] is True
        assert session.get(Article, article_id).status == ArticleStatus.FAILED


def test_cancelled_publish_preserves_cancellation_when_reconciliation_fails(
    runtime_db, monkeypatch
):
    """A database outage during cleanup must not swallow worker cancellation."""
    _, (job_id,) = _create_article_and_jobs(runtime_db)
    entered = asyncio.Event()

    async def never_finishes(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    def reconciliation_outage(*args, **kwargs):
        raise RuntimeError("database unavailable during cancellation")

    monkeypatch.setattr(worker_mod, "_try_publishers", never_finishes)
    monkeypatch.setattr(worker_mod, "mark_running_job_uncertain", reconciliation_outage)
    monkeypatch.setattr(worker_mod, "capture_exception", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_execution_timeout_seconds=60),
    )

    async def cancel_in_flight():
        task = asyncio.create_task(worker_mod.execute_job(job_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_in_flight())


def test_successful_platform_call_with_db_finalize_error_fails_closed(runtime_db, monkeypatch):
    """A finalize error must fail closed without erasing a confirmed receipt."""
    _, (job_id,) = _create_article_and_jobs(runtime_db)

    async def platform_success(*args, **kwargs):
        return PublishResult(success=True, platform_post_id="possibly-published")

    def finalize_outage(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(worker_mod, "_try_publishers", platform_success)
    monkeypatch.setattr(worker_mod, "mark_published", finalize_outage)
    monkeypatch.setattr(worker_mod, "capture_exception", lambda *args, **kwargs: None)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert result.effect_applied is True
    assert result.outcome_uncertain is False
    assert result.platform_post_id == "possibly-published"
    assert "回执已确认" in (result.error or "")
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.scheduled_at is None
        assert job.platform_post_id == "possibly-published"
        assert "回执已确认" in job.error
        assert job.raw_response["outcome_uncertain"] is False
        assert job.raw_response["effect_applied"] is True
        assert job.raw_response["reconciliation_required"] is True


def test_stale_reconciliation_recovers_durable_confirmed_receipt(runtime_db, tmp_path, monkeypatch):
    """A process crash after journaling must retain the exact platform identity."""
    _, (job_id,) = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.RUNNING,),
        attempts=1,
    )
    operation_id = "b" * 32
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        job.raw_response = {"operation_id": operation_id}
        session.commit()

    monkeypatch.setattr(receipt_mod.settings, "data_dir", tmp_path)
    receipt_mod.write_publish_receipt(
        job_id=job_id,
        operation_id=operation_id,
        publisher_kind="youtube_uploader",
        result=PublishResult(
            success=True,
            platform_post_id="video123",
            platform_url="https://www.youtube.com/watch?v=video123",
            raw_response={
                "adapter": "youtubeuploader",
                "adapter_version": "v1.25.5",
                "outcome": "confirmed",
            },
        ),
    )

    assert worker_mod.mark_running_job_uncertain(job_id, "worker crashed") is True

    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.platform_post_id == "video123"
        assert job.platform_url.endswith("video123")
        assert job.publisher_kind == "youtube_uploader"
        assert job.raw_response["receipt_recovered"] is True
        assert job.raw_response["effect_applied"] is True
        assert job.raw_response["outcome_uncertain"] is False
        assert job.raw_response["reconciliation_required"] is True
        assert "回执已确认" in job.error


def test_publish_timeout_is_terminal_unknown_outcome(runtime_db, monkeypatch):
    """A hard timeout is not a safe automatic retry boundary."""
    _, (job_id,) = _create_article_and_jobs(runtime_db, max_attempts=3)

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_mod, "_try_publishers", never_finishes)
    monkeypatch.setattr(
        worker_mod,
        "settings",
        SimpleNamespace(job_execution_timeout_seconds=0.01),
    )

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert "平台结果未知" in (result.error or "")
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.attempts == 1
        assert job.scheduled_at is None
        assert job.raw_response["outcome_uncertain"] is True


def test_busy_account_operation_lease_retries_without_entering_publisher(runtime_db, monkeypatch):
    """Profile contention is a known no-write failure, never an unknown effect."""
    _, (job_id,) = _create_article_and_jobs(runtime_db, max_attempts=3)

    class BusyLease:
        async def __aenter__(self):
            raise worker_mod.AccountOperationLeaseTimeout("busy")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        worker_mod,
        "AccountOperationLease",
        lambda *args, **kwargs: BusyLease(),
    )

    async def must_not_publish(*args, **kwargs):
        raise AssertionError("publisher must not run without the account lease")

    monkeypatch.setattr(worker_mod, "_try_publishers", must_not_publish)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.RETRYING
        assert job.attempts == 0
        assert job.raw_response["account_operation_busy"] is True
        assert job.raw_response.get("outcome_uncertain") is not True


def test_initial_metrics_flush_failure_isolated_by_savepoint(runtime_db, monkeypatch):
    """A swallowed SQL flush error must not poison publication finalization."""
    _, (job_id,) = _create_article_and_jobs(runtime_db)

    async def published(*args, **kwargs):
        return PublishResult(
            success=True,
            platform_post_id="post-with-bad-initial-metric",
            platform_url="https://example.invalid/post",
            raw_response={
                "initial_metadata": {
                    "view_count": 1,
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                }
            },
        )

    real_metrics = worker_mod.Metrics

    def constraint_violating_metrics(**kwargs):
        # The real ORM flush fails its owner/source check. The helper catches
        # that database exception, reproducing a rollback-only nested tx.
        kwargs["collection_task_id"] = 999_999
        return real_metrics(**kwargs)

    monkeypatch.setattr(worker_mod, "_try_publishers", published)
    monkeypatch.setattr(worker_mod, "Metrics", constraint_violating_metrics)
    monkeypatch.setattr(worker_mod, "capture_exception", lambda *args, **kwargs: None)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.SUCCESS
        assert (
            len(
                session.scalars(
                    select(MetricsCollectionTask).where(MetricsCollectionTask.job_id == job_id)
                ).all()
            )
            == 3
        )
        assert session.scalar(select(Metrics.id).where(Metrics.job_id == job_id)) is None


def test_publisher_uncertain_result_is_terminal_without_retry(runtime_db, monkeypatch):
    """An adapter-level ambiguous write must stop durable retry as well as fallback."""
    _, (job_id,) = _create_article_and_jobs(runtime_db, max_attempts=3)

    async def unconfirmed_write(*args, **kwargs):
        return PublishResult(
            success=False,
            outcome_uncertain=True,
            error="CLI response missing post id",
            raw_response={"outcome": "unknown", "write_started": True},
        )

    monkeypatch.setattr(worker_mod, "_try_publishers", unconfirmed_write)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.outcome_uncertain is True
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.attempts == 1
        assert job.scheduled_at is None
        assert job.raw_response["outcome_uncertain"] is True
        assert "平台结果未知" in job.error


def test_preview_success_does_not_mark_job_or_article_published(runtime_db, monkeypatch):
    """A successful renderer is not a successful external publication."""
    article_id, (job_id,) = _create_article_and_jobs(runtime_db, max_attempts=3)

    async def preview_only(*args, **kwargs):
        return PublishResult(
            success=True,
            effect_applied=False,
            raw_response={"dry_run": True, "preview": "redacted"},
        )

    monkeypatch.setattr(worker_mod, "_try_publishers", preview_only)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is False
    assert result.effect_applied is False
    with runtime_db() as session:
        job = session.get(PublishJob, job_id)
        article = session.get(Article, article_id)
        assert job.status == JobStatus.FAILED
        assert job.attempts == 1
        assert job.scheduled_at is None
        assert job.raw_response["dry_run"] is True
        assert article.status == ArticleStatus.FAILED


def test_stale_running_recovery_fails_closed_without_retry(runtime_db):
    """A hard-crashed worker must not leave a job RUNNING forever."""
    now = datetime.utcnow()
    article_id, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.RUNNING, JobStatus.RUNNING),
    )
    stale_id, active_id = job_ids
    with runtime_db() as session:
        session.get(PublishJob, stale_id).started_at = now - timedelta(hours=3)
        session.get(PublishJob, active_id).started_at = now - timedelta(minutes=5)
        session.commit()

    recovered = runtime_mod.reconcile_stale_running_jobs(
        now=now,
        stale_after_seconds=3600,
    )

    assert recovered == [stale_id]
    with runtime_db() as session:
        stale = session.get(PublishJob, stale_id)
        active = session.get(PublishJob, active_id)
        assert stale.status == JobStatus.FAILED
        assert stale.scheduled_at is None
        assert "平台结果未知" in stale.error
        assert stale.raw_response["outcome_uncertain"] is True
        assert active.status == JobStatus.RUNNING
        # One active sibling keeps the fan-out article nonterminal.
        assert session.get(Article, article_id).status == ArticleStatus.PUBLISHING


def test_due_scan_respects_configured_concurrency_limit(runtime_db, monkeypatch):
    """A backlog must not open an unbounded number of browser publishers."""
    _, job_ids = _create_article_and_jobs(
        runtime_db,
        statuses=(JobStatus.PENDING,) * 5,
    )
    active = 0
    peak = 0

    async def bounded_fake(job_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return PublishResult(success=True, platform_post_id=str(job_id))

    monkeypatch.setattr(worker_mod, "execute_job", bounded_fake)
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(
            auto_publish_enabled=True,
            scheduler_max_concurrency=2,
            job_running_timeout_seconds=7200,
        ),
    )

    results = asyncio.run(runtime_mod.scan_due_jobs())

    assert set(results) == set(job_ids)
    assert peak == 2


def test_worker_loop_retries_after_transient_scan_failure(runtime_db, monkeypatch):
    """A temporary database failure must not terminate the long-running worker."""
    calls = 0
    stop = asyncio.Event()

    async def flaky_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        stop.set()
        return {}

    monkeypatch.setattr(runtime_mod, "scan_due_jobs", flaky_scan)
    monkeypatch.setattr(runtime_mod, "capture_exception", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(auto_publish_enabled=True, scheduler_poll_seconds=0.01),
    )

    asyncio.run(
        runtime_mod.run_worker_loop(
            poll_seconds=0.01,
            stop_event=stop,
        )
    )

    assert calls == 2


def test_worker_loop_stays_alive_when_auto_publish_disabled(runtime_db, monkeypatch):
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(auto_publish_enabled=False, scheduler_poll_seconds=0.01),
    )

    async def exercise_loop():
        stop = asyncio.Event()
        task = asyncio.create_task(runtime_mod.run_worker_loop(poll_seconds=0.01, stop_event=stop))
        await asyncio.sleep(0.02)
        assert task.done() is False
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_loop())


def test_worker_loop_scans_metrics_when_auto_publish_is_disabled(runtime_db, monkeypatch):
    stop = asyncio.Event()
    metrics_scans = 0
    publish_scans = 0

    async def scan_metrics(*args, **kwargs):
        nonlocal metrics_scans
        metrics_scans += 1
        return {}

    async def scan_publish(*args, **kwargs):
        nonlocal publish_scans
        publish_scans += 1
        stop.set()
        return {}

    monkeypatch.setattr(runtime_mod, "scan_due_metrics_collection_tasks", scan_metrics)
    monkeypatch.setattr(runtime_mod, "scan_due_jobs", scan_publish)
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(auto_publish_enabled=False, scheduler_poll_seconds=0.01),
    )

    asyncio.run(
        runtime_mod.run_worker_loop(
            poll_seconds=0.01,
            stop_event=stop,
        )
    )

    assert metrics_scans == 1
    assert publish_scans == 1


def test_publish_scan_has_priority_and_long_metrics_does_not_delay_next_poll(
    runtime_db,
    monkeypatch,
):
    stop = asyncio.Event()
    metrics_started = asyncio.Event()
    metrics_cancelled = asyncio.Event()
    events: list[str] = []
    publish_scans = 0

    async def slow_metrics(*args, **kwargs):
        events.append("metrics")
        metrics_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            metrics_cancelled.set()
            raise

    async def scan_publish(*args, **kwargs):
        nonlocal publish_scans
        publish_scans += 1
        events.append(f"publish-{publish_scans}")
        if publish_scans == 1:
            assert metrics_started.is_set() is False
        else:
            assert metrics_started.is_set()
            stop.set()
        return {}

    monkeypatch.setattr(runtime_mod, "scan_due_metrics_collection_tasks", slow_metrics)
    monkeypatch.setattr(runtime_mod, "scan_due_jobs", scan_publish)
    monkeypatch.setattr(
        runtime_mod,
        "settings",
        SimpleNamespace(auto_publish_enabled=True, scheduler_poll_seconds=0.01),
    )

    asyncio.run(
        asyncio.wait_for(
            runtime_mod.run_worker_loop(
                poll_seconds=0.01,
                stop_event=stop,
            ),
            timeout=1,
        )
    )

    assert events[:2] == ["publish-1", "metrics"]
    assert publish_scans == 2
    assert metrics_cancelled.is_set()
