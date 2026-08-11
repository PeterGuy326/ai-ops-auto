"""Web UI session、CSRF 与入口日志的安全回归测试。"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_ops.api.auth import (
    UI_SESSION_COOKIE,
    UI_SESSION_MAX_AGE_SECONDS,
    create_ui_session_cookie,
    ui_csrf_token,
    validate_ui_session_cookie,
)
from ai_ops.api.main import app, get_session
from ai_ops.config import settings
from ai_ops.core.enums import ArticleStatus
from ai_ops.core.models import Article, Base, PublishJob


@pytest.fixture
def ui_app(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )

    def _override():
        session = testing_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(settings, "api_key", "prod-ui-secret")
    monkeypatch.setattr(
        "ai_ops.scheduler.worker.schedule_job_runs",
        lambda jobs: None,
    )
    app.dependency_overrides[get_session] = _override
    client = TestClient(app, base_url="https://testserver")
    try:
        yield client, testing_session
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def _login(client: TestClient, key: str = "prod-ui-secret"):
    return client.post(
        "/ui/login",
        data={"api_key": key},
        follow_redirects=False,
    )


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None, "page should contain a CSRF hidden input"
    return match.group(1)


def test_all_ui_pages_require_login_when_api_key_is_configured(ui_app):
    client, _ = ui_app
    for path in ("/ui", "/ui/topics", "/ui/articles", "/ui/accounts", "/ui/jobs"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"

    # /api 前缀会被 middleware 改写，不能成为绕过入口。
    response = client.get("/api/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_login_failure_does_not_echo_or_store_submitted_key(ui_app):
    client, _ = ui_app
    submitted = "wrong-key-that-must-not-leak"
    response = _login(client, submitted)
    assert response.status_code == 401
    assert submitted not in response.text
    assert UI_SESSION_COOKIE not in response.cookies
    assert "API key 不正确" in response.text


def test_login_issues_limited_signed_cookie_without_api_key(ui_app):
    client, _ = ui_app
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"

    set_cookie = response.headers["set-cookie"]
    lower = set_cookie.lower()
    assert "httponly" in lower
    assert "secure" in lower
    assert "samesite=strict" in lower
    assert "path=/ui" in lower
    assert f"max-age={UI_SESSION_MAX_AGE_SECONDS}" in lower
    assert settings.api_key not in set_cookie

    token = response.cookies[UI_SESSION_COOKIE]
    session = validate_ui_session_cookie(token)
    assert session is not None

    # 有效 session 可穿过鉴权层；不存在的路由应到达 FastAPI 并返回 404。
    assert client.get("/ui/not-a-route").status_code == 404


def test_session_signature_tamper_and_expiry_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "signing-key-not-in-cookie")
    token = create_ui_session_cookie(now=1_000)
    assert settings.api_key not in token
    assert validate_ui_session_cookie(token, now=1_001) is not None

    parts = token.split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    tampered = ".".join(parts)
    assert validate_ui_session_cookie(tampered, now=1_001) is None
    assert validate_ui_session_cookie(
        token,
        now=1_000 + UI_SESSION_MAX_AGE_SECONDS + 1,
    ) is None


def test_logout_requires_bound_csrf_token_and_clears_cookie(ui_app):
    client, _ = ui_app
    response = _login(client)
    session = validate_ui_session_cookie(response.cookies[UI_SESSION_COOKIE])
    assert session is not None

    assert client.post("/ui/logout", follow_redirects=False).status_code == 403
    assert client.post(
        "/ui/logout",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    ).status_code == 403

    response = client.post(
        "/ui/logout",
        data={"csrf_token": ui_csrf_token(session)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/ui", follow_redirects=False).status_code == 303


def test_approve_and_distribute_require_current_session_csrf(ui_app):
    client, testing_session = ui_app
    api_headers = {"X-API-Key": settings.api_key}
    topic_id = client.post(
        "/topics",
        json={"name": "安全测试", "category": "test"},
        headers=api_headers,
    ).json()["id"]
    account_id = client.post(
        "/accounts",
        json={"platform": "douyin", "nickname": "安全测试号"},
        headers=api_headers,
    ).json()["id"]
    article_id = client.post(
        "/articles",
        json={
            "topic_id": topic_id,
            "title": "CSRF 不得误发",
            "content_type": "video",
            "target_platforms": ["douyin"],
        },
        headers=api_headers,
    ).json()["id"]

    # 未登录的 UI 写操作在进入 handler 前就被拒绝。
    assert client.post(
        f"/ui/articles/{article_id}/approve",
        follow_redirects=False,
    ).status_code == 401

    _login(client)
    assert client.post(
        f"/ui/articles/{article_id}/approve",
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/ui/articles/{article_id}/approve",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    ).status_code == 403
    with testing_session() as session:
        assert session.get(Article, article_id).status == ArticleStatus.DRAFT

    detail = client.get(f"/ui/articles/{article_id}")
    csrf = _csrf_from(detail.text)
    assert client.post(
        f"/ui/articles/{article_id}/approve",
        data={"csrf_token": csrf},
        follow_redirects=False,
    ).status_code == 303

    # approve 后 token 仍属于同一会话；缺失 token 的 distribute 不得建 job。
    assert client.post(
        f"/ui/articles/{article_id}/distribute",
        data={"account_ids": account_id},
        follow_redirects=False,
    ).status_code == 403
    with testing_session() as session:
        assert session.scalar(select(func.count(PublishJob.id))) == 0

    assert client.post(
        f"/ui/articles/{article_id}/distribute",
        data={"account_ids": account_id, "csrf_token": csrf},
        follow_redirects=False,
    ).status_code == 303
    with testing_session() as session:
        assert session.scalar(select(func.count(PublishJob.id))) == 1


def test_csrf_token_cannot_be_reused_by_another_session(ui_app):
    client_a, _ = ui_app
    response_a = _login(client_a)
    session_a = validate_ui_session_cookie(response_a.cookies[UI_SESSION_COOKIE])
    assert session_a is not None

    client_b = TestClient(app, base_url="https://testserver")
    try:
        _login(client_b)
        response = client_b.post(
            "/ui/logout",
            data={"csrf_token": ui_csrf_token(session_a)},
            follow_redirects=False,
        )
        assert response.status_code == 403
    finally:
        client_b.close()


def test_empty_api_key_keeps_explicit_dev_mode_compatible(ui_app, monkeypatch):
    client, testing_session = ui_app
    monkeypatch.setattr(settings, "api_key", "")
    topic_id = client.post(
        "/topics",
        json={"name": "dev", "category": "test"},
    ).json()["id"]
    article_id = client.post(
        "/articles",
        json={"topic_id": topic_id, "title": "dev 草稿", "content_type": "video"},
    ).json()["id"]

    assert client.get("/ui").status_code == 200
    # dev 模式特意保留无 session、无 CSRF 的本地工作流。
    assert client.post(
        f"/ui/articles/{article_id}/approve",
        follow_redirects=False,
    ).status_code == 303
    with testing_session() as session:
        assert session.get(Article, article_id).status == ArticleStatus.READY


def test_entrypoint_never_logs_database_password():
    repo_root = Path(__file__).resolve().parent.parent
    database_url = "postgresql://entry-user:s3cr3t-pass@db.internal:5432/ai_ops"
    env = os.environ.copy()
    env.update({"DATABASE_URL": database_url, "SKIP_MIGRATIONS": "1"})
    result = subprocess.run(
        ["bash", str(repo_root / "docker-entrypoint.sh"), "true"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    output = result.stdout + result.stderr
    assert "DATABASE_URL=<configured>" in output
    assert database_url not in output
    assert "s3cr3t-pass" not in output
    assert "entry-user" not in output
