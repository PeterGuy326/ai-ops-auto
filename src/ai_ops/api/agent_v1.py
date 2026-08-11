"""Versioned HTTP transport for the Agent-native control plane.

The router deliberately contains no creator-ops business logic.  It binds the
stable v1 DTOs to authentication, authorization, idempotency, and the shared
``AgentControlPlane`` used by the CLI and future transports.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.background import BackgroundTask

from ..agent_contract.schemas import (
    MAX_CONTRACT_REQUEST_BODY_BYTES,
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
from ..agent_contract.service import AgentContractError, AgentControlPlane
from ..config import (
    SCOPE_APPROVAL_DECIDE,
    SCOPE_APPROVAL_READ,
    SCOPE_APPROVAL_REQUEST,
    SCOPE_CONTENT_STAGE,
    SCOPE_JOB_READ,
    SCOPE_METRICS_COLLECT,
    SCOPE_PERFORMANCE_READ,
    SCOPE_PLAN_CREATE,
    SCOPE_SCHEDULE_CREATE,
)
from .auth import Principal, require_scopes

logger = logging.getLogger(__name__)


class _RequestBodyTooLarge(Exception):
    """Internal sentinel raised before FastAPI buffers an oversized body."""


class _InvalidContentLength(Exception):
    """Internal sentinel for an ambiguous or malformed declared body size."""


def _install_bounded_request_receive(request: Request) -> None:
    """Bound raw ASGI bytes before authentication dependencies or JSON parsing."""

    declared_values = [
        value.strip()
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if len(declared_values) > 1:
        raise _InvalidContentLength
    if declared_values:
        declared = declared_values[0]
        if not declared or not declared.isdigit():
            raise _InvalidContentLength
        try:
            declared_size = int(declared)
        except (ValueError, OverflowError):
            raise _InvalidContentLength from None
        if declared_size > MAX_CONTRACT_REQUEST_BODY_BYTES:
            raise _RequestBodyTooLarge

    cached_body = getattr(request, "_body", None)
    if isinstance(cached_body, bytes) and len(cached_body) > MAX_CONTRACT_REQUEST_BODY_BYTES:
        raise _RequestBodyTooLarge

    original_receive = request._receive
    received = 0

    async def bounded_receive():
        nonlocal received
        message = await original_receive()
        if message.get("type") == "http.request":
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise _InvalidContentLength
            received += len(body)
            if received > MAX_CONTRACT_REQUEST_BODY_BYTES:
                raise _RequestBodyTooLarge
        return message

    request._receive = bounded_receive


class ErrorDetail(BaseModel):
    """Stable, intentionally small error payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=1000)


class ErrorEnvelope(BaseModel):
    """Every handled v1 failure uses this versioned envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    error: ErrorDetail


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    try:
        envelope = ErrorEnvelope(error=ErrorDetail(code=code, message=message))
    except ValidationError:
        # Domain/adaptor exceptions are not trusted wire payloads.  If a future
        # code path supplies an invalid or oversized detail, collapse it to the
        # same small redacted internal error instead of raising while already
        # handling the original failure.
        status_code = 500
        headers = None
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code="internal_error",
                message="The request could not be completed",
            )
        )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


_HTTP_ERROR_DETAILS = {
    400: ("invalid_request", "The request could not be processed"),
    401: ("authentication_required", "A valid Agent bearer token is required"),
    403: ("insufficient_scope", "The principal lacks the required scope"),
    404: ("not_found", "The requested resource was not found"),
    405: ("method_not_allowed", "The HTTP method is not allowed"),
}


class AgentV1Route(APIRoute):
    """Keep validation, dependency, domain, and unexpected errors on one wire format."""

    def get_route_handler(self):
        route_handler = super().get_route_handler()
        expects_request_body = self.body_field is not None

        async def stable_route_handler(request: Request):
            try:
                _install_bounded_request_receive(request)
                # Consume through the bounded receive wrapper before FastAPI's
                # body parser or bearer-token dependencies run.  FastAPI wraps
                # arbitrary receive exceptions as a generic HTTPException, so
                # doing this at the route boundary also preserves our stable
                # 413 envelope for chunked and under-reported requests.
                if expects_request_body:
                    await request.body()
                return await route_handler(request)
            except _RequestBodyTooLarge:
                return _error_response(
                    413,
                    "request_too_large",
                    "Request body exceeds the Agent v1 transport limit",
                )
            except _InvalidContentLength:
                return _error_response(
                    400,
                    "invalid_request",
                    "The request could not be processed",
                )
            except RequestValidationError:
                return _error_response(
                    422,
                    "invalid_request",
                    "Request validation failed",
                )
            except AgentContractError as exc:
                if not 400 <= exc.status_code < 600:
                    logger.error(
                        "Agent control plane returned an invalid HTTP status; error_type=%s",
                        type(exc).__name__,
                    )
                    return _error_response(
                        500,
                        "internal_error",
                        "The request could not be completed",
                    )
                if exc.status_code >= 500:
                    return _error_response(
                        exc.status_code,
                        "internal_error",
                        "The request could not be completed",
                    )
                return _error_response(exc.status_code, exc.code, exc.message)
            except HTTPException as exc:
                code, message = _HTTP_ERROR_DETAILS.get(
                    exc.status_code,
                    ("request_error", "The request could not be completed"),
                )
                return _error_response(
                    exc.status_code,
                    code,
                    message,
                    headers=exc.headers,
                )
            except Exception as exc:
                # Do not interpolate the exception: adapter and database errors
                # can contain bearer tokens, credentials, or raw platform data.
                logger.error(
                    "Unhandled Agent v1 request failure; error_type=%s",
                    type(exc).__name__,
                )
                return _error_response(
                    500,
                    "internal_error",
                    "The request could not be completed",
                )

        return stable_route_handler


_ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope},
    401: {"model": ErrorEnvelope},
    403: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    413: {"model": ErrorEnvelope},
    416: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
    500: {"model": ErrorEnvelope},
}

router = APIRouter(
    prefix="/v1",
    tags=["agent-control-plane-v1"],
    route_class=AgentV1Route,
)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    ),
]
StablePathIdentifier = Annotated[str, Path(min_length=1, max_length=128)]
PositivePathIdentifier = Annotated[int, Path(gt=0)]


def get_agent_control_plane() -> AgentControlPlane:
    """Construct the default service; callers may override this FastAPI dependency."""

    return AgentControlPlane()


# Short alias for embeddings that standardize on ``get_control_plane``.  Both
# names reference the same dependency key, so FastAPI overrides work with
# either import without creating a second service factory.
get_control_plane = get_agent_control_plane


@router.post(
    "/contents",
    response_model=StageContentResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
def stage_content(
    data: StageContentRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_CONTENT_STAGE))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> StageContentResponse:
    return control_plane.stage_content(
        principal=principal,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/publication-plans",
    response_model=PlanPublicationResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
def create_publication_plan(
    data: PlanPublicationRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_PLAN_CREATE))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> PlanPublicationResponse:
    return control_plane.plan_publication(
        principal=principal,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/approval-requests",
    response_model=ApprovalResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
def request_approval(
    data: RequestApprovalRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_APPROVAL_REQUEST))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> ApprovalResponse:
    return control_plane.request_approval(
        principal=principal,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/approval-requests/{approval_id}",
    response_model=ApprovalReviewResponse,
    responses=_ERROR_RESPONSES,
)
def get_approval(
    approval_id: StablePathIdentifier,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_APPROVAL_READ))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> ApprovalReviewResponse:
    return control_plane.get_approval(
        principal=principal,
        approval_id=approval_id,
    )


@router.get(
    "/approval-requests/{approval_id}/assets/{asset_id}",
    response_class=StreamingResponse,
    responses={
        **_ERROR_RESPONSES,
        200: {"content": {"application/octet-stream": {}}},
    },
)
def get_approval_asset(
    approval_id: StablePathIdentifier,
    asset_id: PositivePathIdentifier,
    request: Request,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_APPROVAL_READ))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> StreamingResponse:
    if "range" in request.headers:
        raise AgentContractError(
            "range_not_supported",
            "Approval asset downloads do not support byte ranges",
            status_code=416,
        )
    asset = control_plane.get_approval_asset(
        principal=principal,
        approval_id=approval_id,
        asset_id=asset_id,
    )

    def verified_chunks():
        remaining = asset.size_bytes
        try:
            while remaining:
                chunk = asset.handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("verified asset ended before its declared size")
                remaining -= len(chunk)
                yield chunk
            if asset.handle.read(1):
                raise RuntimeError("verified asset exceeded its declared size")
        finally:
            asset.close()

    try:
        return StreamingResponse(
            verified_chunks(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{asset.filename}"',
                "Content-Length": str(asset.size_bytes),
                "X-Content-Type-Options": "nosniff",
                "X-Content-SHA256": asset.sha256,
            },
            background=BackgroundTask(asset.close),
        )
    except Exception:
        asset.close()
        raise


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalDecisionResponse,
    responses=_ERROR_RESPONSES,
)
def decide_approval(
    approval_id: StablePathIdentifier,
    data: ApprovalDecisionRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_APPROVAL_DECIDE))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> ApprovalDecisionResponse:
    return control_plane.decide_approval(
        principal=principal,
        approval_id=approval_id,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/publication-plans/{plan_id}/schedule",
    response_model=ScheduleResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
def schedule_publication_plan(
    plan_id: StablePathIdentifier,
    data: ScheduleRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_SCHEDULE_CREATE))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> ScheduleResponse:
    if data.plan_id != plan_id:
        raise AgentContractError(
            "plan_id_mismatch",
            "The plan_id in the request body must match the path",
            status_code=409,
        )
    return control_plane.schedule(
        principal=principal,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    responses=_ERROR_RESPONSES,
)
def get_job_status(
    job_id: PositivePathIdentifier,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_JOB_READ))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> JobStatusResponse:
    return control_plane.get_job_status(principal=principal, job_id=job_id)


@router.post(
    "/jobs/{job_id}/metrics-collections",
    response_model=CollectMetricsResponse,
    responses=_ERROR_RESPONSES,
)
async def collect_job_metrics(
    job_id: PositivePathIdentifier,
    data: CollectMetricsRequest,
    idempotency_key: IdempotencyKey,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_METRICS_COLLECT))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> CollectMetricsResponse:
    if data.job_id != job_id:
        raise AgentContractError(
            "job_id_mismatch",
            "The job_id in the request body must match the path",
            status_code=409,
        )
    return await control_plane.collect_metrics(
        principal=principal,
        request=data,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/performance-reviews",
    response_model=PerformanceReviewResponse,
    responses=_ERROR_RESPONSES,
)
def review_performance(
    data: PerformanceReviewRequest,
    principal: Annotated[Principal, Depends(require_scopes(SCOPE_PERFORMANCE_READ))],
    control_plane: Annotated[AgentControlPlane, Depends(get_agent_control_plane)],
) -> PerformanceReviewResponse:
    return control_plane.review_performance(principal=principal, request=data)


__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "get_agent_control_plane",
    "get_control_plane",
    "router",
]
