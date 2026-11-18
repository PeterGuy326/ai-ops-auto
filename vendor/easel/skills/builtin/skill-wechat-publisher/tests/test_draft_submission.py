import json

import pytest
import requests

import api


@pytest.fixture(params=["news", "newspic"])
def submit(request, monkeypatch):
    monkeypatch.setattr(api, "get_access_token", lambda: "private-token")
    if request.param == "news":
        return lambda: api.add_draft({"title": "fixture", "content": "body", "thumb_media_id": "cover"})
    return lambda: api.add_newspic_draft("fixture", "body", ["image"])


def install(monkeypatch, *, status=200, body=None, error=None):
    calls = []

    def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if error:
            raise error
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(api.requests, "request", send)
    return calls


@pytest.mark.parametrize("error", [requests.Timeout("private-token"), requests.ConnectionError("private-token")])
def test_transport_failure_is_unknown_and_never_retried(submit, monkeypatch, error):
    calls = install(monkeypatch, error=error)
    with pytest.raises(api.DraftOutcomeUnknown) as caught:
        submit()
    assert len(calls) == 1
    assert "private-token" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("status", [301, 302, 400, 401, 429, 500, 502, 503])
def test_http_failure_or_redirect_never_repeats_side_effect(submit, monkeypatch, status):
    calls = install(monkeypatch, status=status, body={"errmsg": "private provider body"})
    with pytest.raises(api.DraftOutcomeUnknown):
        submit()
    assert len(calls) == 1
    assert calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize("body", [None, [], {}, {"media_id": ""}, {"media_id": 12},
                                   {"media_id": "x" * 513}, {"media_id": "ok", "errcode": False}])
def test_malformed_success_remains_unknown(submit, monkeypatch, body):
    calls = install(monkeypatch, body=body)
    with pytest.raises(api.DraftOutcomeUnknown):
        submit()
    assert len(calls) == 1


def test_explicit_provider_rejection_is_not_reported_as_success(submit, monkeypatch):
    calls = install(monkeypatch, body={"errcode": 40007, "errmsg": "private response"})
    with pytest.raises(RuntimeError) as caught:
        submit()
    assert not isinstance(caught.value, api.DraftOutcomeUnknown)
    assert "40007" in str(caught.value) and "private" not in str(caught.value)
    assert len(calls) == 1


def test_valid_receipt_is_returned_and_request_shape_preserved(submit, monkeypatch):
    calls = install(monkeypatch, body={"media_id": "fixture-receipt"})
    assert submit() == "fixture-receipt"
    method, url, kwargs = calls[0]
    assert method == "POST" and "/draft/add?" in url
    assert json.loads(kwargs["data"])["articles"][0]["title"] == "fixture"
    assert kwargs["timeout"] == api.API_TIMEOUT_SEC
    assert len(calls) == 1


def test_invalid_local_json_does_not_start_a_request(monkeypatch):
    monkeypatch.setattr(api, "get_access_token", lambda: "fixture")
    calls = install(monkeypatch)
    with pytest.raises(ValueError):
        api.add_draft({"content": float("nan")})
    assert calls == []
