"""Synchronous HTTP client for the versioned Agent control-plane API.

The client deliberately returns contract DTOs instead of untyped dictionaries.
Authentication is sent only in the ``Authorization`` header, redirects are not
followed, and transport/server failures are reduced to the stable, redacted
``ClientError`` surface.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schemas import (
    MAX_CONTRACT_RESPONSE_BODY_BYTES,
    ApprovalAssetDownloadResponse,
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
    ScheduleRequest,
    ScheduleResponse,
    StageContentRequest,
    StageContentResponse,
)


DEFAULT_AGENT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_APPROVAL_ASSET_BYTES = 512 * 1024 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024
_MAX_JSON_RESPONSE_BODY_BYTES = MAX_CONTRACT_RESPONSE_BODY_BYTES

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

RequestT = TypeVar("RequestT", bound=BaseModel)
ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ClientError(RuntimeError):
    """Stable, credential-free failure raised by :class:`AgentContractClient`."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, object]:
        """Return the public v1 error envelope used by HTTP and the CLI."""

        return {
            "schema_version": 1,
            "error": {"code": self.code, "message": self.message},
        }


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=1000)


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    error: _ErrorDetail


def _safe_base_url(value: str) -> str:
    """Validate an origin without ever reflecting it in an exception."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ClientError(
            "invalid_base_url",
            "Agent API base URL must be an absolute HTTP(S) origin",
        )
    try:
        parsed = httpx.URL(value)
    except Exception:
        raise ClientError(
            "invalid_base_url",
            "Agent API base URL must be an absolute HTTP(S) origin",
        ) from None

    # Disallow user info, query strings and fragments so a credential cannot be
    # smuggled into a URL or copied into an intermediary's access log.  Bearer
    # tokens may cross cleartext HTTP only on a literal loopback destination;
    # resolving arbitrary hostnames here would introduce DNS-rebinding risk.
    is_loopback = False
    if parsed.host == "localhost":
        is_loopback = True
    elif parsed.host is not None and "%" not in parsed.host:
        try:
            is_loopback = ipaddress.ip_address(parsed.host).is_loopback
        except ValueError:
            pass
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or (parsed.scheme == "http" and not is_loopback)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {b"", b"/", "", "/"}
    ):
        raise ClientError(
            "invalid_base_url",
            "Agent API base URL must use HTTPS, except for loopback HTTP",
        )
    return value.rstrip("/")


def _safe_token(value: str) -> str:
    """Reject header-unsafe tokens without including their value in errors."""

    if (
        not isinstance(value, str)
        or len(value) < 32
        or len(value) > 4096
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ClientError(
            "invalid_token",
            "AI Ops bearer token is missing or invalid",
        )
    return value


def _request_model(
    value: RequestT | Mapping[str, Any],
    expected_type: type[RequestT],
) -> RequestT:
    if isinstance(value, expected_type):
        return value
    try:
        return expected_type.model_validate(value)
    except (TypeError, ValidationError):
        raise ClientError(
            "invalid_request",
            f"Request does not match {expected_type.__name__}",
        ) from None


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ClientError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 8-128 URL-safe characters",
        )
    return value


def _path_identifier(value: str | int, *, name: str) -> str:
    raw = str(value)
    if not raw or len(raw) > 128 or raw != raw.strip():
        raise ClientError("invalid_identifier", f"{name} must be a non-empty identifier")
    return quote(raw, safe="")


def _iter_raw_response(response: httpx.Response, *, chunk_size: int):
    """Yield wire bytes for real streams and already-buffered test transports."""

    if response.is_stream_consumed:
        yield response.content
        return
    yield from response.iter_raw(chunk_size=chunk_size)


def _inode_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _unlink_if_inode_matches(path: Path, expected: tuple[int, int]) -> bool:
    """Best-effort cleanup without deleting an unknown/replaced output path."""

    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode) or _inode_identity(current) != expected:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


class AgentContractClient:
    """Small synchronous client for the stable v1 Agent operations."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_AGENT_API_URL,
        token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        origin = _safe_base_url(base_url)
        origin_scheme = httpx.URL(origin).scheme
        bearer_token = _safe_token(token)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ClientError("invalid_timeout", "Agent API timeout must be positive")

        self._client = httpx.Client(
            base_url=origin,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {bearer_token}",
            },
            timeout=float(timeout),
            transport=transport,
            follow_redirects=False,
            # A loopback HTTP token must never be forwarded to an environment
            # proxy. HTTPS origins may retain standard proxy configuration.
            trust_env=origin_scheme != "http",
        )

    def __enter__(self) -> AgentContractClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        response_type: type[ResponseT],
        *,
        request: BaseModel | None = None,
        idempotency_key: str | None = None,
    ) -> ResponseT:
        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _idempotency_key(idempotency_key)
        request_options: dict[str, Any] = {"headers": headers}
        if request is not None:
            request_options["json"] = request.model_dump(mode="json")

        try:
            with self._client.stream(method, path, **request_options) as response:
                max_response_bytes = (
                    _MAX_JSON_RESPONSE_BODY_BYTES if response.is_success else _MAX_ERROR_BODY_BYTES
                )
                chunks: list[bytes] = []
                total = 0
                for chunk in _iter_raw_response(response, chunk_size=8192):
                    total += len(chunk)
                    if total > max_response_bytes:
                        if response.is_success:
                            raise ClientError(
                                "invalid_response",
                                f"Agent API returned an invalid {response_type.__name__}",
                                status_code=response.status_code,
                            )
                        raise ClientError(
                            "http_error",
                            "Agent API request failed",
                            status_code=response.status_code,
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
        except ClientError:
            raise
        except httpx.TimeoutException:
            raise ClientError("timeout", "Agent API request timed out") from None
        except httpx.RequestError:
            raise ClientError(
                "transport_error",
                "Agent API request failed before receiving a response",
            ) from None
        except Exception:
            # Header/transport implementations can raise exceptions outside the
            # normal httpx hierarchy.  Do not expose their credential-bearing
            # repr through the public client contract.
            raise ClientError(
                "transport_error",
                "Agent API request failed before receiving a response",
            ) from None

        if not response.is_success:
            raise self._http_error(response, body=body)

        try:
            return response_type.model_validate_json(body, strict=True)
        except (TypeError, ValueError, ValidationError):
            raise ClientError(
                "invalid_response",
                f"Agent API returned an invalid {response_type.__name__}",
                status_code=response.status_code,
            ) from None

    @staticmethod
    def _http_error(response: httpx.Response, *, body: bytes | None = None) -> ClientError:
        try:
            envelope = _ErrorEnvelope.model_validate_json(
                response.content if body is None else body,
                strict=True,
            )
        except (TypeError, ValueError, ValidationError):
            return ClientError(
                "http_error",
                "Agent API request failed",
                status_code=response.status_code,
            )

        # The DTO already validates this, but keeping this guard immediately
        # beside the exception boundary makes the stability invariant explicit.
        code = envelope.error.code
        if _ERROR_CODE.fullmatch(code) is None:
            code = "http_error"
        return ClientError(
            code,
            envelope.error.message,
            status_code=response.status_code,
        )

    def stage_content(
        self,
        request: StageContentRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> StageContentResponse:
        payload = _request_model(request, StageContentRequest)
        return self._request(
            "POST",
            "/v1/contents",
            StageContentResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def plan_publication(
        self,
        request: PlanPublicationRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> PlanPublicationResponse:
        payload = _request_model(request, PlanPublicationRequest)
        return self._request(
            "POST",
            "/v1/publication-plans",
            PlanPublicationResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def request_approval(
        self,
        request: RequestApprovalRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> ApprovalResponse:
        payload = _request_model(request, RequestApprovalRequest)
        return self._request(
            "POST",
            "/v1/approval-requests",
            ApprovalResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def get_approval(self, approval_id: str) -> ApprovalReviewResponse:
        identifier = _path_identifier(approval_id, name="approval_id")
        return self._request(
            "GET",
            f"/v1/approval-requests/{identifier}",
            ApprovalReviewResponse,
        )

    def download_approval_asset(
        self,
        approval_id: str,
        asset_id: int,
        destination: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_APPROVAL_ASSET_BYTES,
    ) -> ApprovalAssetDownloadResponse:
        """Stream one reviewed asset to a new file and verify its SHA-256."""

        approval_identifier = _path_identifier(approval_id, name="approval_id")
        if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
            raise ClientError("invalid_identifier", "asset_id must be a positive integer")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ClientError("invalid_size_limit", "Asset download limit must be positive")
        try:
            requested_path = Path(destination)
            if not requested_path.name:
                raise ValueError
            parent = requested_path.parent.resolve(strict=True)
            parent_stat = os.stat(parent, follow_symlinks=False)
            if not stat.S_ISDIR(parent_stat.st_mode) or requested_path.is_symlink():
                raise ValueError
            if os.name == "posix" and (
                parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022
            ):
                raise ValueError
            output_path = parent / requested_path.name
        except (OSError, TypeError, ValueError):
            raise ClientError(
                "invalid_output_path",
                "Asset output must name a new file in an existing directory",
            ) from None
        if output_path.exists() or output_path.is_symlink():
            raise ClientError("output_exists", "Asset output file already exists")

        temp_path: Path | None = None
        temp_fd = -1
        output_linked = False
        output_committed = False
        expected_output_inode: tuple[int, int] | None = None
        try:
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=".ai-ops-approval-asset-",
                suffix=".tmp",
                dir=parent,
            )
            temp_path = Path(temp_name)
            with self._client.stream(
                "GET",
                f"/v1/approval-requests/{approval_identifier}/assets/{asset_id}",
                headers={
                    "Accept": "application/octet-stream",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if not response.is_success:
                    chunks: list[bytes] = []
                    error_bytes = 0
                    for chunk in _iter_raw_response(response, chunk_size=8192):
                        error_bytes += len(chunk)
                        if error_bytes > _MAX_ERROR_BODY_BYTES:
                            raise ClientError(
                                "http_error",
                                "Agent API request failed",
                                status_code=response.status_code,
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    buffered = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=body,
                        request=response.request,
                    )
                    raise self._http_error(buffered)

                expected_sha256 = response.headers.get("X-Content-SHA256", "")
                raw_length = response.headers.get("Content-Length", "")
                content_type = (
                    response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
                try:
                    expected_size = int(raw_length)
                except (TypeError, ValueError):
                    raise ClientError(
                        "invalid_asset_response",
                        "Approval asset response is missing verified metadata",
                    ) from None
                if (
                    re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
                    or content_type != "application/octet-stream"
                    or content_encoding not in {"", "identity"}
                    or expected_size < 0
                    or expected_size > max_bytes
                ):
                    raise ClientError(
                        "invalid_asset_response",
                        "Approval asset response is missing verified metadata",
                    )

                digest = hashlib.sha256()
                total = 0
                with os.fdopen(temp_fd, "wb", closefd=False) as output:
                    for chunk in _iter_raw_response(response, chunk_size=1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ClientError(
                                "asset_too_large",
                                "Approval asset exceeds the configured download limit",
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    try:
                        os.fchmod(output.fileno(), 0o600)
                    except OSError:
                        pass
                if total != expected_size or digest.hexdigest() != expected_sha256:
                    raise ClientError(
                        "asset_integrity_failed",
                        "Approval asset failed integrity verification",
                    )

            verified_stat = os.fstat(temp_fd)
            named_stat = os.stat(temp_path, follow_symlinks=False)
            verified_identity = (
                verified_stat.st_dev,
                verified_stat.st_ino,
                verified_stat.st_size,
            )
            expected_output_inode = _inode_identity(verified_stat)
            if not stat.S_ISREG(verified_stat.st_mode) or verified_identity != (
                named_stat.st_dev,
                named_stat.st_ino,
                named_stat.st_size,
            ):
                raise ClientError(
                    "output_commit_failed",
                    "Approval asset temporary file identity changed",
                )
            os.link(temp_path, output_path)
            output_linked = True
            committed_stat = os.stat(output_path, follow_symlinks=False)
            if not stat.S_ISREG(committed_stat.st_mode) or verified_identity != (
                committed_stat.st_dev,
                committed_stat.st_ino,
                committed_stat.st_size,
            ):
                raise ClientError(
                    "output_commit_failed",
                    "Approval asset temporary file identity changed",
                )
            directory_fd = -1
            try:
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(parent, directory_flags)
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                if directory_fd >= 0:
                    try:
                        os.close(directory_fd)
                    except OSError:
                        pass
            result = ApprovalAssetDownloadResponse(
                approval_id=str(approval_id),
                asset_id=asset_id,
                sha256=expected_sha256,
                size_bytes=total,
            )
            output_committed = True
            return result
        except ClientError:
            raise
        except FileExistsError:
            raise ClientError("output_exists", "Asset output file already exists") from None
        except httpx.TimeoutException:
            raise ClientError("timeout", "Agent API request timed out") from None
        except httpx.RequestError:
            raise ClientError(
                "transport_error",
                "Agent API request failed before receiving a response",
            ) from None
        except OSError:
            raise ClientError(
                "output_write_error",
                "Approval asset could not be saved",
            ) from None
        finally:
            if output_linked and not output_committed and expected_output_inode is not None:
                _unlink_if_inode_matches(output_path, expected_output_inode)
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def decide_approval(
        self,
        approval_id: str,
        request: ApprovalDecisionRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> ApprovalDecisionResponse:
        identifier = _path_identifier(approval_id, name="approval_id")
        payload = _request_model(request, ApprovalDecisionRequest)
        return self._request(
            "POST",
            f"/v1/approvals/{identifier}/decision",
            ApprovalDecisionResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def schedule(
        self,
        request: ScheduleRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> ScheduleResponse:
        payload = _request_model(request, ScheduleRequest)
        plan_id = _path_identifier(payload.plan_id, name="plan_id")
        return self._request(
            "POST",
            f"/v1/publication-plans/{plan_id}/schedule",
            ScheduleResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def get_job_status(self, job_id: int) -> JobStatusResponse:
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            raise ClientError("invalid_identifier", "job_id must be a positive integer")
        return self._request("GET", f"/v1/jobs/{job_id}", JobStatusResponse)

    def collect_metrics(
        self,
        request: CollectMetricsRequest | Mapping[str, Any],
        idempotency_key: str,
    ) -> CollectMetricsResponse:
        payload = _request_model(request, CollectMetricsRequest)
        return self._request(
            "POST",
            f"/v1/jobs/{payload.job_id}/metrics-collections",
            CollectMetricsResponse,
            request=payload,
            idempotency_key=idempotency_key,
        )

    def review_performance(
        self,
        request: PerformanceReviewRequest | Mapping[str, Any],
    ) -> PerformanceReviewResponse:
        payload = _request_model(request, PerformanceReviewRequest)
        return self._request(
            "POST",
            "/v1/performance-reviews",
            PerformanceReviewResponse,
            request=payload,
        )


# Short alias for interactive callers while retaining the descriptive public
# name used throughout the project documentation.
AgentClient = AgentContractClient


__all__ = [
    "DEFAULT_AGENT_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "AgentClient",
    "AgentContractClient",
    "ClientError",
]
