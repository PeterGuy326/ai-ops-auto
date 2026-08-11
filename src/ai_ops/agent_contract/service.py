"""Domain service for the versioned Agent control-plane contract.

The service is deliberately transport independent.  HTTP, CLI, and a future
MCP adapter all call the same methods, so approval and idempotency policy cannot
drift between entry points.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
from typing import Any, BinaryIO, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

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
    settings,
)
from ..core.enums import AccountHealth, ArticleStatus, JobStatus, Platform
from ..core.db_clock import database_utc_now
from ..core.external_identity import normalize_zhihu_external_account_id
from ..core.models import (
    Account,
    AgentOperation,
    ApprovalRequest,
    Article,
    Asset,
    Metrics,
    PublicationPlan,
    PublishJob,
    Topic,
)
from ..core.time import as_utc_naive
from ..publishers.base import AgentContractRendererUnavailable
from ..publishers.plugin_sdk import (
    PublisherPluginResolutionError,
    is_publisher_plugin_instance,
    publisher_kind_value,
)
from .assets import (
    AssetTooLargeError,
    AssetVaultError,
    VaultedAsset,
    import_asset_to_vault,
    inspect_import_asset_size,
    open_verified_vaulted_asset,
)
from .bindings import account_binding_digest
from .digest import canonical_sha256, plan_digest
from .schemas import (
    MAX_PLAN_TARGETS,
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalReviewResponse,
    ApprovalReviewTarget,
    ApprovalResponse,
    ApprovalState,
    CollectMetricsRequest,
    CollectMetricsResponse,
    JobStatusResponse,
    MetricQuality,
    MetricSnapshot,
    MetricsCollectionState,
    PerformanceReviewItem,
    PerformanceReviewRequest,
    PerformanceReviewResponse,
    PerformanceTotals,
    PlanPublicationRequest,
    PlanPublicationResponse,
    PostIdentity,
    PublicationTarget,
    RendererBinding,
    RequestApprovalRequest,
    ScheduleRequest,
    ScheduleResponse,
    StageContentRequest,
    StageContentResponse,
    validate_renderer_contract,
)
from .snapshot import (
    StoredApprovalContent,
    build_stored_content_metadata_snapshot,
    build_stored_content_snapshot,
    parse_stored_content_snapshot,
    publish_content_from_snapshot,
    public_content_snapshot,
    stored_content_digest,
    validate_stored_content_total,
    verify_stored_content_snapshot,
)


class PrincipalLike(Protocol):
    """The identity fields required by the domain service."""

    principal_id: str
    principal_type: str
    scopes: frozenset[str]


class PublisherRegistryLike(Protocol):
    def resolve(self, platform: Platform) -> list[Any]: ...


class AgentContractError(RuntimeError):
    """A stable, credential-free error suitable for every transport."""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class _IdempotencyCollision(RuntimeError):
    pass


ResponseT = TypeVar("ResponseT", bound=BaseModel)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RUNNABLE_ACCOUNT_HEALTH = {AccountHealth.HEALTHY, AccountHealth.UNKNOWN}


def _account_external_identity(account: Account, platform: Platform) -> str:
    profile = account.profile
    raw_identity = profile.get("external_account_id") if isinstance(profile, dict) else None
    if platform == Platform.ZHIHU:
        return normalize_zhihu_external_account_id(raw_identity)
    raise ValueError("unsupported external account identity contract")


@dataclass(frozen=True, slots=True)
class _VerifiedPlan:
    """Internal exact subject after content, target, and vault verification."""

    content_digest: str
    snapshot: StoredApprovalContent
    targets: list[PublicationTarget]
    accounts: dict[int, Account]


@dataclass(frozen=True, slots=True)
class _ExternalOperationClaim:
    """One expiring ownership claim for an out-of-transaction operation."""

    operation_id: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class ApprovalAssetFile:
    """Verified private file handle and metadata for a streaming adapter."""

    asset_id: int
    sha256: str
    size_bytes: int
    handle: BinaryIO
    filename: str

    def close(self) -> None:
        self.handle.close()


def _principal_type(principal: PrincipalLike) -> str:
    value = principal.principal_type
    return value.value if hasattr(value, "value") else str(value)


def _database_id(value: str | int, resource: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AgentContractError(
            "invalid_identifier",
            f"{resource} identifier must be a positive integer",
        ) from exc
    if parsed <= 0 or str(parsed) != str(value):
        raise AgentContractError(
            "invalid_identifier",
            f"{resource} identifier must be a positive integer",
        )
    return parsed


def _safe_reason(value: object, *, fallback: str) -> str:
    """Return bounded single-line text without forwarding exception reprs."""

    if not isinstance(value, str) or not value.strip():
        return fallback
    cleaned = " ".join(value.split())
    return cleaned[:1000]


class AgentControlPlane:
    """Stable Python implementation of the ten Roadmap operations."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        metrics_collector: Callable[..., Any] | None = None,
        asset_import_root: str | Path | None = None,
        asset_vault_root: str | Path | None = None,
        asset_max_bytes: int | None = None,
        asset_max_total_bytes: int | None = None,
        publisher_registry: PublisherRegistryLike | None = None,
    ) -> None:
        if session_factory is None:
            from ..core.db import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._metrics_collector = metrics_collector
        self._asset_import_root = Path(
            asset_import_root if asset_import_root is not None else settings.agent_asset_import_root
        )
        self._asset_vault_root = Path(
            asset_vault_root if asset_vault_root is not None else settings.agent_asset_vault_root
        )
        self._asset_max_bytes = (
            asset_max_bytes if asset_max_bytes is not None else settings.agent_asset_max_bytes
        )
        self._asset_max_total_bytes = (
            asset_max_total_bytes
            if asset_max_total_bytes is not None
            else settings.agent_asset_max_total_bytes
        )
        if publisher_registry is None:
            from ..publishers.registry import default_registry

            publisher_registry = default_registry
        self._publisher_registry = publisher_registry

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise AgentContractError(
                "invalid_idempotency_key",
                "Idempotency-Key must be 8-128 URL-safe characters",
            )
        return value

    @staticmethod
    def _require_scope(principal: PrincipalLike, scope: str) -> None:
        scopes = frozenset(getattr(principal, "scopes", ()))
        if scope not in scopes:
            raise AgentContractError(
                "insufficient_scope",
                "The authenticated principal lacks the required scope",
                status_code=403,
            )

    @staticmethod
    def _operation_payload(operation: str, request: object) -> dict[str, object]:
        if isinstance(request, BaseModel):
            payload: object = request.model_dump(mode="json", exclude_none=False)
        else:
            payload = request
        return {"operation": operation, "request": payload}

    @staticmethod
    def _load_operation_response(
        operation: AgentOperation,
        *,
        request_digest: str,
        response_type: type[ResponseT],
    ) -> ResponseT:
        if operation.request_digest != request_digest:
            raise AgentContractError(
                "idempotency_key_reused",
                "Idempotency-Key was already used with a different request",
                status_code=409,
            )
        if operation.response_json is None:
            raise AgentContractError(
                "operation_in_progress",
                "An operation with this Idempotency-Key is already in progress",
                status_code=409,
            )
        return response_type.model_validate(operation.response_json)

    def _find_operation(
        self,
        session: Session,
        *,
        principal: PrincipalLike,
        operation: str,
        idempotency_key: str,
    ) -> AgentOperation | None:
        return session.scalar(
            select(AgentOperation).where(
                AgentOperation.principal_id == principal.principal_id,
                AgentOperation.operation == operation,
                AgentOperation.idempotency_key == idempotency_key,
            )
        )

    def _run_idempotent(
        self,
        *,
        principal: PrincipalLike,
        operation: str,
        idempotency_key: str,
        request: object,
        response_type: type[ResponseT],
        action: Callable[[Session], ResponseT],
        response_status_code: int = 200,
    ) -> ResponseT:
        key = self._validate_idempotency_key(idempotency_key)
        request_digest = canonical_sha256(self._operation_payload(operation, request))
        try:
            with self._session() as session:
                existing = self._find_operation(
                    session,
                    principal=principal,
                    operation=operation,
                    idempotency_key=key,
                )
                if existing is not None:
                    return self._load_operation_response(
                        existing,
                        request_digest=request_digest,
                        response_type=response_type,
                    )

                ledger = AgentOperation(
                    principal_id=principal.principal_id,
                    principal_type=_principal_type(principal),
                    operation=operation,
                    idempotency_key=key,
                    request_digest=request_digest,
                )
                session.add(ledger)
                try:
                    # Claim the unique key before any business mutation.  A
                    # competing transaction blocks/fails here, not after jobs
                    # or approval facts have been created.
                    session.flush()
                except IntegrityError as exc:
                    raise _IdempotencyCollision from exc

                response = action(session)
                ledger.response_status_code = response_status_code
                ledger.response_json = response.model_dump(mode="json")
                session.flush()
                return response
        except _IdempotencyCollision:
            with self._session() as session:
                existing = self._find_operation(
                    session,
                    principal=principal,
                    operation=operation,
                    idempotency_key=key,
                )
                if existing is None:
                    raise AgentContractError(
                        "operation_conflict",
                        "The operation could not acquire its idempotency key",
                        status_code=409,
                    ) from None
                return self._load_operation_response(
                    existing,
                    request_digest=request_digest,
                    response_type=response_type,
                )

    @staticmethod
    def _asset_contract_error(error: AssetVaultError) -> AgentContractError:
        status_code = 400 if error.code in {"asset_source_rejected", "asset_too_large"} else 503
        return AgentContractError(error.code, str(error), status_code=status_code)

    def _article_snapshot(self, article: Article) -> StoredApprovalContent:
        try:
            return build_stored_content_snapshot(
                article,
                vault_root=self._asset_vault_root,
                max_bytes=self._asset_max_bytes,
                max_total_bytes=self._asset_max_total_bytes,
            )
        except AssetVaultError as exc:
            raise self._asset_contract_error(exc) from None
        except (TypeError, ValueError):
            raise AgentContractError(
                "content_snapshot_invalid",
                "Staged content cannot be represented as an immutable snapshot",
                status_code=409,
            ) from None

    @staticmethod
    def _target_dicts(targets: list[PublicationTarget]) -> list[dict[str, object]]:
        return [target.model_dump(mode="json") for target in targets]

    def stage_content(
        self,
        principal: PrincipalLike,
        request: StageContentRequest,
        *,
        idempotency_key: str,
    ) -> StageContentResponse:
        """Persist content and assets in DRAFT without external side effects."""

        self._require_scope(principal, SCOPE_CONTENT_STAGE)

        def action(session: Session) -> StageContentResponse:
            if session.get(Topic, request.topic_id) is None:
                raise AgentContractError(
                    "topic_not_found",
                    "The requested topic does not exist",
                    status_code=404,
                )

            # Authorize and size every source before the first permanent vault
            # import.  A bad path late in the request or an obviously excessive
            # aggregate must not leave earlier content-addressed files behind.
            preflight_total = 0
            try:
                for item in request.assets:
                    preflight_total += inspect_import_asset_size(
                        item.local_path,
                        import_root=self._asset_import_root,
                        max_bytes=self._asset_max_bytes,
                    )
                    if preflight_total > self._asset_max_total_bytes:
                        raise AssetTooLargeError(
                            "content assets exceed the configured total size limit"
                        )
            except AssetVaultError as exc:
                raise self._asset_contract_error(exc) from None

            article = Article(
                topic_id=request.topic_id,
                title=request.title,
                body=request.body,
                content_type=request.content_type,
                status=ArticleStatus.DRAFT,
                target_platforms=[platform.value for platform in request.target_platforms],
                target_account_ids=[],
                extra=request.extra,
            )
            session.add(article)
            session.flush()
            imported_total = 0
            for item in request.assets:
                try:
                    remaining_bytes = self._asset_max_total_bytes - imported_total
                    if remaining_bytes <= 0:
                        raise AssetTooLargeError(
                            "content assets exceed the configured total size limit"
                        )
                    vaulted = import_asset_to_vault(
                        item.local_path,
                        import_root=self._asset_import_root,
                        vault_root=self._asset_vault_root,
                        max_bytes=min(self._asset_max_bytes, remaining_bytes),
                    )
                    imported_total += vaulted.size_bytes
                except AssetVaultError as exc:
                    raise self._asset_contract_error(exc) from None
                article.assets.append(
                    Asset(
                        asset_type=item.asset_type,
                        source=item.source,
                        local_path=str(vaulted.vault_path),
                        content_sha256=vaulted.sha256,
                        size_bytes=vaulted.size_bytes,
                        storage_kind="agent_vault_v1",
                        meta=item.meta,
                    )
                )
            session.flush()
            snapshot = self._article_snapshot(article)
            return StageContentResponse(
                content_id=article.id,
                state=ArticleStatus.DRAFT,
                content_digest=stored_content_digest(snapshot),
                created_at=article.created_at,
            )

        return self._run_idempotent(
            principal=principal,
            operation="stage_content",
            idempotency_key=idempotency_key,
            request=request,
            response_type=StageContentResponse,
            action=action,
            response_status_code=201,
        )

    @staticmethod
    def _renderer_accepts_asset_manifest(snapshot: StoredApprovalContent, descriptor: Any) -> bool:
        counts: dict[object, int] = {}
        for asset in snapshot.assets:
            counts[asset.asset_type] = counts.get(asset.asset_type, 0) + 1
        rules = tuple(getattr(descriptor, "asset_rules", ()) or ())
        allowed_types = {getattr(rule, "asset_type", None) for rule in rules}
        if set(counts) - allowed_types:
            return False
        for rule in rules:
            count = counts.get(rule.asset_type, 0)
            if count < rule.min_count:
                return False
            if rule.max_count is not None and count > rule.max_count:
                return False
        return True

    def _resolve_renderer_binding(
        self,
        platform: Platform,
        snapshot: StoredApprovalContent,
    ) -> RendererBinding:
        content = publish_content_from_snapshot(snapshot)
        capable = False
        rejected = False
        try:
            publishers = self._publisher_registry.resolve(platform)
        except PublisherPluginResolutionError:
            raise AgentContractError(
                "publisher_registry_unavailable",
                "Publisher plugin configuration is invalid",
                status_code=503,
            ) from None
        for publisher in publishers:
            third_party = is_publisher_plugin_instance(publisher)
            descriptor = getattr(publisher, "agent_contract_renderer_descriptor", None)
            if descriptor is None:
                continue
            capable = True
            if getattr(descriptor, "platform", None) != platform or publisher_kind_value(
                getattr(publisher, "kind", "")
            ) != publisher_kind_value(descriptor.publisher_kind):
                raise AgentContractError(
                    "renderer_contract_invalid",
                    "A configured Publisher has an invalid Agent renderer contract",
                    status_code=503,
                )
            if not self._renderer_accepts_asset_manifest(snapshot, descriptor):
                rejected = True
                continue
            try:
                material = publisher.agent_contract_digest_material(content)
                if not isinstance(material, dict) or set(material) != {"renderer", "payload"}:
                    raise ValueError
                renderer = validate_renderer_contract(
                    material["renderer"],
                    expected_platform=platform,
                    expected_publisher_kind=publisher_kind_value(descriptor.publisher_kind),
                )
                if renderer.model_dump(mode="json") != descriptor.digest_material():
                    raise ValueError
                payload = material["payload"]
                if not isinstance(payload, dict):
                    raise ValueError
                return RendererBinding.from_projection(
                    renderer=renderer,
                    payload=payload,
                )
            except AgentContractRendererUnavailable:
                rejected = True
                continue
            except (Exception, SystemExit) as exc:
                if isinstance(exc, SystemExit) and not third_party:
                    raise
                raise AgentContractError(
                    "renderer_contract_invalid",
                    "A configured Publisher failed to project its Agent renderer contract",
                    status_code=503,
                ) from None
        if capable or rejected:
            raise AgentContractError(
                "content_not_supported_by_renderer",
                "Staged content is not supported by an exact Agent renderer",
                status_code=409,
            )
        raise AgentContractError(
            "exact_renderer_unavailable",
            "No exact Agent renderer is enabled for a requested platform",
            status_code=409,
        )

    def _resolve_targets(
        self,
        session: Session,
        article: Article,
        snapshot: StoredApprovalContent,
        requested_account_ids: list[int],
    ) -> list[PublicationTarget]:
        # This is also enforced by PlanPublicationRequest.  Keep a service-layer
        # guard so direct callers cannot use model_construct/unchecked values to
        # turn an empty v1 selection into an all-account fan-out.
        if not requested_account_ids:
            raise AgentContractError(
                "target_accounts_required",
                "At least one explicit target account is required",
                status_code=400,
            )
        if len(requested_account_ids) > MAX_PLAN_TARGETS or len(set(requested_account_ids)) != len(
            requested_account_ids
        ):
            raise AgentContractError(
                "target_account_selection_invalid",
                f"Target accounts must be unique and limited to {MAX_PLAN_TARGETS}",
                status_code=400,
            )
        try:
            allowed_platforms = {
                platform if isinstance(platform, Platform) else Platform(platform)
                for platform in (article.target_platforms or [])
            }
        except ValueError as exc:
            raise AgentContractError(
                "invalid_content_targets",
                "Staged content contains an unknown target platform",
                status_code=409,
            ) from exc
        if not allowed_platforms:
            raise AgentContractError(
                "no_target_platforms",
                "Staged content has no target platforms",
                status_code=409,
            )

        effective_ids = list(requested_account_ids)
        query = select(Account).where(Account.id.in_(effective_ids))
        accounts = list(session.scalars(query.order_by(Account.id.asc())).all())

        found_ids = {account.id for account in accounts}
        missing = sorted(set(effective_ids) - found_ids)
        if missing:
            raise AgentContractError(
                "target_account_not_found",
                "One or more requested target accounts do not exist",
                status_code=404,
            )

        targets: list[PublicationTarget] = []
        renderer_bindings: dict[Platform, RendererBinding] = {}
        for account in accounts:
            platform = Platform(account.platform)
            if platform not in allowed_platforms:
                raise AgentContractError(
                    "target_platform_mismatch",
                    "A requested account is outside the staged target platforms",
                    status_code=409,
                )
            health = AccountHealth(account.health)
            if health not in _RUNNABLE_ACCOUNT_HEALTH:
                raise AgentContractError(
                    "target_account_unavailable",
                    "A requested account is not currently eligible for publishing",
                    status_code=409,
                )
            try:
                binding_digest = account_binding_digest(account)
            except (TypeError, ValueError):
                raise AgentContractError(
                    "target_account_invalid",
                    "A requested account cannot be bound to an approval target",
                    status_code=409,
                ) from None
            execution = renderer_bindings.get(platform)
            if execution is None:
                execution = self._resolve_renderer_binding(platform, snapshot)
                renderer_bindings[platform] = execution
            approved_external_account_id: str | None = None
            if execution.renderer.requires_external_account_id:
                try:
                    approved_external_account_id = _account_external_identity(account, platform)
                except ValueError:
                    raise AgentContractError(
                        "target_external_account_identity_missing",
                        "A requested account lacks a valid stable external account identity",
                        status_code=409,
                    ) from None
            targets.append(
                PublicationTarget(
                    account_id=account.id,
                    platform=platform,
                    account_binding_digest=binding_digest,
                    approved_external_account_id=approved_external_account_id,
                    execution=execution,
                )
            )
        if not targets:
            raise AgentContractError(
                "no_eligible_targets",
                "No eligible account matches the staged target platforms",
                status_code=409,
            )
        return targets

    def plan_publication(
        self,
        principal: PrincipalLike,
        request: PlanPublicationRequest,
        *,
        idempotency_key: str,
    ) -> PlanPublicationResponse:
        """Resolve concrete targets and persist an immutable approval subject."""

        self._require_scope(principal, SCOPE_PLAN_CREATE)

        def action(session: Session) -> PlanPublicationResponse:
            article = session.scalar(
                select(Article)
                .where(Article.id == request.content_id)
                .options(selectinload(Article.assets))
            )
            if article is None:
                raise AgentContractError(
                    "content_not_found",
                    "The requested staged content does not exist",
                    status_code=404,
                )
            if ArticleStatus(article.status) != ArticleStatus.DRAFT:
                raise AgentContractError(
                    "content_not_draft",
                    "Only DRAFT content can be planned",
                    status_code=409,
                )
            snapshot = self._article_snapshot(article)
            targets = self._resolve_targets(
                session,
                article,
                snapshot,
                request.account_ids,
            )
            content_hash = stored_content_digest(snapshot)
            planned_for = as_utc_naive(request.planned_for) or datetime.utcnow()
            plan_hash = plan_digest(
                content_digest=content_hash,
                targets=targets,
                planned_for=planned_for,
            )
            plan = PublicationPlan(
                article_id=article.id,
                state="draft",
                content_digest=content_hash,
                plan_digest=plan_hash,
                content_snapshot=snapshot.model_dump(mode="json"),
                targets=self._target_dicts(targets),
                planned_for=planned_for,
                created_by=principal.principal_id,
                created_by_type=_principal_type(principal),
            )
            session.add(plan)
            session.flush()
            return PlanPublicationResponse(
                plan_id=str(plan.id),
                content_digest=content_hash,
                plan_digest=plan_hash,
                targets=targets,
                planned_for=planned_for,
            )

        return self._run_idempotent(
            principal=principal,
            operation="plan_publication",
            idempotency_key=idempotency_key,
            request=request,
            response_type=PlanPublicationResponse,
            action=action,
            response_status_code=201,
        )

    def _verify_plan_metadata(
        self,
        session: Session,
        plan: PublicationPlan,
        *,
        require_runnable_targets: bool = False,
    ) -> _VerifiedPlan:
        """Recompute bound metadata without reading every vaulted asset byte."""

        try:
            snapshot = parse_stored_content_snapshot(plan.content_snapshot)
            validate_stored_content_total(
                snapshot,
                max_total_bytes=self._asset_max_total_bytes,
            )
            live_snapshot = build_stored_content_metadata_snapshot(plan.article)
            targets = [PublicationTarget.model_validate(value) for value in plan.targets]
        except (AssetVaultError, TypeError, ValueError):
            raise AgentContractError(
                "approval_subject_changed",
                "Content, assets, targets, or timing changed after planning",
                status_code=409,
            ) from None

        content_hash = stored_content_digest(snapshot)
        current_plan_digest = plan_digest(
            content_digest=content_hash,
            targets=targets,
            planned_for=plan.planned_for,
        )
        if (
            snapshot.content_id != plan.article_id
            or live_snapshot.model_dump(mode="json") != snapshot.model_dump(mode="json")
            or content_hash != plan.content_digest
            or current_plan_digest != plan.plan_digest
        ):
            raise AgentContractError(
                "approval_subject_changed",
                "Content, assets, targets, or timing changed after planning",
                status_code=409,
            )
        accounts = self._validate_current_targets(
            session,
            targets,
            require_runnable=require_runnable_targets,
        )
        return _VerifiedPlan(
            content_digest=content_hash,
            snapshot=snapshot,
            targets=targets,
            accounts=accounts,
        )

    def _verify_plan_snapshot(
        self,
        session: Session,
        plan: PublicationPlan,
        *,
        require_runnable_targets: bool = False,
    ) -> _VerifiedPlan:
        """Verify metadata plus each approved vaulted byte sequence exactly once."""

        verified = self._verify_plan_metadata(
            session,
            plan,
            require_runnable_targets=require_runnable_targets,
        )
        try:
            verify_stored_content_snapshot(
                verified.snapshot,
                vault_root=self._asset_vault_root,
                max_bytes=self._asset_max_bytes,
                max_total_bytes=self._asset_max_total_bytes,
            )
        except AssetVaultError:
            raise AgentContractError(
                "approval_subject_changed",
                "Content, assets, targets, or timing changed after planning",
                status_code=409,
            ) from None
        return verified

    def request_approval(
        self,
        principal: PrincipalLike,
        request: RequestApprovalRequest,
        *,
        idempotency_key: str,
    ) -> ApprovalResponse:
        """Request an independent decision for one exact plan digest."""

        self._require_scope(principal, SCOPE_APPROVAL_REQUEST)

        plan_id = _database_id(request.plan_id, "plan")

        def action(session: Session) -> ApprovalResponse:
            plan = session.scalar(
                select(PublicationPlan)
                .where(PublicationPlan.id == plan_id)
                .options(selectinload(PublicationPlan.article).selectinload(Article.assets))
            )
            if plan is None:
                raise AgentContractError(
                    "plan_not_found",
                    "The requested publication plan does not exist",
                    status_code=404,
                )
            if plan.state != "draft":
                raise AgentContractError(
                    "plan_not_requestable",
                    "Only a draft plan can request approval",
                    status_code=409,
                )
            self._verify_plan_snapshot(session, plan)
            now = datetime.utcnow()
            expires_at = as_utc_naive(request.expires_at)
            if expires_at is not None and expires_at <= now:
                raise AgentContractError(
                    "invalid_approval_expiry",
                    "Approval expiry must be in the future",
                )

            claimed = session.execute(
                update(PublicationPlan)
                .where(PublicationPlan.id == plan.id, PublicationPlan.state == "draft")
                .values(state="approval_pending", updated_at=now)
            )
            if claimed.rowcount != 1:
                raise AgentContractError(
                    "plan_not_requestable",
                    "The plan was concurrently submitted for approval",
                    status_code=409,
                )
            approval = ApprovalRequest(
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                status="pending",
                requested_by=principal.principal_id,
                requested_by_type=_principal_type(principal),
                requested_at=now,
                expires_at=expires_at,
            )
            session.add(approval)
            session.flush()
            return ApprovalResponse(
                approval_id=str(approval.id),
                plan_id=str(plan.id),
                state=ApprovalState.PENDING,
                plan_digest=plan.plan_digest,
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
            )

        return self._run_idempotent(
            principal=principal,
            operation="request_approval",
            idempotency_key=idempotency_key,
            request=request,
            response_type=ApprovalResponse,
            action=action,
            response_status_code=201,
        )

    def get_approval(
        self,
        principal: PrincipalLike,
        approval_id: str | int,
    ) -> ApprovalReviewResponse:
        """Return the exact, redacted subject a human is being asked to approve."""

        self._require_scope(principal, SCOPE_APPROVAL_READ)
        if _principal_type(principal) != "human":
            raise AgentContractError(
                "human_reviewer_required",
                "Only a human principal may review an approval subject",
                status_code=403,
            )
        parsed_approval_id = _database_id(approval_id, "approval")

        with self._session() as session:
            approval = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.id == parsed_approval_id)
                .options(
                    selectinload(ApprovalRequest.plan)
                    .selectinload(PublicationPlan.article)
                    .selectinload(Article.assets)
                )
            )
            if approval is None:
                raise AgentContractError(
                    "approval_not_found",
                    "The requested approval does not exist",
                    status_code=404,
                )
            plan = approval.plan
            if approval.plan_digest != plan.plan_digest:
                raise AgentContractError(
                    "approval_subject_changed",
                    "The approval no longer matches its publication plan",
                    status_code=409,
                )
            verified = self._verify_plan_metadata(session, plan)
            review_targets: list[ApprovalReviewTarget] = []
            for target in verified.targets:
                account = verified.accounts[target.account_id]
                review_targets.append(
                    ApprovalReviewTarget(
                        account_id=target.account_id,
                        platform=target.platform,
                        account_binding_digest=target.account_binding_digest,
                        approved_external_account_id=target.approved_external_account_id,
                        execution=target.execution,
                        account_display=account.nickname,
                    )
                )

            try:
                state = ApprovalState(approval.status)
            except ValueError as exc:
                raise AgentContractError(
                    "approval_not_reviewable",
                    "The approval state cannot be reviewed",
                    status_code=409,
                ) from exc
            if (
                state == ApprovalState.PENDING
                and approval.expires_at is not None
                and approval.expires_at <= datetime.utcnow()
            ):
                state = ApprovalState.EXPIRED

            return ApprovalReviewResponse(
                approval_id=str(approval.id),
                plan_id=str(plan.id),
                state=state,
                plan_digest=plan.plan_digest,
                content_digest=verified.content_digest,
                content=public_content_snapshot(verified.snapshot),
                targets=review_targets,
                planned_for=plan.planned_for,
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
            )

    def get_approval_asset(
        self,
        principal: PrincipalLike,
        approval_id: str | int,
        asset_id: str | int,
    ) -> ApprovalAssetFile:
        """Resolve one verified review asset without exposing its host path."""

        self._require_scope(principal, SCOPE_APPROVAL_READ)
        if _principal_type(principal) != "human":
            raise AgentContractError(
                "human_reviewer_required",
                "Only a human principal may review an approval asset",
                status_code=403,
            )
        parsed_approval_id = _database_id(approval_id, "approval")
        parsed_asset_id = _database_id(asset_id, "asset")

        with self._session() as session:
            approval = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.id == parsed_approval_id)
                .options(
                    selectinload(ApprovalRequest.plan)
                    .selectinload(PublicationPlan.article)
                    .selectinload(Article.assets)
                )
            )
            if approval is None:
                raise AgentContractError(
                    "approval_not_found",
                    "The requested approval does not exist",
                    status_code=404,
                )
            plan = approval.plan
            if approval.plan_digest != plan.plan_digest:
                raise AgentContractError(
                    "approval_subject_changed",
                    "The approval no longer matches its publication plan",
                    status_code=409,
                )
            verified = self._verify_plan_metadata(session, plan)
            matches = [
                asset for asset in verified.snapshot.assets if asset.asset_id == parsed_asset_id
            ]
            if len(matches) != 1:
                raise AgentContractError(
                    "approval_asset_not_found",
                    "The requested asset is not part of this approval",
                    status_code=404,
                )
            asset = matches[0]
            record = VaultedAsset(
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                vault_path=Path(asset.storage_path),
            )
            filename = f"asset-{asset.asset_id}{asset.storage_suffix}"

        # Open only after the read transaction has exited.  If commit/rollback
        # or session cleanup fails, there is no live file descriptor to leak.
        try:
            opened = open_verified_vaulted_asset(
                record,
                vault_root=self._asset_vault_root,
                max_bytes=self._asset_max_bytes,
            )
        except AssetVaultError:
            raise AgentContractError(
                "approval_subject_changed",
                "The approved asset failed integrity verification",
                status_code=409,
            ) from None
        return ApprovalAssetFile(
            asset_id=asset.asset_id,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            handle=opened.handle,
            filename=filename,
        )

    def decide_approval(
        self,
        principal: PrincipalLike,
        approval_id: str | int,
        request: ApprovalDecisionRequest,
        *,
        idempotency_key: str,
    ) -> ApprovalDecisionResponse:
        """Record the human decision; requesters and plan creators cannot self-sign."""

        parsed_approval_id = _database_id(approval_id, "approval")
        self._require_scope(principal, SCOPE_APPROVAL_DECIDE)
        if _principal_type(principal) != "human":
            raise AgentContractError(
                "human_approver_required",
                "Only a human principal may decide an approval",
                status_code=403,
            )
        operation_request = {
            "approval_id": parsed_approval_id,
            "decision": request.model_dump(mode="json"),
        }

        def action(session: Session) -> ApprovalDecisionResponse:
            approval = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.id == parsed_approval_id)
                .options(
                    selectinload(ApprovalRequest.plan)
                    .selectinload(PublicationPlan.article)
                    .selectinload(Article.assets)
                )
            )
            if approval is None:
                raise AgentContractError(
                    "approval_not_found",
                    "The requested approval does not exist",
                    status_code=404,
                )
            plan = approval.plan
            now = datetime.utcnow()
            if request.expected_plan_digest != approval.plan_digest:
                raise AgentContractError(
                    "approval_digest_mismatch",
                    "The reviewed plan digest does not match this approval",
                    status_code=409,
                )
            if approval.status != "pending":
                raise AgentContractError(
                    "approval_already_decided",
                    "This approval is no longer pending",
                    status_code=409,
                )
            if approval.expires_at is not None and approval.expires_at <= now:
                raise AgentContractError(
                    "approval_expired",
                    "This approval request has expired",
                    status_code=409,
                )
            if principal.principal_id in {approval.requested_by, plan.created_by}:
                raise AgentContractError(
                    "self_approval_forbidden",
                    "The requester or plan creator cannot decide the approval",
                    status_code=403,
                )
            if approval.plan_digest != plan.plan_digest:
                raise AgentContractError(
                    "approval_subject_changed",
                    "The approval no longer matches its publication plan",
                    status_code=409,
                )
            self._verify_plan_snapshot(session, plan)

            decision = request.decision.value
            claimed = session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval.id,
                    ApprovalRequest.status == "pending",
                )
                .values(
                    status=decision,
                    decided_by=principal.principal_id,
                    decided_by_type=_principal_type(principal),
                    decided_at=now,
                    decision_reason=request.reason,
                    updated_at=now,
                )
            )
            if claimed.rowcount != 1:
                raise AgentContractError(
                    "approval_already_decided",
                    "The approval was concurrently decided",
                    status_code=409,
                )
            plan.state = "approved" if request.decision == ApprovalDecision.APPROVED else "rejected"
            plan.updated_at = now
            return ApprovalDecisionResponse(
                approval_id=str(approval.id),
                plan_id=str(plan.id),
                state=ApprovalState(decision),
                plan_digest=plan.plan_digest,
                reason=request.reason,
                decided_at=now,
            )

        return self._run_idempotent(
            principal=principal,
            operation="decide_approval",
            idempotency_key=idempotency_key,
            request=operation_request,
            response_type=ApprovalDecisionResponse,
            action=action,
        )

    @staticmethod
    def _validate_current_targets(
        session: Session,
        targets: list[PublicationTarget],
        *,
        require_runnable: bool,
    ) -> dict[int, Account]:
        expected_ids = {target.account_id for target in targets}
        if not targets or len(expected_ids) != len(targets):
            raise AgentContractError(
                "approval_subject_changed",
                "Approved publication targets are missing or duplicated",
                status_code=409,
            )
        accounts = list(
            session.scalars(
                select(Account).where(Account.id.in_(expected_ids)).order_by(Account.id.asc())
            ).all()
        )
        if {account.id for account in accounts} != expected_ids:
            raise AgentContractError(
                "target_account_not_found",
                "An approved target account no longer exists",
                status_code=409,
            )
        expected = {target.account_id: target for target in targets}
        for account in accounts:
            target = expected[account.id]
            try:
                platform = Platform(account.platform)
                binding_digest = account_binding_digest(account)
            except (TypeError, ValueError):
                raise AgentContractError(
                    "approval_subject_changed",
                    "An approved account binding is no longer valid",
                    status_code=409,
                ) from None
            if platform != target.platform or binding_digest != target.account_binding_digest:
                raise AgentContractError(
                    "approval_subject_changed",
                    "An approved account binding changed after approval",
                    status_code=409,
                )
            if target.approved_external_account_id is not None:
                try:
                    current_external_account_id = _account_external_identity(account, platform)
                except ValueError:
                    raise AgentContractError(
                        "approval_subject_changed",
                        "An approved external account identity is no longer valid",
                        status_code=409,
                    ) from None
                if current_external_account_id != target.approved_external_account_id:
                    raise AgentContractError(
                        "approval_subject_changed",
                        "An approved external account identity changed after approval",
                        status_code=409,
                    )
            if require_runnable and AccountHealth(account.health) not in _RUNNABLE_ACCOUNT_HEALTH:
                raise AgentContractError(
                    "target_account_unavailable",
                    "An approved target account is no longer eligible",
                    status_code=409,
                )
        return {account.id: account for account in accounts}

    def schedule(
        self,
        principal: PrincipalLike,
        request: ScheduleRequest,
        *,
        idempotency_key: str,
    ) -> ScheduleResponse:
        """Create durable jobs only after verifying the exact approved snapshot."""

        self._require_scope(principal, SCOPE_SCHEDULE_CREATE)

        plan_id = _database_id(request.plan_id, "plan")

        def action(session: Session) -> ScheduleResponse:
            plan = session.scalar(
                select(PublicationPlan)
                .where(PublicationPlan.id == plan_id)
                .options(
                    selectinload(PublicationPlan.article).selectinload(Article.assets),
                    selectinload(PublicationPlan.approval_requests),
                    selectinload(PublicationPlan.jobs),
                )
            )
            if plan is None:
                raise AgentContractError(
                    "plan_not_found",
                    "The requested publication plan does not exist",
                    status_code=404,
                )
            if plan.state == "scheduled":
                job_ids = sorted(job.id for job in plan.jobs)
                if not job_ids:
                    raise AgentContractError(
                        "scheduled_plan_incomplete",
                        "The scheduled plan has no durable jobs",
                        status_code=409,
                    )
                return ScheduleResponse(
                    plan_id=str(plan.id),
                    plan_digest=plan.plan_digest,
                    job_ids=job_ids,
                    planned_for=plan.planned_for,
                )
            if plan.state != "approved":
                raise AgentContractError(
                    "plan_not_approved",
                    "The publication plan has not been approved",
                    status_code=409,
                )

            now = datetime.utcnow()
            approvals = [
                item
                for item in plan.approval_requests
                if item.status == "approved" and item.plan_digest == plan.plan_digest
            ]
            if len(approvals) != 1:
                raise AgentContractError(
                    "independent_approval_missing",
                    "The plan lacks one matching approved decision",
                    status_code=409,
                )
            approval = approvals[0]
            if approval.decided_by_type != "human" or approval.decided_by in {
                approval.requested_by,
                plan.created_by,
            }:
                raise AgentContractError(
                    "independent_approval_missing",
                    "The plan lacks an independent human decision",
                    status_code=409,
                )
            if approval.expires_at is not None and approval.expires_at <= now:
                raise AgentContractError(
                    "approval_expired",
                    "The approval expired before scheduling",
                    status_code=409,
                )
            if ArticleStatus(plan.article.status) not in {
                ArticleStatus.DRAFT,
                ArticleStatus.READY,
            }:
                raise AgentContractError(
                    "content_not_schedulable",
                    "The staged content is no longer schedulable",
                    status_code=409,
                )

            verified = self._verify_plan_snapshot(
                session,
                plan,
                require_runnable_targets=True,
            )
            claimed = session.execute(
                update(PublicationPlan)
                .where(
                    PublicationPlan.id == plan.id,
                    PublicationPlan.state == "approved",
                )
                .values(state="scheduled", updated_at=now)
            )
            if claimed.rowcount != 1:
                raise AgentContractError(
                    "schedule_conflict",
                    "The plan was concurrently scheduled",
                    status_code=409,
                )

            # A content item may have more than one independently approved
            # plan.  Claim the article with a database compare-and-swap so two
            # different plans cannot both pass an earlier ORM status read and
            # create competing durable jobs under concurrent transactions.
            article_claim = session.execute(
                update(Article)
                .where(
                    Article.id == plan.article_id,
                    Article.status.in_((ArticleStatus.DRAFT, ArticleStatus.READY)),
                )
                .values(
                    status=ArticleStatus.SCHEDULED,
                    scheduled_at=plan.planned_for,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if article_claim.rowcount != 1:
                raise AgentContractError(
                    "schedule_conflict",
                    "The content was concurrently claimed by another publication plan",
                    status_code=409,
                )

            jobs: list[PublishJob] = []
            for target in verified.targets:
                job = PublishJob(
                    article_id=plan.article_id,
                    plan_id=plan.id,
                    account_id=target.account_id,
                    platform=target.platform,
                    status=JobStatus.PENDING,
                    approved_planned_for=plan.planned_for,
                    scheduled_at=plan.planned_for,
                )
                session.add(job)
                jobs.append(job)
            session.flush()
            return ScheduleResponse(
                plan_id=str(plan.id),
                plan_digest=plan.plan_digest,
                job_ids=sorted(job.id for job in jobs),
                planned_for=plan.planned_for,
            )

        return self._run_idempotent(
            principal=principal,
            operation="schedule",
            idempotency_key=idempotency_key,
            request=request,
            response_type=ScheduleResponse,
            action=action,
            response_status_code=201,
        )

    def get_job_status(
        self,
        principal: PrincipalLike,
        job_id: int,
    ) -> JobStatusResponse:
        """Return explainable job state without publisher raw responses."""

        self._require_scope(principal, SCOPE_JOB_READ)
        with self._session() as session:
            job = session.get(PublishJob, job_id)
            if job is None:
                raise AgentContractError(
                    "job_not_found",
                    "The requested job does not exist",
                    status_code=404,
                )
            raw = job.raw_response if isinstance(job.raw_response, dict) else {}
            uncertain = bool(raw.get("outcome_uncertain"))
            effect_applied = bool(raw.get("effect_applied"))
            adapter_reconciliation = bool(
                raw.get("needs_reconciliation") or raw.get("reconciliation_required")
            )
            reconciliation = bool(
                uncertain
                or effect_applied
                or adapter_reconciliation
                or "平台结果未知" in (job.error or "")
            )
            state = JobStatus(job.status)
            partial_effect = state in {JobStatus.FAILED, JobStatus.DEAD} and (
                effect_applied or adapter_reconciliation
            )
            identity = None
            if job.platform_post_id or job.platform_url:
                identity = PostIdentity(
                    platform_post_id=job.platform_post_id,
                    platform_url=job.platform_url,
                )
            error_code = None
            error_message = None
            if partial_effect:
                error_code = "partial_effect"
                error_message = (
                    "A platform-side effect may have occurred; human readback is required"
                )
            elif reconciliation:
                error_code = "platform_outcome_uncertain"
                error_message = "Platform outcome is uncertain; human readback is required"
            elif state in {JobStatus.FAILED, JobStatus.DEAD}:
                error_code = "publish_failed"
                error_message = "Publishing failed; inspect restricted operator logs"
            return JobStatusResponse(
                job_id=job.id,
                plan_id=str(job.plan_id) if job.plan_id is not None else None,
                content_id=job.article_id,
                account_id=job.account_id,
                platform=job.platform,
                state=job.status,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                planned_for=(
                    job.approved_planned_for if job.plan_id is not None else job.scheduled_at
                ),
                started_at=job.started_at,
                finished_at=job.finished_at,
                publisher_id=job.publisher_kind or None,
                post_identity=identity,
                outcome_uncertain=uncertain,
                reconciliation_required=reconciliation,
                error_code=error_code,
                error_message=error_message,
            )

    def _claim_external_operation(
        self,
        *,
        principal: PrincipalLike,
        operation: str,
        idempotency_key: str,
        request: object,
        response_type: type[ResponseT],
    ) -> ResponseT | _ExternalOperationClaim:
        """Claim or reclaim a key before a bounded external read."""

        key = self._validate_idempotency_key(idempotency_key)
        request_digest = canonical_sha256(self._operation_payload(operation, request))
        for _ in range(2):
            lease_token = secrets.token_hex(32)
            now = datetime.utcnow()
            lease_expires_at = now + timedelta(
                seconds=settings.agent_external_operation_lease_seconds
            )
            try:
                with self._session() as session:
                    existing = self._find_operation(
                        session,
                        principal=principal,
                        operation=operation,
                        idempotency_key=key,
                    )
                    if existing is not None:
                        if existing.request_digest != request_digest:
                            raise AgentContractError(
                                "idempotency_key_reused",
                                "Idempotency-Key was already used with a different request",
                                status_code=409,
                            )
                        if existing.response_json is not None:
                            return self._load_operation_response(
                                existing,
                                request_digest=request_digest,
                                response_type=response_type,
                            )
                        if (
                            existing.lease_token is not None
                            and existing.lease_expires_at is not None
                            and existing.lease_expires_at > now
                        ):
                            raise AgentContractError(
                                "operation_in_progress",
                                "An operation with this Idempotency-Key is already in progress",
                                status_code=409,
                            )
                        claimed = session.execute(
                            update(AgentOperation)
                            .where(
                                AgentOperation.id == existing.id,
                                AgentOperation.request_digest == request_digest,
                                AgentOperation.response_json.is_(None),
                                or_(
                                    AgentOperation.lease_expires_at.is_(None),
                                    AgentOperation.lease_expires_at <= now,
                                ),
                            )
                            .values(
                                lease_token=lease_token,
                                lease_expires_at=lease_expires_at,
                                updated_at=now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if claimed.rowcount != 1:
                            raise _IdempotencyCollision
                        return _ExternalOperationClaim(
                            operation_id=existing.id,
                            lease_token=lease_token,
                        )

                    ledger = AgentOperation(
                        principal_id=principal.principal_id,
                        principal_type=_principal_type(principal),
                        operation=operation,
                        idempotency_key=key,
                        request_digest=request_digest,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                    )
                    session.add(ledger)
                    try:
                        session.flush()
                    except IntegrityError as exc:
                        raise _IdempotencyCollision from exc
                    return _ExternalOperationClaim(
                        operation_id=ledger.id,
                        lease_token=lease_token,
                    )
            except _IdempotencyCollision:
                continue

        raise AgentContractError(
            "operation_conflict",
            "The operation could not acquire its idempotency key",
            status_code=409,
        )

    def _release_external_operation(
        self,
        *,
        claim: _ExternalOperationClaim,
    ) -> None:
        """Make an interrupted owned claim immediately eligible for recovery."""

        try:
            with self._session() as session:
                session.execute(
                    update(AgentOperation)
                    .where(
                        AgentOperation.id == claim.operation_id,
                        AgentOperation.lease_token == claim.lease_token,
                        AgentOperation.response_json.is_(None),
                    )
                    .values(
                        lease_expires_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
        except Exception as exc:
            # Preserve the original cancellation/failure.  The bounded lease
            # remains the durable fallback if the release write is unavailable.
            import logging

            logging.getLogger(__name__).error(
                "Agent external-operation lease release failed; error_type=%s",
                type(exc).__name__,
            )

    def _finish_external_operation(
        self,
        *,
        principal: PrincipalLike,
        operation: str,
        idempotency_key: str,
        claim: _ExternalOperationClaim,
        response: BaseModel,
    ) -> None:
        with self._session() as session:
            finished_at = datetime.utcnow()
            finalized = session.execute(
                update(AgentOperation)
                .where(
                    AgentOperation.id == claim.operation_id,
                    AgentOperation.principal_id == principal.principal_id,
                    AgentOperation.operation == operation,
                    AgentOperation.idempotency_key == idempotency_key,
                    AgentOperation.lease_token == claim.lease_token,
                    AgentOperation.lease_expires_at > database_utc_now(session),
                    AgentOperation.response_json.is_(None),
                )
                .values(
                    response_status_code=200,
                    response_json=response.model_dump(mode="json"),
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=finished_at,
                )
                .execution_options(synchronize_session=False)
            )
            if finalized.rowcount != 1:
                raise AgentContractError(
                    "operation_conflict",
                    "The operation ledger could not be finalized",
                    status_code=409,
                )

    async def collect_metrics(
        self,
        principal: PrincipalLike,
        request: CollectMetricsRequest,
        *,
        idempotency_key: str,
    ) -> CollectMetricsResponse:
        """Collect one snapshot while redacting adapter-specific raw data."""

        self._require_scope(principal, SCOPE_METRICS_COLLECT)

        replay_or_claim = self._claim_external_operation(
            principal=principal,
            operation="collect_metrics",
            idempotency_key=idempotency_key,
            request=request,
            response_type=CollectMetricsResponse,
        )
        if isinstance(replay_or_claim, CollectMetricsResponse):
            return replay_or_claim
        claim = replay_or_claim

        try:
            with self._session() as session:
                persisted = session.scalar(
                    select(Metrics).where(Metrics.agent_operation_id == claim.operation_id)
                )
            if persisted is not None and persisted.job_id != request.job_id:
                raise AgentContractError(
                    "operation_conflict",
                    "The operation ledger is bound to a different metrics job",
                    status_code=409,
                )
            if persisted is None:
                collector = self._metrics_collector
                if collector is None:
                    from ..scheduler.metrics import collect_one

                    collector = collect_one
                try:
                    result = await asyncio.wait_for(
                        collector(
                            request.job_id,
                            source="manual",
                            agent_operation_id=claim.operation_id,
                            agent_operation_lease_token=claim.lease_token,
                        ),
                        timeout=settings.agent_metrics_collection_timeout_seconds,
                    )
                except TimeoutError:
                    result = {
                        "skipped": True,
                        "reason": "Metrics collector timed out",
                        "unavailable": True,
                    }
                except Exception:
                    result = {
                        "skipped": True,
                        "reason": "Metrics collector is temporarily unavailable",
                        "unavailable": True,
                    }
                # A previous lease owner may have persisted the uniquely bound
                # snapshot just as this owner attempted the same insert.  The
                # durable fact wins over the collector exception; otherwise we
                # could finalize a permanent `unavailable` replay beside a
                # successfully stored metric.
                with self._session() as session:
                    persisted = session.scalar(
                        select(Metrics).where(Metrics.agent_operation_id == claim.operation_id)
                    )
                if persisted is not None:
                    if persisted.job_id != request.job_id:
                        raise AgentContractError(
                            "operation_conflict",
                            "The operation ledger is bound to a different metrics job",
                            status_code=409,
                        )
                    result = {"collected": True, "replayed_from_ledger": True}
            else:
                result = {"collected": True, "replayed_from_ledger": True}

            response: CollectMetricsResponse
            if not isinstance(result, dict) or result.get("skipped"):
                state = (
                    MetricsCollectionState.UNAVAILABLE
                    if isinstance(result, dict) and result.get("unavailable")
                    else MetricsCollectionState.SKIPPED
                )
                reason = result.get("reason") if isinstance(result, dict) else None
                response = CollectMetricsResponse(
                    job_id=request.job_id,
                    state=state,
                    reason=_safe_reason(reason, fallback="Metrics are unavailable for this job"),
                )
            else:
                with self._session() as session:
                    snapshot = session.scalar(
                        select(Metrics).where(
                            Metrics.agent_operation_id == claim.operation_id,
                            Metrics.job_id == request.job_id,
                        )
                    )
                if snapshot is None:
                    response = CollectMetricsResponse(
                        job_id=request.job_id,
                        state=MetricsCollectionState.UNAVAILABLE,
                        reason="Collector returned without a persisted snapshot",
                    )
                else:
                    response = CollectMetricsResponse(
                        job_id=request.job_id,
                        state=MetricsCollectionState.COLLECTED,
                        metrics=MetricSnapshot(
                            collected_at=snapshot.collected_at,
                            likes=snapshot.likes,
                            comments=snapshot.comments,
                            shares=snapshot.shares,
                            views=snapshot.views,
                            source=snapshot.source,
                            quality=MetricQuality.OBSERVED,
                        ),
                    )
            self._finish_external_operation(
                principal=principal,
                operation="collect_metrics",
                idempotency_key=idempotency_key,
                claim=claim,
                response=response,
            )
            return response
        except BaseException:
            self._release_external_operation(claim=claim)
            raise

    @staticmethod
    def _metric_snapshot(metric: Metrics | None) -> MetricSnapshot | None:
        if metric is None:
            return None
        quality = MetricQuality.SYNTHETIC if metric.source == "demo" else MetricQuality.OBSERVED
        return MetricSnapshot(
            collected_at=metric.collected_at,
            likes=metric.likes,
            comments=metric.comments,
            shares=metric.shares,
            views=metric.views,
            source=metric.source,
            quality=quality,
        )

    def review_performance(
        self,
        principal: PrincipalLike,
        request: PerformanceReviewRequest,
    ) -> PerformanceReviewResponse:
        """Return normalized latest snapshots and explicit coverage."""

        self._require_scope(principal, SCOPE_PERFORMANCE_READ)
        with self._session() as session:
            jobs = list(
                session.scalars(
                    select(PublishJob)
                    .where(PublishJob.id.in_(request.job_ids))
                    .order_by(PublishJob.id.asc())
                ).all()
            )
            if {job.id for job in jobs} != set(request.job_ids):
                raise AgentContractError(
                    "job_not_found",
                    "One or more requested jobs do not exist",
                    status_code=404,
                )
            # Rank inside the database so review memory and ORM hydration stay
            # bounded by the requested job count, regardless of metric history
            # depth.  ``id`` is the deterministic tie-breaker when collectors
            # persist multiple snapshots with the same timestamp.
            ranked_metric_query = select(
                Metrics.id.label("metric_id"),
                func.row_number()
                .over(
                    partition_by=Metrics.job_id,
                    order_by=(Metrics.collected_at.desc(), Metrics.id.desc()),
                )
                .label("metric_rank"),
            ).where(Metrics.job_id.in_(request.job_ids))
            if request.window_start is not None:
                ranked_metric_query = ranked_metric_query.where(
                    Metrics.collected_at >= as_utc_naive(request.window_start),
                    Metrics.collected_at < as_utc_naive(request.window_end),
                )
            ranked_metrics = ranked_metric_query.subquery()
            metrics = list(
                session.scalars(
                    select(Metrics)
                    .join(ranked_metrics, Metrics.id == ranked_metrics.c.metric_id)
                    .where(ranked_metrics.c.metric_rank == 1)
                    .order_by(Metrics.job_id.asc())
                ).all()
            )

            latest = {metric.job_id: metric for metric in metrics}
            items = [
                PerformanceReviewItem(
                    job_id=job.id,
                    content_id=job.article_id,
                    account_id=job.account_id,
                    platform=job.platform,
                    metrics=self._metric_snapshot(latest.get(job.id)),
                )
                for job in jobs
            ]
            snapshots = [item.metrics for item in items if item.metrics is not None]
            totals = PerformanceTotals(
                jobs_reviewed=len(items),
                jobs_with_metrics=len(snapshots),
                likes=sum(item.likes or 0 for item in snapshots),
                comments=sum(item.comments or 0 for item in snapshots),
                shares=sum(item.shares or 0 for item in snapshots),
                views=sum(item.views or 0 for item in snapshots),
            )
            missing = len(items) - len(snapshots)
            findings = []
            if missing:
                findings.append(f"{missing} job(s) have no metric snapshot in this window")
            if not items:
                findings.append("No jobs were reviewed")
            reviewed_at = datetime.utcnow()
            review_id = (
                "review-"
                + canonical_sha256(
                    {
                        "job_ids": [job.id for job in jobs],
                        "metric_ids": [latest[job.id].id for job in jobs if job.id in latest],
                        "window_start": request.window_start,
                        "window_end": request.window_end,
                    }
                )[:24]
            )
            return PerformanceReviewResponse(
                review_id=review_id,
                reviewed_at=reviewed_at,
                items=items,
                totals=totals,
                findings=findings,
            )


__all__ = [
    "AgentContractError",
    "AgentControlPlane",
    "ApprovalAssetFile",
    "PrincipalLike",
]
