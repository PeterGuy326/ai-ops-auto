"""BANNED account recovery is probe-only and fail-closed."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.core.enums import AccountHealth, Platform
from ai_ops.core.models import Account, Base


@pytest.fixture
def health_env(monkeypatch):
    from ai_ops.scheduler import health as health_mod

    engine = create_engine("sqlite:///:memory:", future=True)
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
    try:
        yield health_mod, SessionLocal
    finally:
        engine.dispose()


def _banned_account(SessionLocal, *, probe_at: datetime) -> int:
    with SessionLocal() as session:
        account = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="banned-account",
            profile={"paused_until": probe_at.isoformat(), "paused_reason": "risk"},
            health=AccountHealth.BANNED,
            encrypted_credential=b"",
        )
        session.add(account)
        session.commit()
        return account.id


def test_banned_account_is_not_probed_before_deadline(health_env, monkeypatch):
    health_mod, SessionLocal = health_env
    account_id = _banned_account(
        SessionLocal, probe_at=datetime.utcnow() + timedelta(days=1)
    )
    called = False

    async def should_not_probe(account_id, credential):
        nonlocal called
        called = True
        return AccountHealth.HEALTHY

    publisher = MagicMock()
    publisher.health_check = should_not_probe
    monkeypatch.setattr(
        health_mod.default_registry, "resolve", lambda platform: [publisher]
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert result["count"] == 0
    assert account_id not in result["results"]
    assert called is False
    with SessionLocal() as session:
        assert session.get(Account, account_id).health == AccountHealth.BANNED


def test_due_banned_account_recovers_only_after_healthy_probe(health_env, monkeypatch):
    health_mod, SessionLocal = health_env
    account_id = _banned_account(
        SessionLocal, probe_at=datetime.utcnow() - timedelta(seconds=1)
    )

    async def healthy(account_id, credential):
        return AccountHealth.HEALTHY

    publisher = MagicMock()
    publisher.health_check = healthy
    monkeypatch.setattr(
        health_mod.default_registry, "resolve", lambda platform: [publisher]
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert result["results"][account_id] == AccountHealth.HEALTHY.value
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.HEALTHY
        assert "paused_until" not in account.profile
        assert "paused_reason" not in account.profile


@pytest.mark.parametrize(
    "observed",
    [
        AccountHealth.UNKNOWN,
        AccountHealth.DEGRADED,
        AccountHealth.EXPIRED,
        AccountHealth.BANNED,
    ],
)
def test_due_banned_account_remains_banned_on_nonhealthy_probe(
    health_env, monkeypatch, observed
):
    health_mod, SessionLocal = health_env
    account_id = _banned_account(
        SessionLocal, probe_at=datetime.utcnow() - timedelta(seconds=1)
    )

    async def unhealthy(account_id, credential):
        return observed

    publisher = MagicMock()
    publisher.health_check = unhealthy
    monkeypatch.setattr(
        health_mod.default_registry, "resolve", lambda platform: [publisher]
    )

    result = asyncio.run(health_mod.check_all_accounts())

    assert result["results"][account_id] == AccountHealth.BANNED.value
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        assert account.health == AccountHealth.BANNED
        assert account.profile["last_ban_probe_health"] == observed.value
        assert account.profile["last_ban_probe_at"]
