"""The generic account login route shares the account/profile lease."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from ai_ops.api.main import app
from ai_ops.config import settings
from ai_ops.core import db as db_mod
from ai_ops.core.db import enable_sqlite_foreign_keys
from ai_ops.core.enums import AccountHealth, Platform
from ai_ops.core.models import Account, Base
from ai_ops.core.schemas import PublishResult
from ai_ops.publishers.base import PublisherBase
from ai_ops.publishers.plugin_sdk import (
    PUBLISHER_PLUGIN_API_VERSION,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
    instantiate_validated_publisher,
)
from ai_ops.publishers.registry import default_registry
from ai_ops.runtime import account_lease as lease_mod


_LEGACY_API_KEY = "legacy-account-login-test-key-0001"


@pytest.fixture
def authenticated_login_api(monkeypatch) -> Iterator[tuple[TestClient, object, int]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    monkeypatch.setattr(settings, "api_key", _LEGACY_API_KEY)
    monkeypatch.setattr(settings, "agent_principals", [])
    monkeypatch.setattr(settings, "legacy_dev_auth_bypass", False)

    with db_mod.SessionLocal.begin() as session:
        account = Account(
            platform=Platform.ZHIHU,
            nickname="lease-protected-login",
            profile={},
            encrypted_credential=b"",
            health=AccountHealth.HEALTHY,
        )
        session.add(account)
        session.flush()
        account_id = account.id

    client = TestClient(app)
    try:
        yield client, db_mod.SessionLocal, account_id
    finally:
        client.close()
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _post_login(client: TestClient, account_id: int):
    return client.post(
        f"/accounts/{account_id}/login",
        headers={"X-API-Key": _LEGACY_API_KEY},
    )


def test_login_holds_account_lease_through_credential_commit(
    authenticated_login_api,
    monkeypatch,
):
    client, SessionLocal, account_id = authenticated_login_api
    state = {"held": False, "publisher_called": False, "encrypted": None}

    class TrackingLease:
        def __init__(self, leased_account_id, *, timeout_seconds):
            assert leased_account_id == account_id
            assert timeout_seconds == settings.account_operation_lock_timeout_seconds

        async def __aenter__(self):
            assert state["held"] is False
            state["held"] = True
            return self

        async def __aexit__(self, *_args):
            assert state["held"] is True
            state["held"] = False

    class Publisher:
        async def login(self, login_account_id, credential):
            assert login_account_id == account_id
            assert state["held"] is True
            state["publisher_called"] = True
            credential["cookies"] = [{"name": "session", "value": "fresh"}]
            return True

    class Store:
        def encrypt(self, credential):
            assert state["held"] is True
            state["encrypted"] = credential
            return b"sealed-login-credential"

    monkeypatch.setattr(lease_mod, "AccountOperationLease", TrackingLease)
    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [Publisher()])
    monkeypatch.setattr("ai_ops.accounts.store.get_store", lambda: Store())

    response = _post_login(client, account_id)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "account_id": account_id, "error": None}
    assert state == {
        "held": False,
        "publisher_called": True,
        "encrypted": {"cookies": [{"name": "session", "value": "fresh"}]},
    }
    with SessionLocal() as session:
        assert session.get(Account, account_id).encrypted_credential == b"sealed-login-credential"


def test_login_lock_contention_is_generic_and_never_enters_publisher(
    authenticated_login_api,
    monkeypatch,
):
    client, _SessionLocal, account_id = authenticated_login_api
    called = False

    class BusyLease:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            raise lease_mod.AccountOperationLeaseTimeout("private lock path")

        async def __aexit__(self, *_args):
            return None

    class Publisher:
        async def login(self, *_args):
            nonlocal called
            called = True
            return True

    monkeypatch.setattr(lease_mod, "AccountOperationLease", BusyLease)
    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [Publisher()])

    response = _post_login(client, account_id)

    assert response.status_code == 409
    assert response.json() == {"detail": "账号正在执行其他操作，请稍后重试"}
    assert "private lock path" not in response.text
    assert called is False


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_detail"),
    [
        (TimeoutError("private timeout detail"), 408, "登录超时（5 分钟内未完成扫码）"),
        (RuntimeError("private adapter token"), 503, "登录服务暂时不可用，请稍后重试"),
    ],
)
def test_login_failures_are_generic_and_release_the_account_lease(
    authenticated_login_api,
    monkeypatch,
    failure,
    expected_status,
    expected_detail,
):
    client, _SessionLocal, account_id = authenticated_login_api
    state = {"held": False}

    class TrackingLease:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            state["held"] = True
            return self

        async def __aexit__(self, *_args):
            state["held"] = False

    class Publisher:
        async def login(self, *_args):
            assert state["held"] is True
            raise failure

    monkeypatch.setattr(lease_mod, "AccountOperationLease", TrackingLease)
    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [Publisher()])

    response = _post_login(client, account_id)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert str(failure) not in response.text
    assert state["held"] is False


def test_unsuccessful_login_does_not_reflect_adapter_error_text(
    authenticated_login_api,
    monkeypatch,
):
    client, _SessionLocal, account_id = authenticated_login_api

    class NoopLease:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Publisher:
        last_login_error = "private adapter output with credential"

        async def login(self, *_args):
            return False

    monkeypatch.setattr(lease_mod, "AccountOperationLease", NoopLease)
    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [Publisher()])

    response = _post_login(client, account_id)

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "account_id": account_id,
        "error": "登录未成功；请检查账号状态或在可信终端完成登录",
    }
    assert "private adapter output" not in response.text


def test_plugin_login_system_exit_is_generic_and_does_not_stop_api(
    authenticated_login_api,
    monkeypatch,
):
    client, _SessionLocal, account_id = authenticated_login_api

    class NoopLease:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class ExitingLoginPublisher(PublisherBase):
        platform = Platform.ZHIHU
        kind = "fixture_login"

        async def login(self, account_id, credential):
            raise SystemExit("token=must-not-leak")

        async def publish(self, account_id, credential, content):
            return PublishResult(success=True, platform_post_id="unused")

        async def health_check(self, account_id, credential):
            return AccountHealth.HEALTHY

    plugin = PublisherPlugin(
        manifest=PublisherPluginManifest(
            plugin_id="fixture.login",
            plugin_version="1.0.0",
            api_version=PUBLISHER_PLUGIN_API_VERSION,
            platform=Platform.ZHIHU,
            publisher_kind="fixture_login",
            adapter_version="1",
            capabilities=(
                PublisherPluginCapability.HEALTH_CHECK,
                PublisherPluginCapability.LOGIN,
                PublisherPluginCapability.PUBLISH,
            ),
        ),
        factory=ExitingLoginPublisher,
    )
    publisher = instantiate_validated_publisher(
        "fixture-ai-ops:fixture.login",
        plugin,
    )
    monkeypatch.setattr(lease_mod, "AccountOperationLease", NoopLease)
    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [publisher])

    response = _post_login(client, account_id)

    assert response.status_code == 503
    assert response.json() == {"detail": "登录服务暂时不可用，请稍后重试"}
    assert "must-not-leak" not in response.text
