"""Read-only GitHub Pages proof layer backed by the official ``gh`` CLI.

The existing publisher keeps ownership of every write (local files, git
commit, and git push).  This module only proves that the exact accepted commit
was deployed and that the resulting public page contains this invocation's
marker.  It intentionally has no workflow-dispatch or Pages-create surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

import httpx

from ..config import canonical_github_pages_base, valid_github_pages_repository


_GITHUB_HOST = "github.com"
_GITHUB_API_VERSION = "2026-03-10"
_GH_VERSION_RE = re.compile(r"gh version (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s.*)?\Z")
_SHA_RE = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_MAX_GH_JSON_BYTES = 16 * 1024
_MAX_GH_HEADER_BYTES = 32 * 1024
_MAX_SERVER_DELAY_SECONDS = 24 * 60 * 60
_MAX_RATE_LIMIT_REMAINING = 1_000_000_000
_MAX_RATE_LIMIT_RESET_EPOCH = 9_999_999_999
_GH_NUMERIC_HEADER_LIMITS = {
    "x-poll-interval": _MAX_SERVER_DELAY_SECONDS,
    "retry-after": _MAX_SERVER_DELAY_SECONDS,
    "x-ratelimit-remaining": _MAX_RATE_LIMIT_REMAINING,
    "x-ratelimit-reset": _MAX_RATE_LIMIT_RESET_EPOCH,
}
_PAGES_SITE_JQ = (
    '{"status":.status,"html_url":.html_url,"build_type":.build_type,'
    '"source":.source,"https_enforced":.https_enforced,"public":.public}'
)
_DEPLOYMENT_JQ = '{"status":.status}'
_DEPLOYMENT_PENDING = frozenset(
    {
        "deployment_in_progress",
        "syncing_files",
        "finished_file_sync",
        "updating_pages",
        "purging_cdn",
    }
)
_DEPLOYMENT_FAILED = frozenset(
    {
        "deployment_cancelled",
        "deployment_failed",
        "deployment_content_failed",
        "deployment_attempt_error",
        "deployment_lost",
    }
)


CommandRunner = Callable[..., Awaitable[Any]]


@dataclass(slots=True, frozen=True)
class GhPagesConfig:
    repository: str
    branch: str
    base_url: str
    expected_version: str
    token_configured: bool
    command_timeout_seconds: int
    deploy_timeout_seconds: int
    poll_seconds: int
    readback_timeout_seconds: int
    readback_request_timeout_seconds: int
    readback_max_bytes: int
    binary: str = "gh"


@dataclass(slots=True, frozen=True)
class GhProofResult:
    success: bool
    error: str | None = None
    outcome_uncertain: bool = False


@dataclass(slots=True, frozen=True)
class _PagesSite:
    html_url: str
    build_type: str


@dataclass(slots=True, frozen=True)
class _GhApiResponse:
    status_code: int
    body: str
    poll_interval_seconds: int | None = None
    retry_after_seconds: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_epoch: int | None = None


def valid_github_repository(value: str) -> bool:
    """Accept one canonical github.com ``owner/repository`` identity."""

    return valid_github_pages_repository(value)


def approved_gh_api_argv(
    argv: Sequence[str],
    *,
    repository: str,
    binary: str = "gh",
) -> bool:
    """Authorize only the verifier's two read-only GitHub API command shapes."""

    if not valid_github_repository(repository):
        return False
    site_endpoint = f"repos/{repository}/pages"
    endpoint: str | None = None
    jq: str | None = None
    if len(argv) == 14:
        endpoint = argv[2]
        jq = argv[13]
    if endpoint == site_endpoint:
        expected_jq = _PAGES_SITE_JQ
    elif endpoint is not None and re.fullmatch(
        re.escape(site_endpoint) + r"/deployments/[0-9a-fA-F]{40,64}",
        endpoint,
    ):
        expected_jq = _DEPLOYMENT_JQ
    else:
        return False
    return (
        list(argv)
        == [
            binary,
            "api",
            endpoint,
            "--hostname",
            _GITHUB_HOST,
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
            "--include",
            "--jq",
            expected_jq,
        ]
        and jq == expected_jq
    )


def github_repository_from_push_url(value: str) -> str | None:
    """Parse a single ordinary github.com HTTPS/SSH push URL.

    Local paths, URL rewrites such as ``ext::``, embedded HTTPS credentials,
    query/fragment components, and non-GitHub hosts are rejected.
    """

    raw = value.strip()
    if not raw or "\n" in raw or "\r" in raw:
        return None

    scp = re.fullmatch(r"git@github\.com:(?P<path>[^:?#]+)", raw, re.IGNORECASE)
    if scp is not None:
        path = scp.group("path")
    else:
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in {"https", "ssh"}:
            return None
        if (parsed.hostname or "").lower() != _GITHUB_HOST:
            return None
        if parsed.password is not None or parsed.query or parsed.fragment:
            return None
        if parsed.scheme == "https" and parsed.username is not None:
            return None
        if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            return None
        if (parsed.scheme == "https" and port not in {None, 443}) or (
            parsed.scheme == "ssh" and port not in {None, 22}
        ):
            return None
        path = parsed.path.removeprefix("/")

    if path.endswith(".git"):
        path = path[:-4]
    if not valid_github_repository(path):
        return None
    return path


def _canonical_pages_base(value: str, *, repository: str) -> tuple[str, str] | None:
    """Return the approved github.io host/path boundary for initial canaries."""

    return canonical_github_pages_base(value, repository=repository)


def _strict_json_object(value: str) -> dict[str, Any] | None:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    try:
        if not value or len(value.encode("utf-8")) > _MAX_GH_JSON_BYTES:
            return None
        parsed = json.loads(value, object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _included_api_response(value: str) -> _GhApiResponse | None:
    """Parse the response envelope and a finite set of numeric polling headers."""

    try:
        normalized = value.replace("\r\n", "\n")
        headers, body = normalized.split("\n\n", 1)
        if len(headers.encode("utf-8")) > _MAX_GH_HEADER_BYTES:
            return None
    except (UnicodeError, ValueError):
        return None
    lines = headers.splitlines()
    if not lines:
        return None
    status = re.fullmatch(r"HTTP/[^\s]+ (?P<code>[1-5][0-9]{2})(?: .*)?", lines[0])
    if status is None:
        return None
    numeric_headers: dict[str, int] = {}
    for line in lines[1:]:
        if ":" not in line:
            return None
        raw_name, raw_value = line.split(":", 1)
        name = raw_name.strip().lower()
        if not name:
            return None
        maximum = _GH_NUMERIC_HEADER_LIMITS.get(name)
        if maximum is None:
            continue
        value_text = raw_value.strip()
        if name in numeric_headers or re.fullmatch(r"[0-9]{1,10}", value_text) is None:
            return None
        parsed_value = int(value_text)
        if parsed_value > maximum:
            return None
        numeric_headers[name] = parsed_value
    return _GhApiResponse(
        status_code=int(status.group("code")),
        body=body,
        poll_interval_seconds=numeric_headers.get("x-poll-interval"),
        retry_after_seconds=numeric_headers.get("retry-after"),
        rate_limit_remaining=numeric_headers.get("x-ratelimit-remaining"),
        rate_limit_reset_epoch=numeric_headers.get("x-ratelimit-reset"),
    )


def _rate_limit_delay_seconds(response: _GhApiResponse) -> float | None:
    """Return a server-authorized rate-limit delay, if the headers prove one."""

    if response.retry_after_seconds is not None:
        return float(response.retry_after_seconds)
    if response.rate_limit_remaining == 0 and response.rate_limit_reset_epoch is not None:
        return max(0.0, float(response.rate_limit_reset_epoch) - time.time())
    return None


class GitHubPagesGhVerifier:
    """Prove Pages deployment/readback using fixed, read-only contracts."""

    def __init__(self, config: GhPagesConfig, *, cwd: Path, runner: CommandRunner) -> None:
        self.config = config
        self.cwd = cwd
        self._runner = runner

    @property
    def repository(self) -> str:
        return self.config.repository

    def _api_argv(self, endpoint: str, jq: str) -> list[str]:
        return [
            self.config.binary,
            "api",
            endpoint,
            "--hostname",
            _GITHUB_HOST,
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
            "--include",
            "--jq",
            jq,
        ]

    async def preflight(self, *, remote_url: str) -> GhProofResult:
        """Check identity, binary contract, auth, and Pages configuration."""

        if Path(self.config.binary).name != "gh":
            return GhProofResult(False, "GitHub CLI executable does not match the audited contract")
        if not valid_github_repository(self.config.repository):
            return GhProofResult(False, "GitHub Pages repository identity is invalid")
        remote_repository = github_repository_from_push_url(remote_url)
        if remote_repository is None:
            return GhProofResult(
                False, "GitHub Pages push remote URL is not an approved github.com URL"
            )
        if remote_repository.lower() != self.config.repository.lower():
            return GhProofResult(
                False, "GitHub Pages push remote does not match the verified repository"
            )
        if (
            _canonical_pages_base(
                self.config.base_url,
                repository=self.config.repository,
            )
            is None
        ):
            return GhProofResult(
                False,
                "GitHub Pages verified readback currently requires the repository owner github.io HTTPS origin",
            )
        if not self.config.token_configured:
            return GhProofResult(False, "GitHub Pages gh verification token is not configured")
        version = await self._runner(
            [self.config.binary, "--version"],
            cwd=self.cwd,
            timeout_seconds=self.config.command_timeout_seconds,
        )
        if not version.ok:
            return GhProofResult(False, "GitHub CLI is unavailable")
        first_line = version.stdout.splitlines()[0].strip() if version.stdout.splitlines() else ""
        match = _GH_VERSION_RE.fullmatch(first_line)
        if match is None or match.group("version") != self.config.expected_version:
            return GhProofResult(False, "GitHub CLI version does not match the audited contract")
        site, error = await self._pages_site()
        if site is None:
            return GhProofResult(False, error or "GitHub Pages site preflight failed")
        return GhProofResult(True)

    async def confirm_site(self) -> GhProofResult:
        """Re-read Pages metadata after deployment without exposing its payload."""

        site, error = await self._pages_site()
        return GhProofResult(site is not None, error, outcome_uncertain=site is None)

    async def _pages_site(self) -> tuple[_PagesSite | None, str | None]:
        endpoint = f"repos/{self.config.repository}/pages"
        result = await self._runner(
            self._api_argv(endpoint, _PAGES_SITE_JQ),
            cwd=self.cwd,
            timeout_seconds=self.config.command_timeout_seconds,
        )
        response = _included_api_response(result.stdout)
        if not result.ok or response is None or response.status_code != 200:
            return None, "GitHub Pages site metadata is unavailable"
        payload = _strict_json_object(response.body)
        if payload is None or set(payload) != {
            "status",
            "html_url",
            "build_type",
            "source",
            "https_enforced",
            "public",
        }:
            return None, "GitHub Pages site metadata is malformed"
        if payload["status"] is not None and not isinstance(payload["status"], str):
            return None, "GitHub Pages site status is malformed"
        html_url = payload["html_url"]
        build_type = payload["build_type"]
        if not isinstance(html_url, str) or not 1 <= len(html_url) <= 512:
            return None, "GitHub Pages site URL is malformed"
        if build_type != "workflow":
            return None, "GitHub Pages verified mode requires a workflow publishing source"
        if payload["https_enforced"] is not True:
            return None, "GitHub Pages verified mode requires enforced HTTPS"
        if payload["public"] is not True:
            return None, "GitHub Pages verified mode requires a public site"
        configured = _canonical_pages_base(
            self.config.base_url,
            repository=self.config.repository,
        )
        advertised = _canonical_pages_base(html_url, repository=self.config.repository)
        if configured is None or advertised != configured:
            return None, "GitHub Pages site URL does not match the approved base URL"
        source = payload["source"]
        if source is not None:
            if not isinstance(source, dict) or set(source).difference({"branch", "path"}):
                return None, "GitHub Pages publishing source is malformed"
            source_branch = source.get("branch")
            source_path = source.get("path")
            if not isinstance(source_branch, str) or not source_branch:
                return None, "GitHub Pages publishing source branch is malformed"
            if source_path not in {"/", "/docs"}:
                return None, "GitHub Pages publishing source path is malformed"
            if source_branch != self.config.branch:
                return None, "GitHub Pages publishing source branch does not match the push target"
        return _PagesSite(html_url=html_url, build_type=build_type), None

    async def wait_for_deployment(self, commit_sha: str) -> GhProofResult:
        if _SHA_RE.fullmatch(commit_sha) is None:
            return GhProofResult(False, "GitHub Pages deployment commit is invalid")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.deploy_timeout_seconds
        endpoint = f"repos/{self.config.repository}/pages/deployments/{commit_sha}"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return GhProofResult(
                    False,
                    "GitHub Pages deployment verification timed out",
                    outcome_uncertain=True,
                )
            attempt_timed_out = False
            try:
                async with asyncio.timeout(
                    min(remaining, float(self.config.command_timeout_seconds))
                ):
                    result = await self._runner(
                        self._api_argv(endpoint, _DEPLOYMENT_JQ),
                        cwd=self.cwd,
                        timeout_seconds=self.config.command_timeout_seconds,
                    )
            except TimeoutError:
                attempt_timed_out = True

            response = None if attempt_timed_out else _included_api_response(result.stdout)
            poll_delay = float(self.config.poll_seconds)
            if response is not None:
                if response.poll_interval_seconds is not None:
                    poll_delay = max(poll_delay, float(response.poll_interval_seconds))
                if response.retry_after_seconds is not None:
                    poll_delay = max(poll_delay, float(response.retry_after_seconds))
            if (
                not attempt_timed_out
                and result.ok
                and response is not None
                and response.status_code == 200
            ):
                payload = _strict_json_object(response.body)
                if (
                    payload is None
                    or set(payload) != {"status"}
                    or not isinstance(payload["status"], str)
                ):
                    return GhProofResult(
                        False,
                        "GitHub Pages deployment status is malformed",
                        outcome_uncertain=True,
                    )
                status = payload["status"]
                if status == "succeed":
                    return GhProofResult(True)
                if status in _DEPLOYMENT_FAILED:
                    return GhProofResult(
                        False, "GitHub Pages deployment reached a terminal failure"
                    )
                if status not in _DEPLOYMENT_PENDING:
                    return GhProofResult(
                        False,
                        "GitHub Pages deployment returned an unknown status",
                        outcome_uncertain=True,
                    )
            elif not attempt_timed_out and result.ok:
                return GhProofResult(
                    False,
                    "GitHub Pages deployment response envelope is malformed",
                    outcome_uncertain=True,
                )
            elif not attempt_timed_out and not result.started:
                return GhProofResult(
                    False,
                    "GitHub CLI became unavailable after source acceptance",
                    outcome_uncertain=True,
                )
            elif not attempt_timed_out and (
                result.returncode in {2, 4}
                or (response is not None and response.status_code == 401)
            ):
                return GhProofResult(
                    False,
                    "GitHub Pages API access stopped after source acceptance",
                    outcome_uncertain=True,
                )
            elif (
                not attempt_timed_out
                and response is not None
                and response.status_code in {403, 429}
            ):
                rate_limit_delay = _rate_limit_delay_seconds(response)
                if rate_limit_delay is None:
                    return GhProofResult(
                        False,
                        "GitHub Pages API access stopped after source acceptance",
                        outcome_uncertain=True,
                    )
                poll_delay = max(poll_delay, rate_limit_delay)
            elif (
                not attempt_timed_out
                and response is not None
                and response.status_code not in {404, 500, 502, 503, 504}
            ):
                return GhProofResult(
                    False,
                    "GitHub Pages deployment lookup returned an unexpected HTTP status",
                    outcome_uncertain=True,
                )

            remaining = deadline - loop.time()
            if remaining <= 0:
                return GhProofResult(
                    False,
                    "GitHub Pages deployment verification timed out",
                    outcome_uncertain=True,
                )
            if poll_delay >= remaining:
                return GhProofResult(
                    False,
                    "GitHub Pages deployment verification timed out",
                    outcome_uncertain=True,
                )
            await asyncio.sleep(poll_delay)

    async def wait_for_readback(self, *, article_url: str, marker: str) -> GhProofResult:
        if not re.fullmatch(r"[0-9a-f]{64}", marker):
            return GhProofResult(False, "GitHub Pages readback marker is invalid")
        if not self._approved_article_url(article_url):
            return GhProofResult(
                False,
                "GitHub Pages article URL is outside the approved readback boundary",
                outcome_uncertain=True,
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.readback_timeout_seconds
        needle = f'data-ai-ops-publication="{marker}"'.encode()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return GhProofResult(
                    False,
                    "GitHub Pages public marker readback timed out",
                    outcome_uncertain=True,
                )
            attempt_timeout = min(
                remaining,
                float(self.config.readback_request_timeout_seconds),
            )
            try:
                async with asyncio.timeout(attempt_timeout):
                    status = await self._readback_once(article_url, needle)
            except TimeoutError:
                status = "retry"
            if status == "verified":
                return GhProofResult(True)
            if status == "rejected":
                return GhProofResult(
                    False,
                    "GitHub Pages public readback violated the approved response contract",
                    outcome_uncertain=True,
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                return GhProofResult(
                    False,
                    "GitHub Pages public marker readback timed out",
                    outcome_uncertain=True,
                )
            await asyncio.sleep(min(float(self.config.poll_seconds), remaining))

    def _approved_article_url(self, article_url: str) -> bool:
        base = _canonical_pages_base(self.config.base_url, repository=self.config.repository)
        if base is None:
            return False
        try:
            parsed = urlsplit(article_url)
            port = parsed.port
        except ValueError:
            return False
        base_host, base_path = base
        if not (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == base_host
            and port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/")
            and "\\" not in parsed.path
        ):
            return False
        normalized_path = parsed.path[1:-1] if parsed.path.endswith("/") else parsed.path[1:]
        raw_parts = normalized_path.split("/")
        if not raw_parts or any(not part for part in raw_parts):
            return False
        decoded_parts: list[bytes] = []
        for part in raw_parts:
            if re.search(r"%(?![0-9A-Fa-f]{2})", part):
                return False
            decoded = unquote_to_bytes(part)
            if (
                decoded in {b".", b".."}
                or b"/" in decoded
                or b"\\" in decoded
                or any(value < 0x20 or value == 0x7F for value in decoded)
            ):
                return False
            decoded_parts.append(decoded)
        base_parts = [part.encode("ascii") for part in base_path.strip("/").split("/") if part]
        return (
            len(decoded_parts) > len(base_parts) and decoded_parts[: len(base_parts)] == base_parts
        )

    async def _readback_once(self, article_url: str, needle: bytes) -> str:
        timeout = httpx.Timeout(float(self.config.readback_request_timeout_seconds))
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,
                headers={
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ai-ops-auto/pages-verifier",
                },
            ) as client:
                async with client.stream("GET", article_url) as response:
                    if 300 <= response.status_code < 400:
                        return "rejected"
                    if response.status_code != 200:
                        return "retry"
                    content_type = response.headers.get("content-type", "")
                    if content_type.split(";", 1)[0].strip().lower() != "text/html":
                        return "rejected"
                    content_encoding = response.headers.get("content-encoding")
                    if (
                        content_encoding is not None
                        and content_encoding.strip().lower() != "identity"
                    ):
                        return "rejected"
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            parsed_length = int(content_length)
                            if parsed_length < 0 or parsed_length > self.config.readback_max_bytes:
                                return "rejected"
                        except ValueError:
                            return "rejected"
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > self.config.readback_max_bytes:
                            return "rejected"
                        body.extend(chunk)
                    return "verified" if needle in body else "retry"
        except (httpx.TimeoutException, httpx.TransportError):
            return "retry"
        except Exception:
            return "rejected"
