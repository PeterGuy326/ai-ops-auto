"""Task F · API 鉴权单测。

覆盖：
  - dev 模式（空 api_key）：缺 header 也放行 + 触发 warning
  - prod 模式：缺 header → 401
  - prod 模式：错误 key → 401
  - prod 模式：正确 key → 200
  - 常量时间比较：长度不同也不能短路返回 200（间接验证）
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_ops.api import auth as auth_mod
from ai_ops.api.auth import api_key_dev_mode, require_api_key
from ai_ops.config import settings


@pytest.fixture
def app():
    """最小 FastAPI app，挂一个受保护路由 + 一个公开路由——隔离 main.py 的复杂依赖。"""
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    def protected():
        return {"ok": True, "scope": "protected"}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _reset_dev_warn_and_key(monkeypatch):
    """每个 test 前重置 dev warn 标志位 + 让 api_key 默认非空（强制 prod 模式）。
    单个 test 需要 dev 模式时再 monkeypatch 回去。"""
    auth_mod._reset_dev_warn_for_test()
    monkeypatch.setattr(settings, "api_key", "test-secret-123")
    yield
    auth_mod._reset_dev_warn_for_test()


class TestDevMode:
    """dev 模式 = settings.api_key 为空字符串"""

    def test_dev_mode_no_header_passes(self, monkeypatch, app, caplog):
        """dev 模式：不带 header 也放行，且触发 warning。"""
        import logging
        monkeypatch.setattr(settings, "api_key", "")
        auth_mod._reset_dev_warn_for_test()

        with caplog.at_level(logging.WARNING, logger="ai_ops.api.auth"):
            client = TestClient(app)
            r = client.get("/protected")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "scope": "protected"}
        # 应有 dev 模式 warning
        assert any("dev 模式" in rec.message for rec in caplog.records)

    def test_dev_mode_warning_only_once(self, monkeypatch, app, caplog):
        """dev 模式 warning 不刷屏——多次请求只 warn 一次。"""
        import logging
        monkeypatch.setattr(settings, "api_key", "")
        auth_mod._reset_dev_warn_for_test()

        with caplog.at_level(logging.WARNING, logger="ai_ops.api.auth"):
            client = TestClient(app)
            for _ in range(5):
                client.get("/protected")
        warn_count = sum(1 for rec in caplog.records if "dev 模式" in rec.message)
        assert warn_count == 1, f"expected 1 warning, got {warn_count}"

    def test_api_key_dev_mode_helper(self, monkeypatch):
        """api_key_dev_mode() helper 反映 settings 状态。"""
        monkeypatch.setattr(settings, "api_key", "")
        assert api_key_dev_mode() is True
        monkeypatch.setattr(settings, "api_key", "x")
        assert api_key_dev_mode() is False


class TestProdMode:
    """prod 模式 = settings.api_key 非空"""

    def test_missing_header_returns_401(self, app):
        """没带 X-API-Key header → 401。"""
        client = TestClient(app)
        r = client.get("/protected")
        assert r.status_code == 401
        assert "invalid or missing API key" in r.json()["detail"]

    def test_wrong_key_returns_401(self, app):
        """带了错的 key → 401。"""
        client = TestClient(app)
        r = client.get("/protected", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401
        assert "invalid or missing API key" in r.json()["detail"]

    def test_correct_key_passes(self, app):
        """正确 key → 200。"""
        client = TestClient(app)
        r = client.get("/protected", headers={"X-API-Key": "test-secret-123"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "scope": "protected"}

    def test_health_endpoint_always_public(self, app):
        """/health 不挂 Depends，必须公开（不影响探活）。"""
        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200

    def test_empty_string_key_still_rejected(self, app):
        """带 header 但 value 是空字符串 → 401（不能因为 == 短路放行）。"""
        client = TestClient(app)
        r = client.get("/protected", headers={"X-API-Key": ""})
        assert r.status_code == 401

    def test_partial_match_key_rejected(self, app):
        """带 header 但是 key 前缀（验证常量时间比较，不是 startswith）。"""
        client = TestClient(app)
        r = client.get("/protected", headers={"X-API-Key": "test-secret-12"})
        assert r.status_code == 401


class TestMainAppIntegration:
    """复用真实 main.py app 验证路由清单——确保 /health 和 /ui/* 公开。"""

    def test_health_public_on_main_app(self, monkeypatch):
        """主 app 上 /health 不需要 key。"""
        monkeypatch.setattr(settings, "api_key", "any-key")
        from ai_ops.api.main import app as main_app
        client = TestClient(main_app)
        r = client.get("/health")
        assert r.status_code == 200

    def test_protected_routes_reject_without_key(self, monkeypatch):
        """主 app 上 /accounts 没带 key → 401。"""
        monkeypatch.setattr(settings, "api_key", "any-key")
        from ai_ops.api.main import app as main_app
        client = TestClient(main_app)
        r = client.get("/accounts")
        assert r.status_code == 401

    def test_docs_public(self, monkeypatch):
        """/docs 必须公开（dev 体验）。"""
        monkeypatch.setattr(settings, "api_key", "any-key")
        from ai_ops.api.main import app as main_app
        client = TestClient(main_app)
        r = client.get("/docs")
        assert r.status_code == 200
