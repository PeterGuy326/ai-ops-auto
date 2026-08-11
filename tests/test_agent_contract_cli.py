"""Script-safe CLI contract for the Agent HTTP adapter."""

from __future__ import annotations

import json
import uuid

import pytest
from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.agent_contract import cli_commands
from ai_ops.agent_contract.client import ClientError
from ai_ops.agent_contract.schemas import (
    MAX_CONTRACT_REQUEST_BODY_BYTES,
    ApprovalAssetDownloadResponse,
    ApprovalContentSnapshot,
    ApprovalDecisionResponse,
    ApprovalReviewResponse,
    ApprovalReviewTarget,
    ApprovalResponse,
    CollectMetricsResponse,
    JobStatusResponse,
    PerformanceReviewResponse,
    PerformanceTotals,
    PlanPublicationResponse,
    PublicationTarget,
    RendererBinding,
    RendererContract,
    ScheduleResponse,
    StageContentResponse,
)


NOW = "2026-08-11T00:00:00Z"
DIGEST = "b" * 64
TOKEN = "environment-only-agent-token"
EXTERNAL_ACCOUNT_ID = "zhihu:id:cli-test-account"


def _execution() -> RendererBinding:
    renderer = RendererContract(
        renderer_id="test.zhihu",
        contract_version="1",
        adapter_version="test-1",
        platform="zhihu",
        publisher_kind="zhihu_cli",
        requires_external_account_id=True,
    )
    return RendererBinding.from_projection(
        renderer=renderer,
        payload={"action": "article"},
    )


class FakeHttpClient:
    instances: list[FakeHttpClient] = []

    def __init__(self, *, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True

    def stage_content(self, request, key):
        self.calls.append(("stage_content", request, key))
        return StageContentResponse(
            content_id=10,
            state="draft",
            content_digest=DIGEST,
            created_at=NOW,
        )

    def plan_publication(self, request, key):
        self.calls.append(("plan_publication", request, key))
        return PlanPublicationResponse(
            plan_id="20",
            content_digest=DIGEST,
            plan_digest=DIGEST,
            targets=[
                PublicationTarget(
                    account_id=2,
                    platform="zhihu",
                    account_binding_digest=DIGEST,
                    approved_external_account_id=EXTERNAL_ACCOUNT_ID,
                    execution=_execution(),
                )
            ],
            planned_for=NOW,
        )

    def request_approval(self, request, key):
        self.calls.append(("request_approval", request, key))
        return ApprovalResponse(
            approval_id="30",
            plan_id="20",
            state="pending",
            plan_digest=DIGEST,
            requested_at=NOW,
        )

    def get_approval(self, approval_id):
        self.calls.append(("get_approval", approval_id))
        return ApprovalReviewResponse(
            approval_id=approval_id,
            plan_id="20",
            state="pending",
            plan_digest=DIGEST,
            content_digest=DIGEST,
            content=ApprovalContentSnapshot(
                content_id=10,
                title="Title",
                body="Body",
                content_type="long_article",
            ),
            targets=[
                ApprovalReviewTarget(
                    account_id=2,
                    platform="zhihu",
                    account_binding_digest=DIGEST,
                    approved_external_account_id=EXTERNAL_ACCOUNT_ID,
                    execution=_execution(),
                    account_display="review-account",
                )
            ],
            planned_for=NOW,
            requested_at=NOW,
        )

    def decide_approval(self, approval_id, request, key):
        self.calls.append(("decide_approval", approval_id, request, key))
        return ApprovalDecisionResponse(
            approval_id=approval_id,
            plan_id="20",
            state="approved",
            plan_digest=DIGEST,
            decided_at=NOW,
        )

    def download_approval_asset(self, approval_id, asset_id, output):
        self.calls.append(("download_approval_asset", approval_id, asset_id, output))
        return ApprovalAssetDownloadResponse(
            approval_id=approval_id,
            asset_id=asset_id,
            sha256=DIGEST,
            size_bytes=123,
        )

    def schedule(self, request, key):
        self.calls.append(("schedule", request, key))
        return ScheduleResponse(
            plan_id=request.plan_id,
            plan_digest=DIGEST,
            job_ids=[40],
            planned_for=NOW,
        )

    def get_job_status(self, job_id):
        self.calls.append(("get_job_status", job_id))
        return JobStatusResponse(
            job_id=job_id,
            plan_id="20",
            content_id=10,
            account_id=2,
            platform="zhihu",
            state="pending",
            attempts=0,
            max_attempts=3,
        )

    def collect_metrics(self, request, key):
        self.calls.append(("collect_metrics", request, key))
        return CollectMetricsResponse(
            job_id=request.job_id,
            state="unavailable",
            reason="not supported",
        )

    def review_performance(self, request):
        self.calls.append(("review_performance", request))
        return PerformanceReviewResponse(
            review_id="review-1",
            reviewed_at=NOW,
            totals=PerformanceTotals(
                jobs_reviewed=0,
                jobs_with_metrics=0,
                likes=0,
                comments=0,
                shares=0,
                views=0,
            ),
        )


@pytest.fixture(autouse=True)
def fake_http_client(monkeypatch):
    FakeHttpClient.instances = []
    monkeypatch.setattr(cli_commands, "AgentContractClient", FakeHttpClient)
    return FakeHttpClient


COMMAND_CASES = [
    (
        "stage-content",
        ["--input", "-"],
        {
            "schema_version": 1,
            "topic_id": 1,
            "title": "Title",
            "body": "Body",
            "content_type": "long_article",
            "target_platforms": ["zhihu"],
        },
        "stage_content",
        True,
    ),
    (
        "plan-publication",
        ["--input", "-"],
        {"schema_version": 1, "content_id": 10, "account_ids": [7]},
        "plan_publication",
        True,
    ),
    (
        "request-approval",
        ["--input", "-"],
        {"schema_version": 1, "plan_id": "20"},
        "request_approval",
        True,
    ),
    (
        "get-approval",
        ["30"],
        None,
        "get_approval",
        False,
    ),
    (
        "decide-approval",
        ["30", "--input", "-"],
        {
            "schema_version": 1,
            "expected_plan_digest": DIGEST,
            "decision": "approved",
            "reason": "reviewed",
        },
        "decide_approval",
        True,
    ),
    (
        "schedule",
        ["--input", "-"],
        {"schema_version": 1, "plan_id": "20"},
        "schedule",
        True,
    ),
    (
        "get-job-status",
        ["40"],
        None,
        "get_job_status",
        False,
    ),
    (
        "collect-metrics",
        ["--input", "-"],
        {"schema_version": 1, "job_id": 40},
        "collect_metrics",
        True,
    ),
    (
        "review-performance",
        ["--input", "-"],
        {"schema_version": 1, "job_ids": [40]},
        "review_performance",
        False,
    ),
]


@pytest.mark.parametrize(
    ("command", "arguments", "request_json", "method_name", "is_mutation"),
    COMMAND_CASES,
)
def test_each_roadmap_command_uses_http_and_writes_one_json_document(
    monkeypatch,
    command,
    arguments,
    request_json,
    method_name,
    is_mutation,
):
    # A DB seam that explodes proves these commands do not initialize or use it.
    monkeypatch.setattr(cli, "_init_db", lambda: (_ for _ in ()).throw(AssertionError("DB used")))
    invocation_input = "" if request_json is None else json.dumps(request_json)

    result = CliRunner().invoke(
        cli.app,
        ["agent", command, *arguments],
        input=invocation_input,
        env={"AI_OPS_TOKEN": TOKEN, "AI_OPS_URL": "https://agent.example"},
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["schema_version"] == 1
    assert TOKEN not in result.stdout

    instance = FakeHttpClient.instances[-1]
    assert instance.base_url == "https://agent.example"
    assert instance.token == TOKEN
    assert instance.closed is True
    assert instance.calls[0][0] == method_name
    if is_mutation:
        key = instance.calls[0][-1]
        assert uuid.UUID(key).version == 4
    else:
        assert len(instance.calls[0]) == 2


def test_file_input_explicit_key_and_base_url_option_override_environment(tmp_path):
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps({"schema_version": 1, "plan_id": "20"}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "agent",
            "schedule",
            "--input",
            str(request_file),
            "--idempotency-key",
            "caller-key-001",
            "--base-url",
            "https://option.example",
        ],
        env={"AI_OPS_TOKEN": TOKEN, "AI_OPS_URL": "https://environment.example"},
    )

    assert result.exit_code == 0, result.output
    instance = FakeHttpClient.instances[-1]
    assert instance.base_url == "https://option.example"
    assert instance.calls[0][-1] == "caller-key-001"


def test_stage_cli_accepts_a_legal_request_larger_than_the_old_four_mib_cap(tmp_path):
    request_json = {
        "schema_version": 1,
        "topic_id": 1,
        "title": "Large bounded stage request",
        "body": "b" * (1024 * 1024),
        "content_type": "long_article",
        "target_platforms": ["zhihu"],
        "extra": {"payload": "e" * 64_000},
        "assets": [
            {
                "asset_type": "image",
                "source": "ai_generated",
                "local_path": f"{index}.jpg",
                "meta": {"payload": "m" * 16_000},
            }
            for index in range(256)
        ],
    }
    encoded = json.dumps(request_json, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) > 4 * 1024 * 1024
    assert len(encoded.encode("utf-8")) <= MAX_CONTRACT_REQUEST_BODY_BYTES
    request_file = tmp_path / "large-stage.json"
    request_file.write_text(encoded, encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        ["agent", "stage-content", "--input", str(request_file)],
        env={"AI_OPS_TOKEN": TOKEN, "AI_OPS_URL": "https://agent.example"},
    )

    assert result.exit_code == 0, result.output
    instance = FakeHttpClient.instances[-1]
    assert instance.calls[0][0] == "stage_content"
    assert len(instance.calls[0][1].assets) == 256


def test_download_approval_asset_uses_human_http_client_and_emits_metadata(tmp_path):
    destination = tmp_path / "reviewed.png"

    result = CliRunner().invoke(
        cli.app,
        [
            "agent",
            "download-approval-asset",
            "30",
            "61",
            "--output",
            str(destination),
        ],
        env={"AI_OPS_TOKEN": TOKEN, "AI_OPS_URL": "https://agent.example"},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "approval_id": "30",
        "asset_id": 61,
        "schema_version": 1,
        "sha256": DIGEST,
        "size_bytes": 123,
    }
    instance = FakeHttpClient.instances[-1]
    assert instance.calls == [("download_approval_asset", "30", 61, str(destination))]


def test_missing_token_and_invalid_input_are_single_redacted_json_errors():
    runner = CliRunner()
    missing = runner.invoke(
        cli.app,
        ["agent", "get-job-status", "40"],
        env={"AI_OPS_TOKEN": "", "AI_OPS_URL": "https://agent.example"},
    )
    invalid = runner.invoke(
        cli.app,
        ["agent", "schedule", "--input", "-"],
        input="not-json",
        env={"AI_OPS_TOKEN": TOKEN},
    )

    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"]["code"] == "missing_token"
    assert len(missing.stdout.splitlines()) == 1
    assert invalid.exit_code == 1
    assert json.loads(invalid.stdout) == {
        "schema_version": 1,
        "error": {
            "code": "invalid_json",
            "message": "--input must contain one valid JSON document",
        },
    }
    assert len(invalid.stdout.splitlines()) == 1
    assert FakeHttpClient.instances == [] or all(
        not instance.calls for instance in FakeHttpClient.instances
    )


def test_client_error_is_forwarded_as_one_stable_envelope(monkeypatch):
    secret = "must-never-appear-in-command-output"

    def fail(_self, _job_id):
        raise ClientError("job_not_found", "The requested job does not exist", status_code=404)

    monkeypatch.setattr(FakeHttpClient, "get_job_status", fail)
    result = CliRunner().invoke(
        cli.app,
        ["agent", "get-job-status", "999"],
        env={"AI_OPS_TOKEN": secret},
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "error": {
            "code": "job_not_found",
            "message": "The requested job does not exist",
        },
    }
    assert len(result.stdout.splitlines()) == 1
    assert secret not in result.output


def test_token_has_no_cli_option_and_is_not_echoed_on_parser_failure():
    secret = "argv-secret-must-not-be-accepted"
    help_result = CliRunner().invoke(cli.app, ["agent", "stage-content", "--help"])
    rejected = CliRunner().invoke(
        cli.app,
        ["agent", "get-job-status", "40", "--token", secret],
        env={"AI_OPS_TOKEN": TOKEN},
    )

    assert help_result.exit_code == 0
    assert "--token" not in help_result.stdout
    assert rejected.exit_code != 0
    assert secret not in rejected.output


def test_gen_principal_token_emits_one_time_token_and_verifier(monkeypatch):
    import hashlib
    import secrets

    monkeypatch.setattr(secrets, "token_urlsafe", lambda _size: "fixed-generated-secret")

    result = CliRunner().invoke(cli.app, ["gen-principal-token"])

    assert result.exit_code == 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["token"] == "aop_fixed-generated-secret"
    assert payload["token_sha256"] == hashlib.sha256(payload["token"].encode("utf-8")).hexdigest()
