import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/model_tool_probe.py"
spec = importlib.util.spec_from_file_location("model_tool_probe", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CALL = {"id": "call-1", "type": "function", "function": {
    "name": "lookup_receipt", "arguments": '{"ticket":"probe"}'}}


def completion(message, reason="tool_calls", usage=True):
    body = {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": reason}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
    return body


def run(handler, **kwargs):
    return asyncio.run(module.probe("https://fixture.invalid/v1", "private-credential", "fixture-model",
                                    transport=httpx.MockTransport(handler), **kwargs))


def test_success_requires_receipt_from_tool_and_records_only_aggregates():
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["authorization"] == "Bearer private-credential"
        assert request.url.path == "/v1/chat/completions"
        assert payload["max_tokens"] == 256 and payload["stream"] is False
        if len(requests) == 1:
            assert payload["tool_choice"] == "auto"
            return httpx.Response(200, json=completion({"tool_calls": [CALL], "reasoning": "private-trace"}))
        assert payload["tool_choice"] == "none"
        assert payload["messages"][-1]["tool_call_id"] == CALL["id"]
        receipt = payload["messages"][-1]["content"]
        assert receipt not in json.dumps(requests[0])
        assert "private-trace" not in json.dumps(payload)
        return httpx.Response(200, json=completion({"content": receipt}, "stop"))

    result = run(handler)
    assert result["status"] == "passed" and result["fixture_tool_calls"] == 1
    assert result["requests_started"] == result["responses_received"] == 2
    assert result["prompt_tokens"] == 20 and result["completion_tokens"] == 10
    assert result["cost"] is None and result["elapsed_seconds"] >= 0
    assert all(value not in json.dumps(result) for value in
               ("private-", "fixture.invalid", "call-1", requests[-1]["messages"][-1]["content"]))


@pytest.mark.parametrize("change", [
    {"type": "custom"}, {"id": ""}, {"id": " "}, {"id": "x" * 513},
    {"function": {"name": "delete_file", "arguments": '{"ticket":"probe"}'}},
    {"function": {"name": "lookup_receipt", "arguments": '{"ticket":"wrong"}'}},
    {"function": {"name": "lookup_receipt", "arguments": '{"ticket":"probe","extra":1}'}},
    {"function": {"name": "lookup_receipt", "arguments": '{"ticket":"wrong","ticket":"probe"}'}},
    {"function": {"name": "lookup_receipt", "arguments": 'NaN'}},
])
def test_invalid_tool_call_never_executes_or_retries(change):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion({"tool_calls": [{**copy.deepcopy(CALL), **change}]}))

    result = run(handler)
    assert result["status"] in {"tool_contract_failed", "invalid_response"}
    assert result["fixture_tool_calls"] == 0 and len(requests) == 1


@pytest.mark.parametrize("body", [None, [], {}, {"choices": []},
    completion({"content": "guessed"}, "stop"), completion({"tool_calls": [CALL, CALL]}),
    completion({"tool_calls": [CALL]}, "length"),
    completion({"tool_calls": [CALL], "refusal": "private-refusal"})])
def test_malformed_incomplete_or_parallel_first_response_cannot_pass(body):
    result = run(lambda request: httpx.Response(200, content=json.dumps(body)))
    assert result["status"] != "passed" and result["fixture_tool_calls"] == 0
    assert result["requests_started"] == 1 and "private-" not in json.dumps(result)


@pytest.mark.parametrize("reason,calls,content", [
    ("length", None, "receipt"), ("stop", None, "wrong"), ("tool_calls", [CALL], "receipt"),
])
def test_final_response_must_complete_and_match_exactly(reason, calls, content):
    def handler(request):
        messages = json.loads(request.content)["messages"]
        if len(messages) == 1:
            return httpx.Response(200, json=completion({"tool_calls": [CALL]}))
        answer = messages[-1]["content"] if content == "receipt" else content
        return httpx.Response(200, json=completion({"content": answer, "tool_calls": calls}, reason))

    result = run(handler)
    assert result["status"] == "answer_contract_failed" and result["requests_started"] == 2


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": True, "completion_tokens": 1},
                                    {"prompt_tokens": 1, "completion_tokens": -1}])
def test_missing_or_invalid_usage_is_unknown_not_partial_or_zero(usage):
    def handler(request):
        messages = json.loads(request.content)["messages"]
        if len(messages) == 1:
            return httpx.Response(200, json=completion({"tool_calls": [CALL]}))
        body = completion({"content": messages[-1]["content"]}, "stop")
        body["usage"] = usage
        return httpx.Response(200, json=body)

    result = run(handler)
    assert result["status"] == "passed"
    assert result["prompt_tokens"] is result["completion_tokens"] is result["cost"] is None


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_and_redirects_are_not_retried_or_logged(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.invalid"}, content=b"private-body")

    result = run(handler)
    assert result["status"] == "http_error" and len(requests) == 1
    assert result["prompt_tokens"] is None and "private-" not in json.dumps(result)


def test_total_timeout_cancels_pending_second_request_without_retry():
    cancelled = []

    async def handler(request):
        if len(json.loads(request.content)["messages"]) == 1:
            return httpx.Response(200, json=completion({"tool_calls": [CALL]}))
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    result = run(handler, timeout=0.1)
    assert result["status"] == "timeout" and result["requests_started"] == 2 and cancelled == [True]
    assert result["prompt_tokens"] is None


def test_transport_failure_and_oversize_response_are_sanitized():
    def failed(request):
        raise httpx.ConnectError("private-credential")

    result = run(failed)
    assert result["status"] == "transport_error" and "private-" not in json.dumps(result)
    result = run(lambda request: httpx.Response(200, content=b"x" * (module.MAX_RESPONSE_BYTES + 1)))
    assert result["status"] == "response_too_large" and result["requests_started"] == 1


@pytest.mark.parametrize("extra", [{"tool_calls": {}}, {"tool_calls": ""}, {"function_call": {}}])
def test_malformed_final_call_fields_do_not_pass(extra):
    def handler(request):
        messages = json.loads(request.content)["messages"]
        if len(messages) == 1:
            return httpx.Response(200, json=completion({"tool_calls": [CALL]}))
        return httpx.Response(200, json=completion({"content": messages[-1]["content"], **extra}, "stop"))

    assert run(handler)["status"] == "invalid_response"


@pytest.mark.parametrize("options", [{"max_tokens": 0}, {"max_tokens": 1025}, {"max_tokens": True},
                                     {"timeout": float("nan")}, {"timeout": 0}, {"timeout": 61}])
def test_invalid_limits_rejected_without_requests(options):
    def handler(request):
        pytest.fail("Invalid configuration must not send a request")

    with pytest.raises(ValueError):
        run(handler, **options)


@pytest.mark.parametrize("url", ["http://remote.invalid/v1", "https://u:secret@host/v1",
                                 "https://host/v1?key=secret", "https://host/v1#secret", "file:///x",
                                 "https://host:invalid/v1"])
def test_unsafe_endpoint_rejected_before_network(url):
    with pytest.raises(ValueError):
        module.endpoint(url)


def test_cli_requires_explicit_live_and_configuration(monkeypatch):
    monkeypatch.delenv("EASEL_PROBE_API_KEY", raising=False)
    for arguments in (["--model", "fixture"], ["--model", "fixture", "--live"]):
        result = subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True)
        assert result.returncode == 2 and not result.stdout and "Traceback" not in result.stderr
