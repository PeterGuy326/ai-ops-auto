"""High-value integration tests for the Agent control-plane domain service.

These tests deliberately exercise the service against a real SQLite schema.
Publisher/network behavior stays offline; the only fake is the metrics adapter,
which persists the same normalized ``Metrics`` fact that a real adapter would.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ops.agent_contract import approval_content_digest, plan_digest
from ai_ops.agent_contract.schemas import (
    MAX_STAGE_BODY_BYTES,
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalState,
    AssetInput,
    CollectMetricsRequest,
    MetricsCollectionState,
    PerformanceReviewRequest,
    PlanPublicationRequest,
    RequestApprovalRequest,
    ScheduleRequest,
    StageContentRequest,
)
from ai_ops.agent_contract.service import AgentContractError, AgentControlPlane
from ai_ops.config import (
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
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import (
    Account,
    ApprovalRequest,
    Article,
    Base,
    Metrics,
    PublicationPlan,
    PublishJob,
    Topic,
)
from ai_ops.scheduler.worker import _build_verified_contract_content
from tests.agent_contract_fakes import (
    EXACT_RENDERER_REGISTRY,
    NO_EXACT_RENDERER_REGISTRY,
    ExactZhihuTestPublisher,
)


PLANNED_FOR = datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)


@dataclass(frozen=True)
class _Principal:
    principal_id: str
    principal_type: str
    scopes: frozenset[str]


@dataclass
class _Harness:
    session_factory: sessionmaker[Session]
    service: AgentControlPlane
    agent: _Principal
    human: _Principal
    topic_id: int
    account_id: int
    collector_calls: list[tuple[int, str]]
    import_root: Path
    vault_root: Path


@pytest.fixture
def control_plane(tmp_path) -> _Harness:
    database_path = tmp_path / "agent-control-plane.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        future=True,
    )
    import_root = tmp_path / "agent-import"
    import_root.mkdir()
    vault_root = tmp_path / "agent-vault"
    (import_root / "approved-cover.png").write_bytes(b"approved-cover-bytes")

    with session_factory() as session:
        topic = Topic(
            name="agent-contract-topic",
            target_platforms=[Platform.ZHIHU.value],
        )
        account = Account(
            platform=Platform.ZHIHU,
            nickname="healthy-zhihu",
            health=AccountHealth.HEALTHY,
            profile={
                "external_account_id": "zhihu:id:service-test-account",
                "raw_response": {"private": "account-profile-secret"},
            },
            encrypted_credential=b"encrypted-account-secret",
        )
        session.add_all([topic, account])
        session.commit()
        topic_id = topic.id
        account_id = account.id

    collector_calls: list[tuple[int, str]] = []

    async def persist_observed_metrics(
        job_id: int,
        *,
        source: str,
        agent_operation_id: int,
        agent_operation_lease_token: str,
    ):
        assert len(agent_operation_lease_token) == 64
        collector_calls.append((job_id, source))
        with session_factory() as session:
            session.add(
                Metrics(
                    job_id=job_id,
                    agent_operation_id=agent_operation_id,
                    collected_at=datetime(2031, 5, 6, 8, 0),
                    likes=11,
                    comments=3,
                    shares=2,
                    views=101,
                    source=source,
                    raw={"adapter_secret": "must-not-cross-the-contract"},
                )
            )
            session.commit()
        return {"collected": True}

    harness = _Harness(
        session_factory=session_factory,
        service=AgentControlPlane(
            session_factory=session_factory,
            metrics_collector=persist_observed_metrics,
            asset_import_root=import_root,
            asset_vault_root=vault_root,
            asset_max_bytes=1024,
            publisher_registry=EXACT_RENDERER_REGISTRY,
        ),
        agent=_Principal(
            "writer-agent",
            "agent",
            frozenset(
                {
                    SCOPE_CONTENT_STAGE,
                    SCOPE_PLAN_CREATE,
                    SCOPE_APPROVAL_REQUEST,
                    SCOPE_SCHEDULE_CREATE,
                    SCOPE_JOB_READ,
                    SCOPE_METRICS_COLLECT,
                    SCOPE_PERFORMANCE_READ,
                }
            ),
        ),
        human=_Principal(
            "human-editor",
            "human",
            frozenset({SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE}),
        ),
        topic_id=topic_id,
        account_id=account_id,
        collector_calls=collector_calls,
        import_root=import_root,
        vault_root=vault_root,
    )
    try:
        yield harness
    finally:
        engine.dispose()


def _stage_request(harness: _Harness, *, body: str = "Original approved body"):
    return StageContentRequest(
        topic_id=harness.topic_id,
        title="An agent-native operating model",
        body=body,
        content_type=ContentType.LONG_ARTICLE,
        target_platforms=[Platform.ZHIHU],
        extra={"campaign": "contract-v1", "language": "zh-CN"},
        assets=[
            AssetInput(
                asset_type=AssetType.IMAGE,
                source=AssetSource.AI_GENERATED,
                local_path="approved-cover.png",
                meta={"role": "cover", "alt": "approved cover"},
            )
        ],
    )


def _stage(harness: _Harness, *, key_prefix: str):
    return harness.service.stage_content(
        harness.agent,
        _stage_request(harness),
        idempotency_key=f"{key_prefix}-stage-0001",
    )


def _approved_plan(harness: _Harness, *, key_prefix: str):
    staged = _stage(harness, key_prefix=key_prefix)
    plan = harness.service.plan_publication(
        harness.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=[harness.account_id],
            planned_for=PLANNED_FOR,
        ),
        idempotency_key=f"{key_prefix}-plan-0001",
    )
    approval = harness.service.request_approval(
        harness.agent,
        RequestApprovalRequest(plan_id=plan.plan_id),
        idempotency_key=f"{key_prefix}-request-0001",
    )
    decision = harness.service.decide_approval(
        harness.human,
        approval.approval_id,
        ApprovalDecisionRequest(
            expected_plan_digest=plan.plan_digest,
            decision=ApprovalDecision.APPROVED,
            reason="Reviewed the exact content, target, and execution time.",
        ),
        idempotency_key=f"{key_prefix}-decision-0001",
    )
    return staged, plan, approval, decision


def _pending_plan_without_assets(harness: _Harness, *, key_prefix: str):
    staged = harness.service.stage_content(
        harness.agent,
        _stage_request(harness).model_copy(update={"assets": []}),
        idempotency_key=f"{key_prefix}-stage-0001",
    )
    plan = harness.service.plan_publication(
        harness.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=[harness.account_id],
            planned_for=PLANNED_FOR,
        ),
        idempotency_key=f"{key_prefix}-plan-0001",
    )
    approval = harness.service.request_approval(
        harness.agent,
        RequestApprovalRequest(
            plan_id=plan.plan_id,
            expires_at=PLANNED_FOR + timedelta(days=1),
        ),
        idempotency_key=f"{key_prefix}-request-0001",
    )
    return staged, plan, approval


def test_stage_preflights_every_source_before_creating_any_vault_file(control_plane):
    request = _stage_request(control_plane).model_copy(
        update={
            "assets": [
                AssetInput(
                    asset_type=AssetType.IMAGE,
                    local_path="approved-cover.png",
                ),
                AssetInput(
                    asset_type=AssetType.IMAGE,
                    local_path="missing-late-in-request.png",
                ),
            ]
        }
    )

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.stage_content(
            control_plane.agent,
            request,
            idempotency_key="preflight-late-invalid-0001",
        )

    assert raised.value.code == "asset_source_rejected"
    assert not control_plane.vault_root.exists()


def test_stage_rejects_aggregate_preflight_before_creating_the_vault(control_plane):
    (control_plane.import_root / "first.bin").write_bytes(b"123456")
    (control_plane.import_root / "second.bin").write_bytes(b"abcdef")
    control_plane.service._asset_max_total_bytes = 10
    request = _stage_request(control_plane).model_copy(
        update={
            "assets": [
                AssetInput(asset_type=AssetType.IMAGE, local_path="first.bin"),
                AssetInput(asset_type=AssetType.IMAGE, local_path="second.bin"),
            ]
        }
    )

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.stage_content(
            control_plane.agent,
            request,
            idempotency_key="preflight-aggregate-0001",
        )

    assert raised.value.code == "asset_too_large"
    assert not control_plane.vault_root.exists()


def test_stage_applies_the_remaining_aggregate_quota_to_each_import(
    control_plane,
    monkeypatch,
):
    from ai_ops.agent_contract import service as service_module

    (control_plane.import_root / "first.bin").write_bytes(b"123456")
    (control_plane.import_root / "second.bin").write_bytes(b"abcd")
    control_plane.service._asset_max_total_bytes = 10
    actual_limits: list[int] = []
    real_import = service_module.import_asset_to_vault

    def recording_import(source, *, import_root, vault_root, max_bytes):
        actual_limits.append(max_bytes)
        return real_import(
            source,
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(service_module, "import_asset_to_vault", recording_import)
    request = _stage_request(control_plane).model_copy(
        update={
            "assets": [
                AssetInput(asset_type=AssetType.IMAGE, local_path="first.bin"),
                AssetInput(asset_type=AssetType.IMAGE, local_path="second.bin"),
            ]
        }
    )

    staged = control_plane.service.stage_content(
        control_plane.agent,
        request,
        idempotency_key="remaining-aggregate-quota-0001",
    )

    assert staged.content_id > 0
    assert actual_limits == [10, 4]


def test_service_never_treats_an_unchecked_empty_target_list_as_all_accounts(control_plane):
    staged = _stage(control_plane, key_prefix="empty-target-defense")
    unchecked = PlanPublicationRequest.model_construct(
        content_id=staged.content_id,
        account_ids=[],
        planned_for=PLANNED_FOR,
    )

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.plan_publication(
            control_plane.agent,
            unchecked,
            idempotency_key="empty-target-defense-plan-0001",
        )

    assert raised.value.code == "target_accounts_required"
    assert raised.value.status_code == 400
    with control_plane.session_factory() as session:
        assert session.scalar(select(func.count(PublicationPlan.id))) == 0


def test_service_bounds_an_unchecked_target_list_before_querying_accounts(control_plane):
    staged = _stage(control_plane, key_prefix="target-limit-defense")
    unchecked = PlanPublicationRequest.model_construct(
        content_id=staged.content_id,
        account_ids=list(range(1, 18)),
        planned_for=PLANNED_FOR,
    )

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.plan_publication(
            control_plane.agent,
            unchecked,
            idempotency_key="target-limit-defense-plan-0001",
        )

    assert raised.value.code == "target_account_selection_invalid"
    assert raised.value.status_code == 400


@pytest.mark.parametrize(
    ("body", "extra"),
    [
        ("x" * (MAX_STAGE_BODY_BYTES + 1), {}),
        ("bounded body", {"payload": "x" * (64 * 1024)}),
    ],
)
def test_plan_rejects_a_legacy_draft_outside_stage_snapshot_bounds(
    control_plane,
    body,
    extra,
):
    with control_plane.session_factory() as session:
        legacy = Article(
            topic_id=control_plane.topic_id,
            title="Legacy unbounded draft",
            body=body,
            content_type=ContentType.LONG_ARTICLE,
            status=ArticleStatus.DRAFT,
            target_platforms=[Platform.ZHIHU.value],
            target_account_ids=[],
            extra=extra,
        )
        session.add(legacy)
        session.commit()
        content_id = legacy.id

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.plan_publication(
            control_plane.agent,
            PlanPublicationRequest(
                content_id=content_id,
                account_ids=[control_plane.account_id],
                planned_for=PLANNED_FOR,
            ),
            idempotency_key=f"legacy-bounds-plan-{content_id:04d}",
        )

    assert raised.value.code == "content_snapshot_invalid"
    assert raised.value.status_code == 409
    with control_plane.session_factory() as session:
        assert session.scalar(select(func.count(PublicationPlan.id))) == 0


@pytest.mark.asyncio
async def test_full_agent_control_plane_workflow_is_durable_and_redacted(control_plane):
    staged, plan, approval, decision = _approved_plan(
        control_plane,
        key_prefix="happy",
    )

    assert staged.state.value == "draft"
    assert plan.content_digest == staged.content_digest
    assert plan.targets[0].account_id == control_plane.account_id
    assert plan.targets[0].approved_external_account_id == ("zhihu:id:service-test-account")
    execution = plan.targets[0].execution
    assert execution.renderer.renderer_id == "tests.zhihu.exact-payload"
    assert execution.renderer.publisher_kind.value == "zhihu_cli"
    assert execution.renderer.contract_version == "1"
    assert execution.renderer.adapter_version == "test-1"
    assert execution.payload["image_slots"] == [{"asset_type": "image", "index": 0}]
    assert len(execution.payload_digest) == 64
    assert str(control_plane.vault_root) not in execution.model_dump_json()
    assert approval.state is ApprovalState.PENDING
    assert approval.plan_digest == plan.plan_digest
    assert decision.state is ApprovalState.APPROVED
    assert decision.plan_digest == plan.plan_digest

    scheduled = control_plane.service.schedule(
        control_plane.agent,
        ScheduleRequest(plan_id=plan.plan_id),
        idempotency_key="happy-schedule-0001",
    )
    assert scheduled.plan_digest == plan.plan_digest
    assert len(scheduled.job_ids) == 1
    job_id = scheduled.job_ids[0]

    with control_plane.session_factory() as session:
        job = session.get(PublishJob, job_id)
        assert job is not None
        job.status = JobStatus.SUCCESS
        job.attempts = 1
        job.started_at = datetime(2031, 5, 6, 7, 9)
        job.finished_at = datetime(2031, 5, 6, 7, 10)
        job.publisher_kind = "zhihu_cli"
        job.platform_post_id = "answer-123"
        job.platform_url = "https://www.zhihu.com/question/1/answer/123"
        job.raw_response = {
            "credential": "publisher-cookie",
            "raw_response": {"private": "adapter payload"},
        }
        session.commit()

    status = control_plane.service.get_job_status(control_plane.agent, job_id)
    status_payload = status.model_dump(mode="json")
    assert status.state is JobStatus.SUCCESS
    assert status.publisher_id == "zhihu_cli"
    assert status.post_identity is not None
    assert status.post_identity.platform_post_id == "answer-123"
    assert "raw_response" not in status_payload
    assert "publisher-cookie" not in json.dumps(status_payload)

    collected = await control_plane.service.collect_metrics(
        control_plane.agent,
        CollectMetricsRequest(job_id=job_id),
        idempotency_key="happy-metrics-0001",
    )
    assert collected.state is MetricsCollectionState.COLLECTED
    assert collected.metrics is not None
    assert collected.metrics.likes == 11
    assert collected.metrics.views == 101
    assert control_plane.collector_calls == [(job_id, "manual")]
    assert "adapter_secret" not in collected.model_dump_json()

    review = control_plane.service.review_performance(
        control_plane.agent,
        PerformanceReviewRequest(job_ids=[job_id]),
    )
    assert review.totals.jobs_reviewed == 1
    assert review.totals.jobs_with_metrics == 1
    assert review.totals.likes == 11
    assert review.totals.comments == 3
    assert review.totals.shares == 2
    assert review.totals.views == 101
    assert review.findings == []
    assert review.items[0].metrics == collected.metrics
    assert "adapter_secret" not in review.model_dump_json()


@pytest.mark.parametrize(
    ("signal", "job_state", "expected_code", "expected_uncertain"),
    [
        ("effect_applied", JobStatus.FAILED, "partial_effect", False),
        ("needs_reconciliation", JobStatus.DEAD, "partial_effect", False),
        ("reconciliation_required", JobStatus.FAILED, "partial_effect", False),
        ("outcome_uncertain", JobStatus.FAILED, "platform_outcome_uncertain", True),
        ("effect_applied", JobStatus.SUCCESS, "platform_outcome_uncertain", False),
    ],
)
def test_job_status_projects_all_reconciliation_signals_without_raw_details(
    control_plane,
    signal,
    job_state,
    expected_code,
    expected_uncertain,
):
    with control_plane.session_factory() as session:
        article = Article(
            topic_id=control_plane.topic_id,
            title="status projection",
            body="safe body",
            content_type=ContentType.LONG_ARTICLE,
            status=(
                ArticleStatus.PUBLISHED if job_state is JobStatus.SUCCESS else ArticleStatus.FAILED
            ),
            target_platforms=[Platform.ZHIHU.value],
            target_account_ids=[control_plane.account_id],
            extra={},
        )
        session.add(article)
        session.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=control_plane.account_id,
            platform=Platform.ZHIHU,
            status=job_state,
            attempts=1,
            max_attempts=3,
            raw_response={signal: True, "adapter_secret": "must-not-leak"},
        )
        session.add(job)
        session.commit()
        job_id = job.id

    status = control_plane.service.get_job_status(control_plane.agent, job_id)

    assert status.reconciliation_required is True
    assert status.outcome_uncertain is expected_uncertain
    assert status.error_code == expected_code
    if expected_code == "partial_effect":
        assert status.error_message == (
            "A platform-side effect may have occurred; human readback is required"
        )
    assert "adapter_secret" not in status.model_dump_json()
    assert "must-not-leak" not in status.model_dump_json()


def test_agent_cannot_self_sign_its_own_approval(control_plane):
    staged = _stage(control_plane, key_prefix="self-sign")
    plan = control_plane.service.plan_publication(
        control_plane.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=[control_plane.account_id],
            planned_for=PLANNED_FOR,
        ),
        idempotency_key="self-sign-plan-0001",
    )
    approval = control_plane.service.request_approval(
        control_plane.agent,
        RequestApprovalRequest(plan_id=plan.plan_id),
        idempotency_key="self-sign-request-0001",
    )

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.decide_approval(
            control_plane.agent,
            approval.approval_id,
            ApprovalDecisionRequest(
                expected_plan_digest=plan.plan_digest,
                decision=ApprovalDecision.APPROVED,
            ),
            idempotency_key="self-sign-decision-0001",
        )

    assert raised.value.code == "insufficient_scope"
    assert raised.value.status_code == 403
    with control_plane.session_factory() as session:
        persisted = session.get(ApprovalRequest, int(approval.approval_id))
        assert persisted is not None
        assert persisted.status == "pending"
        assert persisted.decided_by is None


def test_human_reads_exact_credential_free_subject_before_deciding(control_plane):
    staged, plan, approval = _pending_plan_without_assets(
        control_plane,
        key_prefix="review",
    )

    review = control_plane.service.get_approval(
        control_plane.human,
        approval.approval_id,
    )
    payload = review.model_dump(mode="json")

    assert review.approval_id == approval.approval_id
    assert review.plan_id == plan.plan_id
    assert review.plan_digest == plan.plan_digest
    assert review.content_digest == plan.content_digest
    assert review.content.content_id == staged.content_id
    assert review.content.title == "An agent-native operating model"
    assert review.content.body == "Original approved body"
    assert review.content.assets == []
    assert review.targets[0].account_display == "healthy-zhihu"
    assert review.targets[0].approved_external_account_id == ("zhihu:id:service-test-account")
    assert review.targets[0].execution == plan.targets[0].execution
    assert review.targets[0].execution.payload == {
        "action": "publish-test-article",
        "title": "An agent-native operating model",
        "body": "Original approved body",
        "content_type": "long_article",
        "extra": {"campaign": "contract-v1", "language": "zh-CN"},
        "image_slots": [],
    }
    assert review.planned_for == PLANNED_FOR
    assert approval_content_digest(review.content) == review.content_digest
    assert (
        plan_digest(
            content_digest=review.content_digest,
            targets=review.targets,
            planned_for=review.planned_for,
        )
        == review.plan_digest
    )
    serialized = json.dumps(payload)
    assert "encrypted-account-secret" not in serialized
    assert "account-profile-secret" not in serialized
    assert "raw_response" not in serialized
    assert "local_path" not in serialized

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.get_approval(
            control_plane.agent,
            approval.approval_id,
        )
    assert raised.value.code == "insufficient_scope"
    assert raised.value.status_code == 403


def test_plan_requires_a_canonical_stable_zhihu_account_identity(control_plane):
    with control_plane.session_factory() as session:
        account = session.get(Account, control_plane.account_id)
        assert account is not None
        account.profile = {"external_account_id": "mutable-url-token-only"}
        session.commit()

    staged = _stage(control_plane, key_prefix="missing-external-identity")
    with pytest.raises(AgentContractError) as raised:
        control_plane.service.plan_publication(
            control_plane.agent,
            PlanPublicationRequest(
                content_id=staged.content_id,
                account_ids=[control_plane.account_id],
                planned_for=PLANNED_FOR,
            ),
            idempotency_key="missing-external-identity-plan-0001",
        )

    assert raised.value.code == "target_external_account_identity_missing"
    assert raised.value.status_code == 409


def test_plan_fails_closed_without_an_exact_renderer(control_plane):
    staged = _stage(control_plane, key_prefix="renderer-unavailable")
    service_without_renderer = AgentControlPlane(
        session_factory=control_plane.session_factory,
        asset_import_root=control_plane.import_root,
        asset_vault_root=control_plane.vault_root,
        asset_max_bytes=1024,
        publisher_registry=NO_EXACT_RENDERER_REGISTRY,
    )

    with pytest.raises(AgentContractError) as raised:
        service_without_renderer.plan_publication(
            control_plane.agent,
            PlanPublicationRequest(
                content_id=staged.content_id,
                account_ids=[control_plane.account_id],
                planned_for=PLANNED_FOR,
            ),
            idempotency_key="renderer-unavailable-plan-0001",
        )

    assert raised.value.code == "exact_renderer_unavailable"
    assert raised.value.status_code == 409
    with control_plane.session_factory() as session:
        assert session.scalar(select(func.count(PublicationPlan.id))) == 0


def test_plan_rejects_a_renderer_whose_runtime_identity_drifts(control_plane):
    class _DriftedPublisher(ExactZhihuTestPublisher):
        def agent_contract_digest_material(self, content):
            material = super().agent_contract_digest_material(content)
            material["renderer"]["renderer_id"] = "tests.drifted-renderer"
            return material

    class _DriftedRegistry:
        @staticmethod
        def resolve(platform):
            return [_DriftedPublisher()] if platform is Platform.ZHIHU else []

    staged = _stage(control_plane, key_prefix="renderer-drift")
    service = AgentControlPlane(
        session_factory=control_plane.session_factory,
        asset_import_root=control_plane.import_root,
        asset_vault_root=control_plane.vault_root,
        asset_max_bytes=1024,
        publisher_registry=_DriftedRegistry(),
    )

    with pytest.raises(AgentContractError) as raised:
        service.plan_publication(
            control_plane.agent,
            PlanPublicationRequest(
                content_id=staged.content_id,
                account_ids=[control_plane.account_id],
                planned_for=PLANNED_FOR,
            ),
            idempotency_key="renderer-drift-plan-0001",
        )

    assert raised.value.code == "renderer_contract_invalid"
    assert raised.value.status_code == 503


def test_human_can_resolve_the_exact_asset_bytes_in_the_review_bundle(control_plane):
    staged = _stage(control_plane, key_prefix="review-asset")
    plan = control_plane.service.plan_publication(
        control_plane.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=[control_plane.account_id],
            planned_for=PLANNED_FOR,
        ),
        idempotency_key="review-asset-plan-0001",
    )
    approval = control_plane.service.request_approval(
        control_plane.agent,
        RequestApprovalRequest(plan_id=plan.plan_id),
        idempotency_key="review-asset-request-0001",
    )
    review = control_plane.service.get_approval(
        control_plane.human,
        approval.approval_id,
    )
    reviewed_asset = review.content.assets[0]

    resolved = control_plane.service.get_approval_asset(
        control_plane.human,
        approval.approval_id,
        reviewed_asset.asset_id,
    )

    assert resolved.sha256 == reviewed_asset.sha256
    assert resolved.size_bytes == reviewed_asset.size_bytes
    try:
        assert resolved.handle.read() == b"approved-cover-bytes"
    finally:
        resolved.close()
    assert resolved.filename == f"asset-{reviewed_asset.asset_id}.png"
    with pytest.raises(AgentContractError) as missing:
        control_plane.service.get_approval_asset(
            control_plane.human,
            approval.approval_id,
            999_999,
        )
    assert missing.value.code == "approval_asset_not_found"


def test_decision_rejects_wrong_or_stale_review_digest(control_plane):
    staged, plan, approval = _pending_plan_without_assets(
        control_plane,
        key_prefix="stale-review",
    )

    with pytest.raises(AgentContractError) as wrong_digest:
        control_plane.service.decide_approval(
            control_plane.human,
            approval.approval_id,
            ApprovalDecisionRequest(
                expected_plan_digest="c" * 64,
                decision=ApprovalDecision.APPROVED,
            ),
            idempotency_key="stale-review-wrong-digest-0001",
        )
    assert wrong_digest.value.code == "approval_digest_mismatch"
    assert wrong_digest.value.status_code == 409

    with control_plane.session_factory() as session:
        article = session.get(Article, staged.content_id)
        assert article is not None
        article.body = "Changed after the human reviewed the bundle"
        session.commit()

    with pytest.raises(AgentContractError) as stale_subject:
        control_plane.service.decide_approval(
            control_plane.human,
            approval.approval_id,
            ApprovalDecisionRequest(
                expected_plan_digest=plan.plan_digest,
                decision=ApprovalDecision.APPROVED,
            ),
            idempotency_key="stale-review-changed-subject-0001",
        )
    assert stale_subject.value.code == "approval_subject_changed"
    assert stale_subject.value.status_code == 409

    with control_plane.session_factory() as session:
        persisted = session.get(ApprovalRequest, int(approval.approval_id))
        assert persisted is not None
        assert persisted.status == "pending"
        assert persisted.decided_by is None


@pytest.mark.parametrize(
    "tamper",
    ["body", "asset", "vault-bytes", "targets", "account-binding", "planned_for"],
)
def test_schedule_fails_closed_when_approved_snapshot_is_tampered(
    control_plane,
    tamper,
):
    staged, plan, _approval, _decision = _approved_plan(
        control_plane,
        key_prefix=f"tamper-{tamper}",
    )

    with control_plane.session_factory() as session:
        persisted_plan = session.get(PublicationPlan, int(plan.plan_id))
        article = session.get(Article, staged.content_id)
        assert persisted_plan is not None
        assert article is not None
        if tamper == "body":
            article.body = "Body replaced after the human decision"
        elif tamper == "asset":
            assert article.assets
            article.assets[0].meta = {"role": "cover", "alt": "tampered asset"}
        elif tamper == "vault-bytes":
            assert article.assets
            vault_path = Path(article.assets[0].local_path)
            vault_path.chmod(0o600)
            vault_path.write_bytes(b"replaced-after-human-approval")
        elif tamper == "targets":
            persisted_plan.targets = [
                {
                    "account_id": control_plane.account_id,
                    "platform": Platform.YOUTUBE.value,
                }
            ]
        elif tamper == "account-binding":
            account = session.get(Account, control_plane.account_id)
            assert account is not None
            account.encrypted_credential = b"rotated-after-human-approval"
        else:
            persisted_plan.planned_for += timedelta(minutes=5)
        session.commit()

    with pytest.raises(AgentContractError) as raised:
        control_plane.service.schedule(
            control_plane.agent,
            ScheduleRequest(plan_id=plan.plan_id),
            idempotency_key=f"tamper-{tamper}-schedule-0001",
        )

    assert raised.value.code == "approval_subject_changed"
    assert raised.value.status_code == 409
    with control_plane.session_factory() as session:
        job_count = session.scalar(
            select(func.count(PublishJob.id)).where(PublishJob.plan_id == int(plan.plan_id))
        )
        persisted_plan = session.get(PublicationPlan, int(plan.plan_id))
        assert job_count == 0
        assert persisted_plan is not None
        assert persisted_plan.state == "approved"


def test_stage_import_detaches_approval_bytes_from_mutable_source(control_plane):
    staged = _stage(control_plane, key_prefix="source-detached")
    (control_plane.import_root / "approved-cover.png").write_bytes(b"source-file-was-replaced")

    plan = control_plane.service.plan_publication(
        control_plane.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=[control_plane.account_id],
            planned_for=PLANNED_FOR,
        ),
        idempotency_key="source-detached-plan-0001",
    )

    assert plan.content_digest == staged.content_digest
    with control_plane.session_factory() as session:
        article = session.get(Article, staged.content_id)
        assert article is not None
        assert article.assets[0].local_path.startswith(str(control_plane.vault_root))
        assert Path(article.assets[0].local_path).read_bytes() == b"approved-cover-bytes"


def test_worker_uses_scheduled_snapshot_and_rechecks_account_binding(
    control_plane,
    monkeypatch,
):
    staged, plan, _approval, _decision = _approved_plan(
        control_plane,
        key_prefix="worker-snapshot",
    )
    scheduled = control_plane.service.schedule(
        control_plane.agent,
        ScheduleRequest(plan_id=plan.plan_id),
        idempotency_key="worker-snapshot-schedule-0001",
    )

    with control_plane.session_factory() as session:
        article = session.get(Article, staged.content_id)
        assert article is not None
        article.body = "Mutable row changed after durable scheduling"
        session.commit()

    from ai_ops.scheduler import worker

    monkeypatch.setattr(worker.settings, "agent_asset_vault_root", control_plane.vault_root)
    monkeypatch.setattr(worker.settings, "agent_asset_max_bytes", 1024)
    with control_plane.session_factory() as session:
        job = session.get(PublishJob, scheduled.job_ids[0])
        account = session.get(Account, control_plane.account_id)
        assert job is not None
        assert account is not None
        content = _build_verified_contract_content(session, job, account)
        assert content.body == "Original approved body"
        assert content.exact_approval is True
        assert content.approved_external_account_id == "zhihu:id:service-test-account"
        assert len(content.approved_assets) == 1
        assert content.approved_assets[0].storage_path == content.images[0]
        assert content.approved_assets[0].sha256 == Path(content.images[0]).stem
        assert "approved_assets" not in content.model_dump()
        assert "approved_external_account_id" not in content.model_dump()

        assert job.approved_planned_for == PLANNED_FOR.replace(tzinfo=None)
        job.scheduled_at += timedelta(seconds=1)
        session.flush()
        # Retry/backoff owns scheduled_at and must not invalidate the immutable
        # approved plan binding.
        retried_content = _build_verified_contract_content(session, job, account)
        assert retried_content.body == "Original approved body"

        job.approved_planned_for += timedelta(seconds=1)
        session.flush()
        with pytest.raises(ValueError, match="plan is not executable"):
            _build_verified_contract_content(session, job, account)
        job.approved_planned_for -= timedelta(seconds=1)

        account.profile = {"credential_generation": "rotated"}
        session.flush()
        with pytest.raises(ValueError, match="target binding changed"):
            _build_verified_contract_content(session, job, account)


def test_worker_defers_asset_byte_verification_to_final_materialization(
    control_plane,
    monkeypatch,
):
    staged, plan, _approval, _decision = _approved_plan(
        control_plane,
        key_prefix="worker-final-asset-verification",
    )
    scheduled = control_plane.service.schedule(
        control_plane.agent,
        ScheduleRequest(plan_id=plan.plan_id),
        idempotency_key="worker-final-asset-verification-schedule-0001",
    )
    with control_plane.session_factory() as session:
        article = session.get(Article, staged.content_id)
        assert article is not None and article.assets
        vault_path = Path(article.assets[0].local_path)
        approved_size = article.assets[0].size_bytes
        vault_path.chmod(0o600)
        vault_path.write_bytes(b"x" * approved_size)

    from ai_ops.scheduler import worker

    monkeypatch.setattr(worker.settings, "agent_asset_vault_root", control_plane.vault_root)
    monkeypatch.setattr(worker.settings, "agent_asset_max_bytes", 1024)
    with control_plane.session_factory() as session:
        job = session.get(PublishJob, scheduled.job_ids[0])
        account = session.get(Account, control_plane.account_id)
        assert job is not None and account is not None
        # Planning metadata/digests remain valid without an early byte pass.
        content = _build_verified_contract_content(session, job, account)

    with pytest.raises(worker.ExactAssetMaterializationError):
        with worker._materialized_exact_assets(content):
            pytest.fail("tampered asset reached the Publisher boundary")
    assert not list(control_plane.vault_root.glob(".agent-execution-*"))


@pytest.mark.parametrize(
    ("target_case", "expected_code", "expected_status"),
    [
        ("missing", "target_account_not_found", 404),
        ("wrong-platform", "target_platform_mismatch", 409),
        ("blocked", "target_account_unavailable", 409),
    ],
)
def test_explicit_invalid_targets_are_rejected(
    control_plane,
    target_case,
    expected_code,
    expected_status,
):
    if target_case == "missing":
        target_account_id = 999_999
    else:
        with control_plane.session_factory() as session:
            account = Account(
                platform=(Platform.YOUTUBE if target_case == "wrong-platform" else Platform.ZHIHU),
                nickname=f"target-{target_case}",
                health=(
                    AccountHealth.HEALTHY
                    if target_case == "wrong-platform"
                    else AccountHealth.BANNED
                ),
            )
            session.add(account)
            session.commit()
            target_account_id = account.id

    staged = _stage(control_plane, key_prefix=f"target-{target_case}")
    with pytest.raises(AgentContractError) as raised:
        control_plane.service.plan_publication(
            control_plane.agent,
            PlanPublicationRequest(
                content_id=staged.content_id,
                account_ids=[target_account_id],
                planned_for=PLANNED_FOR,
            ),
            idempotency_key=f"target-{target_case}-plan-0001",
        )

    assert raised.value.code == expected_code
    assert raised.value.status_code == expected_status
    with control_plane.session_factory() as session:
        assert session.scalar(select(func.count(PublicationPlan.id))) == 0
