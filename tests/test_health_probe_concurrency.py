"""Fail-safe account/profile concurrency for the daily health scanner."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


@pytest.fixture
def health_concurrency_env(tmp_path, monkeypatch):
    from ai_ops.scheduler import health as health_mod

    engine = create_engine(
        f"sqlite:///{tmp_path / 'health-concurrency.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )

    @contextmanager
    def fake_scope():
        session = SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(health_mod, "session_scope", fake_scope)
    monkeypatch.setattr(health_mod, "get_credential", lambda session, account_id: {})
    monkeypatch.setattr(health_mod.settings, "data_dir", tmp_path / "control-data")
    try:
        yield health_mod, SessionLocal
    finally:
        engine.dispose()


def _seed_account_and_job(
    SessionLocal,
    *,
    job_status: JobStatus = JobStatus.PENDING,
) -> tuple[int, int, datetime]:
    checked_at = datetime.utcnow() - timedelta(hours=1)
    with SessionLocal() as session:
        topic = Topic(name=f"health-race-{datetime.utcnow().timestamp()}")
        session.add(topic)
        session.flush()
        account = Account(
            platform=Platform.ZHIHU,
            nickname="health-race-account",
            health=AccountHealth.HEALTHY,
            last_health_check_at=checked_at,
            encrypted_credential=b"",
        )
        session.add(account)
        session.flush()
        article = Article(
            topic_id=topic.id,
            title="health race",
            body="body",
            content_type=ContentType.LONG_ARTICLE,
            status=ArticleStatus.SCHEDULED,
        )
        session.add(article)
        session.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=account.id,
            platform=account.platform,
            status=job_status,
            started_at=datetime.utcnow() - timedelta(minutes=1)
            if job_status == JobStatus.RUNNING
            else None,
        )
        session.add(job)
        session.commit()
        return account.id, job.id, checked_at


def _publisher(health_check):
    publisher = MagicMock()
    publisher.health_check = health_check
    return publisher


def test_running_publish_skips_probe_and_preserves_health(
    health_concurrency_env,
    monkeypatch,
):
    health_mod, SessionLocal = health_concurrency_env
    account_id, _, checked_at = _seed_account_and_job(
        SessionLocal,
        job_status=JobStatus.RUNNING,
    )
    calls = 0

    async def must_not_touch_profile(account_id, credential):
        nonlocal calls
        calls += 1
        return AccountHealth.EXPIRED

    monkeypatch.setattr(
        health_mod.default_registry,
        "resolve",
        lambda platform: [_publisher(must_not_touch_profile)],
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert calls == 0
    assert result["count"] == 0
    assert account_id not in result["results"]
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.HEALTHY
        assert account.last_health_check_at == checked_at


def test_busy_cross_process_account_lease_skips_profile_probe(
    health_concurrency_env,
    monkeypatch,
):
    from ai_ops.runtime.account_lease import AccountOperationLease

    health_mod, SessionLocal = health_concurrency_env
    account_id, _, checked_at = _seed_account_and_job(SessionLocal)
    calls = 0

    async def must_not_touch_profile(account_id, credential):
        nonlocal calls
        calls += 1
        return AccountHealth.EXPIRED

    monkeypatch.setattr(
        health_mod.default_registry,
        "resolve",
        lambda platform: [_publisher(must_not_touch_profile)],
    )

    async def exercise():
        async with AccountOperationLease(account_id, timeout_seconds=0):
            return await health_mod.check_all_accounts()

    result = asyncio.run(exercise())

    assert calls == 0
    assert result["count"] == 0
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.HEALTHY
        assert account.last_health_check_at == checked_at


def test_publish_started_during_probe_discards_result_even_after_publish_finishes(
    health_concurrency_env,
    monkeypatch,
):
    health_mod, SessionLocal = health_concurrency_env
    account_id, job_id, checked_at = _seed_account_and_job(SessionLocal)

    async def publish_during_probe(account_id, credential):
        with SessionLocal() as session:
            job = session.get(PublishJob, job_id)
            account = session.get(Account, account_id)
            now = datetime.utcnow()
            job.status = JobStatus.SUCCESS
            job.started_at = now
            job.finished_at = now
            account.last_publish_at = now
            session.commit()
        return AccountHealth.EXPIRED

    monkeypatch.setattr(
        health_mod.default_registry,
        "resolve",
        lambda platform: [_publisher(publish_during_probe)],
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert result["count"] == 0
    assert account_id not in result["results"]
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.HEALTHY
        assert account.last_health_check_at == checked_at
        assert account.last_publish_at is not None


def test_newer_health_version_wins_over_stale_probe(
    health_concurrency_env,
    monkeypatch,
):
    health_mod, SessionLocal = health_concurrency_env
    account_id, _, checked_at = _seed_account_and_job(SessionLocal)
    newer_check_at = datetime.utcnow()

    async def concurrent_health_update(account_id, credential):
        with SessionLocal() as session:
            account = session.get(Account, account_id)
            account.health = AccountHealth.DEGRADED
            account.last_health_check_at = newer_check_at
            session.commit()
        return AccountHealth.HEALTHY

    monkeypatch.setattr(
        health_mod.default_registry,
        "resolve",
        lambda platform: [_publisher(concurrent_health_update)],
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert result["count"] == 0
    assert account_id not in result["results"]
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.DEGRADED
        assert account.last_health_check_at == newer_check_at
        assert account.last_health_check_at != checked_at
