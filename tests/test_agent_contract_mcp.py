"""MCP stdio bridge contract for the Agent-native HTTP client.

The tests use the official SDK's in-memory client.  They intentionally assert
that MCP remains a thin, credential-redacting adapter and never grows a second
control-plane implementation or exposes the human approval credential surface.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from mcp import Client
from pydantic import BaseModel
import pytest
from jsonschema import Draft202012Validator

from ai_ops.agent_contract import mcp_server
from ai_ops.agent_contract.client import ClientError
from ai_ops.agent_contract.schemas import (
    ApprovalResponse,
    CollectMetricsRequest,
    CollectMetricsResponse,
    JobStatusResponse,
    PerformanceReviewRequest,
    PerformanceReviewResponse,
    PerformanceTotals,
    PlanPublicationRequest,
    PlanPublicationResponse,
    PublicationTarget,
    RendererBinding,
    RendererContract,
    RequestApprovalRequest,
    ScheduleRequest,
    ScheduleResponse,
    StageContentRequest,
    StageContentResponse,
)


NOW = "2026-08-12T00:00:00Z"
DIGEST = "a" * 64
TOKEN_SECRET = "mcp-token-secret-that-must-never-be-returned"
URL_SECRET = "https://secret-control-plane.example"
REQUEST_SECRET = "request-body-secret-that-must-never-be-returned"
IDEMPOTENCY_KEY = "mcp-test-idem-001"

AGENT_TOOL_NAMES = {
    "stage_content",
    "plan_publication",
    "request_approval",
    "schedule",
    "get_job_status",
    "collect_metrics",
    "review_performance",
}
HUMAN_TOOL_NAMES = {
    "get_approval",
    "download_approval_asset",
    "decide_approval",
}
IDEMPOTENT_WRITE_TOOLS = {
    "stage_content",
    "plan_publication",
    "request_approval",
    "schedule",
}
READ_ONLY_TOOLS = {"get_job_status", "review_performance"}
OUTPUT_TITLES = {
    "stage_content": "StageContentResponseOrError",
    "plan_publication": "PlanPublicationResponseOrError",
    "request_approval": "ApprovalResponseOrError",
    "schedule": "ScheduleResponseOrError",
    "get_job_status": "JobStatusResponseOrError",
    "collect_metrics": "CollectMetricsResponseOrError",
    "review_performance": "PerformanceReviewResponseOrError",
}


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


def _responses() -> dict[str, BaseModel]:
    return {
        "stage_content": StageContentResponse(
            content_id=10,
            state="draft",
            content_digest=DIGEST,
            created_at=NOW,
        ),
        "plan_publication": PlanPublicationResponse(
            plan_id="20",
            content_digest=DIGEST,
            plan_digest=DIGEST,
            targets=[
                PublicationTarget(
                    account_id=2,
                    platform="zhihu",
                    account_binding_digest=DIGEST,
                    approved_external_account_id="zhihu:id:mcp-test-account",
                    execution=_execution(),
                )
            ],
            planned_for=NOW,
        ),
        "request_approval": ApprovalResponse(
            approval_id="30",
            plan_id="20",
            state="pending",
            plan_digest=DIGEST,
            requested_at=NOW,
        ),
        "schedule": ScheduleResponse(
            plan_id="20",
            plan_digest=DIGEST,
            job_ids=[40],
            planned_for=NOW,
        ),
        "get_job_status": JobStatusResponse(
            job_id=40,
            plan_id="20",
            content_id=10,
            account_id=2,
            platform="zhihu",
            state="pending",
            attempts=0,
            max_attempts=3,
        ),
        "collect_metrics": CollectMetricsResponse(
            job_id=40,
            state="unavailable",
            reason="publisher does not expose metrics",
        ),
        "review_performance": PerformanceReviewResponse(
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
        ),
    }


class FakeAgentContractClient:
    """Record the exact HTTP-client boundary used by each MCP invocation."""

    instances: list[FakeAgentContractClient] = []
    outcomes: dict[str, BaseModel | BaseException] = {}

    def __init__(self, *, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.calls: list[tuple[Any, ...]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def close(self) -> None:
        self.closed = True

    def _dispatch(self, operation: str, *arguments: object) -> BaseModel:
        self.calls.append((operation, *arguments))
        outcome = self.__class__.outcomes[operation]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def stage_content(self, request, idempotency_key):
        return self._dispatch("stage_content", request, idempotency_key)

    def plan_publication(self, request, idempotency_key):
        return self._dispatch("plan_publication", request, idempotency_key)

    def request_approval(self, request, idempotency_key):
        return self._dispatch("request_approval", request, idempotency_key)

    def schedule(self, request, idempotency_key):
        return self._dispatch("schedule", request, idempotency_key)

    def get_job_status(self, job_id):
        return self._dispatch("get_job_status", job_id)

    def collect_metrics(self, request, idempotency_key):
        return self._dispatch("collect_metrics", request, idempotency_key)

    def review_performance(self, request):
        return self._dispatch("review_performance", request)


@pytest.fixture
def fake_http_client(monkeypatch):
    FakeAgentContractClient.instances = []
    FakeAgentContractClient.outcomes = _responses()
    monkeypatch.setattr(mcp_server, "AgentContractClient", FakeAgentContractClient)
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN_SECRET)
    monkeypatch.setenv(mcp_server.URL_ENV, URL_SECRET)
    return FakeAgentContractClient


def _request_schema(tool) -> Mapping[str, Any]:
    request_ref = tool.input_schema["properties"]["request"]["$ref"]
    request_name = request_ref.rsplit("/", 1)[-1]
    return tool.input_schema["$defs"][request_name]


def _assert_matching_json_result(result, payload: dict[str, object], *, is_error: bool) -> None:
    assert result.is_error is is_error
    assert result.structured_content == payload
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    expected_text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert result.content[0].text == expected_text
    assert json.loads(result.content[0].text) == payload


def _wire_text(result) -> str:
    return json.dumps(
        result.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_tool_inventory_schemas_and_annotations_are_exact():
    async with Client(mcp_server.server) as client:
        listed = await client.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == AGENT_TOOL_NAMES
    assert HUMAN_TOOL_NAMES.isdisjoint(tools)

    for name, tool in tools.items():
        assert tool.description
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["type"] == "object"
        assert len(tool.output_schema["oneOf"]) == 2
        assert tool.output_schema["title"] == OUTPUT_TITLES[name]
        Draft202012Validator.check_schema(tool.output_schema)
        assert tool.annotations is not None
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.destructive_hint is False

        if name in IDEMPOTENT_WRITE_TOOLS:
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.open_world_hint is False
        elif name in READ_ONLY_TOOLS:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.open_world_hint is False
        else:
            assert name == "collect_metrics"
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.open_world_hint is True

    request_tools = AGENT_TOOL_NAMES - {"get_job_status"}
    for name in request_tools:
        tool = tools[name]
        expected_properties = {"request"}
        if name not in READ_ONLY_TOOLS:
            expected_properties.add("idempotency_key")
        assert set(tool.input_schema["properties"]) == expected_properties
        assert set(tool.input_schema["required"]) == expected_properties
        nested_request = _request_schema(tool)
        assert nested_request["additionalProperties"] is False
        assert nested_request["properties"]["schema_version"]["const"] == 1

    for name in IDEMPOTENT_WRITE_TOOLS | {"collect_metrics"}:
        key_schema = tools[name].input_schema["properties"]["idempotency_key"]
        assert key_schema["minLength"] == 8
        assert key_schema["maxLength"] == 128
        assert key_schema["pattern"] == r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
        assert "never generated" in key_schema["description"]

    status_schema = tools["get_job_status"].input_schema
    assert set(status_schema["properties"]) == {"job_id"}
    assert status_schema["required"] == ["job_id"]
    assert status_schema["properties"]["job_id"]["exclusiveMinimum"] == 0
    assert "real external publication" in tools["schedule"].description
    assert "not a read-only query" in tools["collect_metrics"].description


SUCCESS_CASES = [
    (
        "stage_content",
        {
            "request": {
                "schema_version": 1,
                "topic_id": 1,
                "title": "Title",
                "body": REQUEST_SECRET,
                "content_type": "long_article",
                "target_platforms": ["zhihu"],
            },
            "idempotency_key": "mcp-stage-001",
        },
        StageContentRequest,
        "mcp-stage-001",
    ),
    (
        "plan_publication",
        {
            "request": {"schema_version": 1, "content_id": 10, "account_ids": [2]},
            "idempotency_key": "mcp-plan-001",
        },
        PlanPublicationRequest,
        "mcp-plan-001",
    ),
    (
        "request_approval",
        {
            "request": {"schema_version": 1, "plan_id": "20"},
            "idempotency_key": "mcp-approval-001",
        },
        RequestApprovalRequest,
        "mcp-approval-001",
    ),
    (
        "schedule",
        {
            "request": {"schema_version": 1, "plan_id": "20"},
            "idempotency_key": "mcp-schedule-001",
        },
        ScheduleRequest,
        "mcp-schedule-001",
    ),
    (
        "get_job_status",
        {"job_id": 40},
        None,
        None,
    ),
    (
        "collect_metrics",
        {
            "request": {"schema_version": 1, "job_id": 40},
            "idempotency_key": "mcp-metrics-001",
        },
        CollectMetricsRequest,
        "mcp-metrics-001",
    ),
    (
        "review_performance",
        {"request": {"schema_version": 1, "job_ids": [40]}},
        PerformanceReviewRequest,
        None,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "request_type", "expected_idempotency_key"),
    SUCCESS_CASES,
)
async def test_every_tool_maps_once_to_the_http_client_and_closes_it(
    fake_http_client,
    tool_name,
    arguments,
    request_type,
    expected_idempotency_key,
):
    response = fake_http_client.outcomes[tool_name]
    assert isinstance(response, BaseModel)

    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool(tool_name, arguments)

    expected_payload = response.model_dump(mode="json")
    _assert_matching_json_result(result, expected_payload, is_error=False)
    Draft202012Validator(tools[tool_name].output_schema).validate(result.structured_content)
    assert len(fake_http_client.instances) == 1
    instance = fake_http_client.instances[0]
    assert instance.base_url == URL_SECRET
    assert instance.token == TOKEN_SECRET
    assert instance.closed is True
    assert len(instance.calls) == 1
    assert instance.calls[0][0] == tool_name

    if request_type is None:
        assert instance.calls[0] == (tool_name, arguments["job_id"])
    else:
        actual_request = instance.calls[0][1]
        assert isinstance(actual_request, request_type)
        assert actual_request.model_dump(mode="json", exclude_unset=True) == arguments["request"]
        if expected_idempotency_key is None:
            assert instance.calls[0] == (tool_name, actual_request)
        else:
            assert instance.calls[0] == (
                tool_name,
                actual_request,
                expected_idempotency_key,
            )

    wire = _wire_text(result)
    assert TOKEN_SECRET not in wire
    assert URL_SECRET not in wire
    if tool_name == "stage_content":
        assert REQUEST_SECRET not in wire


@pytest.mark.asyncio
async def test_missing_token_returns_stable_error_without_constructing_client(
    fake_http_client,
    monkeypatch,
):
    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    arguments = SUCCESS_CASES[0][1]

    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("stage_content", arguments)

    payload = {
        "schema_version": 1,
        "error": {
            "code": "missing_token",
            "message": "AI_OPS_TOKEN environment variable is required",
        },
    }
    _assert_matching_json_result(result, payload, is_error=True)
    Draft202012Validator(tools["stage_content"].output_schema).validate(
        result.structured_content
    )
    assert fake_http_client.instances == []
    wire = _wire_text(result)
    assert URL_SECRET not in wire
    assert REQUEST_SECRET not in wire


@pytest.mark.asyncio
async def test_client_error_preserves_stable_envelope_and_redacts_inputs(fake_http_client):
    fake_http_client.outcomes["stage_content"] = ClientError(
        "content_not_found",
        "The requested content does not exist",
        status_code=404,
    )

    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("stage_content", SUCCESS_CASES[0][1])

    payload = {
        "schema_version": 1,
        "error": {
            "code": "content_not_found",
            "message": "The requested content does not exist",
        },
    }
    _assert_matching_json_result(result, payload, is_error=True)
    Draft202012Validator(tools["stage_content"].output_schema).validate(
        result.structured_content
    )
    assert len(fake_http_client.instances) == 1
    assert fake_http_client.instances[0].closed is True
    wire = _wire_text(result)
    for secret in (TOKEN_SECRET, URL_SECRET, REQUEST_SECRET):
        assert secret not in wire


@pytest.mark.asyncio
async def test_unknown_client_exception_is_redacted_and_client_is_closed(fake_http_client):
    fake_http_client.outcomes["stage_content"] = RuntimeError(
        f"transport leaked {TOKEN_SECRET} {URL_SECRET} {REQUEST_SECRET}"
    )

    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("stage_content", SUCCESS_CASES[0][1])

    payload = {
        "schema_version": 1,
        "error": {
            "code": "client_failure",
            "message": "Agent API MCP tool failed",
        },
    }
    _assert_matching_json_result(result, payload, is_error=True)
    Draft202012Validator(tools["stage_content"].output_schema).validate(
        result.structured_content
    )
    assert len(fake_http_client.instances) == 1
    assert fake_http_client.instances[0].closed is True
    wire = _wire_text(result)
    for secret in (TOKEN_SECRET, URL_SECRET, REQUEST_SECRET):
        assert secret not in wire


@pytest.mark.asyncio
async def test_invalid_tool_schema_is_rejected_before_client_construction(fake_http_client):
    arguments = {
        "request": {
            "schema_version": 1,
            "topic_id": 0,
            "title": REQUEST_SECRET,
            "content_type": "long_article",
        },
        "idempotency_key": IDEMPOTENCY_KEY,
    }

    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("stage_content", arguments)

    payload = {
        "schema_version": 1,
        "error": {
            "code": "invalid_request",
            "message": "MCP tool arguments do not match the versioned Agent contract",
        },
    }
    _assert_matching_json_result(result, payload, is_error=True)
    Draft202012Validator(tools["stage_content"].output_schema).validate(
        result.structured_content
    )
    assert fake_http_client.instances == []
    wire = _wire_text(result)
    assert "topic_id" not in wire
    for secret in (TOKEN_SECRET, URL_SECRET, REQUEST_SECRET):
        assert secret not in wire
