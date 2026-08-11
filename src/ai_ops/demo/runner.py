"""A deterministic five-minute-value demo of the creator-ops control plane."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import URL, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..content import distributor
from ..core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ..core.models import Account, Article, Base, Metrics, PublishJob, Topic
from .backends import FAKE_PUBLISHER_KIND, FakePublisher
from ..publishers.registry import PublisherRegistry
from ..runtime.account_lease import AccountOperationLease
from ..runtime.receipts import (
    read_publish_receipt,
    remove_publish_receipt,
    write_publish_receipt,
)
from ..scheduler.worker import WorkerExecutionContext, execute_job


DEMO_VERSION = "offline-demo-v1"
DEMO_TOPIC = "Agent 原生内容运营"
DEMO_TITLE = "一条内容如何安全走完审核、发布与数据回流"
DEMO_BODY = (
    "这是一条完全离线的演示内容。它会经过显式审核状态转换、持久任务、"
    "可核验发布回执与指标回流，但不会连接任何真实平台。"
)


class DemoStage(BaseModel):
    name: str
    passed: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class DemoPublishPlan(BaseModel):
    mode: str = "dry_run"
    article_id: int
    account_id: int
    platform: str
    publisher_kind: str = FAKE_PUBLISHER_KIND
    would_create_jobs: int = 1
    external_calls: int = 0
    credentials_required: bool = False


class DemoStorageSummary(BaseModel):
    kind: str = "sqlite_file"
    isolated: bool = True
    database_path: str | None = None
    retained: bool = False
    cleanup_performed: bool = False


class DemoEntitySummary(BaseModel):
    topic_id: int
    account_id: int
    article_id: int
    job_id: int
    metric_id: int


class DemoPublishSummary(BaseModel):
    success: bool
    publisher_kind: str
    platform_post_id: str
    platform_url: str
    effect_applied: bool


class DemoMetricsSummary(BaseModel):
    source: str
    snapshots: int
    initial_views: int
    views: int
    likes: int
    comments: int
    shares: int


class DemoReviewSummary(BaseModel):
    passed: bool
    checks: dict[str, bool]
    row_counts: dict[str, int]
    article_status: str
    job_status: str


class DemoRunSummary(BaseModel):
    """Stable result model for both ``--json`` and human CLI rendering."""

    demo_version: str = DEMO_VERSION
    ok: bool
    exit_code: int
    synthetic: bool = True
    notice: str = "SYNTHETIC — NO EXTERNAL ACTION"
    offline: bool = True
    external_calls: int = 0
    credentials_used: bool = False
    plan: DemoPublishPlan
    entities: DemoEntitySummary
    publish: DemoPublishSummary
    metrics: DemoMetricsSummary
    review: DemoReviewSummary
    stages: list[DemoStage]
    storage: DemoStorageSummary

    def to_human_text(self) -> str:
        state = "PASS" if self.review.passed else "FAIL"
        storage = self.storage.database_path if self.storage.retained else "已安全清理"
        return "\n".join(
            (
                f"离线演示：{state}",
                self.notice,
                f"链路：{' → '.join(stage.name for stage in self.stages)}",
                f"任务：#{self.entities.job_id} {self.review.job_status}",
                f"回执：{self.publish.platform_post_id}",
                (
                    "指标："
                    f"{self.metrics.views} 浏览 / {self.metrics.likes} 赞 / "
                    f"{self.metrics.comments} 评论 / {self.metrics.shares} 分享"
                ),
                f"外部调用：{self.external_calls}；凭证：未使用；数据：{storage}",
            )
        )


def build_dry_run_plan(
    session: Session,
    *,
    article_id: int,
    account_id: int,
) -> DemoPublishPlan:
    """Validate a publish target and describe it without creating a job."""
    article = session.get(Article, article_id)
    account = session.get(Account, account_id)
    if article is None:
        raise ValueError(f"article {article_id} not found")
    if account is None:
        raise ValueError(f"account {account_id} not found")
    if ArticleStatus(article.status) != ArticleStatus.READY:
        raise ValueError("dry-run planning requires a reviewed READY article")
    if AccountHealth(account.health) not in {
        AccountHealth.HEALTHY,
        AccountHealth.UNKNOWN,
    }:
        raise ValueError("dry-run planning requires a runnable account")
    platform = Platform(account.platform)
    targets = {Platform(value) for value in (article.target_platforms or [])}
    if platform not in targets:
        raise ValueError("account platform is not one of the article targets")
    return DemoPublishPlan(
        article_id=article.id,
        account_id=account.id,
        platform=platform.value,
    )


def _new_session_factory(database_path: Path):
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database_path)),
        future=True,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    return engine, factory


def _demo_database_candidates(database_path: Path) -> tuple[Path, ...]:
    return (
        database_path,
        Path(f"{database_path}-journal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-wal"),
    )


def _assert_demo_targets_available(database_path: Path) -> None:
    """Refuse every target name, including dangling symlinks, before work."""
    if any(os.path.lexists(candidate) for candidate in _demo_database_candidates(database_path)):
        raise FileExistsError("offline demo database or sidecar already exists")


def _session_scope_factory(session_factory):
    @contextmanager
    def scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def _final_review(session: Session, *, article_id: int, job_id: int) -> DemoReviewSummary:
    article = session.get(Article, article_id)
    job = session.get(PublishJob, job_id)
    account = session.get(Account, job.account_id) if job is not None else None
    initial_metric = session.scalar(
        select(Metrics).where(Metrics.job_id == job_id, Metrics.source == "initial")
    )
    demo_metric = session.scalar(
        select(Metrics).where(Metrics.job_id == job_id, Metrics.source == "demo")
    )
    row_counts = {
        "topics": session.scalar(select(func.count()).select_from(Topic)) or 0,
        "accounts": session.scalar(select(func.count()).select_from(Account)) or 0,
        "articles": session.scalar(select(func.count()).select_from(Article)) or 0,
        "publish_jobs": session.scalar(select(func.count()).select_from(PublishJob)) or 0,
        "metrics": session.scalar(select(func.count()).select_from(Metrics)) or 0,
    }
    checks = {
        "expected_row_counts": row_counts
        == {
            "topics": 1,
            "accounts": 1,
            "articles": 1,
            "publish_jobs": 1,
            "metrics": 2,
        },
        "article_reached_published": (
            article is not None and ArticleStatus(article.status) == ArticleStatus.PUBLISHED
        ),
        "durable_job_succeeded": (
            job is not None and JobStatus(job.status) == JobStatus.SUCCESS
        ),
        "fake_publisher_receipt_persisted": bool(
            job is not None
            and job.publisher_kind == FAKE_PUBLISHER_KIND
            and job.platform_post_id
            and job.platform_url
        ),
        "fake_metrics_persisted": bool(
            demo_metric is not None
            and demo_metric.views == 128
            and demo_metric.likes == 17
            and demo_metric.comments == 4
            and demo_metric.shares == 3
        ),
        "initial_metrics_persisted_by_worker": bool(
            initial_metric is not None and initial_metric.views == 1
        ),
        "zero_credentials": account is not None and account.encrypted_credential == b"",
        "synthetic_markers_persisted": bool(
            job is not None
            and (job.raw_response or {}).get("demo") is True
            and (job.raw_response or {}).get("synthetic") is True
            and demo_metric is not None
            and (demo_metric.raw or {}).get("demo") is True
            and (demo_metric.raw or {}).get("synthetic") is True
        ),
    }
    return DemoReviewSummary(
        passed=all(checks.values()),
        checks=checks,
        row_counts=row_counts,
        article_status=(ArticleStatus(article.status).value if article is not None else "missing"),
        job_status=(JobStatus(job.status).value if job is not None else "missing"),
    )


async def run_offline_demo(
    database_path: str | Path | None = None,
    *,
    keep_data: bool | None = None,
) -> DemoRunSummary:
    """Run the full offline demo against a newly-created SQLite database.

    ``database_path`` must not already exist, so the demo can never overwrite a
    real or prior database.  With no explicit path, a private temporary
    directory is created.  Temporary data is removed by default; an explicit
    path is retained by default.  ``keep_data`` can override either default.
    """
    owned_temp_dir: Path | None = None
    requested_path: Path | None = None
    if database_path is None:
        owned_temp_dir = Path(tempfile.mkdtemp(prefix="ai-ops-offline-demo-"))
        db_path = owned_temp_dir / "demo.sqlite3"
        retain = bool(keep_data) if keep_data is not None else False
    else:
        requested_path = Path(
            os.path.abspath(Path(database_path).expanduser())
        )
        retain = bool(keep_data) if keep_data is not None else True
        _assert_demo_targets_available(requested_path)
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        _assert_demo_targets_available(requested_path)
        # Execute inside a private sibling directory. The requested path is
        # materialized only after a successful, closed database run, so cleanup
        # never needs to delete caller-owned sidecar names.
        owned_temp_dir = Path(
            tempfile.mkdtemp(
                prefix=".ai-ops-offline-demo-",
                dir=requested_path.parent,
            )
        )
        db_path = owned_temp_dir / "demo.sqlite3"

    # Atomic exclusive creation closes the check/open race and ensures a demo
    # never adopts or truncates an existing database.
    db_path.touch(mode=0o600, exist_ok=False)
    engine = None
    runtime_dir = Path(tempfile.mkdtemp(prefix="ai-ops-offline-runtime-"))
    stages: list[DemoStage] = []
    completed_summary: DemoRunSummary | None = None
    cleanup_performed = False
    try:
        engine, SessionLocal = _new_session_factory(db_path)
        Base.metadata.create_all(engine)

        # 1. Ingest into the content library as a DRAFT.
        with SessionLocal.begin() as session:
            topic = Topic(
                name=DEMO_TOPIC,
                category="demo",
                keywords=["agent", "creator ops"],
                persona={"voice": "clear"},
                target_platforms=[Platform.ZHIHU.value],
                notes="offline demo",
            )
            session.add(topic)
            session.flush()
            account = Account(
                platform=Platform.ZHIHU,
                nickname="离线演示账号",
                profile={"demo": True},
                topic_id=topic.id,
                encrypted_credential=b"",
                health=AccountHealth.HEALTHY,
                daily_quota=1,
                # Keep the demo account outside the production nurture gate so
                # the real worker can exercise its normal rate-limit check.
                created_at=datetime(2000, 1, 1),
            )
            session.add(account)
            session.flush()
            article = distributor.stage_to_library(
                session,
                topic_id=topic.id,
                title=DEMO_TITLE,
                body=DEMO_BODY,
                content_type=ContentType.LONG_ARTICLE,
                target_platforms=[Platform.ZHIHU],
                extra={"demo": True, "tags": ["AI Agent", "内容运营"]},
            )
            topic_id, account_id, article_id = topic.id, account.id, article.id
        stages.append(
            DemoStage(
                name="ingest",
                details={"article_status": ArticleStatus.DRAFT.value},
            )
        )

        # 2. Exercise the same review gate used by normal content distribution.
        with SessionLocal.begin() as session:
            reviewed = distributor.approve(session, article_id)
            reviewed_status = ArticleStatus(reviewed.status).value
        stages.append(DemoStage(name="review", details={"article_status": reviewed_status}))

        # 3. Produce a side-effect-free plan and prove it created no job.
        with SessionLocal() as session:
            jobs_before = session.scalar(select(func.count()).select_from(PublishJob)) or 0
            plan = build_dry_run_plan(
                session,
                article_id=article_id,
                account_id=account_id,
            )
            jobs_after = session.scalar(select(func.count()).select_from(PublishJob)) or 0
        if jobs_before != jobs_after:
            raise RuntimeError("dry-run plan unexpectedly mutated durable jobs")
        stages.append(
            DemoStage(
                name="dry-run plan",
                details={"external_calls": 0, "jobs_created": 0},
            )
        )

        # 4. Create and commit the durable job through the real distributor.
        with SessionLocal.begin() as session:
            jobs = distributor.distribute(session, article_id, account_ids=[account_id])
            job_id = jobs[0].id

        # Dispose every connection and rebuild the engine before continuing.
        # Finding the PENDING row after this boundary demonstrates persistence,
        # rather than accidentally relying on one session's identity map.
        engine.dispose()
        engine, SessionLocal = _new_session_factory(db_path)
        with SessionLocal() as session:
            durable_job = session.get(PublishJob, job_id)
            durable = durable_job is not None and durable_job.status == JobStatus.PENDING
        if not durable:
            raise RuntimeError("publish job did not survive database reopen")
        stages.append(
            DemoStage(
                name="durable job",
                details={"job_status": JobStatus.PENDING.value, "survived_reopen": True},
            )
        )

        # 5. Claim, execute, and finalize through the real worker state machine,
        # with every side-effecting dependency made explicit and local.
        publisher = FakePublisher()
        registry = PublisherRegistry()
        registry.register(Platform.ZHIHU, lambda: publisher, priority=1)
        suppressed_metric_schedules: list[int] = []
        suppressed_notifications: list[dict] = []
        execution_context = WorkerExecutionContext(
            session_scope_factory=_session_scope_factory(SessionLocal),
            registry=registry,
            schedule_after_publish=lambda claimed_job_id: suppressed_metric_schedules.append(
                claimed_job_id
            ),
            notify_success=lambda snapshot: suppressed_notifications.append(dict(snapshot)),
            notify_failed=lambda snapshot: suppressed_notifications.append(dict(snapshot)),
            similarity_checker=lambda **kwargs: False,
            rate_limit_checker=lambda *_args, **_kwargs: SimpleNamespace(
                allowed=True,
                reason="offline demo policy",
                retry_at=None,
            ),
            account_lease_factory=lambda leased_account_id, *, timeout_seconds: AccountOperationLease(
                leased_account_id,
                timeout_seconds=timeout_seconds,
                data_dir=runtime_dir,
            ),
            receipt_writer=lambda **kwargs: write_publish_receipt(
                **kwargs,
                data_dir=runtime_dir,
            ),
            receipt_reader=lambda receipt_job_id, operation_id: read_publish_receipt(
                receipt_job_id,
                operation_id,
                data_dir=runtime_dir,
            ),
            receipt_remover=lambda receipt_job_id, operation_id: remove_publish_receipt(
                receipt_job_id,
                operation_id,
                data_dir=runtime_dir,
            ),
            receipt_data_dir=runtime_dir,
            report_exception=lambda *_args, **_kwargs: False,
            job_execution_timeout_seconds=30,
            account_operation_lock_timeout_seconds=5,
        )
        publish_result = await execute_job(
            job_id,
            execution_context=execution_context,
        )
        if not publish_result.success or not publish_result.effect_applied:
            raise RuntimeError("fake publisher failed its deterministic contract")
        stages.append(
            DemoStage(
                name="fake publish",
                details={
                    "job_status": JobStatus.SUCCESS.value,
                    "external_calls": 0,
                    "real_worker_state_machine": True,
                    "metric_schedules_suppressed": len(suppressed_metric_schedules),
                    "notifications_suppressed": len(suppressed_notifications),
                },
            )
        )

        # 6. Collect through the explicit fake metrics backend and persist it.
        metric_data = await publisher.collect_metrics(
            publish_result.platform_post_id or "",
            publish_result.platform_url,
            {},
        )
        with SessionLocal.begin() as session:
            metric = Metrics(
                job_id=job_id,
                views=metric_data["views"],
                likes=metric_data["likes"],
                comments=metric_data["comments"],
                shares=metric_data["shares"],
                raw=metric_data["raw"],
                source="demo",
            )
            session.add(metric)
            session.flush()
            metric_id = metric.id
        stages.append(DemoStage(name="fake metrics", details={"metrics_rows": 2}))

        # 7. Reopen once more and review only committed database state.
        engine.dispose()
        engine, SessionLocal = _new_session_factory(db_path)
        with SessionLocal() as session:
            review = _final_review(session, article_id=article_id, job_id=job_id)
        stages.append(DemoStage(name="final review", passed=review.passed, details=review.checks))

        completed_summary = DemoRunSummary(
            ok=review.passed,
            exit_code=0 if review.passed else 1,
            plan=plan,
            entities=DemoEntitySummary(
                topic_id=topic_id,
                account_id=account_id,
                article_id=article_id,
                job_id=job_id,
                metric_id=metric_id,
            ),
            publish=DemoPublishSummary(
                success=publish_result.success,
                publisher_kind=FAKE_PUBLISHER_KIND,
                platform_post_id=publish_result.platform_post_id or "",
                platform_url=publish_result.platform_url or "",
                effect_applied=bool(publish_result.effect_applied),
            ),
            metrics=DemoMetricsSummary(
                source="demo",
                snapshots=2,
                initial_views=1,
                views=metric_data["views"],
                likes=metric_data["likes"],
                comments=metric_data["comments"],
                shares=metric_data["shares"],
            ),
            review=review,
            stages=stages,
            storage=DemoStorageSummary(),
        )
        if engine is not None:
            engine.dispose()
            engine = None
        if requested_path is not None and retain:
            _assert_demo_targets_available(requested_path)
            # The private working directory is a sibling, so hard-linking is an
            # atomic create-without-overwrite operation on the same filesystem.
            os.link(db_path, requested_path)
    finally:
        if engine is not None:
            engine.dispose()
        try:
            shutil.rmtree(runtime_dir)
        except OSError:
            pass
        runtime_cleanup_performed = not os.path.lexists(runtime_dir)
        should_remove_owned_dir = owned_temp_dir is not None and (
            requested_path is not None or not retain or completed_summary is None
        )
        owned_cleanup_performed = False
        if should_remove_owned_dir:
            try:
                shutil.rmtree(owned_temp_dir)
            except OSError:
                pass
            owned_cleanup_performed = not os.path.lexists(owned_temp_dir)
        if completed_summary is not None and not retain:
            cleanup_performed = runtime_cleanup_performed and owned_cleanup_performed

    if completed_summary is None:  # pragma: no cover - the original error propagates
        raise RuntimeError("offline demo did not complete")
    actual_retained = retain or os.path.lexists(db_path)
    if retain:
        visible_database_path: Path | None = requested_path or db_path
    elif actual_retained and db_path.is_file():
        # Cleanup failures must be visible to callers; never claim that a
        # credential-free demo database disappeared when it remains on disk.
        visible_database_path = db_path
    else:
        visible_database_path = None
    completed_summary.storage = DemoStorageSummary(
        database_path=(str(visible_database_path) if visible_database_path else None),
        retained=actual_retained,
        cleanup_performed=cleanup_performed,
    )
    return completed_summary
