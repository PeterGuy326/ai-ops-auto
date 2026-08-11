"""Versioned DTOs for the Agent-native Creator Ops contract.

These models are deliberately separate from ORM models and publisher payloads.
They expose stable control-plane facts only: no credentials and no adapter
``raw_response`` data cross this boundary.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from ..core.enums import (
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
    PublisherKind,
)
from .digest import canonical_json_bytes, canonical_sha256, normalize_utc_datetime


SCHEMA_VERSION = 1

# Keep v1 request and response sizes deterministic.  These limits are part of
# the public contract rather than transport-specific web-server settings so
# Python, CLI, HTTP, and future MCP callers all receive the same validation.
MAX_PLAN_TARGETS = 16
MAX_STAGE_ASSETS = 256
MAX_PERFORMANCE_REVIEW_JOBS = 256
MAX_SIGNED_64 = (2**63) - 1
MAX_STAGE_BODY_BYTES = 1024 * 1024
MAX_STAGE_EXTRA_BYTES = 64 * 1024
MAX_STAGE_EXTRA_DEPTH = 8
MAX_STAGE_EXTRA_ITEMS = 1024
MAX_ASSET_META_BYTES = 16 * 1024
MAX_ASSET_META_DEPTH = 8
MAX_ASSET_META_ITEMS = 256
MAX_RENDERER_PAYLOAD_BYTES = 256 * 1024
MAX_RENDERER_PAYLOAD_DEPTH = 16
MAX_RENDERER_PAYLOAD_ITEMS = 8192

# The HTTP and CLI limits bound encoded JSON, not only the decoded Pydantic
# values.  A one-byte control character can occupy six bytes in a JSON string,
# so the request envelope includes that worst-case expansion for body/path
# fields plus the independently bounded JSON objects and one MiB of structural
# slack.  The response envelope additionally covers the largest approval review
# (all asset metadata plus one renderer projection per target).  Rounding up to
# MiB keeps these public transport limits stable and easy to operate.
# They describe the supported compact UTF-8 JSON profile.  Semantically
# equivalent documents inflated with arbitrary whitespace or alternate escape
# spellings may be rejected at the transport boundary.
_MIB = 1024 * 1024
_JSON_STRING_MAX_EXPANSION = 6
_MAX_STAGE_PATH_CHARS = 1024
_MAX_TRANSPORT_FIELD_OVERHEAD_BYTES = _MIB


def _round_up_mib(value: int) -> int:
    return ((value + _MIB - 1) // _MIB) * _MIB


MAX_CONTRACT_REQUEST_BODY_BYTES = _round_up_mib(
    (_JSON_STRING_MAX_EXPANSION * MAX_STAGE_BODY_BYTES)
    + MAX_STAGE_EXTRA_BYTES
    + MAX_STAGE_ASSETS
    * (MAX_ASSET_META_BYTES + (_JSON_STRING_MAX_EXPANSION * _MAX_STAGE_PATH_CHARS) + 512)
    + _MAX_TRANSPORT_FIELD_OVERHEAD_BYTES
)
MAX_CONTRACT_RESPONSE_BODY_BYTES = _round_up_mib(
    (_JSON_STRING_MAX_EXPANSION * MAX_STAGE_BODY_BYTES)
    + MAX_STAGE_EXTRA_BYTES
    + MAX_STAGE_ASSETS * (MAX_ASSET_META_BYTES + 1024)
    + MAX_PLAN_TARGETS * (MAX_RENDERER_PAYLOAD_BYTES + 4096)
    + (2 * _MIB)
)

PositiveIdentifier = Annotated[int, Field(gt=0, le=MAX_SIGNED_64)]
NonNegativeCounter = Annotated[int, Field(ge=0, le=MAX_SIGNED_64)]
StableIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
DigestValue = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SafeMessage = Annotated[str, Field(max_length=1000)]


def _validate_json_limits(
    value: JsonValue,
    *,
    field_name: str,
    max_bytes: int,
    max_depth: int,
    max_items: int,
) -> None:
    """Reject JSON trees that can amplify storage, hashing, or projection work."""

    item_count = 0
    pending: list[tuple[JsonValue, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, dict):
            if depth > max_depth:
                raise ValueError(f"{field_name} exceeds the maximum JSON depth of {max_depth}")
            item_count += len(current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if depth > max_depth:
                raise ValueError(f"{field_name} exceeds the maximum JSON depth of {max_depth}")
            item_count += len(current)
            pending.extend((item, depth + 1) for item in current)
        if item_count > max_items:
            raise ValueError(f"{field_name} exceeds the maximum JSON item count of {max_items}")

    if len(canonical_json_bytes(value)) > max_bytes:
        raise ValueError(f"{field_name} exceeds the {max_bytes}-byte JSON limit")


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    @field_validator("*", mode="after")
    @classmethod
    def _normalize_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return normalize_utc_datetime(value)
        return value


class _VersionedContractModel(_ContractModel):
    schema_version: Literal[1] = SCHEMA_VERSION


class PlanState(str, Enum):
    PLANNED = "planned"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ScheduleState(str, Enum):
    SCHEDULED = "scheduled"


class MetricsCollectionState(str, Enum):
    COLLECTED = "collected"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


class MetricQuality(str, Enum):
    OBSERVED = "observed"
    PARTIAL = "partial"
    ESTIMATED = "estimated"
    SYNTHETIC = "synthetic"


class AssetInput(_ContractModel):
    """One ordered content asset; path authorization remains a service concern."""

    asset_type: AssetType
    source: AssetSource = AssetSource.AI_GENERATED
    local_path: str = Field(min_length=1, max_length=1024)
    meta: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("meta")
    @classmethod
    def _meta_is_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_limits(
            value,
            field_name="asset meta",
            max_bytes=MAX_ASSET_META_BYTES,
            max_depth=MAX_ASSET_META_DEPTH,
            max_items=MAX_ASSET_META_ITEMS,
        )
        return value


class RendererAssetRule(_ContractModel):
    asset_type: AssetType
    min_count: int = Field(ge=0, le=MAX_STAGE_ASSETS)
    max_count: int | None = Field(default=None, ge=0, le=MAX_STAGE_ASSETS)

    @model_validator(mode="after")
    def _maximum_covers_minimum(self):
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count must be greater than or equal to min_count")
        return self


class RendererContract(_ContractModel):
    """Public, credential-free identity and accepted input of one adapter renderer."""

    renderer_id: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=128)
    platform: Platform
    publisher_kind: PublisherKind
    accepted_extra_keys: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=64
    )
    accepts_tags: bool = False
    requires_external_account_id: bool = False
    asset_rules: list[RendererAssetRule] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _contract_collections_are_canonical(self):
        if self.accepted_extra_keys != sorted(set(self.accepted_extra_keys)):
            raise ValueError("accepted_extra_keys must be unique and sorted")
        asset_types = [rule.asset_type for rule in self.asset_rules]
        if len(set(asset_types)) != len(asset_types):
            raise ValueError("asset_rules must not repeat asset types")
        return self


class RendererBinding(_ContractModel):
    """Exact platform projection covered by approval and rechecked by the worker."""

    renderer: RendererContract
    payload: dict[str, JsonValue]
    payload_digest: DigestValue

    @field_validator("payload")
    @classmethod
    def _payload_is_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_limits(
            value,
            field_name="renderer payload",
            max_bytes=MAX_RENDERER_PAYLOAD_BYTES,
            max_depth=MAX_RENDERER_PAYLOAD_DEPTH,
            max_items=MAX_RENDERER_PAYLOAD_ITEMS,
        )
        return value

    @classmethod
    def from_projection(
        cls,
        *,
        renderer: RendererContract,
        payload: dict[str, JsonValue],
    ) -> RendererBinding:
        material = {
            "renderer": renderer.model_dump(mode="json"),
            "payload": payload,
        }
        return cls(
            renderer=renderer,
            payload=payload,
            payload_digest=canonical_sha256(material),
        )

    @model_validator(mode="after")
    def _payload_digest_matches_projection(self):
        material = {
            "renderer": self.renderer.model_dump(mode="json"),
            "payload": self.payload,
        }
        if canonical_sha256(material) != self.payload_digest:
            raise ValueError("payload_digest must match renderer and payload")
        return self


class PublicationTarget(_ContractModel):
    """A concrete account destination selected by publication planning."""

    account_id: PositiveIdentifier
    platform: Platform
    # Binds the logical account/profile and encrypted credential generation
    # without exposing any of those values over the contract.
    account_binding_digest: DigestValue
    # Public stable identity observed through the selected adapter's read-only
    # account probe. It is intentionally separate from encrypted credentials and
    # is shown to the human approver as part of the exact destination.
    approved_external_account_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]*$",
    )
    execution: RendererBinding

    @model_validator(mode="after")
    def _renderer_platform_matches_target(self):
        if self.execution.renderer.platform != self.platform:
            raise ValueError("renderer platform must match publication target")
        requires_identity = self.execution.renderer.requires_external_account_id
        if self.platform == Platform.ZHIHU and not requires_identity:
            raise ValueError("Zhihu renderer must bind a stable external account identity")
        if requires_identity != (self.approved_external_account_id is not None):
            raise ValueError("renderer external account identity requirement is not satisfied")
        return self


class PostIdentity(_ContractModel):
    """Public post identity without an adapter's unfiltered response."""

    platform_post_id: str | None = Field(default=None, max_length=128)
    platform_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _identity_must_have_a_value(self):
        if not self.platform_post_id and not self.platform_url:
            raise ValueError("post identity requires a platform_post_id or platform_url")
        return self


class MetricSnapshot(_ContractModel):
    """Normalized metrics; unavailable values remain null rather than fake zeroes."""

    collected_at: datetime
    likes: NonNegativeCounter | None = None
    comments: NonNegativeCounter | None = None
    shares: NonNegativeCounter | None = None
    views: NonNegativeCounter | None = None
    source: str = Field(min_length=1, max_length=32)
    quality: MetricQuality = MetricQuality.OBSERVED

    @model_validator(mode="after")
    def _snapshot_must_contain_an_observation(self):
        if all(value is None for value in (self.likes, self.comments, self.shares, self.views)):
            raise ValueError("a metric snapshot must contain at least one observed value")
        return self


class StageContentRequest(_VersionedContractModel):
    topic_id: PositiveIdentifier
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=MAX_STAGE_BODY_BYTES)
    content_type: ContentType
    target_platforms: list[Platform] = Field(default_factory=list, max_length=32)
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    assets: list[AssetInput] = Field(default_factory=list, max_length=MAX_STAGE_ASSETS)

    @field_validator("body")
    @classmethod
    def _body_is_bounded_by_encoded_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_STAGE_BODY_BYTES:
            raise ValueError(f"body exceeds the {MAX_STAGE_BODY_BYTES}-byte UTF-8 limit")
        return value

    @field_validator("extra")
    @classmethod
    def _extra_is_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_limits(
            value,
            field_name="extra",
            max_bytes=MAX_STAGE_EXTRA_BYTES,
            max_depth=MAX_STAGE_EXTRA_DEPTH,
            max_items=MAX_STAGE_EXTRA_ITEMS,
        )
        return value

    @field_validator("target_platforms")
    @classmethod
    def _target_platforms_are_unique(cls, value: list[Platform]) -> list[Platform]:
        if len(set(value)) != len(value):
            raise ValueError("target_platforms must not contain duplicates")
        return value


class StageContentResponse(_VersionedContractModel):
    content_id: PositiveIdentifier
    state: ArticleStatus
    content_digest: DigestValue
    created_at: datetime


class PlanPublicationRequest(_VersionedContractModel):
    content_id: PositiveIdentifier
    # v1 never infers destinations.  Every human-approved plan starts from an
    # explicit, bounded account set supplied by the caller.
    account_ids: list[PositiveIdentifier] = Field(
        min_length=1,
        max_length=MAX_PLAN_TARGETS,
    )
    planned_for: datetime | None = None

    @field_validator("account_ids")
    @classmethod
    def _account_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("account_ids must not contain duplicates")
        return value


class PlanPublicationResponse(_VersionedContractModel):
    plan_id: StableIdentifier
    state: PlanState = PlanState.PLANNED
    content_digest: DigestValue
    plan_digest: DigestValue
    targets: list[PublicationTarget] = Field(min_length=1, max_length=MAX_PLAN_TARGETS)
    planned_for: datetime
    approval_required: bool = True

    @field_validator("targets")
    @classmethod
    def _targets_are_unique(cls, value: list[PublicationTarget]) -> list[PublicationTarget]:
        identities = {(target.account_id, target.platform) for target in value}
        if len(identities) != len(value):
            raise ValueError("targets must not contain duplicate account/platform pairs")
        return value


class RequestApprovalRequest(_VersionedContractModel):
    plan_id: StableIdentifier
    expires_at: datetime | None = None


class ApprovalResponse(_VersionedContractModel):
    approval_id: StableIdentifier
    plan_id: StableIdentifier
    state: ApprovalState
    plan_digest: DigestValue
    requested_at: datetime
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _expiry_follows_request(self):
        if self.expires_at is not None and self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be later than requested_at")
        return self


class ApprovalReviewAsset(_ContractModel):
    """One content-addressed asset in the human review bundle.

    ``vaulted_path`` is a logical vault URI.  It deliberately never exposes the
    host filesystem path stored by the worker.
    """

    asset_id: PositiveIdentifier
    asset_type: AssetType
    source: AssetSource
    vaulted_path: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^vault://sha256/[0-9a-f]{64}$",
    )
    sha256: DigestValue
    size_bytes: NonNegativeCounter
    # File suffix is execution-relevant for CLI/media adapters, so it is
    # explicit in human review and covered by content_digest.
    storage_suffix: str = Field(pattern=r"^(?:\.[a-z0-9]{1,10})?$")
    meta: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("meta")
    @classmethod
    def _meta_is_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_limits(
            value,
            field_name="asset meta",
            max_bytes=MAX_ASSET_META_BYTES,
            max_depth=MAX_ASSET_META_DEPTH,
            max_items=MAX_ASSET_META_ITEMS,
        )
        return value


class ApprovalContentSnapshot(_ContractModel):
    """The complete content projection covered by ``content_digest``."""

    content_id: PositiveIdentifier
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=MAX_STAGE_BODY_BYTES)
    content_type: ContentType
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    assets: list[ApprovalReviewAsset] = Field(
        default_factory=list,
        max_length=MAX_STAGE_ASSETS,
    )

    @field_validator("body")
    @classmethod
    def _body_is_bounded(cls, value: str) -> str:
        if len(value) > MAX_STAGE_BODY_BYTES or len(value.encode("utf-8")) > MAX_STAGE_BODY_BYTES:
            raise ValueError(f"body exceeds the {MAX_STAGE_BODY_BYTES}-byte UTF-8 limit")
        return value

    @field_validator("extra")
    @classmethod
    def _extra_is_bounded(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _validate_json_limits(
            value,
            field_name="extra",
            max_bytes=MAX_STAGE_EXTRA_BYTES,
            max_depth=MAX_STAGE_EXTRA_DEPTH,
            max_items=MAX_STAGE_EXTRA_ITEMS,
        )
        return value

    @field_validator("assets")
    @classmethod
    def _review_assets_are_unique(
        cls,
        value: list[ApprovalReviewAsset],
    ) -> list[ApprovalReviewAsset]:
        if len({asset.asset_id for asset in value}) != len(value):
            raise ValueError("review assets must not contain duplicate IDs")
        return value


class ApprovalReviewTarget(PublicationTarget):
    """Approved destination plus a safe human-readable account label."""

    account_display: str = Field(min_length=1, max_length=128)


class ApprovalReviewResponse(_VersionedContractModel):
    """Immutable, credential-free subject shown before a human decision."""

    approval_id: StableIdentifier
    plan_id: StableIdentifier
    state: ApprovalState
    plan_digest: DigestValue
    content_digest: DigestValue
    content: ApprovalContentSnapshot
    targets: list[ApprovalReviewTarget] = Field(
        min_length=1,
        max_length=MAX_PLAN_TARGETS,
    )
    planned_for: datetime
    requested_at: datetime
    expires_at: datetime | None = None

    @field_validator("targets")
    @classmethod
    def _review_targets_are_unique(
        cls,
        value: list[ApprovalReviewTarget],
    ) -> list[ApprovalReviewTarget]:
        identities = {(target.account_id, target.platform) for target in value}
        if len(identities) != len(value):
            raise ValueError("review targets must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _review_expiry_follows_request(self):
        if self.expires_at is not None and self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be later than requested_at")
        return self


class ApprovalAssetDownloadResponse(_VersionedContractModel):
    """Verified metadata emitted after a review asset is atomically saved."""

    approval_id: StableIdentifier
    asset_id: PositiveIdentifier
    sha256: DigestValue
    size_bytes: NonNegativeCounter


class ApprovalDecisionRequest(_VersionedContractModel):
    expected_plan_digest: DigestValue
    decision: ApprovalDecision
    reason: SafeMessage = ""

    @model_validator(mode="after")
    def _rejection_requires_a_reason(self):
        if self.decision == ApprovalDecision.REJECTED and not self.reason.strip():
            raise ValueError("a rejected approval requires a reason")
        return self


class ApprovalDecisionResponse(_VersionedContractModel):
    approval_id: StableIdentifier
    plan_id: StableIdentifier
    state: ApprovalState
    plan_digest: DigestValue
    reason: SafeMessage = ""
    decided_at: datetime

    @model_validator(mode="after")
    def _state_is_a_decision(self):
        if self.state not in {ApprovalState.APPROVED, ApprovalState.REJECTED}:
            raise ValueError("an approval decision response must be approved or rejected")
        return self


class ScheduleRequest(_VersionedContractModel):
    plan_id: StableIdentifier


class ScheduleResponse(_VersionedContractModel):
    plan_id: StableIdentifier
    state: ScheduleState = ScheduleState.SCHEDULED
    plan_digest: DigestValue
    job_ids: list[PositiveIdentifier] = Field(min_length=1, max_length=MAX_PLAN_TARGETS)
    planned_for: datetime

    @field_validator("job_ids")
    @classmethod
    def _job_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("job_ids must not contain duplicates")
        return value


class JobStatusResponse(_VersionedContractModel):
    job_id: PositiveIdentifier
    plan_id: StableIdentifier | None = None
    content_id: PositiveIdentifier
    account_id: PositiveIdentifier
    platform: Platform
    state: JobStatus
    attempts: int = Field(ge=0, le=MAX_SIGNED_64)
    max_attempts: int = Field(ge=1, le=MAX_SIGNED_64)
    planned_for: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    publisher_id: str | None = Field(default=None, min_length=1, max_length=64)
    post_identity: PostIdentity | None = None
    outcome_uncertain: bool = False
    reconciliation_required: bool = False
    error_code: str | None = Field(default=None, min_length=1, max_length=64)
    error_message: SafeMessage | None = None

    @model_validator(mode="after")
    def _attempt_count_is_bounded(self):
        if self.attempts > self.max_attempts:
            raise ValueError("attempts must not exceed max_attempts")
        return self


class CollectMetricsRequest(_VersionedContractModel):
    job_id: PositiveIdentifier


class CollectMetricsResponse(_VersionedContractModel):
    job_id: PositiveIdentifier
    state: MetricsCollectionState
    metrics: MetricSnapshot | None = None
    reason: SafeMessage | None = None

    @model_validator(mode="after")
    def _collection_state_matches_payload(self):
        if self.state == MetricsCollectionState.COLLECTED and self.metrics is None:
            raise ValueError("collected metrics require a snapshot")
        if self.state in {
            MetricsCollectionState.SKIPPED,
            MetricsCollectionState.UNAVAILABLE,
        }:
            if self.metrics is not None:
                raise ValueError("metrics without collection must not include a snapshot")
            if not self.reason:
                raise ValueError("metrics without collection require a reason")
        return self


class PerformanceReviewRequest(_VersionedContractModel):
    job_ids: list[PositiveIdentifier] = Field(
        min_length=1,
        max_length=MAX_PERFORMANCE_REVIEW_JOBS,
    )
    window_start: datetime | None = None
    window_end: datetime | None = None

    @field_validator("job_ids")
    @classmethod
    def _review_job_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("job_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _window_is_complete_and_ordered(self):
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end must be provided together")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end <= self.window_start
        ):
            raise ValueError("window_end must be later than window_start")
        return self


class PerformanceReviewItem(_ContractModel):
    job_id: PositiveIdentifier
    content_id: PositiveIdentifier
    account_id: PositiveIdentifier
    platform: Platform
    metrics: MetricSnapshot | None = None


class PerformanceTotals(_ContractModel):
    jobs_reviewed: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS)
    jobs_with_metrics: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS)
    likes: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS * MAX_SIGNED_64)
    comments: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS * MAX_SIGNED_64)
    shares: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS * MAX_SIGNED_64)
    views: int = Field(ge=0, le=MAX_PERFORMANCE_REVIEW_JOBS * MAX_SIGNED_64)

    @model_validator(mode="after")
    def _coverage_is_bounded(self):
        if self.jobs_with_metrics > self.jobs_reviewed:
            raise ValueError("jobs_with_metrics must not exceed jobs_reviewed")
        return self


class PerformanceReviewResponse(_VersionedContractModel):
    review_id: StableIdentifier
    reviewed_at: datetime
    items: list[PerformanceReviewItem] = Field(
        default_factory=list,
        max_length=MAX_PERFORMANCE_REVIEW_JOBS,
    )
    totals: PerformanceTotals
    findings: list[SafeMessage] = Field(
        default_factory=list,
        max_length=MAX_PERFORMANCE_REVIEW_JOBS,
    )


__all__ = [
    "SCHEMA_VERSION",
    "MAX_CONTRACT_REQUEST_BODY_BYTES",
    "MAX_CONTRACT_RESPONSE_BODY_BYTES",
    "MAX_PERFORMANCE_REVIEW_JOBS",
    "MAX_RENDERER_PAYLOAD_BYTES",
    "MAX_SIGNED_64",
    "MAX_STAGE_ASSETS",
    "ApprovalAssetDownloadResponse",
    "ApprovalDecision",
    "ApprovalDecisionRequest",
    "ApprovalDecisionResponse",
    "ApprovalContentSnapshot",
    "ApprovalReviewAsset",
    "ApprovalReviewResponse",
    "ApprovalReviewTarget",
    "ApprovalResponse",
    "ApprovalState",
    "AssetInput",
    "CollectMetricsRequest",
    "CollectMetricsResponse",
    "JobStatusResponse",
    "MetricQuality",
    "MetricSnapshot",
    "MetricsCollectionState",
    "PerformanceReviewItem",
    "PerformanceReviewRequest",
    "PerformanceReviewResponse",
    "PerformanceTotals",
    "PlanPublicationRequest",
    "PlanPublicationResponse",
    "PlanState",
    "PostIdentity",
    "PublicationTarget",
    "RequestApprovalRequest",
    "RendererAssetRule",
    "RendererBinding",
    "RendererContract",
    "ScheduleRequest",
    "ScheduleResponse",
    "ScheduleState",
    "StageContentRequest",
    "StageContentResponse",
]
