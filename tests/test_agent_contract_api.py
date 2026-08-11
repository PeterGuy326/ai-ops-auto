"""Black-box tests for the Agent contract v1 HTTP transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_ops.agent_contract.schemas import (
    ApprovalContentSnapshot,
    ApprovalDecisionResponse,
    ApprovalReviewAsset,
    ApprovalReviewResponse,
    ApprovalReviewTarget,
    ApprovalResponse,
    ApprovalState,
    CollectMetricsResponse,
    JobStatusResponse,
    MetricsCollectionState,
    PerformanceReviewResponse,
    PerformanceTotals,
    PlanPublicationResponse,
    PublicationTarget,
    RendererBinding,
    RendererContract,
    ScheduleResponse,
    StageContentResponse,
)
from ai_ops.agent_contract.service import AgentContractError, ApprovalAssetFile
from ai_ops.api import agent_v1 as agent_v1_api
from ai_ops.api.agent_v1 import get_agent_control_plane, router
from ai_ops.config import (
    AGENT_V1_SCOPES,
    SCOPE_APPROVAL_DECIDE,
    SCOPE_APPROVAL_READ,
    SCOPE_JOB_READ,
    AgentPrincipalConfig,
    settings,
)
from ai_ops.core.enums import (
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
    PublisherKind,
)


AGENT_TOKEN = "agent-contract-token-" + "a" * 40
APPROVER_TOKEN = "human-approval-token-" + "h" * 40
IDEMPOTENCY_KEY = "request-00000001"
NOW = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
DIGEST = "a" * 64
PLAN_DIGEST = "b" * 64
EXTERNAL_ACCOUNT_ID = "zhihu:id:api-test-account"


def _execution() -> RendererBinding:
    renderer = RendererContract(
        renderer_id="test.zhihu",
        contract_version="1",
        adapter_version="test-1",
        platform=Platform.ZHIHU,
        publisher_kind=PublisherKind.ZHIHU_CLI,
        requires_external_account_id=True,
    )
    return RendererBinding.from_projection(
        renderer=renderer,
        payload={"action": "article"},
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _configured_principal(
    principal_id: str,
    token: str,
    *,
    principal_type: str,
    scopes: tuple[str, ...],
) -> AgentPrincipalConfig:
    return AgentPrincipalConfig(
        principal_id=principal_id,
        type=principal_type,
        token_sha256=_token_hash(token),
        scopes=scopes,
    )


class FakeControlPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, str | None]] = []
        self.failure: Exception | None = None
        self.asset_path: Path | None = None

    def _record(self, operation, principal, payload, idempotency_key=None) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append((operation, principal.principal_id, payload, idempotency_key))

    def stage_content(self, principal, request, *, idempotency_key):
        self._record("stage_content", principal, request, idempotency_key)
        return StageContentResponse(
            content_id=11,
            state=ArticleStatus.DRAFT,
            content_digest=DIGEST,
            created_at=NOW,
        )

    def plan_publication(self, principal, request, *, idempotency_key):
        self._record("plan_publication", principal, request, idempotency_key)
        return PlanPublicationResponse(
            plan_id="21",
            content_digest=DIGEST,
            plan_digest=PLAN_DIGEST,
            targets=[
                PublicationTarget(
                    account_id=31,
                    platform=Platform.ZHIHU,
                    account_binding_digest=DIGEST,
                    approved_external_account_id=EXTERNAL_ACCOUNT_ID,
                    execution=_execution(),
                )
            ],
            planned_for=NOW,
        )

    def request_approval(self, principal, request, *, idempotency_key):
        self._record("request_approval", principal, request, idempotency_key)
        return ApprovalResponse(
            approval_id="41",
            plan_id=request.plan_id,
            state=ApprovalState.PENDING,
            plan_digest=PLAN_DIGEST,
            requested_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )

    def get_approval(self, principal, approval_id):
        self._record("get_approval", principal, approval_id)
        return ApprovalReviewResponse(
            approval_id=approval_id,
            plan_id="21",
            state=ApprovalState.PENDING,
            plan_digest=PLAN_DIGEST,
            content_digest=DIGEST,
            content=ApprovalContentSnapshot(
                content_id=11,
                title="Agent-native publishing",
                body="Stable control plane",
                content_type=ContentType.LONG_ARTICLE,
                assets=[
                    ApprovalReviewAsset(
                        asset_id=61,
                        asset_type=AssetType.IMAGE,
                        source=AssetSource.AI_GENERATED,
                        vaulted_path=f"vault://sha256/{DIGEST}",
                        sha256=DIGEST,
                        size_bytes=1024,
                        storage_suffix=".png",
                        meta={"role": "cover"},
                    )
                ],
            ),
            targets=[
                ApprovalReviewTarget(
                    account_id=31,
                    platform=Platform.ZHIHU,
                    account_binding_digest=DIGEST,
                    approved_external_account_id=EXTERNAL_ACCOUNT_ID,
                    execution=_execution(),
                    account_display="Zhihu creator",
                )
            ],
            planned_for=NOW,
            requested_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )

    def decide_approval(
        self,
        principal,
        approval_id,
        request,
        *,
        idempotency_key,
    ):
        self._record(
            "decide_approval",
            principal,
            (approval_id, request),
            idempotency_key,
        )
        return ApprovalDecisionResponse(
            approval_id=approval_id,
            plan_id="21",
            state=ApprovalState.APPROVED,
            plan_digest=PLAN_DIGEST,
            decided_at=NOW,
        )

    def get_approval_asset(self, principal, approval_id, asset_id):
        self._record("get_approval_asset", principal, (approval_id, asset_id))
        assert self.asset_path is not None
        return ApprovalAssetFile(
            asset_id=asset_id,
            sha256=DIGEST,
            size_bytes=self.asset_path.stat().st_size,
            handle=self.asset_path.open("rb"),
            filename=f"asset-{asset_id}.png",
        )

    def schedule(self, principal, request, *, idempotency_key):
        self._record("schedule", principal, request, idempotency_key)
        return ScheduleResponse(
            plan_id=request.plan_id,
            plan_digest=PLAN_DIGEST,
            job_ids=[51],
            planned_for=NOW,
        )

    def get_job_status(self, principal, job_id):
        self._record("get_job_status", principal, job_id)
        return JobStatusResponse(
            job_id=job_id,
            plan_id="21",
            content_id=11,
            account_id=31,
            platform=Platform.ZHIHU,
            state=JobStatus.PENDING,
            attempts=0,
            max_attempts=3,
            planned_for=NOW,
        )

    async def collect_metrics(self, principal, request, *, idempotency_key):
        self._record("collect_metrics", principal, request, idempotency_key)
        return CollectMetricsResponse(
            job_id=request.job_id,
            state=MetricsCollectionState.SKIPPED,
            reason="No published post is available",
        )

    def review_performance(self, principal, request):
        self._record("review_performance", principal, request)
        return PerformanceReviewResponse(
            review_id="review-0001",
            reviewed_at=NOW,
            items=[],
            totals=PerformanceTotals(
                jobs_reviewed=0,
                jobs_with_metrics=0,
                likes=0,
                comments=0,
                shares=0,
                views=0,
            ),
            findings=["No jobs were reviewed"],
        )


@pytest.fixture
def fake_control_plane(tmp_path) -> FakeControlPlane:
    control_plane = FakeControlPlane()
    control_plane.asset_path = tmp_path / "private-vault-file.png"
    control_plane.asset_path.write_bytes(b"reviewed-asset-bytes")
    return control_plane


@pytest.fixture
def client(monkeypatch, fake_control_plane) -> TestClient:
    agent_scopes = tuple(sorted(AGENT_V1_SCOPES - {SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE}))
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [
            _configured_principal(
                "creator-agent",
                AGENT_TOKEN,
                principal_type="agent",
                scopes=agent_scopes,
            ),
            _configured_principal(
                "human-approver",
                APPROVER_TOKEN,
                principal_type="human",
                scopes=(SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE),
            ),
        ],
    )
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_agent_control_plane] = lambda: fake_control_plane
    return TestClient(application)


def _headers(token: str = AGENT_TOKEN, *, idempotent: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotent:
        headers["Idempotency-Key"] = IDEMPOTENCY_KEY
    return headers


def _stage_body() -> dict[str, object]:
    return {
        "topic_id": 1,
        "title": "Agent-native publishing",
        "body": "Stable control plane",
        "content_type": "long_article",
        "target_platforms": ["zhihu"],
    }


def test_main_application_mounts_the_exact_v1_routes():
    from ai_ops.api.main import app

    # FastAPI 0.135+ keeps included routers lazy in ``app.routes``.  OpenAPI is
    # the public, flattened route contract across both eager and lazy versions.
    paths = app.openapi()["paths"]
    actual = {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/v1/")
        for method in operations
    }
    assert actual == {
        ("POST", "/v1/contents"),
        ("POST", "/v1/publication-plans"),
        ("POST", "/v1/approval-requests"),
        ("GET", "/v1/approval-requests/{approval_id}"),
        (
            "GET",
            "/v1/approval-requests/{approval_id}/assets/{asset_id}",
        ),
        ("POST", "/v1/approvals/{approval_id}/decision"),
        ("POST", "/v1/publication-plans/{plan_id}/schedule"),
        ("GET", "/v1/jobs/{job_id}"),
        ("POST", "/v1/jobs/{job_id}/metrics-collections"),
        ("POST", "/v1/performance-reviews"),
    }


def test_human_downloads_review_asset_without_host_path_disclosure(
    client,
    fake_control_plane,
):
    response = client.get(
        "/v1/approval-requests/41/assets/61",
        headers=_headers(APPROVER_TOKEN, idempotent=False),
    )

    assert response.status_code == 200
    assert response.content == b"reviewed-asset-bytes"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-sha256"] == DIGEST
    assert response.headers["cache-control"] == "no-store"
    assert "asset-61.png" in response.headers["content-disposition"]
    assert str(fake_control_plane.asset_path) not in repr(response.headers)
    assert fake_control_plane.calls[0][0] == "get_approval_asset"


def test_review_asset_download_rejects_ranges_before_opening_asset(
    client,
    fake_control_plane,
):
    response = client.get(
        "/v1/approval-requests/41/assets/61",
        headers={
            **_headers(APPROVER_TOKEN, idempotent=False),
            "Range": "bytes=0-3",
        },
    )

    assert response.status_code == 416
    assert response.json()["error"]["code"] == "range_not_supported"
    assert fake_control_plane.calls == []


def test_all_v1_routes_delegate_to_the_shared_control_plane(
    client,
    fake_control_plane,
):
    requests = [
        ("post", "/v1/contents", _stage_body(), _headers(), 201),
        (
            "post",
            "/v1/publication-plans",
            {"content_id": 11, "account_ids": [31], "planned_for": NOW.isoformat()},
            _headers(),
            201,
        ),
        (
            "post",
            "/v1/approval-requests",
            {"plan_id": "21", "expires_at": (NOW + timedelta(days=1)).isoformat()},
            _headers(),
            201,
        ),
        (
            "get",
            "/v1/approval-requests/41",
            None,
            _headers(APPROVER_TOKEN, idempotent=False),
            200,
        ),
        (
            "post",
            "/v1/approvals/41/decision",
            {
                "expected_plan_digest": PLAN_DIGEST,
                "decision": "approved",
                "reason": "Reviewed",
            },
            _headers(APPROVER_TOKEN),
            200,
        ),
        (
            "post",
            "/v1/publication-plans/21/schedule",
            {"plan_id": "21"},
            _headers(),
            201,
        ),
        ("get", "/v1/jobs/51", None, _headers(idempotent=False), 200),
        (
            "post",
            "/v1/jobs/51/metrics-collections",
            {"job_id": 51},
            _headers(),
            200,
        ),
        (
            "post",
            "/v1/performance-reviews",
            {"job_ids": [51]},
            _headers(idempotent=False),
            200,
        ),
    ]

    for method, path, body, headers, expected_status in requests:
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == expected_status, response.text
        assert response.json()["schema_version"] == 1

    assert [call[0] for call in fake_control_plane.calls] == [
        "stage_content",
        "plan_publication",
        "request_approval",
        "get_approval",
        "decide_approval",
        "schedule",
        "get_job_status",
        "collect_metrics",
        "review_performance",
    ]
    assert [call[1] for call in fake_control_plane.calls] == [
        "creator-agent",
        "creator-agent",
        "creator-agent",
        "human-approver",
        "human-approver",
        "creator-agent",
        "creator-agent",
        "creator-agent",
        "creator-agent",
    ]
    assert [call[3] for call in fake_control_plane.calls] == [
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_KEY,
        None,
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_KEY,
        None,
        IDEMPOTENCY_KEY,
        None,
    ]
    assert fake_control_plane.calls[3][2] == "41"
    _, decision = fake_control_plane.calls[4][2]
    assert decision.expected_plan_digest == PLAN_DIGEST


@pytest.mark.parametrize(
    ("method", "path", "body", "token"),
    [
        ("post", "/v1/contents", _stage_body(), AGENT_TOKEN),
        (
            "post",
            "/v1/publication-plans",
            {"content_id": 11, "account_ids": [31]},
            AGENT_TOKEN,
        ),
        ("post", "/v1/approval-requests", {"plan_id": "21"}, AGENT_TOKEN),
        (
            "post",
            "/v1/approvals/41/decision",
            {"expected_plan_digest": PLAN_DIGEST, "decision": "approved"},
            APPROVER_TOKEN,
        ),
        (
            "post",
            "/v1/publication-plans/21/schedule",
            {"plan_id": "21"},
            AGENT_TOKEN,
        ),
        (
            "post",
            "/v1/jobs/51/metrics-collections",
            {"job_id": 51},
            AGENT_TOKEN,
        ),
    ],
)
def test_every_mutation_requires_an_idempotency_key(
    client,
    fake_control_plane,
    method,
    path,
    body,
    token,
):
    response = client.request(
        method,
        path,
        json=body,
        headers=_headers(token, idempotent=False),
    )

    assert response.status_code == 422
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "invalid_request",
            "message": "Request validation failed",
        },
    }
    assert fake_control_plane.calls == []


@pytest.mark.parametrize(
    "account_ids",
    [[], list(range(1, 18))],
)
def test_plan_requires_a_bounded_explicit_account_selection(
    client,
    fake_control_plane,
    account_ids,
):
    response = client.post(
        "/v1/publication-plans",
        json={"content_id": 11, "account_ids": account_ids},
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "invalid_request",
            "message": "Request validation failed",
        },
    }
    assert fake_control_plane.calls == []


def test_stage_json_shape_limits_return_the_stable_validation_envelope(
    client,
    fake_control_plane,
):
    body = _stage_body()
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(9):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    body["extra"] = nested

    response = client.post("/v1/contents", json=body, headers=_headers())

    assert response.status_code == 422
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "invalid_request",
            "message": "Request validation failed",
        },
    }
    assert fake_control_plane.calls == []


def test_declared_oversized_body_is_rejected_before_authentication(
    client,
    fake_control_plane,
    monkeypatch,
):
    monkeypatch.setattr(agent_v1_api, "MAX_CONTRACT_REQUEST_BODY_BYTES", 64)

    response = client.post(
        "/v1/contents",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "65"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "request_too_large",
            "message": "Request body exceeds the Agent v1 transport limit",
        },
    }
    assert fake_control_plane.calls == []


@pytest.mark.parametrize("declared_length", [None, "2"])
def test_streamed_or_underreported_body_is_counted_before_json_parsing(
    client,
    fake_control_plane,
    monkeypatch,
    declared_length,
):
    monkeypatch.setattr(agent_v1_api, "MAX_CONTRACT_REQUEST_BODY_BYTES", 64)
    secret = "body-secret-that-must-not-be-reflected"

    def chunks():
        yield b'{"payload":"'
        yield (secret * 3).encode("utf-8")
        yield b'"}'

    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    response = client.post("/v1/contents", content=chunks(), headers=headers)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert secret not in response.text
    assert fake_control_plane.calls == []


def test_scope_checks_are_route_specific(client, monkeypatch, fake_control_plane):
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [
            _configured_principal(
                "reader",
                AGENT_TOKEN,
                principal_type="agent",
                scopes=(SCOPE_JOB_READ,),
            )
        ],
    )

    assert client.get("/v1/jobs/51", headers=_headers(idempotent=False)).status_code == 200
    denied = client.post("/v1/contents", json=_stage_body(), headers=_headers())

    assert denied.status_code == 403
    assert denied.json() == {
        "schema_version": 1,
        "error": {
            "code": "insufficient_scope",
            "message": "The principal lacks the required scope",
        },
    }
    assert [call[0] for call in fake_control_plane.calls] == ["get_job_status"]


def test_approval_review_scope_does_not_grant_decision_access(
    client,
    monkeypatch,
    fake_control_plane,
):
    monkeypatch.setattr(
        settings,
        "agent_principals",
        [
            _configured_principal(
                "reviewer",
                APPROVER_TOKEN,
                principal_type="human",
                scopes=(SCOPE_APPROVAL_READ,),
            )
        ],
    )

    review = client.get(
        "/v1/approval-requests/41",
        headers=_headers(APPROVER_TOKEN, idempotent=False),
    )
    denied = client.post(
        "/v1/approvals/41/decision",
        json={"expected_plan_digest": PLAN_DIGEST, "decision": "approved"},
        headers=_headers(APPROVER_TOKEN),
    )

    assert review.status_code == 200
    assert review.json()["plan_digest"] == PLAN_DIGEST
    assert review.json()["content"]["assets"][0] == {
        "asset_id": 61,
        "asset_type": "image",
        "source": "ai_generated",
        "vaulted_path": f"vault://sha256/{DIGEST}",
        "sha256": DIGEST,
        "size_bytes": 1024,
        "storage_suffix": ".png",
        "meta": {"role": "cover"},
    }
    assert "local_path" not in review.text
    assert "credential" not in review.text
    assert "raw_response" not in review.text
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"
    assert fake_control_plane.calls == [("get_approval", "reviewer", "41", None)]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer unknown-token"},
        {"X-API-Key": "legacy-admin-key"},
        {"Authorization": "Bearer legacy-admin-key"},
    ],
)
def test_v1_never_accepts_missing_unknown_or_legacy_credentials(
    client,
    monkeypatch,
    headers,
):
    monkeypatch.setattr(settings, "api_key", "legacy-admin-key")

    response = client.get("/v1/jobs/51", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "authentication_required",
            "message": "A valid Agent bearer token is required",
        },
    }
    assert "legacy-admin-key" not in response.text


def test_path_and_body_identifiers_must_match(client, fake_control_plane):
    schedule = client.post(
        "/v1/publication-plans/21/schedule",
        json={"plan_id": "22"},
        headers=_headers(),
    )
    metrics = client.post(
        "/v1/jobs/51/metrics-collections",
        json={"job_id": 52},
        headers=_headers(),
    )

    assert schedule.status_code == 409
    assert schedule.json()["error"]["code"] == "plan_id_mismatch"
    assert metrics.status_code == 409
    assert metrics.json()["error"]["code"] == "job_id_mismatch"
    assert fake_control_plane.calls == []


def test_domain_errors_preserve_only_the_stable_domain_contract(
    client,
    fake_control_plane,
):
    fake_control_plane.failure = AgentContractError(
        "content_not_draft",
        "Only DRAFT content can be planned",
        status_code=409,
    )

    response = client.post("/v1/contents", json=_stage_body(), headers=_headers())

    assert response.status_code == 409
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "content_not_draft",
            "message": "Only DRAFT content can be planned",
        },
    }


def test_invalid_or_oversized_domain_error_details_fail_closed(
    client,
    fake_control_plane,
):
    secret = "domain-secret-that-must-not-cross-the-wire"
    fake_control_plane.failure = AgentContractError(
        "INVALID-DOMAIN-CODE",
        secret * 2000,
        status_code=400,
    )

    response = client.post("/v1/contents", json=_stage_body(), headers=_headers())

    assert response.status_code == 500
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed",
        },
    }
    assert secret not in response.text


def test_unexpected_exceptions_are_redacted(client, fake_control_plane):
    secret = "postgresql://operator:do-not-leak@example.invalid/database"
    fake_control_plane.failure = RuntimeError(secret)

    response = client.post("/v1/contents", json=_stage_body(), headers=_headers())

    assert response.status_code == 500
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed",
        },
    }
    assert secret not in response.text


def test_body_validation_uses_the_stable_error_envelope(client):
    response = client.post(
        "/v1/contents",
        json={"title": "missing required fields"},
        headers=_headers(),
    )

    assert response.status_code == 422
    assert response.json() == {
        "schema_version": 1,
        "error": {
            "code": "invalid_request",
            "message": "Request validation failed",
        },
    }


def test_performance_review_is_a_read_operation_without_idempotency_header(client):
    response = client.post(
        "/v1/performance-reviews",
        json={"job_ids": [51]},
        headers=_headers(idempotent=False),
    )

    assert response.status_code == 200
    assert response.json()["review_id"] == "review-0001"
