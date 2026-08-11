"""HTTP transport contract for the Agent-native Python client."""

from __future__ import annotations

import hashlib
import json
import os

import httpx
import pytest

import ai_ops.agent_contract.client as client_module
from ai_ops.agent_contract.client import AgentContractClient, ClientError
from ai_ops.agent_contract.schemas import (
    MAX_CONTRACT_RESPONSE_BODY_BYTES,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalReviewResponse,
    ApprovalResponse,
    CollectMetricsRequest,
    CollectMetricsResponse,
    JobStatusResponse,
    PerformanceReviewRequest,
    PerformanceReviewResponse,
    PlanPublicationRequest,
    PlanPublicationResponse,
    RequestApprovalRequest,
    RendererBinding,
    RendererContract,
    ScheduleRequest,
    ScheduleResponse,
    StageContentRequest,
    StageContentResponse,
)


NOW = "2026-08-11T00:00:00Z"
DIGEST = "a" * 64
TOKEN = "agent-token-that-must-stay-in-the-header"
EXTERNAL_ACCOUNT_ID = "zhihu:id:client-test-account"
_RENDERER = RendererContract(
    renderer_id="test.zhihu",
    contract_version="1",
    adapter_version="test-1",
    platform="zhihu",
    publisher_kind="zhihu_cli",
    requires_external_account_id=True,
)
EXECUTION = RendererBinding.from_projection(
    renderer=_RENDERER,
    payload={"action": "article"},
).model_dump(mode="json")


def _responses() -> dict[tuple[str, str], dict[str, object]]:
    return {
        ("POST", "/v1/contents"): {
            "schema_version": 1,
            "content_id": 10,
            "state": "draft",
            "content_digest": DIGEST,
            "created_at": NOW,
        },
        ("POST", "/v1/publication-plans"): {
            "schema_version": 1,
            "plan_id": "20",
            "state": "planned",
            "content_digest": DIGEST,
            "plan_digest": DIGEST,
            "targets": [
                {
                    "account_id": 2,
                    "platform": "zhihu",
                    "account_binding_digest": DIGEST,
                    "approved_external_account_id": EXTERNAL_ACCOUNT_ID,
                    "execution": EXECUTION,
                }
            ],
            "planned_for": NOW,
            "approval_required": True,
        },
        ("POST", "/v1/approval-requests"): {
            "schema_version": 1,
            "approval_id": "30",
            "plan_id": "20",
            "state": "pending",
            "plan_digest": DIGEST,
            "requested_at": NOW,
        },
        ("GET", "/v1/approval-requests/30"): {
            "schema_version": 1,
            "approval_id": "30",
            "plan_id": "20",
            "state": "pending",
            "plan_digest": DIGEST,
            "content_digest": DIGEST,
            "content": {
                "content_id": 10,
                "title": "A title",
                "body": "Body",
                "content_type": "long_article",
                "extra": {},
                "assets": [
                    {
                        "asset_id": 11,
                        "asset_type": "image",
                        "source": "ai_generated",
                        "vaulted_path": f"vault://sha256/{DIGEST}",
                        "sha256": DIGEST,
                        "size_bytes": 123,
                        "storage_suffix": ".png",
                        "meta": {"role": "cover"},
                    }
                ],
            },
            "targets": [
                {
                    "account_id": 2,
                    "platform": "zhihu",
                    "account_binding_digest": DIGEST,
                    "approved_external_account_id": EXTERNAL_ACCOUNT_ID,
                    "execution": EXECUTION,
                    "account_display": "review-account",
                }
            ],
            "planned_for": NOW,
            "requested_at": NOW,
        },
        ("POST", "/v1/approvals/30/decision"): {
            "schema_version": 1,
            "approval_id": "30",
            "plan_id": "20",
            "state": "approved",
            "plan_digest": DIGEST,
            "reason": "reviewed",
            "decided_at": NOW,
        },
        ("POST", "/v1/publication-plans/20/schedule"): {
            "schema_version": 1,
            "plan_id": "20",
            "state": "scheduled",
            "plan_digest": DIGEST,
            "job_ids": [40],
            "planned_for": NOW,
        },
        ("GET", "/v1/jobs/40"): {
            "schema_version": 1,
            "job_id": 40,
            "plan_id": "20",
            "content_id": 10,
            "account_id": 2,
            "platform": "zhihu",
            "state": "pending",
            "attempts": 0,
            "max_attempts": 3,
        },
        ("POST", "/v1/jobs/40/metrics-collections"): {
            "schema_version": 1,
            "job_id": 40,
            "state": "unavailable",
            "reason": "publisher does not expose metrics",
        },
        ("POST", "/v1/performance-reviews"): {
            "schema_version": 1,
            "review_id": "review-1",
            "reviewed_at": NOW,
            "items": [],
            "totals": {
                "jobs_reviewed": 0,
                "jobs_with_metrics": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
                "views": 0,
            },
            "findings": [],
        },
    }


def test_all_methods_use_fixed_v1_routes_headers_and_strict_dtos():
    seen: list[httpx.Request] = []
    responses = _responses()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = responses[(request.method, request.url.path)]
        return httpx.Response(200, json=payload)

    client = AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    try:
        results = [
            client.stage_content(
                StageContentRequest(
                    topic_id=1,
                    title="A title",
                    body="Body",
                    content_type="long_article",
                    target_platforms=["zhihu"],
                ),
                "idem-001",
            ),
            client.plan_publication(
                PlanPublicationRequest(content_id=10, account_ids=[7]),
                "idem-002",
            ),
            client.request_approval(RequestApprovalRequest(plan_id="20"), "idem-003"),
            client.get_approval("30"),
            client.decide_approval(
                "30",
                ApprovalDecisionRequest(
                    expected_plan_digest=DIGEST,
                    decision="approved",
                    reason="reviewed",
                ),
                "idem-004",
            ),
            client.schedule(ScheduleRequest(plan_id="20"), "idem-005"),
            client.get_job_status(40),
            client.collect_metrics(CollectMetricsRequest(job_id=40), "idem-006"),
            client.review_performance(PerformanceReviewRequest(job_ids=[40])),
        ]
    finally:
        client.close()

    assert [type(result) for result in results] == [
        StageContentResponse,
        PlanPublicationResponse,
        ApprovalResponse,
        ApprovalReviewResponse,
        ApprovalDecisionResponse,
        ScheduleResponse,
        JobStatusResponse,
        CollectMetricsResponse,
        PerformanceReviewResponse,
    ]
    assert [(request.method, request.url.path) for request in seen] == list(responses)

    expected_keys = {
        0: "idem-001",
        1: "idem-002",
        2: "idem-003",
        4: "idem-004",
        5: "idem-005",
        7: "idem-006",
    }
    for index, request in enumerate(seen):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        assert TOKEN not in str(request.url)
        assert TOKEN.encode() not in request.content
        if index in expected_keys:
            assert request.headers["idempotency-key"] == expected_keys[index]
        else:
            assert "idempotency-key" not in request.headers

    assert seen[3].content == b""
    assert seen[6].content == b""
    assert json.loads(seen[4].content) == {
        "schema_version": 1,
        "expected_plan_digest": DIGEST,
        "decision": "approved",
        "reason": "reviewed",
    }
    assert json.loads(seen[5].content) == {"schema_version": 1, "plan_id": "20"}
    assert json.loads(seen[7].content) == {"schema_version": 1, "job_id": 40}


def test_schedule_and_metrics_derive_path_identifier_from_validated_body():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/schedule"):
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "plan_id": "plan/with slash",
                    "state": "scheduled",
                    "plan_digest": DIGEST,
                    "job_ids": [1],
                    "planned_for": NOW,
                },
            )
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "job_id": 7,
                "state": "unavailable",
                "reason": "not supported",
            },
        )

    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.schedule(ScheduleRequest(plan_id="plan/with slash"), "schedule-1")
        client.collect_metrics(CollectMetricsRequest(job_id=7), "metrics-1")

    assert paths == [
        "/v1/publication-plans/plan/with slash/schedule",
        "/v1/jobs/7/metrics-collections",
    ]


def test_review_asset_download_is_streamed_verified_and_atomically_saved(tmp_path):
    payload = b"exact-reviewed-asset-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
            content=payload,
        )

    destination = tmp_path / "reviewed.png"
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.download_approval_asset("30", 61, destination)

    assert destination.read_bytes() == payload
    assert destination.stat().st_mode & 0o777 == 0o600
    assert result.approval_id == "30"
    assert result.asset_id == 61
    assert result.sha256 == digest
    assert result.size_bytes == len(payload)
    assert seen[0].url.path == "/v1/approval-requests/30/assets/61"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["Accept"] == "application/octet-stream"
    assert seen[0].headers["Accept-Encoding"] == "identity"


def test_review_asset_download_rejects_overwrite_and_cleans_corrupt_partial(tmp_path):
    existing = tmp_path / "existing.bin"
    existing.write_bytes(b"keep")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "7",
                "X-Content-SHA256": "0" * 64,
            },
            content=b"corrupt",
        )

    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as overwrite:
            client.download_approval_asset("30", 61, existing)
        corrupt_output = tmp_path / "corrupt.bin"
        with pytest.raises(ClientError) as corrupt:
            client.download_approval_asset("30", 61, corrupt_output)

    assert overwrite.value.code == "output_exists"
    assert existing.read_bytes() == b"keep"
    assert corrupt.value.code == "asset_integrity_failed"
    assert not corrupt_output.exists()
    assert not list(tmp_path.glob(".ai-ops-approval-asset-*.tmp"))
    assert calls == 1


def test_review_asset_download_rejects_replaced_temporary_inode_without_deleting_it(
    tmp_path,
    monkeypatch,
):
    payload = b"verified-download"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
            content=payload,
        )

    real_link = os.link

    def swap_before_link(source, destination):
        source_path = os.fspath(source)
        os.unlink(source_path)
        with open(source_path, "wb") as replacement:
            replacement.write(b"unverified replacement")
        real_link(source_path, destination)

    monkeypatch.setattr(os, "link", swap_before_link)
    destination = tmp_path / "must-not-exist.bin"
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, destination)

    assert captured.value.code == "output_commit_failed"
    assert destination.read_bytes() == b"unverified replacement"


def test_review_asset_download_cleans_its_output_when_post_link_stat_fails(
    tmp_path,
    monkeypatch,
):
    payload = b"verified-download"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
            content=payload,
        )

    destination = tmp_path / "post-link-stat-failure.bin"
    real_link = os.link
    real_stat = os.stat
    linked = False
    failed_once = False

    def tracked_link(source, target, *args, **kwargs):
        nonlocal linked
        real_link(source, target, *args, **kwargs)
        linked = True

    def fail_first_committed_stat(path, *args, **kwargs):
        nonlocal failed_once
        if linked and not failed_once and os.fspath(path) == os.fspath(destination):
            failed_once = True
            raise OSError("injected post-link stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", tracked_link)
    monkeypatch.setattr(os, "stat", fail_first_committed_stat)
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, destination)

    assert failed_once is True
    assert captured.value.code == "output_write_error"
    assert not destination.exists()
    assert not list(tmp_path.glob(".ai-ops-approval-asset-*.tmp"))


def test_review_asset_download_cleans_same_inode_after_post_link_verification_failure(
    tmp_path,
    monkeypatch,
):
    payload = b"verified-download"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
            content=payload,
        )

    destination = tmp_path / "post-link-verification-failure.bin"
    real_link = os.link
    real_stat = os.stat
    linked = False
    tampered = False

    def tracked_link(source, target, *args, **kwargs):
        nonlocal linked
        real_link(source, target, *args, **kwargs)
        linked = True

    def tamper_before_committed_stat(path, *args, **kwargs):
        nonlocal tampered
        if linked and not tampered and os.fspath(path) == os.fspath(destination):
            with destination.open("ab") as output:
                output.write(b"tamper")
            tampered = True
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", tracked_link)
    monkeypatch.setattr(os, "stat", tamper_before_committed_stat)
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, destination)

    assert tampered is True
    assert captured.value.code == "output_commit_failed"
    assert not destination.exists()
    assert not list(tmp_path.glob(".ai-ops-approval-asset-*.tmp"))


def test_review_asset_download_never_deletes_output_replaced_after_link(
    tmp_path,
    monkeypatch,
):
    payload = b"verified-download"
    replacement = b"another-process-output"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "X-Content-SHA256": digest,
            },
            content=payload,
        )

    destination = tmp_path / "replaced-after-link.bin"
    real_link = os.link
    real_stat = os.stat
    real_unlink = os.unlink
    linked = False
    replaced = False

    def tracked_link(source, target, *args, **kwargs):
        nonlocal linked
        real_link(source, target, *args, **kwargs)
        linked = True

    def replace_before_committed_stat(path, *args, **kwargs):
        nonlocal replaced
        if linked and not replaced and os.fspath(path) == os.fspath(destination):
            real_unlink(destination)
            destination.write_bytes(replacement)
            replaced = True
            raise OSError("injected replacement after link")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", tracked_link)
    monkeypatch.setattr(os, "stat", replace_before_committed_stat)
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, destination)

    assert replaced is True
    assert captured.value.code == "output_write_error"
    assert destination.read_bytes() == replacement
    assert not list(tmp_path.glob(".ai-ops-approval-asset-*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions only")
def test_review_asset_download_requires_a_private_output_directory(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)

    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, shared / "asset.bin")

    assert captured.value.code == "invalid_output_path"


@pytest.mark.parametrize(
    ("headers", "content", "expected_code"),
    [
        (
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": "7",
                "Content-Encoding": "gzip",
                "X-Content-SHA256": hashlib.sha256(b"payload").hexdigest(),
            },
            b"payload",
            "invalid_asset_response",
        ),
        (
            {"Content-Type": "application/json"},
            b"x" * (64 * 1024 + 1),
            "http_error",
        ),
    ],
)
def test_review_asset_download_rejects_encoded_bytes_and_oversized_errors(
    tmp_path,
    headers,
    content,
    expected_code,
):
    status_code = 200 if expected_code == "invalid_asset_response" else 500

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers=headers,
            stream=httpx.ByteStream(content),
        )

    destination = tmp_path / "rejected.bin"
    with AgentContractClient(
        base_url="https://control.example",
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.download_approval_asset("30", 61, destination)

    assert captured.value.code == expected_code
    assert not destination.exists()
    assert not list(tmp_path.glob(".ai-ops-approval-asset-*.tmp"))


def test_server_error_envelope_becomes_stable_client_error():
    secret = "authorization-token-must-not-leak"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "schema_version": 1,
                "error": {
                    "code": "idempotency_key_reused",
                    "message": "Idempotency-Key was used with another request",
                },
            },
        )

    with AgentContractClient(
        token=secret,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.schedule(ScheduleRequest(plan_id="20"), "schedule-1")

    error = captured.value
    assert error.code == "idempotency_key_reused"
    assert error.status_code == 409
    assert error.to_dict() == {
        "schema_version": 1,
        "error": {
            "code": "idempotency_key_reused",
            "message": "Idempotency-Key was used with another request",
        },
    }
    assert secret not in str(error)


def test_malformed_error_and_response_bodies_are_never_reflected():
    leaked = "body-secret-that-must-not-be-reflected"
    replies = iter(
        [
            httpx.Response(502, text=leaked),
            httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "job_id": 1,
                    "unexpected": leaked,
                },
            ),
        ]
    )

    with AgentContractClient(
        token=TOKEN,
        transport=httpx.MockTransport(lambda _request: next(replies)),
    ) as client:
        with pytest.raises(ClientError) as first:
            client.get_job_status(1)
        with pytest.raises(ClientError) as second:
            client.get_job_status(1)

    assert (first.value.code, first.value.status_code) == ("http_error", 502)
    assert (second.value.code, second.value.status_code) == ("invalid_response", 200)
    assert leaked not in str(first.value)
    assert leaked not in str(second.value)


@pytest.mark.parametrize(
    ("status_code", "body_size", "expected_code"),
    [
        (502, 64 * 1024 + 1, "http_error"),
        (200, 1025, "invalid_response"),
    ],
)
def test_json_responses_are_bounded_before_buffering(
    status_code,
    body_size,
    expected_code,
    monkeypatch,
):
    if status_code == 200:
        monkeypatch.setattr(client_module, "_MAX_JSON_RESPONSE_BODY_BYTES", 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=b"x" * body_size)

    with AgentContractClient(
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError) as captured:
            client.get_job_status(1)

    assert captured.value.code == expected_code
    assert captured.value.status_code == status_code


def test_client_accepts_a_valid_approval_review_larger_than_two_mib():
    execution = RendererBinding.from_projection(
        renderer=_RENDERER,
        payload={"action": "article", "body_html": "x" * 70_000},
    ).model_dump(mode="json")
    review = ApprovalReviewResponse(
        approval_id="30",
        plan_id="20",
        state="pending",
        plan_digest=DIGEST,
        content_digest=DIGEST,
        content={
            "content_id": 10,
            "title": "Large review",
            "body": "b" * (1024 * 1024),
            "content_type": "long_article",
            "extra": {},
            "assets": [],
        },
        targets=[
            {
                "account_id": account_id,
                "platform": "zhihu",
                "account_binding_digest": DIGEST,
                "approved_external_account_id": f"zhihu:id:review-{account_id}",
                "execution": execution,
                "account_display": f"review-account-{account_id}",
            }
            for account_id in range(1, 17)
        ],
        planned_for=NOW,
        requested_at=NOW,
    )
    body = review.model_dump_json().encode("utf-8")
    assert len(body) > 2 * 1024 * 1024
    assert len(body) <= MAX_CONTRACT_RESPONSE_BODY_BYTES

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=body))
    with AgentContractClient(token=TOKEN, transport=transport) as client:
        received = client.get_approval("30")

    assert received == review


def test_transport_and_local_validation_fail_closed_without_network_or_secrets():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("contains a private endpoint", request=request)

    with AgentContractClient(
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ClientError, match="failed before receiving") as transport_error:
            client.get_job_status(1)
        with pytest.raises(ClientError) as invalid_key:
            client.collect_metrics(CollectMetricsRequest(job_id=1), "short")
        with pytest.raises(ClientError) as invalid_request:
            client.stage_content({"title": TOKEN}, "valid-key")

    assert transport_error.value.code == "transport_error"
    assert TOKEN not in str(transport_error.value)
    assert invalid_key.value.code == "invalid_idempotency_key"
    assert invalid_request.value.code == "invalid_request"
    assert TOKEN not in str(invalid_request.value)
    assert calls == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"base_url": "https://user:password@example.test"}, "invalid_base_url"),
        ({"base_url": "https://example.test/path"}, "invalid_base_url"),
        ({"token": "bad\ntoken"}, "invalid_token"),
        ({"timeout": 0}, "invalid_timeout"),
    ],
)
def test_client_configuration_errors_are_stable(kwargs, code):
    options = {"base_url": "https://example.test", "token": TOKEN, **kwargs}
    with pytest.raises(ClientError) as captured:
        AgentContractClient(**options)
    assert captured.value.code == code
    assert TOKEN not in str(captured.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://LOCALHOST",
        "http://127.0.0.1:8000",
        "http://127.99.10.2",
        "http://[::1]:8000",
        "http://[0:0:0:0:0:0:0:1]",
        "https://control.example",
        "https://10.0.0.5",
    ],
)
def test_client_allows_https_or_literal_loopback_http(base_url):
    with AgentContractClient(base_url=base_url, token=TOKEN):
        pass


@pytest.mark.parametrize(
    "base_url",
    [
        "http://control.example",
        "http://10.0.0.5",
        "http://192.168.1.10",
        "http://169.254.169.254",
        "http://0.0.0.0",
        "http://[::]",
        "http://[fe80::1]",
        "http://localhost.example",
        "http://localhost.",
        "http://2130706433",
    ],
)
def test_client_rejects_cleartext_http_for_non_loopback_origins(base_url):
    with pytest.raises(ClientError) as captured:
        AgentContractClient(base_url=base_url, token=TOKEN)

    assert captured.value.code == "invalid_base_url"
    assert base_url not in str(captured.value)


@pytest.mark.parametrize(
    ("base_url", "expected_trust_env"),
    [
        ("http://127.0.0.1:8000", False),
        ("https://control.example", True),
    ],
)
def test_client_never_routes_loopback_http_bearer_tokens_through_env_proxy(
    monkeypatch,
    base_url,
    expected_trust_env,
):
    options: dict[str, object] = {}

    class _CapturingHttpClient:
        def __init__(self, **kwargs):
            options.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("ai_ops.agent_contract.client.httpx.Client", _CapturingHttpClient)

    with AgentContractClient(base_url=base_url, token=TOKEN):
        pass

    assert options["trust_env"] is expected_trust_env
