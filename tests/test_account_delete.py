"""Account deletion preserves historical PublishJob ownership."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_ops.accounts import manager as account_manager
from ai_ops.api.main import app, get_session
from ai_ops.config import settings
from ai_ops.core.db import enable_sqlite_foreign_keys
from ai_ops.core.enums import AccountHealth, ContentType, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


_LEGACY_API_KEY = "legacy-account-delete-test-key-0001"


@dataclass(frozen=True)
class AccountJobFixture:
    account_id: int
    job_id: int | None


def _create_account_and_optional_job(
    session: Session,
    *,
    job_status: JobStatus | None,
) -> AccountJobFixture:
    topic = Topic(
        name=f"account-delete-{job_status or 'empty'}",
        category="test",
        keywords=[],
        persona={},
        target_platforms=[],
    )
    account = Account(
        platform=Platform.ZHIHU,
        nickname="account-delete-target",
        profile={},
        encrypted_credential=b"",
        health=AccountHealth.HEALTHY,
    )
    session.add_all([topic, account])
    session.flush()

    if job_status is None:
        return AccountJobFixture(account_id=account.id, job_id=None)

    article = Article(
        topic_id=topic.id,
        title="historical publication",
        body="body",
        content_type=ContentType.LONG_ARTICLE,
        target_platforms=[Platform.ZHIHU.value],
        target_account_ids=[account.id],
    )
    session.add(article)
    session.flush()
    job = PublishJob(
        article_id=article.id,
        account_id=account.id,
        platform=Platform.ZHIHU,
        status=job_status,
    )
    session.add(job)
    session.flush()
    return AccountJobFixture(account_id=account.id, job_id=job.id)


@pytest.fixture
def database_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
        session.rollback()
    engine.dispose()


@pytest.fixture
def authenticated_legacy_api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )

    def override_session():
        session = testing_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    previous_overrides = dict(app.dependency_overrides)
    monkeypatch.setattr(settings, "api_key", _LEGACY_API_KEY)
    monkeypatch.setattr(settings, "agent_principals", [])
    monkeypatch.setattr(settings, "legacy_dev_auth_bypass", False)
    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    try:
        yield client, testing_session
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        engine.dispose()


@pytest.mark.parametrize("job_status", list(JobStatus))
def test_manager_refuses_deletion_for_every_publish_job_state(
    database_session,
    job_status,
):
    fixture = _create_account_and_optional_job(
        database_session,
        job_status=job_status,
    )

    with pytest.raises(ValueError, match="已有发布记录"):
        account_manager.delete_account(database_session, fixture.account_id)
    database_session.flush()
    database_session.expire_all()

    account = database_session.get(Account, fixture.account_id)
    job = database_session.get(PublishJob, fixture.job_id)
    assert account is not None
    assert job is not None
    assert job.account_id == account.id
    assert job.status == job_status


def test_authenticated_delete_with_a_job_returns_409_and_preserves_rows(
    authenticated_legacy_api,
):
    client, testing_session = authenticated_legacy_api
    with testing_session.begin() as session:
        fixture = _create_account_and_optional_job(
            session,
            job_status=JobStatus.SUCCESS,
        )

    response = client.delete(
        f"/accounts/{fixture.account_id}",
        headers={"X-API-Key": _LEGACY_API_KEY},
    )

    assert response.status_code == 409
    assert "已有发布记录" in response.json()["detail"]
    with testing_session() as session:
        account = session.get(Account, fixture.account_id)
        job = session.get(PublishJob, fixture.job_id)
        assert account is not None
        assert job is not None
        assert job.account_id == account.id
        assert (
            session.scalar(select(PublishJob.id).where(PublishJob.account_id == account.id))
            == job.id
        )


def test_authenticated_delete_without_jobs_remains_200(authenticated_legacy_api):
    client, testing_session = authenticated_legacy_api
    with testing_session.begin() as session:
        fixture = _create_account_and_optional_job(session, job_status=None)

    unauthenticated = client.delete(f"/accounts/{fixture.account_id}")
    assert unauthenticated.status_code == 401
    with testing_session() as session:
        assert session.get(Account, fixture.account_id) is not None

    response = client.delete(
        f"/accounts/{fixture.account_id}",
        headers={"X-API-Key": _LEGACY_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": fixture.account_id}
    with testing_session() as session:
        assert session.get(Account, fixture.account_id) is None
        assert (
            session.scalar(select(PublishJob.id).where(PublishJob.account_id == fixture.account_id))
            is None
        )


def test_account_identity_update_distinguishes_invalid_input_from_missing_account(
    authenticated_legacy_api,
):
    client, testing_session = authenticated_legacy_api
    with testing_session.begin() as session:
        fixture = _create_account_and_optional_job(session, job_status=None)

    invalid = client.patch(
        f"/accounts/{fixture.account_id}",
        headers={"X-API-Key": _LEGACY_API_KEY},
        json={"external_account_id": "mutable-url-token"},
    )
    missing = client.patch(
        "/accounts/999999",
        headers={"X-API-Key": _LEGACY_API_KEY},
        json={"external_account_id": "zhihu:id:stable-person"},
    )

    assert invalid.status_code == 400
    assert "zhihu:id" in invalid.json()["detail"]
    assert missing.status_code == 404
    with testing_session() as session:
        account = session.get(Account, fixture.account_id)
        assert account is not None
        assert "external_account_id" not in account.profile
