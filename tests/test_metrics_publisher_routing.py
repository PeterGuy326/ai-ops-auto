"""Metrics must follow the adapter that actually performed the publish."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine

from ai_ops.accounts.manager import RateCheckResult
from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
    PublisherKind,
)
from ai_ops.core.models import Account, Article, Base, Metrics, PublishJob, Topic
from ai_ops.core.schemas import PublishResult
from ai_ops.publishers.base import PublisherBase
from ai_ops.publishers.registry import PublisherRegistry, build_default_registry


@pytest.fixture
def production_session_in_memory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _seed_job(SessionLocal, *, publisher_kind: str, status: JobStatus) -> int:
    with SessionLocal() as session:
        topic = Topic(
            name=f"metrics-route-{publisher_kind or 'legacy'}-{status.value}",
            keywords=[],
            persona={},
            target_platforms=[],
        )
        session.add(topic)
        session.flush()
        account = Account(
            platform=Platform.ZHIHU,
            nickname="metrics-route-account",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        session.add(account)
        session.flush()
        article = Article(
            topic_id=topic.id,
            title="metrics route title",
            body="clean body",
            content_type=ContentType.LONG_ARTICLE,
            status=(
                ArticleStatus.SCHEDULED if status == JobStatus.PENDING else ArticleStatus.PUBLISHED
            ),
            extra={},
        )
        session.add(article)
        session.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=account.id,
            platform=Platform.ZHIHU,
            status=status,
            publisher_kind=publisher_kind,
            attempts=0 if status == JobStatus.PENDING else 1,
            max_attempts=3,
            platform_post_id=None if status == JobStatus.PENDING else "7788",
            platform_url=(
                None if status == JobStatus.PENDING else "https://zhuanlan.zhihu.com/p/7788"
            ),
            finished_at=None if status == JobStatus.PENDING else datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id


class _CliWithoutMetrics(PublisherBase):
    platform = Platform.ZHIHU
    kind = PublisherKind.ZHIHU_CLI

    def __init__(self, publish_calls: list[str], metrics_calls: list[str]):
        self.publish_calls = publish_calls
        self.metrics_calls = metrics_calls

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        self.publish_calls.append("cli")
        return PublishResult(
            success=False,
            error="audited CLI preflight failed before write",
            raw_response={"stage": "preflight", "write_started": False},
        )

    async def health_check(self, account_id, credential):
        return AccountHealth.UNKNOWN


class _BrowserWithMetrics(PublisherBase):
    platform = Platform.ZHIHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD
    supports_metrics = True

    def __init__(self, publish_calls: list[str], metrics_calls: list[str]):
        self.publish_calls = publish_calls
        self.metrics_calls = metrics_calls

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        self.publish_calls.append("browser")
        return PublishResult(
            success=True,
            platform_post_id="7788",
            platform_url="https://zhuanlan.zhihu.com/p/7788",
            raw_response={"receipt": "strict_public_article_url"},
        )

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY

    async def collect_metrics(self, post_id, post_url, credential):
        self.metrics_calls.append(post_id)
        return {
            "likes": 8,
            "comments": 2,
            "shares": 0,
            "views": 80,
            "raw": {"collector": "browser"},
        }


def _zhihu_registry():
    publish_calls: list[str] = []
    metrics_calls: list[str] = []
    cli = _CliWithoutMetrics(publish_calls, metrics_calls)
    browser = _BrowserWithMetrics(publish_calls, metrics_calls)
    registry = PublisherRegistry()
    registry.register(Platform.ZHIHU, lambda: cli, priority=5)
    registry.register(Platform.ZHIHU, lambda: browser, priority=10)
    return registry, publish_calls, metrics_calls


def test_enabled_zhihu_cli_registry_routes_only_browser_metrics(monkeypatch):
    from ai_ops.config import settings

    monkeypatch.setattr(settings, "zhihu_cli_enabled", True)
    registry = build_default_registry()

    assert [publisher.kind for publisher in registry.resolve(Platform.ZHIHU)] == [
        PublisherKind.ZHIHU_CLI,
        PublisherKind.SOCIAL_AUTO_UPLOAD,
    ]
    assert (
        registry.resolve_collector(
            Platform.ZHIHU,
            PublisherKind.ZHIHU_CLI.value,
        )
        is None
    )
    assert (
        registry.resolve_collector(
            Platform.ZHIHU,
            PublisherKind.SOCIAL_AUTO_UPLOAD.value,
        ).kind
        == PublisherKind.SOCIAL_AUTO_UPLOAD
    )
    # Legacy rows with no kind skip the unsupported CLI and choose only the
    # explicitly capable Playwright collector.
    assert registry.resolve_collector(Platform.ZHIHU).kind == PublisherKind.SOCIAL_AUTO_UPLOAD


def test_unsupported_actual_adapter_skips_without_fake_zero_or_health_eval(
    production_session_in_memory,
    monkeypatch,
):
    from ai_ops.scheduler import metrics as metrics_mod

    SessionLocal = production_session_in_memory
    job_id = _seed_job(
        SessionLocal,
        publisher_kind=PublisherKind.ZHIHU_CLI.value,
        status=JobStatus.SUCCESS,
    )
    registry, _, metrics_calls = _zhihu_registry()
    monkeypatch.setattr(metrics_mod, "default_registry", registry)
    monkeypatch.setattr(
        metrics_mod,
        "get_credential",
        lambda *args: pytest.fail("unsupported collector must skip before credential load"),
    )

    import ai_ops.accounts.health_monitor as health_mod
    import ai_ops.content.heat_engine as heat_mod

    monkeypatch.setattr(
        health_mod,
        "evaluate_after_metrics",
        lambda *args: pytest.fail("skipped metrics must not trigger account health"),
    )
    monkeypatch.setattr(
        heat_mod,
        "recompute_topic_heat_for_article",
        lambda *args: pytest.fail("skipped metrics must not trigger heat refresh"),
    )

    result = asyncio.run(
        metrics_mod.collect_one(
            job_id,
            interval_index=metrics_mod.HEALTH_EVAL_INTERVAL_INDEX,
        )
    )

    assert result["skipped"] is True
    assert result["publisher_kind"] == PublisherKind.ZHIHU_CLI.value
    assert "不支持 metrics" in result["reason"]
    assert metrics_calls == []
    with SessionLocal() as session:
        assert session.query(Metrics).filter(Metrics.job_id == job_id).count() == 0


def test_actual_fallback_kind_is_persisted_and_drives_collector(
    production_session_in_memory,
    monkeypatch,
):
    from ai_ops.scheduler import metrics as metrics_mod
    from ai_ops.scheduler import worker as worker_mod

    SessionLocal = production_session_in_memory
    job_id = _seed_job(SessionLocal, publisher_kind="", status=JobStatus.PENDING)
    registry, publish_calls, metrics_calls = _zhihu_registry()
    monkeypatch.setattr(worker_mod, "default_registry", registry)
    monkeypatch.setattr(worker_mod, "get_credential", lambda *args: {})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda *args, **kwargs: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "mark_published", lambda *args: None)
    monkeypatch.setattr(worker_mod, "is_paused", lambda *args: False)
    monkeypatch.setattr(
        worker_mod,
        "_pre_publish_check",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda *args: [])

    import ai_ops.notify as notify_mod

    monkeypatch.setattr(notify_mod, "publish_success", lambda *args: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda *args: None)

    result = asyncio.run(worker_mod.execute_job(job_id))

    assert result.success is True
    assert publish_calls == ["cli", "browser"]
    assert result.raw_response["publisher_kind"] == PublisherKind.SOCIAL_AUTO_UPLOAD.value
    with SessionLocal() as session:
        job = session.get(PublishJob, job_id)
        assert job.publisher_kind == PublisherKind.SOCIAL_AUTO_UPLOAD.value

    monkeypatch.setattr(metrics_mod, "default_registry", registry)
    monkeypatch.setattr(metrics_mod, "get_credential", lambda *args: {})
    import ai_ops.content.heat_engine as heat_mod

    monkeypatch.setattr(heat_mod, "recompute_topic_heat_for_article", lambda *args: None)
    collected = asyncio.run(metrics_mod.collect_one(job_id, interval_index=0))

    assert collected["views"] == 80
    assert metrics_calls == ["7788"]
    with SessionLocal() as session:
        metric = session.query(Metrics).filter(Metrics.job_id == job_id).one()
        assert metric.views == 80


def test_supported_collector_error_is_not_persisted_as_zero_or_health_signal(
    production_session_in_memory,
    monkeypatch,
):
    from ai_ops.scheduler import metrics as metrics_mod

    SessionLocal = production_session_in_memory
    job_id = _seed_job(
        SessionLocal,
        publisher_kind=PublisherKind.SOCIAL_AUTO_UPLOAD.value,
        status=JobStatus.SUCCESS,
    )
    registry, _, _ = _zhihu_registry()
    browser = registry.resolve(Platform.ZHIHU)[1]

    async def failed_collection(*args):
        return {
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "views": 0,
            "raw": {"error": "temporary network failure"},
        }

    monkeypatch.setattr(browser, "collect_metrics", failed_collection)
    monkeypatch.setattr(metrics_mod, "default_registry", registry)
    monkeypatch.setattr(metrics_mod, "get_credential", lambda *args: {})

    import ai_ops.accounts.health_monitor as health_mod
    import ai_ops.content.heat_engine as heat_mod

    monkeypatch.setattr(
        health_mod,
        "evaluate_after_metrics",
        lambda *args: pytest.fail("collection errors must not trigger account health"),
    )
    monkeypatch.setattr(
        heat_mod,
        "recompute_topic_heat_for_article",
        lambda *args: pytest.fail("collection errors must not trigger heat refresh"),
    )

    result = asyncio.run(
        metrics_mod.collect_one(
            job_id,
            interval_index=metrics_mod.HEALTH_EVAL_INTERVAL_INDEX,
        )
    )

    assert result == {
        "skipped": True,
        "reason": "collector 报告采集错误",
        "publisher_kind": PublisherKind.SOCIAL_AUTO_UPLOAD.value,
    }
    with SessionLocal() as session:
        assert session.query(Metrics).filter(Metrics.job_id == job_id).count() == 0


def test_supported_collector_exception_is_skipped_without_fake_zero(
    production_session_in_memory,
    monkeypatch,
):
    from ai_ops.scheduler import metrics as metrics_mod

    SessionLocal = production_session_in_memory
    job_id = _seed_job(
        SessionLocal,
        publisher_kind=PublisherKind.SOCIAL_AUTO_UPLOAD.value,
        status=JobStatus.SUCCESS,
    )
    registry, _, _ = _zhihu_registry()
    browser = registry.resolve(Platform.ZHIHU)[1]

    async def explode(*args):
        raise RuntimeError("credential-bearing upstream detail")

    monkeypatch.setattr(browser, "collect_metrics", explode)
    monkeypatch.setattr(metrics_mod, "default_registry", registry)
    monkeypatch.setattr(metrics_mod, "get_credential", lambda *args: {})
    monkeypatch.setattr(metrics_mod, "capture_exception", lambda *args, **kwargs: None)

    result = asyncio.run(metrics_mod.collect_one(job_id, interval_index=0))

    assert result == {
        "skipped": True,
        "reason": "collector 执行失败（RuntimeError）",
        "publisher_kind": PublisherKind.SOCIAL_AUTO_UPLOAD.value,
    }
    assert "credential-bearing" not in repr(result)
    with SessionLocal() as session:
        assert session.query(Metrics).filter(Metrics.job_id == job_id).count() == 0


def test_legacy_empty_kind_uses_only_explicit_collector(
    production_session_in_memory,
    monkeypatch,
):
    from ai_ops.scheduler import metrics as metrics_mod

    SessionLocal = production_session_in_memory
    job_id = _seed_job(SessionLocal, publisher_kind="", status=JobStatus.SUCCESS)
    registry, _, metrics_calls = _zhihu_registry()
    monkeypatch.setattr(metrics_mod, "default_registry", registry)
    monkeypatch.setattr(metrics_mod, "get_credential", lambda *args: {})
    import ai_ops.content.heat_engine as heat_mod

    monkeypatch.setattr(heat_mod, "recompute_topic_heat_for_article", lambda *args: None)

    result = asyncio.run(metrics_mod.collect_one(job_id, interval_index=0))

    assert result["views"] == 80
    assert metrics_calls == ["7788"]
