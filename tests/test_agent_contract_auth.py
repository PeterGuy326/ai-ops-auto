"""Agent contract v1 principal configuration and bearer authorization."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_ops.api import auth as auth_mod
from ai_ops.api.auth import Principal, authenticate, require_scopes
from ai_ops.config import (
    AGENT_V1_SCOPES,
    SCOPE_APPROVAL_DECIDE,
    SCOPE_APPROVAL_READ,
    SCOPE_APPROVAL_REQUEST,
    SCOPE_CONTENT_STAGE,
    SCOPE_JOB_READ,
    SCOPE_METRICS_COLLECT,
    SCOPE_PERFORMANCE_READ,
    SCOPE_PLAN_CREATE,
    SCOPE_SCHEDULE_CREATE,
    AgentPrincipalConfig,
    Settings,
    settings,
)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_token(label: str) -> str:
    return f"{label}-" + "x" * 40


def _principal(
    principal_id: str,
    token: str,
    *,
    principal_type: str = "agent",
    scopes: tuple[str, ...] = (SCOPE_JOB_READ,),
) -> AgentPrincipalConfig:
    return AgentPrincipalConfig(
        principal_id=principal_id,
        type=principal_type,
        token_sha256=_token_hash(token),
        scopes=scopes,
    )


@pytest.fixture(autouse=True)
def _isolated_auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "agent_principals", [])
    monkeypatch.setattr(settings, "legacy_dev_auth_bypass", False)


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()

    @application.get("/identity")
    def identity(principal: Principal = Depends(authenticate)):
        return {
            "principal_id": principal.principal_id,
            "type": principal.type,
            "principal_type": principal.principal_type,
            "scopes": sorted(principal.scopes),
        }

    @application.get("/jobs", dependencies=[Depends(require_scopes(SCOPE_JOB_READ))])
    def jobs():
        return {"ok": True}

    @application.post(
        "/approval",
        dependencies=[Depends(require_scopes(SCOPE_APPROVAL_DECIDE))],
    )
    def approval():
        return {"ok": True}

    @application.get("/legacy", dependencies=[Depends(auth_mod.require_api_key)])
    def legacy():
        return {"ok": True}

    return application


def test_v1_scope_registry_is_exact_and_stable():
    assert AGENT_V1_SCOPES == frozenset(
        {
            "content:stage",
            "plan:create",
            "approval:request",
            "approval:read",
            "approval:decide",
            "schedule:create",
            "job:read",
            "metrics:collect",
            "performance:read",
        }
    )
    assert {
        SCOPE_CONTENT_STAGE,
        SCOPE_PLAN_CREATE,
        SCOPE_APPROVAL_REQUEST,
        SCOPE_APPROVAL_READ,
        SCOPE_APPROVAL_DECIDE,
        SCOPE_SCHEDULE_CREATE,
        SCOPE_JOB_READ,
        SCOPE_METRICS_COLLECT,
        SCOPE_PERFORMANCE_READ,
    } == AGENT_V1_SCOPES


def test_agent_principals_parse_from_json_environment(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv(
        "AGENT_PRINCIPALS",
        json.dumps(
            [
                {
                    "principal_id": "planner-agent",
                    "type": "agent",
                    "token_sha256": _token_hash("planner-token"),
                    "scopes": [SCOPE_CONTENT_STAGE, SCOPE_PLAN_CREATE],
                }
            ]
        ),
    )

    configured = Settings(_env_file=None)

    assert len(configured.agent_principals) == 1
    assert configured.agent_principals[0].principal_id == "planner-agent"
    assert configured.agent_principals[0].scopes == (
        SCOPE_CONTENT_STAGE,
        SCOPE_PLAN_CREATE,
    )


def test_principal_config_forbids_extra_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentPrincipalConfig.model_validate(
            {
                "principal_id": "agent-1",
                "type": "agent",
                "token_sha256": _token_hash("token-1"),
                "scopes": [SCOPE_JOB_READ],
                "raw_token": "must-never-be-configured",
            }
        )


@pytest.mark.parametrize(
    "invalid_hash",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_token_hash_must_be_lowercase_sha256_hex(invalid_hash):
    with pytest.raises(ValidationError, match="token_sha256"):
        AgentPrincipalConfig(
            principal_id="agent-1",
            type="agent",
            token_sha256=invalid_hash,
            scopes=(SCOPE_JOB_READ,),
        )


def test_unknown_or_duplicate_scopes_are_rejected():
    with pytest.raises(ValidationError, match="unknown Agent contract v1 scope"):
        _principal("agent-1", "token-1", scopes=("job:reed",))
    with pytest.raises(ValidationError, match="scopes must be unique"):
        _principal(
            "agent-1",
            "token-1",
            scopes=(SCOPE_JOB_READ, SCOPE_JOB_READ),
        )


@pytest.mark.parametrize("principal_type", ["agent", "service"])
@pytest.mark.parametrize("scope", [SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE])
def test_only_human_principals_may_review_or_decide_approvals(
    principal_type,
    scope,
):
    with pytest.raises(ValidationError, match="only human principals"):
        _principal(
            "non-human",
            "token-1",
            principal_type=principal_type,
            scopes=(scope,),
        )

    approver = _principal(
        "human-approver",
        "token-2",
        principal_type="human",
        scopes=(SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE),
    )
    assert approver.type == "human"


def test_principal_ids_and_token_hashes_must_be_globally_unique():
    with pytest.raises(ValidationError, match="principal_id values must be unique"):
        Settings(
            _env_file=None,
            agent_principals=[
                _principal("same", "token-1"),
                _principal("same", "token-2"),
            ],
        )

    with pytest.raises(ValidationError, match="token_sha256 values must be unique"):
        Settings(
            _env_file=None,
            agent_principals=[
                _principal("agent-1", "same-token"),
                _principal("agent-2", "same-token"),
            ],
        )


def test_agent_token_cannot_reuse_nonempty_legacy_api_key():
    shared_secret = "one-secret-must-not-span-auth-planes"
    with pytest.raises(ValidationError, match="must not reuse the legacy API_KEY"):
        Settings(
            _env_file=None,
            api_key=shared_secret,
            agent_principals=[_principal("agent-1", shared_secret)],
        )

    # This configuration parses, but enabling principals disables the legacy
    # empty-key bypass at runtime.
    configured = Settings(
        _env_file=None,
        api_key="",
        agent_principals=[_principal("agent-1", "independent-token")],
    )
    assert configured.agent_principals[0].principal_id == "agent-1"


@pytest.mark.parametrize(
    "weak_key",
    ["short", "x" * 31, " " + "x" * 32, "x" * 32 + "\n"],
)
def test_legacy_api_key_rejects_weak_or_non_printable_values(weak_key):
    with pytest.raises(ValidationError, match="at least 32 printable ASCII"):
        Settings(_env_file=None, api_key=weak_key)


def test_empty_legacy_api_key_is_valid_but_bypass_stays_disabled_by_default():
    configured = Settings(_env_file=None, api_key="")

    assert configured.api_key == ""
    assert configured.legacy_dev_auth_bypass is False


def test_explicit_legacy_dev_bypass_rejects_non_loopback_configuration():
    with pytest.raises(ValidationError, match="requires API_HOST to be a loopback"):
        Settings(
            _env_file=None,
            api_host="0.0.0.0",
            api_key="",
            legacy_dev_auth_bypass=True,
        )


def test_bearer_authentication_returns_only_principal_identity(monkeypatch, app):
    raw_token = _valid_token("agent-bearer-token")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [_principal("agent-1", raw_token, scopes=(SCOPE_JOB_READ,))],
    )

    response = TestClient(app).get(
        "/identity",
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "principal_id": "agent-1",
        "type": "agent",
        "principal_type": "agent",
        "scopes": [SCOPE_JOB_READ],
    }
    assert raw_token not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic YWdlbnQ6dG9rZW4="},
        {"X-API-Key": "legacy-dev-bypass"},
    ],
)
def test_bearer_v1_always_fails_closed(monkeypatch, app, headers):
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [_principal("agent-1", "correct-token")],
    )

    response = TestClient(app).get("/identity", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "invalid or missing bearer token"}


def test_empty_principal_registry_fails_closed_even_in_legacy_dev_mode(app):
    response = TestClient(app).get(
        "/identity",
        headers={"Authorization": "Bearer any-token"},
    )
    assert response.status_code == 401


def test_short_bearer_is_rejected_before_hashing(monkeypatch, app):
    def unexpected_hash(*_args, **_kwargs):
        raise AssertionError("short bearer token must not be hashed")

    monkeypatch.setattr(auth_mod.hashlib, "sha256", unexpected_hash)

    response = TestClient(app).get(
        "/identity",
        headers={"Authorization": "Bearer too-short"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid or missing bearer token"}


def test_hash_comparison_visits_every_configured_principal(monkeypatch, app):
    token = _valid_token("first-token")
    configured = [
        _principal("first", token),
        _principal("second", _valid_token("second-token")),
        _principal("third", _valid_token("third-token")),
    ]
    monkeypatch.setattr(settings, "agent_principals", configured)
    original_compare = auth_mod.hmac.compare_digest
    comparisons: list[tuple[str, str]] = []

    def tracking_compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", tracking_compare)

    response = TestClient(app).get(
        "/identity",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert len(comparisons) == len(configured)
    assert {right for _, right in comparisons} == {
        principal.token_sha256 for principal in configured
    }
    assert {left for left, _ in comparisons} == {_token_hash(token)}


def test_require_scopes_requires_every_scope(monkeypatch, app):
    token = _valid_token("reader-token")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [_principal("reader", token, scopes=(SCOPE_JOB_READ,))],
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/jobs", headers=headers).status_code == 200
    denied = client.post("/approval", headers=headers)
    assert denied.status_code == 403
    assert denied.json() == {"detail": "insufficient scope"}


def test_human_approval_scopes_are_accepted(monkeypatch, app):
    token = _valid_token("approver-token")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [
            _principal(
                "approver",
                token,
                principal_type="human",
                scopes=(SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE),
            )
        ],
    )

    response = TestClient(app).post(
        "/approval",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_legacy_x_api_key_contract_remains_compatible(monkeypatch, app):
    client = TestClient(app, base_url="http://127.0.0.1")

    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "legacy_dev_auth_bypass", True)
    assert client.get("/legacy").status_code == 200

    monkeypatch.setattr(settings, "api_key", "legacy-admin-key")
    assert client.get("/legacy").status_code == 401
    assert (
        client.get(
            "/legacy",
            headers={"X-API-Key": "legacy-admin-key"},
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": "there-is-no-configured-legacy-key"},
        {"Authorization": "Bearer agent-token"},
    ],
)
def test_legacy_dev_bypass_fails_closed_when_principals_exist(
    monkeypatch,
    app,
    headers,
):
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [_principal("agent-1", "agent-token")],
    )

    response = TestClient(app).get("/legacy", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "X-API-Key"
    assert response.json() == {"detail": "invalid or missing API key"}


def test_configured_legacy_key_remains_independent_from_agent_principals(
    monkeypatch,
    app,
):
    monkeypatch.setattr(settings, "api_key", "legacy-admin-key")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [_principal("agent-1", "agent-token")],
    )
    client = TestClient(app)

    assert (
        client.get(
            "/legacy",
            headers={"Authorization": "Bearer agent-token"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/legacy",
            headers={"X-API-Key": "legacy-admin-key"},
        ).status_code
        == 200
    )


def test_require_scopes_rejects_invalid_declarations():
    with pytest.raises(ValueError, match="required scopes"):
        require_scopes("")
    with pytest.raises(ValueError, match="required scopes"):
        require_scopes(" job:read")
    with pytest.raises(ValueError, match="unknown Agent contract v1 scope"):
        require_scopes("job:reed")
