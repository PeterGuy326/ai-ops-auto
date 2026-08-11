"""Durable 1h/24h/7d metrics task state-machine regression tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from ai_ops.core import db as db_mod
from ai_ops.core.db import enable_sqlite_foreign_keys
from ai_ops.core.enums import (
    AccountHealth,
    ContentType,
    JobStatus,
    MetricsTaskStatus,
    Platform,
)
from ai_ops.core.models import (
    Account,
    AgentOperation,
    Article,
    Base,
    Metrics,
    MetricsCollectionTask,
    PublishJob,
    Topic,
)
from ai_ops.scheduler import metrics as metrics_mod


class FakeMetricsPublisher:
    def __init__(self, *, views: int = 100) -> None:
        self.views = views
        self.calls = 0

    async def collect_metrics(self, post_id, post_url, credential):
        del post_id, post_url, credential
        self.calls += 1
        return {
            "likes": 10,
            "comments": 2,
            "shares": 1,
            "views": self.views,
            "raw": {"fake": True},
        }


@pytest.fixture
def ledger_db(tmp_path: Path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'metrics-ledger.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    monkeypatch.setattr(metrics_mod.settings, "data_dir", tmp_path / "runtime")
    monkeypatch.setattr(metrics_mod.settings, "account_operation_lock_timeout_seconds", 1)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_account_lock_timeout_seconds", 1)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_lease_seconds", 60)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_retry_base_seconds", 1)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_collection_timeout_seconds", 5)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _create_success_job(SessionLocal, *, finished_at: datetime) -> int:
    with SessionLocal() as session:
        topic = Topic(
            name=f"metrics-ledger-{finished_at.timestamp()}",
            keywords=[],
            persona={},
            target_platforms=[],
        )
        account = Account(
            platform=Platform.ZHIHU,
            nickname="metrics-ledger-account",
            profile={},
            encrypted_credential=b"encrypted",
            health=AccountHealth.HEALTHY,
        )
        session.add_all([topic, account])
        session.flush()
        article = Article(
            topic_id=topic.id,
            title="durable metrics",
            body="body",
            content_type=ContentType.LONG_ARTICLE,
            target_platforms=[Platform.ZHIHU.value],
            target_account_ids=[account.id],
            extra={},
        )
        session.add(article)
        session.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=account.id,
            platform=Platform.ZHIHU,
            status=JobStatus.SUCCESS,
            publisher_kind="fake",
            platform_post_id=f"post-{article.id}",
            platform_url=f"https://example.invalid/post/{article.id}",
            finished_at=finished_at,
            raw_response={},
        )
        session.add(job)
        session.commit()
        return job.id


def _ensure_tasks(SessionLocal, job_id: int, *, anchor: datetime, max_attempts=5):
    with SessionLocal() as session:
        tasks = metrics_mod.ensure_metrics_collection_tasks(
            session,
            job_id,
            anchor=anchor,
            max_attempts=max_attempts,
        )
        task_ids = [task.id for task in tasks]
        session.commit()
        return task_ids


def _install_publisher(monkeypatch, publisher):
    monkeypatch.setattr(
        metrics_mod.default_registry,
        "resolve_collector",
        lambda platform, publisher_kind: publisher,
    )
    monkeypatch.setattr(metrics_mod, "get_credential", lambda session, account_id: {})
    monkeypatch.setattr(
        "ai_ops.content.heat_engine.recompute_topic_heat_for_article",
        lambda article_id: None,
    )


def test_fixed_windows_are_immutable_and_creation_is_idempotent(ledger_db):
    anchor = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    first_ids = _ensure_tasks(ledger_db, job_id, anchor=anchor)
    second_ids = _ensure_tasks(ledger_db, job_id, anchor=anchor)

    assert second_ids == first_ids
    with ledger_db() as session:
        tasks = list(
            session.scalars(
                select(MetricsCollectionTask)
                .where(MetricsCollectionTask.job_id == job_id)
                .order_by(MetricsCollectionTask.interval_index)
            )
        )
        assert [task.window_seconds for task in tasks] == [3600, 86400, 604800]
        assert [task.due_at for task in tasks] == [
            anchor + timedelta(seconds=value) for value in metrics_mod.DEFAULT_INTERVALS_SECONDS
        ]
        assert [task.collection_deadline_at - task.due_at for task in tasks] == [
            timedelta(seconds=value) for value in metrics_mod.DEFAULT_DEADLINE_GRACE_SECONDS
        ]

    with ledger_db() as session, pytest.raises(ValueError, match="fixed 1h/24h/7d"):
        metrics_mod.ensure_metrics_collection_tasks(
            session,
            job_id,
            anchor=anchor,
            intervals=(60,),
            deadline_graces=(60,),
        )


def test_ledger_rows_rollback_with_the_publication_transaction(ledger_db):
    anchor = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=anchor)

    with pytest.raises(RuntimeError, match="rollback"):
        with ledger_db.begin() as session:
            metrics_mod.ensure_metrics_collection_tasks(
                session,
                job_id,
                anchor=anchor,
            )
            raise RuntimeError("rollback")

    with ledger_db() as session:
        assert (
            session.scalar(
                select(MetricsCollectionTask.id).where(MetricsCollectionTask.job_id == job_id)
            )
            is None
        )


def test_due_runner_persists_one_snapshot_and_terminal_task(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    publisher = FakeMetricsPublisher(views=321)
    _install_publisher(monkeypatch, publisher)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["views"] == 321
    assert publisher.calls == 1
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        snapshots = list(
            session.scalars(select(Metrics).where(Metrics.collection_task_id == task_id))
        )
        assert task.status == MetricsTaskStatus.SUCCEEDED
        assert task.attempts == 1
        assert task.lease_token is None
        assert task.finished_at is not None
        assert len(snapshots) == 1
        assert snapshots[0].job_id == job_id
        assert snapshots[0].source == "scheduled"

    replay = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))
    assert replay["skipped"] is True
    assert publisher.calls == 1


def test_competing_runners_enter_collector_only_once(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            self.calls += 1
            entered.set()
            await release.wait()
            return {
                "likes": 1,
                "comments": 1,
                "shares": 1,
                "views": 1,
                "raw": {},
            }

    publisher = BlockingPublisher()
    _install_publisher(monkeypatch, publisher)

    async def compete():
        first = asyncio.create_task(metrics_mod.run_metrics_collection_task(task_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(metrics_mod.run_metrics_collection_task(task_id))
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(compete())

    assert publisher.calls == 1
    assert sum(not result.get("skipped", False) for result in results) == 1


def test_expired_owner_is_reclaimed_and_old_token_cannot_write(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    publisher = FakeMetricsPublisher()
    _install_publisher(monkeypatch, publisher)

    old = metrics_mod._claim_metrics_collection_task(
        task_id,
        now=now,
        lease_seconds=2,
    )
    assert old is not None
    assert metrics_mod._claim_metrics_collection_task(task_id, now=now) is None
    new = metrics_mod._claim_metrics_collection_task(
        task_id,
        now=now + timedelta(seconds=3),
    )
    assert new is not None
    assert old.lease_token != new.lease_token

    stale = asyncio.run(
        metrics_mod.collect_one(
            job_id,
            interval_index=0,
            collection_task_id=task_id,
            collection_task_lease_token=old.lease_token,
            account_lease_held=True,
        )
    )
    assert stale["skipped"] is True
    assert "lease was lost" in stale["reason"]
    assert publisher.calls == 0

    fresh = asyncio.run(
        metrics_mod.collect_one(
            job_id,
            interval_index=0,
            collection_task_id=task_id,
            collection_task_lease_token=new.lease_token,
            account_lease_held=True,
        )
    )
    assert fresh["views"] == 100
    assert publisher.calls == 1


def test_cancelled_collection_requeues_without_duplicate_snapshot(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    entered = asyncio.Event()

    class CancelledPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    publisher = CancelledPublisher()
    _install_publisher(monkeypatch, publisher)

    async def cancel_owner():
        owner = asyncio.create_task(metrics_mod.run_metrics_collection_task(task_id))
        await asyncio.wait_for(entered.wait(), timeout=1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(cancel_owner())

    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 1
        assert task.lease_token is None
        assert task.next_attempt_at is not None
        assert (
            session.scalar(select(Metrics.id).where(Metrics.collection_task_id == task_id)) is None
        )


def test_permanent_capability_gap_fails_without_fake_zero_snapshot(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    monkeypatch.setattr(
        metrics_mod.default_registry,
        "resolve_collector",
        lambda platform, publisher_kind: None,
    )

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["skipped"] is True
    assert result["retryable"] is False
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.FAILED
        assert task.attempts == 0
        assert task.last_error
        assert (
            session.scalar(select(Metrics.id).where(Metrics.collection_task_id == task_id)) is None
        )


def test_busy_account_lease_defers_without_claim_or_attempt_and_unblocks_prefix(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    first_anchor = now - timedelta(hours=1, seconds=2)
    second_anchor = now - timedelta(hours=1, seconds=1)
    first_job_id = _create_success_job(ledger_db, finished_at=first_anchor)
    second_job_id = _create_success_job(ledger_db, finished_at=second_anchor)
    first_task_id = _ensure_tasks(ledger_db, first_job_id, anchor=first_anchor)[0]
    second_task_id = _ensure_tasks(ledger_db, second_job_id, anchor=second_anchor)[0]

    assert metrics_mod.get_due_metrics_collection_task_ids(now=now, limit=1) == [first_task_id]

    from ai_ops.runtime import account_lease as lease_mod

    class BusyLease:
        async def __aenter__(self):
            raise lease_mod.AccountOperationLeaseTimeout("busy")

        async def __aexit__(self, *args):
            return None

    observed_timeouts: list[float] = []

    def busy_lease(*args, timeout_seconds, **kwargs):
        observed_timeouts.append(timeout_seconds)
        return BusyLease()

    monkeypatch.setattr(
        lease_mod,
        "AccountOperationLease",
        busy_lease,
    )

    result = asyncio.run(metrics_mod.run_metrics_collection_task(first_task_id))

    assert result["task_state"] == MetricsTaskStatus.QUEUED.value
    assert observed_timeouts == [1.0]
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, first_task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 0
        assert task.lease_token is None
        assert task.started_at is None
        assert task.next_attempt_at > now
        assert task.last_error == "account operation lease is busy"

    # The busy oldest row no longer occupies the next bounded scan, so another
    # account can make progress even when the query limit is one.
    assert metrics_mod.get_due_metrics_collection_task_ids(now=datetime.utcnow(), limit=1) == [
        second_task_id
    ]


def test_account_binding_change_after_lock_does_not_enter_collector(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    replacement_job_id = _create_success_job(ledger_db, finished_at=now)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    with ledger_db() as session:
        replacement_account_id = session.get(PublishJob, replacement_job_id).account_id

    from ai_ops.runtime import account_lease as lease_mod

    class ReassigningLease:
        async def __aenter__(self):
            with ledger_db() as session:
                job = session.get(PublishJob, job_id)
                job.account_id = replacement_account_id
                session.commit()
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        lease_mod,
        "AccountOperationLease",
        lambda *args, **kwargs: ReassigningLease(),
    )
    publisher = FakeMetricsPublisher()
    _install_publisher(monkeypatch, publisher)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["skipped"] is True
    assert "binding changed" in result["reason"]
    assert publisher.calls == 0
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 0


def test_account_lock_backend_error_is_safely_deferred(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]

    from ai_ops.runtime import account_lease as lease_mod

    class BrokenLease:
        async def __aenter__(self):
            raise OSError("private filesystem details")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        lease_mod,
        "AccountOperationLease",
        lambda *args, **kwargs: BrokenLease(),
    )

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result == {
        "skipped": True,
        "reason": "account operation lease is unavailable",
        "task_state": MetricsTaskStatus.QUEUED.value,
    }
    assert "private filesystem" not in str(result)
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 0
        assert task.lease_token is None
        assert task.last_error == "account operation lease is unavailable"


def test_lock_cleanup_error_does_not_hide_committed_snapshot(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]

    from ai_ops.runtime import account_lease as lease_mod

    class CleanupFailingLease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            raise OSError("private unlock details")

    monkeypatch.setattr(
        lease_mod,
        "AccountOperationLease",
        lambda *args, **kwargs: CleanupFailingLease(),
    )
    publisher = FakeMetricsPublisher(views=456)
    _install_publisher(monkeypatch, publisher)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["views"] == 456
    assert "private unlock" not in str(result)
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.SUCCEEDED
        assert session.scalar(select(Metrics.id).where(Metrics.collection_task_id == task_id))


def test_last_failed_collector_attempt_becomes_terminal(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(
        ledger_db,
        job_id,
        anchor=anchor,
        max_attempts=1,
    )[0]

    class BrokenPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            self.calls += 1
            raise RuntimeError("secret adapter details")

    publisher = BrokenPublisher()
    _install_publisher(monkeypatch, publisher)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["skipped"] is True
    assert publisher.calls == 1
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.FAILED
        assert task.attempts == 1
        assert "secret adapter details" not in task.last_error


def test_collector_skip_reason_is_redacted_before_retry_persistence(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]

    class SkippingPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            self.calls += 1
            return {
                "skipped": True,
                "reason": "private-token-from-third-party-cli",
            }

    publisher = SkippingPublisher()
    _install_publisher(monkeypatch, publisher)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["reason"] == "collector 跳过采集"
    assert "private-token" not in str(result)
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 1
        assert task.last_error == "collector 跳过采集"


def test_24h_health_feedback_failure_rolls_back_snapshot_and_retries(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=24, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[1]
    publisher = FakeMetricsPublisher(views=8)
    _install_publisher(monkeypatch, publisher)

    def broken_feedback(*args, **kwargs):
        raise RuntimeError("private health backend details")

    monkeypatch.setattr(
        "ai_ops.accounts.health_monitor.evaluate_after_metrics",
        broken_feedback,
    )
    monkeypatch.setattr(metrics_mod, "capture_exception", lambda *args, **kwargs: None)

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["skipped"] is True
    assert result["task_state"] == MetricsTaskStatus.QUEUED.value
    assert publisher.calls == 1
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.QUEUED
        assert task.attempts == 1
        assert "private health backend details" not in task.last_error
        assert (
            session.scalar(select(Metrics.id).where(Metrics.collection_task_id == task_id)) is None
        )


def test_missed_window_is_failed_without_contacting_platform(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=4)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    publisher = FakeMetricsPublisher()
    _install_publisher(monkeypatch, publisher)

    reconciled = metrics_mod.reconcile_exhausted_metrics_collection_tasks(now=now)

    assert task_id in reconciled
    assert asyncio.run(metrics_mod.run_metrics_collection_task(task_id))["skipped"] is True
    assert publisher.calls == 0
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.FAILED
        assert "window expired" in task.last_error


def test_backfill_marks_historical_windows_missed_instead_of_relabeling_now(ledger_db):
    now = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=now - timedelta(days=30))

    assert metrics_mod.backfill_missing_metrics_collection_tasks() == [job_id]

    with ledger_db() as session:
        tasks = list(
            session.scalars(
                select(MetricsCollectionTask)
                .where(MetricsCollectionTask.job_id == job_id)
                .order_by(MetricsCollectionTask.interval_index)
            )
        )
        assert len(tasks) == 3
        assert all(task.status == MetricsTaskStatus.FAILED for task in tasks)
        assert all("backfill" in task.last_error for task in tasks)


def test_backfill_isolates_one_corrupt_partial_ledger(ledger_db, monkeypatch):
    now = datetime.utcnow()
    bad_job_id = _create_success_job(
        ledger_db,
        finished_at=now - timedelta(seconds=2),
    )
    good_job_id = _create_success_job(
        ledger_db,
        finished_at=now - timedelta(seconds=1),
    )
    with ledger_db() as session:
        bad_job = session.get(PublishJob, bad_job_id)
        wrong_due = bad_job.finished_at + timedelta(hours=2)
        session.add(
            MetricsCollectionTask(
                job_id=bad_job_id,
                interval_index=0,
                window_seconds=3600,
                due_at=wrong_due,
                collection_deadline_at=wrong_due + timedelta(hours=1),
                next_attempt_at=wrong_due,
                status=MetricsTaskStatus.QUEUED,
                attempts=0,
                max_attempts=5,
            )
        )
        session.commit()
    captured: list[int] = []
    monkeypatch.setattr(
        metrics_mod,
        "capture_exception",
        lambda exc, *, scope, job_id: captured.append(job_id),
    )

    repaired = metrics_mod.backfill_missing_metrics_collection_tasks()

    assert repaired == [good_job_id]
    assert captured == [bad_job_id]
    with ledger_db() as session:
        bad_job = session.get(PublishJob, bad_job_id)
        assert bad_job.raw_response["metrics_task_backfill_required"] is True
        good_tasks = list(
            session.scalars(
                select(MetricsCollectionTask).where(MetricsCollectionTask.job_id == good_job_id)
            )
        )
        assert len(good_tasks) == 3


def test_backfill_quarantines_corrupt_oldest_row_without_starving_limit_one(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    bad_job_id = _create_success_job(ledger_db, finished_at=now - timedelta(seconds=2))
    good_job_id = _create_success_job(ledger_db, finished_at=now - timedelta(seconds=1))
    with ledger_db() as session:
        wrong_due = now + timedelta(hours=2)
        session.add(
            MetricsCollectionTask(
                job_id=bad_job_id,
                interval_index=0,
                window_seconds=3600,
                due_at=wrong_due,
                collection_deadline_at=wrong_due + timedelta(hours=1),
                next_attempt_at=wrong_due,
                status=MetricsTaskStatus.QUEUED,
                attempts=0,
                max_attempts=5,
            )
        )
        session.commit()
    monkeypatch.setattr(metrics_mod, "capture_exception", lambda *args, **kwargs: None)

    assert metrics_mod.backfill_missing_metrics_collection_tasks(limit=1) == []
    assert metrics_mod.backfill_missing_metrics_collection_tasks(limit=1) == [good_job_id]

    with ledger_db() as session:
        bad = session.get(PublishJob, bad_job_id)
        assert bad.raw_response["metrics_task_backfill_quarantined"] is True


def test_malformed_legacy_quarantine_value_cannot_abort_backfill(ledger_db):
    now = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=now)
    with ledger_db() as session:
        job = session.get(PublishJob, job_id)
        job.raw_response = {"metrics_task_backfill_quarantined": "oops"}
        session.commit()

    assert metrics_mod.backfill_missing_metrics_collection_tasks(limit=1) == [job_id]

    with ledger_db() as session:
        assert (
            len(
                session.scalars(
                    select(MetricsCollectionTask).where(MetricsCollectionTask.job_id == job_id)
                ).all()
            )
            == 3
        )


def test_database_clock_rejects_stale_owner_despite_stale_python_clock(
    ledger_db,
    monkeypatch,
):
    real_now = datetime.utcnow()
    anchor = real_now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]
    claim = metrics_mod._claim_metrics_collection_task(task_id)
    assert claim is not None
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        task.lease_expires_at = real_now - timedelta(seconds=1)
        session.commit()

    class StaleDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return real_now - timedelta(days=1)

    monkeypatch.setattr(metrics_mod, "datetime", StaleDateTime)

    assert metrics_mod._begin_metrics_collection_attempt(claim) is False
    assert metrics_mod._retry_or_fail_metrics_collection_task(claim, "late owner") is None
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.CLAIMED
        assert task.attempts == 0
        assert task.lease_token == claim.lease_token


def test_agent_collection_timeout_is_not_replaced_by_task_timeout(
    ledger_db,
    monkeypatch,
):
    now = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=now)

    class SlowPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            await asyncio.sleep(0.03)
            return await super().collect_metrics(post_id, post_url, credential)

    publisher = SlowPublisher()
    _install_publisher(monkeypatch, publisher)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_collection_timeout_seconds", 0.01)
    monkeypatch.setattr(metrics_mod.settings, "agent_metrics_collection_timeout_seconds", 1)
    token = "a" * 64
    with ledger_db() as session:
        operation = AgentOperation(
            principal_id="agent-timeout-test",
            principal_type="agent",
            operation="collect_metrics",
            idempotency_key="timeout-owner",
            request_digest="b" * 64,
            lease_token=token,
            lease_expires_at=now + timedelta(minutes=1),
        )
        session.add(operation)
        session.commit()
        operation_id = operation.id

    result = asyncio.run(
        metrics_mod.collect_one(
            job_id,
            source="manual",
            agent_operation_id=operation_id,
            agent_operation_lease_token=token,
            account_lease_held=True,
        )
    )

    assert result["views"] == 100
    assert publisher.calls == 1


def test_legacy_manual_collection_uses_agent_timeout(ledger_db, monkeypatch):
    now = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=now)

    class SlowPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            del post_id, post_url, credential
            self.calls += 1
            await asyncio.sleep(0.03)
            return {
                "likes": 1,
                "comments": 1,
                "shares": 1,
                "views": 1,
                "raw": {},
            }

    publisher = SlowPublisher()
    _install_publisher(monkeypatch, publisher)
    monkeypatch.setattr(metrics_mod.settings, "metrics_task_collection_timeout_seconds", 1)
    monkeypatch.setattr(metrics_mod.settings, "agent_metrics_collection_timeout_seconds", 0.01)

    result = asyncio.run(metrics_mod.collect_one(job_id, source="manual", account_lease_held=True))

    assert result["skipped"] is True
    assert result["reason"] == "metrics collector timed out"
    assert publisher.calls == 1
    with ledger_db() as session:
        assert session.scalar(select(Metrics.id).where(Metrics.job_id == job_id)) is None


@pytest.mark.parametrize("bad_value", [-1, True, "12", 2_147_483_648])
def test_invalid_collector_counts_fail_without_persisting_evidence(
    ledger_db,
    monkeypatch,
    bad_value,
):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=1, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[0]

    class InvalidPublisher(FakeMetricsPublisher):
        async def collect_metrics(self, post_id, post_url, credential):
            del post_id, post_url, credential
            return {
                "likes": 1,
                "comments": 1,
                "shares": 1,
                "views": bad_value,
                "raw": {},
            }

    _install_publisher(monkeypatch, InvalidPublisher())

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["skipped"] is True
    assert result["retryable"] is False
    with ledger_db() as session:
        task = session.get(MetricsCollectionTask, task_id)
        assert task.status == MetricsTaskStatus.FAILED
        assert session.scalar(select(Metrics.id).where(Metrics.job_id == job_id)) is None


def test_24h_feedback_uses_task_metric_not_newer_manual_row(ledger_db, monkeypatch):
    now = datetime.utcnow()
    anchor = now - timedelta(hours=24, seconds=1)
    job_id = _create_success_job(ledger_db, finished_at=anchor)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=anchor)[1]
    with ledger_db() as session:
        manual = Metrics(
            job_id=job_id,
            source="manual",
            views=9999,
            likes=0,
            comments=0,
            shares=0,
            raw={},
            collected_at=now + timedelta(hours=1),
        )
        session.add(manual)
        session.commit()
        manual_id = manual.id

    publisher = FakeMetricsPublisher(views=7)
    _install_publisher(monkeypatch, publisher)
    observed: list[int] = []

    class Action:
        decision = "healthy"
        reason = "exact metric"

    def exact_eval(session, evaluated_job_id, *, metric_id=None):
        del session
        assert evaluated_job_id == job_id
        observed.append(metric_id)
        return Action()

    monkeypatch.setattr(
        "ai_ops.accounts.health_monitor.evaluate_after_metrics",
        exact_eval,
    )

    result = asyncio.run(metrics_mod.run_metrics_collection_task(task_id))

    assert result["health_action"]["reason"] == "exact metric"
    with ledger_db() as session:
        task_metric_id = session.scalar(
            select(Metrics.id).where(Metrics.collection_task_id == task_id)
        )
    assert observed == [task_metric_id]
    assert task_metric_id != manual_id


def test_composite_foreign_key_rejects_cross_job_task_binding(ledger_db):
    now = datetime.utcnow()
    first_job = _create_success_job(ledger_db, finished_at=now)
    second_job = _create_success_job(ledger_db, finished_at=now + timedelta(seconds=1))
    task_id = _ensure_tasks(ledger_db, first_job, anchor=now)[0]

    with ledger_db() as session:
        session.add(
            Metrics(
                job_id=second_job,
                collection_task_id=task_id,
                source="scheduled",
                views=1,
                likes=1,
                comments=1,
                shares=1,
                raw={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_task_bound_metric_cannot_also_use_manual_ledger_or_source(ledger_db):
    now = datetime.utcnow()
    job_id = _create_success_job(ledger_db, finished_at=now)
    task_id = _ensure_tasks(ledger_db, job_id, anchor=now)[0]

    with ledger_db() as session:
        session.add(
            Metrics(
                job_id=job_id,
                collection_task_id=task_id,
                source="manual",
                raw={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_batch_scanner_isolates_one_crashed_task(ledger_db, monkeypatch):
    monkeypatch.setattr(
        metrics_mod,
        "backfill_missing_metrics_collection_tasks",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        metrics_mod,
        "reconcile_exhausted_metrics_collection_tasks",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        metrics_mod,
        "get_due_metrics_collection_task_ids",
        lambda **kwargs: [101, 102],
    )
    captured: list[tuple[str, int]] = []

    async def run_one(task_id):
        if task_id == 101:
            raise RuntimeError("private task details")
        return {"views": 42}

    monkeypatch.setattr(metrics_mod, "run_metrics_collection_task", run_one)
    monkeypatch.setattr(
        metrics_mod,
        "capture_exception",
        lambda exc, *, scope, task_id: captured.append((scope, task_id)),
    )

    results = asyncio.run(metrics_mod.scan_due_metrics_collection_tasks())

    assert results == {
        101: {
            "skipped": True,
            "reason": "durable metrics task crashed",
            "task_state": "runner_error",
        },
        102: {"views": 42},
    }
    assert captured == [("scheduler.metrics.scan_item", 101)]
    assert "private task details" not in str(results)
