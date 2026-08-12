from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_ops.publishers import github_pages_gh as gh_module
from ai_ops.publishers.github_pages_gh import (
    GhPagesConfig,
    GitHubPagesGhVerifier,
    _included_api_response,
    _strict_json_object,
    github_repository_from_push_url,
)


COMMIT_SHA = "a" * 40
MARKER = "b" * 64
REPOSITORY = "Owner/site"
BASE_URL = "https://owner.github.io/site"
ARTICLE_URL = f"{BASE_URL}/posts/offline-proof.html"


@dataclass(slots=True)
class FakeCommandResult:
    started: bool = True
    returncode: int | None = 0
    stdout: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.started and not self.timed_out and self.returncode == 0


class RecordingRunner:
    def __init__(self, results: Iterable[FakeCommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], Path, int]] = []

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> FakeCommandResult:
        self.calls.append((argv, cwd, timeout_seconds))
        if not self.results:
            raise AssertionError(f"unexpected command: {argv!r}")
        result = self.results.pop(0)
        if (
            len(argv) > 1
            and argv[1] == "api"
            and result.returncode == 0
            and "--include" in argv
            and not result.stdout.startswith("HTTP/")
        ):
            result.stdout = (
                "HTTP/2.0 200 OK\nContent-Type: application/json\r\n\r\n" + result.stdout
            )
        return result


def _config(**overrides: Any) -> GhPagesConfig:
    values: dict[str, Any] = {
        "repository": REPOSITORY,
        "branch": "pages",
        "base_url": BASE_URL,
        "expected_version": "2.97.0",
        "token_configured": True,
        "command_timeout_seconds": 7,
        "deploy_timeout_seconds": 10,
        "poll_seconds": 1,
        "readback_timeout_seconds": 10,
        "readback_request_timeout_seconds": 2,
        "readback_max_bytes": 128,
        "binary": "gh",
    }
    values.update(overrides)
    return GhPagesConfig(**values)


def _pages_payload(*, omit: Iterable[str] = (), **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "status": "built",
        "html_url": BASE_URL,
        "build_type": "workflow",
        "source": {"branch": "pages", "path": "/"},
        "https_enforced": True,
        "public": True,
    }
    payload.update(overrides)
    for key in omit:
        payload.pop(key, None)
    return json.dumps(payload)


def _verifier(
    runner: RecordingRunner | None = None,
    **config_overrides: Any,
) -> GitHubPagesGhVerifier:
    if runner is None:
        runner = RecordingRunner([])
    return GitHubPagesGhVerifier(
        _config(**config_overrides), cwd=Path("/offline/repo"), runner=runner
    )


def _expected_api_argv(endpoint: str, jq: str) -> list[str]:
    return [
        "gh",
        "api",
        endpoint,
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "--include",
        "--jq",
        jq,
    ]


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("https://github.com/Owner/site.git", REPOSITORY),
        ("https://github.com:443/Owner/site", REPOSITORY),
        ("ssh://git@github.com/Owner/site.git", REPOSITORY),
        ("ssh://git@github.com:22/Owner/site", REPOSITORY),
        ("git@github.com:Owner/site.git", REPOSITORY),
        ("git@GITHUB.COM:Owner/site", REPOSITORY),
    ],
)
def test_push_url_parser_accepts_only_supported_github_forms(
    remote_url: str,
    expected: str,
) -> None:
    assert github_repository_from_push_url(remote_url) == expected


@pytest.mark.parametrize(
    "remote_url",
    [
        "",
        "/tmp/Owner/site",
        "../Owner/site",
        "file:///tmp/Owner/site",
        "git://github.com/Owner/site.git",
        "https://git@github.com/Owner/site.git",
        "https://token:secret@github.com/Owner/site.git",
        "https://github.example/Owner/site.git",
        "https://github.com.evil.example/Owner/site.git",
        "https://github.com:444/Owner/site.git",
        "ssh://root@github.com/Owner/site.git",
        "ssh://git@github.com:23/Owner/site.git",
        "ssh://git@example.com/Owner/site.git",
        "git@example.com:Owner/site.git",
        "https://github.com/Owner/site.git?token=secret",
        "https://github.com/Owner/site.git#fragment",
        "https://github.com/Owner/site/extra",
        "ext::ssh -i key git@github.com:Owner/site.git",
        "https://github.com/Owner/site.git\nhttps://github.com/Other/site.git",
        "https://github.com/Owner/site.git https://github.com/Other/site.git",
    ],
)
def test_push_url_parser_rejects_credentials_multiple_hosts_and_local_paths(
    remote_url: str,
) -> None:
    assert github_repository_from_push_url(remote_url) is None


@pytest.mark.asyncio
async def test_preflight_uses_fixed_version_and_read_only_pages_api_argv() -> None:
    runner = RecordingRunner(
        [
            FakeCommandResult(stdout="gh version 2.97.0 (2026-07-01)\nrelease notes\n"),
            FakeCommandResult(stdout=_pages_payload()),
        ]
    )
    verifier = _verifier(runner)

    result = await verifier.preflight(remote_url="git@github.com:owner/site.git")

    assert result.success is True
    assert result.error is None
    assert runner.results == []
    assert runner.calls == [
        (["gh", "--version"], Path("/offline/repo"), 7),
        (
            _expected_api_argv(
                "repos/Owner/site/pages",
                '{"status":.status,"html_url":.html_url,"build_type":.build_type,'
                '"source":.source,"https_enforced":.https_enforced,"public":.public}',
            ),
            Path("/offline/repo"),
            7,
        ),
    ]
    flattened = " ".join(part for argv, _, _ in runner.calls for part in argv)
    assert "secret" not in flattened
    assert "Authorization" not in flattened
    assert "--method POST" not in flattened


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_stdout", "expected_error"),
    [
        ("gh version 2.96.0 (old)\n", "version"),
        ("gh version 2.97\n", "version"),
        ("GitHub CLI 2.97.0\n", "version"),
        ("\n", "version"),
    ],
)
async def test_preflight_rejects_non_audited_gh_version_before_api_call(
    version_stdout: str,
    expected_error: str,
) -> None:
    runner = RecordingRunner([FakeCommandResult(stdout=version_stdout)])

    result = await _verifier(runner).preflight(remote_url="https://github.com/Owner/site.git")

    assert result.success is False
    assert expected_error in (result.error or "").lower()
    assert [call[0] for call in runner.calls] == [["gh", "--version"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_overrides", "remote_url", "expected_error"),
    [
        ({"token_configured": False}, "https://github.com/Owner/site.git", "token"),
        ({"repository": "owner/site.git"}, "https://github.com/Owner/site.git", "identity"),
        ({}, "https://github.com/Other/site.git", "does not match"),
        ({}, "https://example.com/Owner/site.git", "approved github.com"),
        ({"base_url": "http://owner.github.io/site"}, "git@github.com:Owner/site.git", "HTTPS"),
        (
            {"base_url": "https://other.github.io/site"},
            "git@github.com:Owner/site.git",
            "github.io",
        ),
        (
            {"base_url": "https://owner.github.io/site/../admin"},
            "git@github.com:Owner/site.git",
            "github.io",
        ),
    ],
)
async def test_preflight_rejects_invalid_static_boundaries_without_running_gh(
    config_overrides: dict[str, Any],
    remote_url: str,
    expected_error: str,
) -> None:
    runner = RecordingRunner([])

    result = await _verifier(runner, **config_overrides).preflight(remote_url=remote_url)

    assert result.success is False
    assert expected_error.lower() in (result.error or "").lower()
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (_pages_payload(build_type="legacy"), "workflow"),
        (_pages_payload(https_enforced=False), "HTTPS"),
        (_pages_payload(public=False), "public"),
        (_pages_payload(public=None), "public"),
        (_pages_payload(omit={"public"}), "malformed"),
        (_pages_payload(html_url="https://owner.github.io/other"), "base URL"),
        (_pages_payload(source=False), "source"),
        (_pages_payload(source={"branch": "main", "path": "/"}), "branch"),
        (_pages_payload(source={"path": "/"}), "branch"),
        (_pages_payload(source={"branch": "", "path": "/"}), "branch"),
        (_pages_payload(source={"branch": "pages", "path": 1}), "path"),
        (_pages_payload(status=1), "status"),
        (_pages_payload(extra=True), "malformed"),
        ("not-json", "malformed"),
    ],
)
async def test_preflight_rejects_unsafe_or_malformed_pages_metadata(
    payload: str,
    expected_error: str,
) -> None:
    runner = RecordingRunner(
        [
            FakeCommandResult(stdout="gh version 2.97.0\n"),
            FakeCommandResult(stdout=payload),
        ]
    )

    result = await _verifier(runner).preflight(remote_url="https://github.com/Owner/site.git")

    assert result.success is False
    assert expected_error.lower() in (result.error or "").lower()


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "[]",
        "null",
        'garbage {"status":"succeed"}',
        '{"status":"succeed"} trailing',
        '{"status":"succeed","status":"failed"}',
        '{"outer":{"key":1,"key":2}}',
        json.dumps({"value": "x" * 20_000}),
    ],
)
def test_strict_json_rejects_non_object_garbage_duplicates_and_oversize(payload: str) -> None:
    assert _strict_json_object(payload) is None


def test_strict_json_returns_object_without_normalizing_values() -> None:
    assert _strict_json_object('{"status":"succeed","nested":{"value":1}}') == {
        "status": "succeed",
        "nested": {"value": 1},
    }


def test_strict_json_treats_unencodable_text_as_invalid_input() -> None:
    assert _strict_json_object("{" + chr(0xD800) + "}") is None


def test_included_api_response_keeps_only_bounded_polling_metadata() -> None:
    parsed = _included_api_response(
        "HTTP/2.0 404 Not Found\nContent-Type: application/json\r\n"
        "X-Poll-Interval: 3\r\nRetry-After: 5\r\n"
        "X-RateLimit-Remaining: 42\r\nX-RateLimit-Reset: 1780000000\r\n"
        'X-Ignored: value\r\n\r\n{"message":"not found"}\n'
    )

    assert parsed is not None
    assert parsed.status_code == 404
    assert parsed.body == '{"message":"not found"}\n'
    assert parsed.poll_interval_seconds == 3
    assert parsed.retry_after_seconds == 5
    assert parsed.rate_limit_remaining == 42
    assert parsed.rate_limit_reset_epoch == 1_780_000_000


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "HTTP/2.0 200 OK\nno-boundary",
        "HTTP/2.0 999 Unknown\nHeader: value\n\n{}",
        "HTTP/2.0 200 OK\nmalformed-header\n\n{}",
        "HTTP/2.0 200 OK\nX: " + "x" * 40_000 + "\n\n{}",
        "HTTP/2.0 200 OK\nX-Poll-Interval: 1\nx-poll-interval: 2\n\n{}",
        "HTTP/2.0 200 OK\nRetry-After: +1\n\n{}",
        "HTTP/2.0 200 OK\nRetry-After: 1.5\n\n{}",
        "HTTP/2.0 200 OK\nRetry-After: 86401\n\n{}",
        "HTTP/2.0 200 OK\nX-RateLimit-Remaining: 1000000001\n\n{}",
        "HTTP/2.0 200 OK\nX-RateLimit-Reset: 10000000000\n\n{}",
    ],
)
def test_included_api_response_rejects_malformed_or_oversize_envelopes(payload: str) -> None:
    assert _included_api_response(payload) is None


@pytest.mark.asyncio
async def test_deployment_retries_pending_and_temporary_api_failure_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gh_module.asyncio, "sleep", fake_sleep)
    runner = RecordingRunner(
        [
            FakeCommandResult(stdout='{"status":"deployment_in_progress"}'),
            FakeCommandResult(
                returncode=1,
                stdout=(
                    "HTTP/2.0 404 Not Found\nContent-Type: application/json\r\n\r\n"
                    '{"message":"not ready"}'
                ),
            ),
            FakeCommandResult(stdout='{"status":"succeed"}'),
        ]
    )

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is True
    assert result.error is None
    assert sleeps == [1.0, 1.0]
    expected = _expected_api_argv(
        f"repos/{REPOSITORY}/pages/deployments/{COMMIT_SHA}",
        '{"status":.status}',
    )
    assert [argv for argv, _, _ in runner.calls] == [expected, expected, expected]


@pytest.mark.asyncio
async def test_deployment_honors_bounded_poll_and_retry_after_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gh_module.asyncio, "sleep", fake_sleep)
    runner = RecordingRunner(
        [
            FakeCommandResult(
                stdout=(
                    "HTTP/2.0 200 OK\r\nX-Poll-Interval: 3\r\n\r\n"
                    '{"status":"deployment_in_progress"}'
                )
            ),
            FakeCommandResult(
                returncode=1,
                stdout=(
                    'HTTP/2.0 503 Unavailable\r\nRetry-After: 4\r\n\r\n{"message":"retry later"}'
                ),
            ),
            FakeCommandResult(stdout='{"status":"succeed"}'),
        ]
    )

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is True
    assert sleeps == [3.0, 4.0]


@pytest.mark.asyncio
async def test_deployment_honors_rate_limit_reset_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gh_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gh_module.time, "time", lambda: 1_000.0)
    runner = RecordingRunner(
        [
            FakeCommandResult(
                returncode=1,
                stdout=(
                    "HTTP/2.0 429 Rate Limited\r\n"
                    "X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1004\r\n\r\n"
                    '{"message":"rate limited"}'
                ),
            ),
            FakeCommandResult(stdout='{"status":"succeed"}'),
        ]
    )

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is True
    assert sleeps == [4.0]


@pytest.mark.asyncio
async def test_deployment_does_not_retry_before_server_delay_beyond_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_sleep(_: float) -> None:
        raise AssertionError("an unsatisfied server delay must not be shortened")

    monkeypatch.setattr(gh_module.asyncio, "sleep", fail_sleep)
    runner = RecordingRunner(
        [
            FakeCommandResult(
                returncode=1,
                stdout=(
                    'HTTP/2.0 429 Rate Limited\r\nRetry-After: 60\r\n\r\n{"message":"rate limited"}'
                ),
            )
        ]
    )

    result = await _verifier(runner, deploy_timeout_seconds=10).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "timed out" in (result.error or "")
    assert len(runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "deployment_cancelled",
        "deployment_failed",
        "deployment_content_failed",
        "deployment_attempt_error",
        "deployment_lost",
    ],
)
async def test_deployment_terminal_failures_are_certain(status: str) -> None:
    runner = RecordingRunner([FakeCommandResult(stdout=json.dumps({"status": status}))])

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is False
    assert "terminal failure" in (result.error or "")


@pytest.mark.asyncio
async def test_deployment_unknown_status_is_uncertain() -> None:
    runner = RecordingRunner([FakeCommandResult(stdout='{"status":"future_state"}')])

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "unknown status" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        '{"status":1}',
        '{"status":"succeed","extra":true}',
        '{"status":"succeed","status":"deployment_failed"}',
        json.dumps({"status": "x" * 20_000}),
    ],
)
async def test_deployment_malformed_status_is_uncertain(payload: str) -> None:
    runner = RecordingRunner([FakeCommandResult(stdout=payload)])

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "malformed" in (result.error or "")


@pytest.mark.asyncio
async def test_deployment_cli_disappearance_after_acceptance_is_uncertain() -> None:
    runner = RecordingRunner([FakeCommandResult(started=False, returncode=None)])

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "unavailable" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_deployment_auth_or_rate_limit_response_stops_polling(status_code: int) -> None:
    runner = RecordingRunner(
        [
            FakeCommandResult(
                returncode=1,
                stdout=(
                    f"HTTP/2.0 {status_code} Rejected\n"
                    'Content-Type: application/json\r\n\r\n{"message":"redacted"}'
                ),
            )
        ]
    )

    result = await _verifier(runner).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "access stopped" in (result.error or "")
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_deployment_timeout_does_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_sleep(_: float) -> None:
        raise AssertionError("deployment timeout must not sleep")

    monkeypatch.setattr(gh_module.asyncio, "sleep", fail_sleep)
    runner = RecordingRunner([FakeCommandResult(stdout='{"status":"deployment_in_progress"}')])

    result = await _verifier(runner, deploy_timeout_seconds=0).wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_deployment_deadline_cancels_a_runner_that_never_returns() -> None:
    calls = 0

    async def never_returns(*_args: Any, **_kwargs: Any) -> FakeCommandResult:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    verifier = GitHubPagesGhVerifier(
        _config(deploy_timeout_seconds=0.02, command_timeout_seconds=1),
        cwd=Path("/offline/repo"),
        runner=never_returns,
    )

    result = await verifier.wait_for_deployment(COMMIT_SHA)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "timed out" in (result.error or "")
    assert calls == 1


@pytest.mark.asyncio
async def test_deployment_rejects_invalid_commit_without_command() -> None:
    runner = RecordingRunner([])

    result = await _verifier(runner).wait_for_deployment("HEAD")

    assert result.success is False
    assert result.outcome_uncertain is False
    assert runner.calls == []


@pytest.mark.parametrize(
    "article_url",
    [
        ARTICLE_URL,
        f"{BASE_URL}/%E4%B8%AD%E6%96%87.html",
        "https://OWNER.github.io:443/site/posts/proof.html",
    ],
)
def test_readback_boundary_accepts_only_descendants_on_configured_origin(article_url: str) -> None:
    assert _verifier()._approved_article_url(article_url) is True


@pytest.mark.parametrize(
    "article_url",
    [
        BASE_URL,
        f"{BASE_URL}/",
        "http://owner.github.io/site/post.html",
        "https://other.github.io/site/post.html",
        "https://owner.github.io.evil.example/site/post.html",
        "https://owner.github.io:444/site/post.html",
        "https://user@owner.github.io/site/post.html",
        "https://user:secret@owner.github.io/site/post.html",
        "https://owner.github.io/site/post.html?preview=true",
        "https://owner.github.io/site/post.html#marker",
        "https://owner.github.io/site-evil/post.html",
        "https://owner.github.io/site/../admin.html",
        "https://owner.github.io/site/%2e%2e/admin.html",
        "https://owner.github.io/site/%2Fadmin.html",
        "https://owner.github.io/site/post%5Cadmin.html",
        "https://owner.github.io/site/%ZZ.html",
        "https://owner.github.io/site/%00.html",
        "https://owner.github.io/site//post.html",
    ],
)
def test_readback_boundary_rejects_noncanonical_or_escaping_urls(article_url: str) -> None:
    assert _verifier()._approved_article_url(article_url) is False


class FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: Iterable[bytes] = (),
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.raw_chunk_sizes: list[int | None] = []

    async def __aenter__(self) -> FakeStreamResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_raw(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        self.raw_chunk_sizes.append(chunk_size)
        for chunk in self.chunks:
            yield chunk


class FakeAsyncClient:
    def __init__(
        self,
        response: FakeStreamResponse | None,
        *,
        calls: list[tuple[Any, ...]],
        stream_error: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        self.response = response
        self.calls = calls
        self.stream_error = stream_error
        self.calls.append(("init", kwargs))

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str) -> FakeStreamResponse:
        self.calls.append(("stream", method, url))
        if self.stream_error is not None:
            raise self.stream_error
        assert self.response is not None
        return self.response


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeStreamResponse | None,
    *,
    stream_error: Exception | None = None,
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def factory(**kwargs: Any) -> FakeAsyncClient:
        return FakeAsyncClient(
            response,
            calls=calls,
            stream_error=stream_error,
            **kwargs,
        )

    monkeypatch.setattr(gh_module.httpx, "AsyncClient", factory)
    return calls


@pytest.mark.asyncio
async def test_readback_once_verifies_marker_and_uses_hardened_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    needle = f'data-ai-ops-publication="{MARKER}"'.encode()
    response = FakeStreamResponse(
        headers={"content-type": "text/html; charset=utf-8"},
        chunks=[b"<html>", needle[:20], needle[20:], b"</html>"],
    )
    calls = _install_fake_httpx(monkeypatch, response)

    status = await _verifier(readback_max_bytes=256)._readback_once(ARTICLE_URL, needle)

    assert status == "verified"
    assert calls[1] == ("stream", "GET", ARTICLE_URL)
    options = calls[0][1]
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert options["headers"] == {
        "Accept": "text/html",
        "Accept-Encoding": "identity",
        "User-Agent": "ai-ops-auto/pages-verifier",
    }
    assert isinstance(options["timeout"], httpx.Timeout)
    assert response.raw_chunk_sizes == [64 * 1024]


@pytest.mark.asyncio
@pytest.mark.parametrize("content_encoding", ["gzip", "br", "deflate", "gzip, br", ""])
async def test_readback_once_rejects_encoded_bodies_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
    content_encoding: str,
) -> None:
    response = FakeStreamResponse(
        headers={
            "content-type": "text/html",
            "content-encoding": content_encoding,
        },
        chunks=[b"compressed payload must not be consumed"],
    )
    _install_fake_httpx(monkeypatch, response)

    status = await _verifier()._readback_once(ARTICLE_URL, b"needle")

    assert status == "rejected"
    assert response.raw_chunk_sizes == []


@pytest.mark.asyncio
async def test_readback_once_accepts_explicit_identity_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeStreamResponse(
        headers={
            "content-type": "text/html",
            "content-encoding": " Identity ",
        },
        chunks=[b"needle"],
    )
    _install_fake_httpx(monkeypatch, response)

    status = await _verifier()._readback_once(ARTICLE_URL, b"needle")

    assert status == "verified"
    assert response.raw_chunk_sizes == [64 * 1024]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            FakeStreamResponse(
                headers={"content-type": "text/html"},
                chunks=[b"<html>marker missing</html>"],
            ),
            "retry",
        ),
        (
            FakeStreamResponse(
                status_code=302,
                headers={"location": "https://evil.example/"},
            ),
            "rejected",
        ),
        (
            FakeStreamResponse(
                headers={"content-type": "application/json"},
                chunks=[b"{}"],
            ),
            "rejected",
        ),
        (
            FakeStreamResponse(
                headers={"content-type": "text/html", "content-length": "129"},
            ),
            "rejected",
        ),
        (
            FakeStreamResponse(
                headers={"content-type": "text/html", "content-length": "invalid"},
            ),
            "rejected",
        ),
        (
            FakeStreamResponse(
                headers={"content-type": "text/html", "content-length": "-1"},
            ),
            "rejected",
        ),
        (
            FakeStreamResponse(
                headers={"content-type": "text/html"},
                chunks=[b"x" * 100, b"y" * 29],
            ),
            "rejected",
        ),
        (FakeStreamResponse(status_code=503), "retry"),
    ],
)
async def test_readback_once_handles_missing_marker_redirect_type_and_size_contracts(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeStreamResponse,
    expected: str,
) -> None:
    _install_fake_httpx(monkeypatch, response)

    status = await _verifier()._readback_once(ARTICLE_URL, b"needle")

    assert status == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("offline timeout"), "retry"),
        (httpx.ConnectError("offline transport"), "retry"),
        (RuntimeError("unexpected client bug"), "rejected"),
    ],
)
async def test_readback_once_classifies_http_client_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
) -> None:
    _install_fake_httpx(monkeypatch, None, stream_error=error)

    status = await _verifier()._readback_once(ARTICLE_URL, b"needle")

    assert status == expected


class SequencedReadbackVerifier(GitHubPagesGhVerifier):
    def __init__(self, statuses: Iterable[str], **config_overrides: Any) -> None:
        super().__init__(
            _config(**config_overrides),
            cwd=Path("/offline/repo"),
            runner=RecordingRunner([]),
        )
        self.statuses = list(statuses)
        self.readback_calls: list[tuple[str, bytes]] = []

    async def _readback_once(self, article_url: str, needle: bytes) -> str:
        self.readback_calls.append((article_url, needle))
        if not self.statuses:
            raise AssertionError("unexpected readback poll")
        return self.statuses.pop(0)


@pytest.mark.asyncio
async def test_wait_for_readback_retries_without_network_then_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gh_module.asyncio, "sleep", fake_sleep)
    verifier = SequencedReadbackVerifier(["retry", "verified"])

    result = await verifier.wait_for_readback(article_url=ARTICLE_URL, marker=MARKER)

    assert result.success is True
    assert result.outcome_uncertain is False
    assert sleeps == [1.0]
    assert verifier.readback_calls == [
        (ARTICLE_URL, f'data-ai-ops-publication="{MARKER}"'.encode()),
        (ARTICLE_URL, f'data-ai-ops-publication="{MARKER}"'.encode()),
    ]


@pytest.mark.asyncio
async def test_wait_for_readback_rejection_is_terminal_and_uncertain() -> None:
    verifier = SequencedReadbackVerifier(["rejected"])

    result = await verifier.wait_for_readback(article_url=ARTICLE_URL, marker=MARKER)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "response contract" in (result.error or "")
    assert len(verifier.readback_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_readback_enforces_total_deadline_during_a_slow_stream() -> None:
    class NeverFinishingReadbackVerifier(SequencedReadbackVerifier):
        def __init__(self) -> None:
            super().__init__(
                [],
                readback_timeout_seconds=0.02,
                readback_request_timeout_seconds=1,
            )
            self.started = 0

        async def _readback_once(self, article_url: str, needle: bytes) -> str:
            del article_url, needle
            self.started += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    verifier = NeverFinishingReadbackVerifier()

    result = await verifier.wait_for_readback(article_url=ARTICLE_URL, marker=MARKER)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert "timed out" in (result.error or "")
    assert verifier.started == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("article_url", "marker", "expected_error"),
    [
        (ARTICLE_URL, "A" * 64, "marker"),
        (ARTICLE_URL, "b" * 63, "marker"),
        ("https://evil.example/post.html", MARKER, "boundary"),
    ],
)
async def test_wait_for_readback_rejects_invalid_input_without_http(
    article_url: str,
    marker: str,
    expected_error: str,
) -> None:
    verifier = SequencedReadbackVerifier([])

    result = await verifier.wait_for_readback(article_url=article_url, marker=marker)

    assert result.success is False
    assert expected_error in (result.error or "")
    assert verifier.readback_calls == []
