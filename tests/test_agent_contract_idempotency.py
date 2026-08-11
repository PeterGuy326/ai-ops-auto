"""Durable idempotency tests for Agent control-plane mutations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_ops.agent_contract.schemas import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    CollectMetricsRequest,
    CollectMetricsResponse,
    MetricsCollectionState,
    PlanPublicationRequest,
    RequestApprovalRequest,
    ScheduleRequest,
    StageContentRequest,
)
from ai_ops.agent_contract.service import AgentContractError, AgentControlPlane
from ai_ops.config import (
    SCOPE_APPROVAL_DECIDE,
    SCOPE_APPROVAL_REQUEST,
    SCOPE_CONTENT_STAGE,
    SCOPE_JOB_READ,
    SCOPE_METRICS_COLLECT,
    SCOPE_PERFORMANCE_READ,
    SCOPE_PLAN_CREATE,
    SCOPE_SCHEDULE_CREATE,
)
from ai_ops.core.enums import AccountHealth, ArticleStatus, ContentType, Platform
from ai_ops.core.models import (
    Account,
    AgentOperation,
    Article,
    Base,
    Metrics,
    PublicationPlan,
    PublishJob,
    Topic,
)
from tests.agent_contract_fakes import EXACT_RENDERER_REGISTRY


@dataclass(frozen=True)
class _Principal:
    principal_id: str
    principal_type: str
    scopes: frozenset[str]


@dataclass
class _Fixture:
    session_factory: sessionmaker[Session]
    service: AgentControlPlane
    agent: _Principal
    human: _Principal
    topic_id: int
    account_ids: tuple[int, int]


@pytest.fixture
def idempotency_fixture(tmp_path) -> _Fixture:
    database_path = tmp_path / "agent-idempotency.sqlite3"
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

    with session_factory() as session:
        topic = Topic(name="idempotency-topic")
        accounts = [
            Account(
                platform=Platform.ZHIHU,
                nickname=f"zhihu-{number}",
                health=AccountHealth.HEALTHY,
                profile={"external_account_id": f"zhihu:id:idempotency-{number}"},
            )
            for number in (1, 2)
        ]
        session.add_all([topic, *accounts])
        session.commit()
        topic_id = topic.id
        account_ids = (accounts[0].id, accounts[1].id)

    fixture = _Fixture(
        session_factory=session_factory,
        service=AgentControlPlane(
            session_factory=session_factory,
            publisher_registry=EXACT_RENDERER_REGISTRY,
        ),
        agent=_Principal(
            "idempotent-agent",
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
            "independent-human",
            "human",
            frozenset({SCOPE_APPROVAL_DECIDE}),
        ),
        topic_id=topic_id,
        account_ids=account_ids,
    )
    try:
        yield fixture
    finally:
        engine.dispose()


def _stage_request(fixture: _Fixture, *, body: str = "Stable request body"):
    return StageContentRequest(
        topic_id=fixture.topic_id,
        title="Idempotent publishing",
        body=body,
        content_type=ContentType.LONG_ARTICLE,
        target_platforms=[Platform.ZHIHU],
        extra={"purpose": "idempotency-regression"},
    )


def _create_approved_plan(fixture: _Fixture):
    staged = fixture.service.stage_content(
        fixture.agent,
        _stage_request(fixture),
        idempotency_key="schedule-once-stage-0001",
    )
    plan = fixture.service.plan_publication(
        fixture.agent,
        PlanPublicationRequest(
            content_id=staged.content_id,
            account_ids=list(fixture.account_ids),
            planned_for=datetime(2032, 1, 2, 3, 4, tzinfo=UTC),
        ),
        idempotency_key="schedule-once-plan-0001",
    )
    approval = fixture.service.request_approval(
        fixture.agent,
        RequestApprovalRequest(plan_id=plan.plan_id),
        idempotency_key="schedule-once-request-0001",
    )
    fixture.service.decide_approval(
        fixture.human,
        approval.approval_id,
        ApprovalDecisionRequest(
            expected_plan_digest=plan.plan_digest,
            decision=ApprovalDecision.APPROVED,
            reason="Independent human review complete.",
        ),
        idempotency_key="schedule-once-decision-0001",
    )
    return plan


def test_same_key_same_request_replays_and_different_request_conflicts(
    idempotency_fixture,
):
    request = _stage_request(idempotency_fixture)
    first = idempotency_fixture.service.stage_content(
        idempotency_fixture.agent,
        request,
        idempotency_key="durable-stage-key-0001",
    )

    # A new service instance proves that replay comes from the durable ledger,
    # not an in-process cache.
    restarted_service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    replay = restarted_service.stage_content(
        idempotency_fixture.agent,
        request,
        idempotency_key="durable-stage-key-0001",
    )

    assert replay == first
    with idempotency_fixture.session_factory() as session:
        assert session.scalar(select(func.count(Article.id))) == 1
        assert (
            session.scalar(
                select(func.count(AgentOperation.id)).where(
                    AgentOperation.operation == "stage_content"
                )
            )
            == 1
        )

    changed_request = _stage_request(
        idempotency_fixture,
        body="Different bytes must never inherit the first result",
    )
    with pytest.raises(AgentContractError) as raised:
        restarted_service.stage_content(
            idempotency_fixture.agent,
            changed_request,
            idempotency_key="durable-stage-key-0001",
        )

    assert raised.value.code == "idempotency_key_reused"
    assert raised.value.status_code == 409
    with idempotency_fixture.session_factory() as session:
        assert session.scalar(select(func.count(Article.id))) == 1
        assert (
            session.scalar(
                select(func.count(AgentOperation.id)).where(
                    AgentOperation.operation == "stage_content"
                )
            )
            == 1
        )


def test_same_plan_with_different_schedule_keys_never_duplicates_jobs(
    idempotency_fixture,
):
    plan = _create_approved_plan(idempotency_fixture)
    request = ScheduleRequest(plan_id=plan.plan_id)

    first = idempotency_fixture.service.schedule(
        idempotency_fixture.agent,
        request,
        idempotency_key="schedule-key-alpha-0001",
    )
    second = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    ).schedule(
        idempotency_fixture.agent,
        request,
        idempotency_key="schedule-key-beta-0001",
    )

    assert second == first
    assert len(first.job_ids) == len(idempotency_fixture.account_ids)
    assert plan.targets[0].execution == plan.targets[1].execution
    assert plan.targets[0].execution.renderer.renderer_id == "tests.zhihu.exact-payload"
    with idempotency_fixture.session_factory() as session:
        assert session.scalar(
            select(func.count(PublishJob.id)).where(PublishJob.plan_id == int(plan.plan_id))
        ) == len(idempotency_fixture.account_ids)
        persisted_plan = session.get(PublicationPlan, int(plan.plan_id))
        assert persisted_plan is not None
        assert persisted_plan.state == "scheduled"
        assert (
            session.scalar(
                select(func.count(AgentOperation.id)).where(AgentOperation.operation == "schedule")
            )
            == 2
        )


def test_schedule_rolls_back_when_content_claim_loses_concurrent_race(
    idempotency_fixture,
    monkeypatch,
):
    plan = _create_approved_plan(idempotency_fixture)
    original_execute = Session.execute

    def lose_article_claim(session, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if getattr(statement, "is_update", False) and getattr(table, "name", None) == "articles":
            # Model the rowcount produced when another approved plan claimed
            # this content after our initial status read but before our CAS.
            return SimpleNamespace(rowcount=0)
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", lose_article_claim)

    with pytest.raises(AgentContractError) as raised:
        idempotency_fixture.service.schedule(
            idempotency_fixture.agent,
            ScheduleRequest(plan_id=plan.plan_id),
            idempotency_key="schedule-content-race-0001",
        )

    assert raised.value.code == "schedule_conflict"
    assert raised.value.status_code == 409
    with idempotency_fixture.session_factory() as session:
        persisted_plan = session.get(PublicationPlan, int(plan.plan_id))
        article = session.get(Article, persisted_plan.article_id)
        assert persisted_plan.state == "approved"
        assert article.status == ArticleStatus.DRAFT
        assert session.scalar(select(func.count(PublishJob.id))) == 0
        assert (
            session.scalar(
                select(func.count(AgentOperation.id)).where(AgentOperation.operation == "schedule")
            )
            == 0
        )


def _schedule_job_for_metrics(fixture: _Fixture, *, key: str) -> int:
    plan = _create_approved_plan(fixture)
    scheduled = fixture.service.schedule(
        fixture.agent,
        ScheduleRequest(plan_id=plan.plan_id),
        idempotency_key=key,
    )
    return scheduled.job_ids[0]


@pytest.mark.asyncio
async def test_cancelled_metrics_claim_is_recoverable_with_the_same_key(
    idempotency_fixture,
):
    job_id = _schedule_job_for_metrics(
        idempotency_fixture,
        key="cancelled-metrics-schedule-0001",
    )
    collector_calls = 0

    async def cancel_then_collect(
        requested_job_id,
        *,
        source,
        agent_operation_id,
        agent_operation_lease_token,
    ):
        assert len(agent_operation_lease_token) == 64
        nonlocal collector_calls
        collector_calls += 1
        if collector_calls == 1:
            raise asyncio.CancelledError
        with idempotency_fixture.session_factory() as session:
            session.add(
                Metrics(
                    job_id=requested_job_id,
                    agent_operation_id=agent_operation_id,
                    likes=3,
                    views=30,
                    source=source,
                    raw={},
                )
            )
            session.commit()
        return {"collected": True}

    service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        metrics_collector=cancel_then_collect,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    request = CollectMetricsRequest(job_id=job_id)

    with pytest.raises(asyncio.CancelledError):
        await service.collect_metrics(
            idempotency_fixture.agent,
            request,
            idempotency_key="cancelled-metrics-key-0001",
        )

    recovered = await service.collect_metrics(
        idempotency_fixture.agent,
        request,
        idempotency_key="cancelled-metrics-key-0001",
    )
    replay = await service.collect_metrics(
        idempotency_fixture.agent,
        request,
        idempotency_key="cancelled-metrics-key-0001",
    )

    assert recovered.state is MetricsCollectionState.COLLECTED
    assert recovered.metrics is not None and recovered.metrics.views == 30
    assert replay == recovered
    assert collector_calls == 2
    with idempotency_fixture.session_factory() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.operation == "collect_metrics",
                AgentOperation.idempotency_key == "cancelled-metrics-key-0001",
            )
        )
        assert operation.response_json is not None
        assert operation.lease_token is None
        assert operation.lease_expires_at is None


@pytest.mark.asyncio
async def test_metrics_retry_reuses_snapshot_after_response_finalization_failure(
    idempotency_fixture,
    monkeypatch,
):
    job_id = _schedule_job_for_metrics(
        idempotency_fixture,
        key="finalize-failure-schedule-0001",
    )
    collector_calls = 0

    async def collect_once(
        requested_job_id,
        *,
        source,
        agent_operation_id,
        agent_operation_lease_token,
    ):
        assert len(agent_operation_lease_token) == 64
        nonlocal collector_calls
        collector_calls += 1
        with idempotency_fixture.session_factory() as session:
            session.add(
                Metrics(
                    job_id=requested_job_id,
                    agent_operation_id=agent_operation_id,
                    likes=7,
                    views=70,
                    source=source,
                    raw={},
                )
            )
            session.commit()
        return {"collected": True}

    service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        metrics_collector=collect_once,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    original_finish = service._finish_external_operation
    finalize_calls = 0

    def fail_first_finalize(**kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("simulated response finalization outage")
        return original_finish(**kwargs)

    monkeypatch.setattr(service, "_finish_external_operation", fail_first_finalize)
    request = CollectMetricsRequest(job_id=job_id)

    with pytest.raises(RuntimeError, match="finalization outage"):
        await service.collect_metrics(
            idempotency_fixture.agent,
            request,
            idempotency_key="finalize-failure-metrics-0001",
        )

    recovered = await service.collect_metrics(
        idempotency_fixture.agent,
        request,
        idempotency_key="finalize-failure-metrics-0001",
    )

    assert recovered.state is MetricsCollectionState.COLLECTED
    assert recovered.metrics is not None and recovered.metrics.views == 70
    assert collector_calls == 1
    with idempotency_fixture.session_factory() as session:
        assert session.scalar(select(func.count(Metrics.id)).where(Metrics.job_id == job_id)) == 1


@pytest.mark.asyncio
async def test_stale_metrics_lease_is_reclaimed_after_process_loss(
    idempotency_fixture,
):
    job_id = _schedule_job_for_metrics(
        idempotency_fixture,
        key="stale-lease-schedule-0001",
    )

    async def persist_metrics(
        requested_job_id,
        *,
        source,
        agent_operation_id,
        agent_operation_lease_token,
    ):
        assert len(agent_operation_lease_token) == 64
        with idempotency_fixture.session_factory() as session:
            session.add(
                Metrics(
                    job_id=requested_job_id,
                    agent_operation_id=agent_operation_id,
                    views=90,
                    source=source,
                    raw={},
                )
            )
            session.commit()
        return {"collected": True}

    service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        metrics_collector=persist_metrics,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    request = CollectMetricsRequest(job_id=job_id)
    claim = service._claim_external_operation(
        principal=idempotency_fixture.agent,
        operation="collect_metrics",
        idempotency_key="stale-lease-metrics-0001",
        request=request,
        response_type=CollectMetricsResponse,
    )
    with idempotency_fixture.session_factory() as session:
        operation = session.get(AgentOperation, claim.operation_id)
        operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()

    with pytest.raises(AgentContractError) as expired_finish:
        service._finish_external_operation(
            principal=idempotency_fixture.agent,
            operation="collect_metrics",
            idempotency_key="stale-lease-metrics-0001",
            claim=claim,
            response=CollectMetricsResponse(
                job_id=job_id,
                state=MetricsCollectionState.UNAVAILABLE,
                reason="stale owner must not finalize",
            ),
        )
    assert expired_finish.value.code == "operation_conflict"

    recovered = await service.collect_metrics(
        idempotency_fixture.agent,
        request,
        idempotency_key="stale-lease-metrics-0001",
    )

    assert recovered.state is MetricsCollectionState.COLLECTED
    assert recovered.metrics is not None and recovered.metrics.views == 90


@pytest.mark.asyncio
async def test_reclaimed_metrics_owner_prefers_snapshot_from_stale_owner_race(
    idempotency_fixture,
):
    job_id = _schedule_job_for_metrics(
        idempotency_fixture,
        key="overlap-race-schedule-0001",
    )
    request = CollectMetricsRequest(job_id=job_id)
    key = "overlap-race-metrics-0001"
    stale_service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    stale_claim = stale_service._claim_external_operation(
        principal=idempotency_fixture.agent,
        operation="collect_metrics",
        idempotency_key=key,
        request=request,
        response_type=CollectMetricsResponse,
    )
    with idempotency_fixture.session_factory() as session:
        operation = session.get(AgentOperation, stale_claim.operation_id)
        operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()

    collector_entered = asyncio.Event()
    allow_collector_insert = asyncio.Event()

    async def overlapping_collector(
        requested_job_id,
        *,
        source,
        agent_operation_id,
        agent_operation_lease_token,
    ):
        assert len(agent_operation_lease_token) == 64
        collector_entered.set()
        await allow_collector_insert.wait()
        with idempotency_fixture.session_factory() as session:
            session.add(
                Metrics(
                    job_id=requested_job_id,
                    agent_operation_id=agent_operation_id,
                    views=222,
                    source=source,
                    raw={},
                )
            )
            session.commit()
        return {"collected": True}

    recovered_service = AgentControlPlane(
        session_factory=idempotency_fixture.session_factory,
        metrics_collector=overlapping_collector,
        publisher_registry=EXACT_RENDERER_REGISTRY,
    )
    recovery = asyncio.create_task(
        recovered_service.collect_metrics(
            idempotency_fixture.agent,
            request,
            idempotency_key=key,
        )
    )
    await asyncio.wait_for(collector_entered.wait(), timeout=2)

    # The expired owner finishes persistence late. The current owner then
    # loses the unique Metrics insert but must adopt this durable snapshot
    # instead of permanently finalizing `unavailable`.
    with idempotency_fixture.session_factory() as session:
        session.add(
            Metrics(
                job_id=job_id,
                agent_operation_id=stale_claim.operation_id,
                views=111,
                source="manual",
                raw={},
            )
        )
        session.commit()
    allow_collector_insert.set()
    recovered = await recovery

    assert recovered.state is MetricsCollectionState.COLLECTED
    assert recovered.metrics is not None and recovered.metrics.views == 111
    with pytest.raises(AgentContractError) as stale_finish:
        stale_service._finish_external_operation(
            principal=idempotency_fixture.agent,
            operation="collect_metrics",
            idempotency_key=key,
            claim=stale_claim,
            response=recovered,
        )
    assert stale_finish.value.code == "operation_conflict"
    replay = await recovered_service.collect_metrics(
        idempotency_fixture.agent,
        request,
        idempotency_key=key,
    )
    assert replay == recovered
