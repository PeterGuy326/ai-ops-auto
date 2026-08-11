"""Machine-oriented CLI commands for the Agent HTTP contract.

These commands are intentionally thin HTTP adapters.  They never import the
database or domain service, always emit exactly one JSON document to stdout,
and read the bearer token only from ``AI_OPS_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
from typing import TypeVar
import uuid

from pydantic import BaseModel, ValidationError
import typer

from .client import AgentContractClient, ClientError, DEFAULT_AGENT_API_URL
from .schemas import (
    MAX_CONTRACT_REQUEST_BODY_BYTES,
    ApprovalDecisionRequest,
    CollectMetricsRequest,
    PerformanceReviewRequest,
    PlanPublicationRequest,
    RequestApprovalRequest,
    ScheduleRequest,
    StageContentRequest,
)


agent_app = typer.Typer(
    help="通过稳定 HTTP 契约调用 Agent-native Creator Ops 控制面。",
    no_args_is_help=True,
)

_MAX_INPUT_BYTES = MAX_CONTRACT_REQUEST_BODY_BYTES
RequestT = TypeVar("RequestT", bound=BaseModel)
ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _echo_json(payload: object) -> None:
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _fail(code: str, message: str, *, exit_code: int = 1) -> None:
    _echo_json(
        {
            "schema_version": 1,
            "error": {"code": code, "message": message},
        }
    )
    raise typer.Exit(code=exit_code)


def _read_input(source: str, request_type: type[RequestT]) -> RequestT:
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        else:
            with Path(source).open("rb") as input_file:
                raw = input_file.read(_MAX_INPUT_BYTES + 1)
    except (OSError, ValueError):
        _fail("input_read_error", "Unable to read --input JSON")

    if len(raw) > _MAX_INPUT_BYTES:
        limit_mib = _MAX_INPUT_BYTES // (1024 * 1024)
        _fail(
            "input_too_large",
            f"--input JSON exceeds the {limit_mib} MiB transport limit",
        )

    try:
        # Parse once to distinguish malformed JSON from a valid document that
        # fails the versioned DTO.  Neither error path reflects user content.
        json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("invalid_json", "--input must contain one valid JSON document")

    try:
        return request_type.model_validate_json(raw, strict=True)
    except (TypeError, ValueError, ValidationError):
        _fail("invalid_input", f"--input does not match {request_type.__name__}")


def _resolve_base_url(option_value: str | None) -> str:
    if option_value is not None:
        return option_value
    return os.environ.get("AI_OPS_URL", DEFAULT_AGENT_API_URL)


def _new_idempotency_key(option_value: str | None) -> str:
    return option_value if option_value is not None else str(uuid.uuid4())


def _call_api(
    operation: Callable[[AgentContractClient], ResponseT],
    *,
    base_url: str | None,
) -> None:
    token = os.environ.get("AI_OPS_TOKEN")
    if not token:
        _fail(
            "missing_token",
            "AI_OPS_TOKEN environment variable is required",
        )

    client: AgentContractClient | None = None
    try:
        client = AgentContractClient(
            base_url=_resolve_base_url(base_url),
            token=token,
        )
        response = operation(client)
    except ClientError as exc:
        _echo_json(exc.to_dict())
        raise typer.Exit(code=1) from None
    except Exception:
        # The CLI is an Agent boundary: exception reprs can contain request
        # bodies, local paths, or headers.  Keep the failure deterministic and
        # redacted while preserving the one-document stdout contract.
        _fail("client_failure", "Agent API command failed")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    if not isinstance(response, BaseModel):
        _fail("invalid_response", "Agent API client returned an invalid DTO")
    _echo_json(response.model_dump(mode="json"))


@agent_app.command("stage-content")
def stage_content(
    input_source: str = typer.Option(
        ...,
        "--input",
        help="请求 JSON 文件；使用 '-' 从 stdin 读取。",
    ),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="8-128 字符幂等键；省略时生成 UUID。",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="控制面 origin；默认读取 AI_OPS_URL。",
    ),
) -> None:
    """暂存内容（Roadmap: stage_content）。"""

    request = _read_input(input_source, StageContentRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(lambda client: client.stage_content(request, key), base_url=base_url)


@agent_app.command("plan-publication")
def plan_publication(
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """创建发布计划（Roadmap: plan_publication）。"""

    request = _read_input(input_source, PlanPublicationRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(lambda client: client.plan_publication(request, key), base_url=base_url)


@agent_app.command("request-approval")
def request_approval(
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """请求人工审批（Roadmap: request_approval）。"""

    request = _read_input(input_source, RequestApprovalRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(lambda client: client.request_approval(request, key), base_url=base_url)


@agent_app.command("decide-approval")
def decide_approval(
    approval_id: str = typer.Argument(..., help="待决策 approval ID。"),
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """人工审批决策（安全补充操作: decide_approval）。"""

    request = _read_input(input_source, ApprovalDecisionRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(
        lambda client: client.decide_approval(approval_id, request, key),
        base_url=base_url,
    )


@agent_app.command("get-approval")
def get_approval(
    approval_id: str = typer.Argument(..., help="待审阅 approval ID。"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """读取不可变、脱敏的人工审批对象。"""

    _call_api(lambda client: client.get_approval(approval_id), base_url=base_url)


@agent_app.command("download-approval-asset")
def download_approval_asset(
    approval_id: str = typer.Argument(..., help="待审阅 approval ID。"),
    asset_id: int = typer.Argument(..., min=1, help="审阅包中的 asset ID。"),
    output: str = typer.Option(..., "--output", help="新建的本地输出文件；拒绝覆盖。"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """下载并校验人工审批素材。"""

    _call_api(
        lambda client: client.download_approval_asset(
            approval_id,
            asset_id,
            output,
        ),
        base_url=base_url,
    )


@agent_app.command("schedule")
def schedule(
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """调度已审批计划（Roadmap: schedule）。"""

    request = _read_input(input_source, ScheduleRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(lambda client: client.schedule(request, key), base_url=base_url)


@agent_app.command("get-job-status")
def get_job_status(
    job_id: int = typer.Argument(..., min=1, help="发布 job ID。"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """查询持久任务状态（Roadmap: get_job_status）。"""

    _call_api(lambda client: client.get_job_status(job_id), base_url=base_url)


@agent_app.command("collect-metrics")
def collect_metrics(
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """采集归一化指标（Roadmap: collect_metrics）。"""

    request = _read_input(input_source, CollectMetricsRequest)
    key = _new_idempotency_key(idempotency_key)
    _call_api(lambda client: client.collect_metrics(request, key), base_url=base_url)


@agent_app.command("review-performance")
def review_performance(
    input_source: str = typer.Option(..., "--input", help="请求 JSON 文件；'-' 表示 stdin。"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """读取表现复盘（Roadmap: review_performance）。"""

    request = _read_input(input_source, PerformanceReviewRequest)
    _call_api(lambda client: client.review_performance(request), base_url=base_url)


__all__ = ["agent_app"]
