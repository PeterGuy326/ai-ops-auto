"""``scripts/canary_prepare.py``: the canary gate, offline.

The script drives a public write, so its contracts are pinned here without a
platform, a server or a real account.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from ai_ops.config import settings
from ai_ops.core import db as db_mod
from ai_ops.core.db import enable_sqlite_foreign_keys
from ai_ops.core.enums import AccountHealth, Platform
from ai_ops.core.models import Account, Article, PublishJob
from ai_ops.core.schemas import PublishResult

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "canary_prepare.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("canary_prepare_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canary = _load_script()


def _stdin(text: str) -> io.StringIO:
    return io.StringIO(text)


def _grant() -> str:
    return '{"app_id":"a","app_secret":"b","refresh_token":"first"}'


class _Api:
    """Stand-in for a token-based adapter: records calls and rotates the grant."""

    kind = "kuaishou_open_api"
    platform = Platform.KUAISHOU
    last_login_error: str | None = None

    def __init__(self, *, login_ok=True, publish_result=None):
        self.login_ok = login_ok
        self.publish_result = publish_result
        self.grants_issued: list[str] = []
        self.lock_held: list[bool] = []

    def _rotate(self, credential):
        self.grants_issued.append(f"rotated-{len(self.grants_issued) + 1}")
        credential["refresh_token"] = self.grants_issued[-1]

    async def login(self, account_id, credential):
        if not self.login_ok:
            self.last_login_error = "快手凭证校验失败（result 100200101）"
            return False
        self._rotate(credential)
        return True

    async def health_check(self, account_id, credential):
        self._rotate(credential)  # the real adapter's probe refreshes again
        return AccountHealth.HEALTHY

    async def publish(self, account_id, credential, content):
        if self.publish_result is None:
            return PublishResult(
                success=True, effect_applied=True, platform_post_id="PHOTO123"
            )
        return self.publish_result


class _Browser:
    """The next Publisher in the chain: what the worker falls through to."""

    kind = "social_auto_upload"
    platform = Platform.KUAISHOU

    async def login(self, *_):
        return True

    async def health_check(self, *_):
        return AccountHealth.HEALTHY

    async def publish(self, *_):
        return PublishResult(
            success=True, effect_applied=True, platform_post_id="BROWSER-POST"
        )


@pytest.fixture
def canary_env(monkeypatch):
    """In-memory database bound to ``SessionLocal``, stand-in adapters, no subprocess."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    enable_sqlite_foreign_keys(engine)
    db_mod.Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    monkeypatch.setattr(settings, "fernet_key", Fernet.generate_key().decode())
    db_mod.SessionLocal.configure(bind=engine)
    api = _Api()
    browser = _Browser()
    from ai_ops.publishers.registry import default_registry

    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [api, browser])
    monkeypatch.setattr(canary, "read_doctor", lambda: {"checks": [
        {
            "id": "safety.publication",
            "outcome": "warn",
            "details": {"enabled_capabilities": ["kuaishou_open_api"]},
        }
    ]})
    monkeypatch.setattr(settings, "nurture_days", 0)
    monkeypatch.setattr(settings, "publish_min_interval_seconds", 0)
    env = {"api": api, "browser": browser}
    try:
        yield env
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _rows(model) -> int:
    with db_mod.SessionLocal() as session:
        return session.query(model).count()


def _prepare(monkeypatch, name="ks", stdin_text=None) -> int:
    """Run ``prepare`` with the given stdin; the default is a complete grant."""
    monkeypatch.setattr(sys, "stdin", _stdin(_grant() if stdin_text is None else stdin_text))
    return canary.main(["prepare", "kuaishou", "--name", name])


# ---------------------------------------------------------------- prepare


def test_prepare_stops_before_publishing(canary_env, monkeypatch, capsys):
    assert _prepare(monkeypatch) == 0

    assert _rows(Account) == 1
    assert _rows(PublishJob) == 0
    assert _rows(Article) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "publish kuaishou --account-id" in output


def test_prepare_reuses_the_same_named_account(canary_env, monkeypatch):
    for _ in range(3):
        assert _prepare(monkeypatch) == 0

    assert _rows(Account) == 1


def test_prepare_reports_the_adapters_real_reason(canary_env, monkeypatch, capsys):
    canary_env["api"].login_ok = False

    assert _prepare(monkeypatch) == 1

    assert "100200101" in capsys.readouterr().err


def test_prepare_creates_nothing_for_a_bad_grant(canary_env, monkeypatch):
    for text in ("", "not json", "[1, 2]", '{"app_id": "a", "app_secret": "b"}'):
        assert _prepare(monkeypatch, stdin_text=text) == 1

    assert _rows(Account) == 0


def test_prepare_refuses_when_the_switch_is_off(canary_env, monkeypatch, capsys):
    monkeypatch.setattr(canary, "read_doctor", lambda: {"checks": [
        {"id": "safety.publication", "outcome": "pass", "details": {
            "enabled_capabilities": []}}
    ]})

    assert _prepare(monkeypatch) == 1

    assert _rows(Account) == 0
    assert "KUAISHOU_OPEN_API_ENABLED" in capsys.readouterr().err


# ------------------------------------------------- the grant that gets stored


def _stored_grant(account_id: int) -> tuple[dict, AccountHealth]:
    from ai_ops.accounts.manager import get_credential

    with db_mod.SessionLocal() as session:
        return get_credential(session, account_id), AccountHealth(
            session.get(Account, account_id).health
        )


def test_the_last_rotated_grant_is_what_gets_stored(canary_env, monkeypatch):
    """Kuaishou's refresh token is single-use: storing an earlier one strands the account."""
    assert _prepare(monkeypatch) == 0
    api = canary_env["api"]

    stored, health = _stored_grant(1)

    assert api.grants_issued == ["rotated-1", "rotated-2"]  # login, then the probe
    assert stored["refresh_token"] == "rotated-2"
    assert stored["app_secret"] == "b"
    assert health is AccountHealth.HEALTHY


def test_refresh_and_probe_both_run_under_the_account_lock(canary_env, monkeypatch):
    """Outside the lock a worker or health probe could read, or rotate, the same grant."""
    held: list[tuple[str, bool]] = []
    from ai_ops.runtime.account_lease import AccountOperationLease

    original_enter, original_exit = AccountOperationLease.__aenter__, AccountOperationLease.__aexit__
    state = {"held": False}

    async def enter(self):
        result = await original_enter(self)
        state["held"] = True
        return result

    async def leave(self, *args):
        state["held"] = False
        return await original_exit(self, *args)

    monkeypatch.setattr(AccountOperationLease, "__aenter__", enter)
    monkeypatch.setattr(AccountOperationLease, "__aexit__", leave)
    api = canary_env["api"]
    original_login, original_health = api.login, api.health_check

    async def login(account_id, credential):
        held.append(("login", state["held"]))
        return await original_login(account_id, credential)

    async def health(account_id, credential):
        held.append(("health", state["held"]))
        return await original_health(account_id, credential)

    monkeypatch.setattr(api, "login", login)
    monkeypatch.setattr(api, "health_check", health)
    assert _prepare(monkeypatch) == 0

    assert [step for step, _ in held] == ["login", "health"]
    assert all(is_held for _, is_held in held)


def test_a_failed_grant_leaves_no_token_behind(canary_env, monkeypatch):
    canary_env["api"].login_ok = False

    assert _prepare(monkeypatch) == 1

    stored, _health = _stored_grant(1)
    assert stored["refresh_token"] == "first"  # untouched: nothing was rotated


# ---------------------------------------------------------------- publish


def _publish(monkeypatch, *extra: str) -> int:
    return canary.main(["publish", "kuaishou", "--account-id", "1", "--video", "v.mp4", *extra])


def test_publish_refuses_without_yes(canary_env, monkeypatch):
    assert _prepare(monkeypatch) == 0

    assert _publish(monkeypatch) == 2

    assert _rows(PublishJob) == 0
    assert _rows(Article) == 0


def test_publish_stops_before_any_job_when_the_grant_is_invalid(canary_env, monkeypatch):
    assert _prepare(monkeypatch) == 0
    canary_env["api"].login_ok = False

    assert _publish(monkeypatch, "--yes") == 1

    assert _rows(PublishJob) == 0
    assert _rows(Article) == 0


def _run_publish_through_pipeline(canary_env, monkeypatch, tmp_path: Path) -> int:
    """The real worker path, with the video check pointed at a scratch directory."""
    assert _prepare(monkeypatch) == 0
    video = tmp_path / "canary.mp4"
    video.write_bytes(b"fake video")
    monkeypatch.setattr(settings, "kuaishou_open_api_asset_root", tmp_path)
    return canary.main(
        ["publish", "kuaishou", "--account-id", "1", "--video", str(video), "--yes"]
    )


def test_publish_through_the_api_adapter_is_a_pass(canary_env, monkeypatch, tmp_path):
    assert _run_publish_through_pipeline(canary_env, monkeypatch, tmp_path) == 0

    with db_mod.SessionLocal() as session:
        job = session.query(PublishJob).one()
        assert str(job.status) == "success"
        assert job.publisher_kind == "kuaishou_open_api"
        assert job.platform_post_id == "PHOTO123"


def test_a_run_that_fell_through_to_another_publisher_is_not_a_pass(
    canary_env, monkeypatch, tmp_path, capsys
):
    """The worker tries the next Publisher when the adapter fails before sending anything."""
    canary_env["api"].publish_result = PublishResult(
        success=False, effect_applied=False, retryable=False, outcome_uncertain=False,
        error="快手封面缺失",
    )

    assert _run_publish_through_pipeline(canary_env, monkeypatch, tmp_path) == 1

    with db_mod.SessionLocal() as session:
        job = session.query(PublishJob).one()
    assert job.publisher_kind == "social_auto_upload"  # the browser path posted it
    assert "回退到了 social_auto_upload" in capsys.readouterr().out


# ------------------------------------------------------- disk-state adapter (bilitool)


class _Bilitool:
    """bilitool keeps its login in the account's own venv, not in the database."""

    kind = "bilitool"
    platform = Platform.BILIBILI

    def __init__(self):
        self.credentials_seen: list[dict] = []
        self.binary = "/profiles/bilibili/account_1/venv/bin/bilitool"

    def _resolved_binary(self, account_id):
        return self.binary

    async def login(self, account_id, credential):
        self.credentials_seen.append(dict(credential))
        return True

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY

    async def publish(self, *_):
        raise AssertionError("bilitool publish must not be reached in these tests")


def _bilibili(monkeypatch, adapter) -> None:
    from ai_ops.publishers.registry import default_registry

    monkeypatch.setattr(default_registry, "resolve", lambda _platform: [adapter])
    monkeypatch.setattr(canary, "read_doctor", lambda: {"checks": [
        {"id": "safety.publication", "outcome": "warn",
         "details": {"enabled_capabilities": ["bilitool"]}}
    ]})
    monkeypatch.setattr(sys, "stdin", _stdin(""))  # bilitool must not read a grant


def test_bilibili_prepare_needs_no_stored_credential(canary_env, monkeypatch):
    """A disk-state account has no encrypted credential; that is not an error."""
    _bilibili(monkeypatch, _Bilitool())

    assert canary.main(["prepare", "bilibili", "--name", "bili"]) == 0

    with db_mod.SessionLocal() as session:
        assert session.get(Account, 1).encrypted_credential == b""


def test_bilibili_prepare_checks_the_binary_the_adapter_will_run(canary_env, monkeypatch, capsys):
    """The adapter runs only the account's venv binary, so that is what is checked."""
    adapter = _Bilitool()
    adapter.binary = None  # nothing installed in the account's venv
    _bilibili(monkeypatch, adapter)

    assert canary.main(["prepare", "bilibili", "--name", "bili"]) == 1

    assert adapter.credentials_seen == []  # stopped before touching the login
    assert "account_1/venv" in capsys.readouterr().err
