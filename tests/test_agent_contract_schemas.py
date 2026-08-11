"""Offline contracts for the versioned Agent-native DTOs and digests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ai_ops.agent_contract import (
    MAX_CONTRACT_REQUEST_BODY_BYTES,
    MAX_CONTRACT_RESPONSE_BODY_BYTES,
    MAX_RENDERER_PAYLOAD_BYTES,
    MAX_SIGNED_64,
    ApprovalAssetDownloadResponse,
    ApprovalContentSnapshot,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalReviewAsset,
    ApprovalReviewResponse,
    ApprovalReviewTarget,
    ApprovalResponse,
    AssetInput,
    CanonicalizationError,
    CollectMetricsRequest,
    CollectMetricsResponse,
    JobStatusResponse,
    MetricSnapshot,
    PerformanceReviewRequest,
    PerformanceReviewResponse,
    PerformanceTotals,
    PlanPublicationRequest,
    PlanPublicationResponse,
    PublicationTarget,
    RequestApprovalRequest,
    RendererBinding,
    RendererAssetRule,
    RendererContract,
    ScheduleRequest,
    ScheduleResponse,
    StageContentRequest,
    StageContentResponse,
    canonical_json,
    canonical_sha256,
    content_digest,
    plan_digest,
)
from ai_ops.agent_contract.schemas import MAX_ASSET_META_BYTES, MAX_STAGE_BODY_BYTES
from ai_ops.core.enums import (
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
    PublisherKind,
)


UTC_NOW = datetime(2026, 8, 11, 2, 3, 4, 5000, tzinfo=timezone.utc)
DIGEST = "a" * 64


def _execution(platform: Platform) -> RendererBinding:
    publisher_kind = (
        PublisherKind.ZHIHU_CLI if platform == Platform.ZHIHU else PublisherKind.SOCIAL_AUTO_UPLOAD
    )
    renderer = RendererContract(
        renderer_id=f"test.{platform.value}",
        contract_version="1",
        adapter_version="test-1",
        platform=platform,
        publisher_kind=publisher_kind,
        requires_external_account_id=platform == Platform.ZHIHU,
    )
    return RendererBinding.from_projection(
        renderer=renderer,
        payload={"action": "test", "platform": platform.value},
    )


def _top_level_examples():
    metric = MetricSnapshot(
        collected_at=UTC_NOW,
        views=10,
        likes=2,
        source="manual",
    )
    target = PublicationTarget(
        account_id=7,
        platform=Platform.ZHIHU,
        account_binding_digest=DIGEST,
        approved_external_account_id="zhihu:id:schema-test-account",
        execution=_execution(Platform.ZHIHU),
    )
    return [
        (
            StageContentRequest,
            {
                "topic_id": 1,
                "title": "Agent contract",
                "content_type": ContentType.LONG_ARTICLE,
            },
        ),
        (
            StageContentResponse,
            {
                "content_id": 2,
                "state": ArticleStatus.DRAFT,
                "content_digest": DIGEST,
                "created_at": UTC_NOW,
            },
        ),
        (PlanPublicationRequest, {"content_id": 2, "account_ids": [7]}),
        (
            PlanPublicationResponse,
            {
                "plan_id": "plan-1",
                "content_digest": DIGEST,
                "plan_digest": DIGEST,
                "targets": [target],
                "planned_for": UTC_NOW,
            },
        ),
        (RequestApprovalRequest, {"plan_id": "plan-1"}),
        (
            ApprovalResponse,
            {
                "approval_id": "approval-1",
                "plan_id": "plan-1",
                "state": "pending",
                "plan_digest": DIGEST,
                "requested_at": UTC_NOW,
            },
        ),
        (
            ApprovalReviewResponse,
            {
                "approval_id": "approval-1",
                "plan_id": "plan-1",
                "state": "pending",
                "plan_digest": DIGEST,
                "content_digest": DIGEST,
                "content": ApprovalContentSnapshot(
                    content_id=2,
                    title="Agent contract",
                    body="Reviewed body",
                    content_type=ContentType.LONG_ARTICLE,
                ),
                "targets": [
                    ApprovalReviewTarget(
                        account_id=7,
                        platform=Platform.ZHIHU,
                        account_binding_digest=DIGEST,
                        approved_external_account_id="zhihu:id:schema-test-account",
                        execution=target.execution,
                        account_display="review-account",
                    )
                ],
                "planned_for": UTC_NOW,
                "requested_at": UTC_NOW - timedelta(minutes=1),
                "expires_at": UTC_NOW + timedelta(days=1),
            },
        ),
        (
            ApprovalAssetDownloadResponse,
            {
                "approval_id": "approval-1",
                "asset_id": 3,
                "sha256": DIGEST,
                "size_bytes": 42,
            },
        ),
        (
            ApprovalDecisionRequest,
            {"expected_plan_digest": DIGEST, "decision": "approved"},
        ),
        (
            ApprovalDecisionResponse,
            {
                "approval_id": "approval-1",
                "plan_id": "plan-1",
                "state": "approved",
                "plan_digest": DIGEST,
                "decided_at": UTC_NOW,
            },
        ),
        (ScheduleRequest, {"plan_id": "plan-1"}),
        (
            ScheduleResponse,
            {
                "plan_id": "plan-1",
                "plan_digest": DIGEST,
                "job_ids": [10],
                "planned_for": UTC_NOW,
            },
        ),
        (
            JobStatusResponse,
            {
                "job_id": 10,
                "content_id": 2,
                "account_id": 7,
                "platform": "zhihu",
                "state": JobStatus.PENDING,
                "attempts": 0,
                "max_attempts": 3,
            },
        ),
        (CollectMetricsRequest, {"job_id": 10}),
        (
            CollectMetricsResponse,
            {"job_id": 10, "state": "collected", "metrics": metric},
        ),
        (PerformanceReviewRequest, {"job_ids": [10]}),
        (
            PerformanceReviewResponse,
            {
                "review_id": "review-1",
                "reviewed_at": UTC_NOW,
                "totals": PerformanceTotals(
                    jobs_reviewed=0,
                    jobs_with_metrics=0,
                    likes=0,
                    comments=0,
                    shares=0,
                    views=0,
                ),
            },
        ),
    ]


@pytest.mark.parametrize(("model", "values"), _top_level_examples())
def test_top_level_contracts_are_versioned_and_forbid_unknown_fields(model, values):
    instance = model(**values)

    assert instance.schema_version == 1
    assert instance.model_dump(mode="json")["schema_version"] == 1
    with pytest.raises(ValidationError):
        model(**values, schema_version=2)
    with pytest.raises(ValidationError):
        model(**values, unknown_contract_field=True)


def test_stage_content_carries_ordered_assets_but_no_idempotency_key():
    request = StageContentRequest(
        topic_id=1,
        title="一篇文章",
        body="正文",
        content_type="long_article",
        target_platforms=["zhihu"],
        extra={"series": 2},
        assets=[
            AssetInput(
                asset_type=AssetType.IMAGE,
                source=AssetSource.AI_GENERATED,
                local_path="data/assets/cover.png",
                meta={"role": "cover"},
            )
        ],
    )

    payload = request.model_dump(mode="json")
    assert payload["assets"] == [
        {
            "asset_type": "image",
            "source": "ai_generated",
            "local_path": "data/assets/cover.png",
            "meta": {"role": "cover"},
        }
    ]
    assert "idempotency_key" not in payload
    with pytest.raises(ValidationError):
        StageContentRequest(
            topic_id=1,
            title="一篇文章",
            content_type="long_article",
            idempotency_key="body-is-not-the-idempotency-boundary",
        )


def test_contract_responses_do_not_define_credentials_or_raw_adapter_results():
    public_models = [
        StageContentResponse,
        PlanPublicationResponse,
        ApprovalResponse,
        ApprovalReviewResponse,
        ApprovalAssetDownloadResponse,
        ApprovalDecisionResponse,
        ScheduleResponse,
        JobStatusResponse,
        CollectMetricsResponse,
        PerformanceReviewResponse,
    ]
    forbidden = {"credential", "credentials", "credential_plain", "raw_response"}

    for model in public_models:
        assert forbidden.isdisjoint(model.model_fields)


def test_contract_datetimes_normalize_to_utc():
    china_time = datetime(2026, 8, 11, 10, 3, 4, 5000, tzinfo=timezone(timedelta(hours=8)))
    request = PlanPublicationRequest(content_id=1, account_ids=[7], planned_for=china_time)

    assert request.planned_for == UTC_NOW
    assert request.model_dump(mode="json")["planned_for"] == "2026-08-11T02:03:04.005000Z"


def test_content_digest_is_stable_and_binds_all_mutable_content_fields():
    assets = [
        {
            "asset_type": "image",
            "source": "ai_generated",
            "local_path": "data/a.png",
            "meta": {"height": 20, "width": 10},
        },
        {
            "asset_type": "video",
            "source": "user_upload",
            "local_path": "data/b.mp4",
            "meta": {},
        },
    ]
    base = {
        "title": "Title",
        "body": "Body",
        "content_type": ContentType.LONG_ARTICLE,
        "extra": {"b": 2, "a": 1},
        "assets": assets,
    }

    digest = content_digest(**base)

    assert len(digest) == 64
    assert digest == content_digest(**{**base, "extra": {"a": 1, "b": 2}})
    assert digest != content_digest(**{**base, "title": "Changed"})
    assert digest != content_digest(**{**base, "body": "Changed"})
    assert digest != content_digest(**{**base, "extra": {"a": 1, "b": 3}})
    assert digest != content_digest(**{**base, "assets": list(reversed(assets))})
    assert digest != content_digest(
        **{
            **base,
            "assets": [{**assets[0], "meta": {"height": 21}}] + assets[1:],
        }
    )


def test_plan_digest_sorts_targets_and_normalizes_equivalent_instants():
    east_eight = datetime(2026, 8, 11, 10, 3, 4, 5000, tzinfo=timezone(timedelta(hours=8)))
    targets = [
        PublicationTarget(
            account_id=2,
            platform="zhihu",
            account_binding_digest="2" * 64,
            approved_external_account_id="zhihu:id:plan-target-2",
            execution=_execution(Platform.ZHIHU),
        ),
        PublicationTarget(
            account_id=1,
            platform="toutiao",
            account_binding_digest="1" * 64,
            execution=_execution(Platform.TOUTIAO),
        ),
    ]

    first = plan_digest(
        content_digest=DIGEST,
        targets=targets,
        planned_for=east_eight,
    )
    reordered = plan_digest(
        content_digest=DIGEST,
        targets=list(reversed(targets)),
        planned_for=UTC_NOW,
    )

    assert first == reordered
    assert first != plan_digest(
        content_digest="b" * 64,
        targets=targets,
        planned_for=UTC_NOW,
    )
    assert first != plan_digest(
        content_digest=DIGEST,
        targets=targets[:1],
        planned_for=UTC_NOW,
    )
    assert first != plan_digest(
        content_digest=DIGEST,
        targets=targets,
        planned_for=UTC_NOW + timedelta(seconds=1),
    )
    changed_external_identity = targets[0].model_copy(
        update={"approved_external_account_id": "zhihu:id:changed-target"}
    )
    assert first != plan_digest(
        content_digest=DIGEST,
        targets=[changed_external_identity, targets[1]],
        planned_for=UTC_NOW,
    )
    changed_execution = targets[0].model_copy(
        update={
            "execution": RendererBinding.from_projection(
                renderer=targets[0].execution.renderer,
                payload={"action": "changed", "platform": Platform.ZHIHU.value},
            )
        }
    )
    assert first != plan_digest(
        content_digest=DIGEST,
        targets=[changed_execution, targets[1]],
        planned_for=UTC_NOW,
    )


def test_renderer_binding_rejects_digest_or_target_platform_drift():
    execution = _execution(Platform.ZHIHU)
    tampered = execution.model_dump(mode="json")
    tampered["payload"]["action"] = "changed-after-approval"

    with pytest.raises(ValidationError, match="payload_digest"):
        RendererBinding.model_validate(tampered)
    with pytest.raises(ValidationError, match="renderer platform"):
        PublicationTarget(
            account_id=1,
            platform=Platform.YOUTUBE,
            account_binding_digest=DIGEST,
            execution=execution,
        )
    with pytest.raises(ValidationError, match="external account identity"):
        PublicationTarget(
            account_id=1,
            platform=Platform.ZHIHU,
            account_binding_digest=DIGEST,
            execution=execution,
        )


def test_canonical_json_has_stable_keys_and_fails_closed_for_unknown_objects():
    assert canonical_json({"z": 1, "中文": [True, None]}) == '{"z":1,"中文":[true,null]}'
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})
    with pytest.raises(CanonicalizationError):
        canonical_json(object())


def test_approval_decision_requires_reviewed_digest_and_rejection_reason():
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(decision="approved")

    assert (
        ApprovalDecisionRequest(
            expected_plan_digest=DIGEST,
            decision="approved",
        ).reason
        == ""
    )
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(
            expected_plan_digest=DIGEST,
            decision="rejected",
        )
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(
            expected_plan_digest=DIGEST,
            decision="rejected",
            reason="   ",
        )
    assert ApprovalDecisionRequest(
        expected_plan_digest=DIGEST,
        decision="rejected",
        reason="内容需要修改",
    )


def test_review_assets_expose_only_content_addressed_logical_vault_paths():
    asset = ApprovalReviewAsset(
        asset_id=9,
        asset_type="image",
        source="ai_generated",
        vaulted_path=f"vault://sha256/{DIGEST}",
        sha256=DIGEST,
        size_bytes=123,
        storage_suffix=".png",
        meta={"role": "cover"},
    )

    assert asset.model_dump(mode="json") == {
        "asset_id": 9,
        "asset_type": "image",
        "source": "ai_generated",
        "vaulted_path": f"vault://sha256/{DIGEST}",
        "sha256": DIGEST,
        "size_bytes": 123,
        "storage_suffix": ".png",
        "meta": {"role": "cover"},
    }
    assert "local_path" not in ApprovalReviewAsset.model_fields
    with pytest.raises(ValidationError):
        ApprovalReviewAsset(
            asset_id=9,
            asset_type="image",
            source="ai_generated",
            vaulted_path="/Users/operator/private/cover.png",
            sha256=DIGEST,
            size_bytes=123,
            storage_suffix=".png",
        )


def test_metrics_never_turn_missing_evidence_into_zeroes():
    with pytest.raises(ValidationError):
        MetricSnapshot(collected_at=UTC_NOW, source="manual")
    with pytest.raises(ValidationError):
        CollectMetricsResponse(job_id=1, state="collected")
    with pytest.raises(ValidationError):
        CollectMetricsResponse(job_id=1, state="unavailable")
    with pytest.raises(ValidationError):
        CollectMetricsResponse(
            job_id=1,
            state="unavailable",
            reason="collector is offline",
            metrics=MetricSnapshot(collected_at=UTC_NOW, source="manual", views=0),
        )

    unavailable = CollectMetricsResponse(
        job_id=1,
        state="unavailable",
        reason="collector is offline",
    )
    assert unavailable.metrics is None


def test_plan_and_review_reject_duplicate_or_ambiguous_selection():
    with pytest.raises(ValidationError, match="at least 1 item"):
        PlanPublicationRequest(content_id=1, account_ids=[])
    with pytest.raises(ValidationError, match="at most 16 items"):
        PlanPublicationRequest(content_id=1, account_ids=list(range(1, 18)))
    with pytest.raises(ValidationError):
        PlanPublicationRequest(content_id=1, account_ids=[2, 2])
    with pytest.raises(ValidationError):
        PerformanceReviewRequest(job_ids=[1, 1])
    with pytest.raises(ValidationError):
        PerformanceReviewRequest(job_ids=[1], window_start=UTC_NOW)
    with pytest.raises(ValidationError):
        PerformanceReviewRequest(
            job_ids=[1],
            window_start=UTC_NOW,
            window_end=UTC_NOW,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"body": "界" * 349_526},
        {"extra": {"payload": "x" * (64 * 1024)}},
        {"extra": {"items": list(range(1025))}},
    ],
)
def test_stage_content_rejects_oversized_or_high_cardinality_payloads(updates):
    values = {
        "topic_id": 1,
        "title": "Bounded stage request",
        "content_type": "long_article",
        **updates,
    }

    with pytest.raises(ValidationError):
        StageContentRequest(**values)


def test_stage_content_rejects_deep_extra_and_asset_metadata():
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(9):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    with pytest.raises(ValidationError, match="maximum JSON depth"):
        StageContentRequest(
            topic_id=1,
            title="Deep extra",
            content_type="long_article",
            extra=deep,
        )
    with pytest.raises(ValidationError, match="asset meta"):
        AssetInput(
            asset_type="image",
            local_path="cover.jpg",
            meta={"payload": "x" * (16 * 1024)},
        )
    with pytest.raises(ValidationError, match="maximum JSON item count"):
        AssetInput(
            asset_type="image",
            local_path="cover.jpg",
            meta={"items": list(range(256))},
        )


def test_transport_envelope_contains_the_largest_canonical_stage_shape():
    request = StageContentRequest(
        topic_id=1,
        title="Largest canonical request",
        body="b" * MAX_STAGE_BODY_BYTES,
        content_type=ContentType.LONG_ARTICLE,
        target_platforms=[Platform.ZHIHU],
        extra={"payload": "e" * 64_000},
        assets=[
            AssetInput(
                asset_type=AssetType.IMAGE,
                local_path=f"{index}.jpg",
                meta={"payload": "m" * 16_000},
            )
            for index in range(256)
        ],
    )

    encoded = request.model_dump_json().encode("utf-8")

    assert len(encoded) > 4 * 1024 * 1024
    assert len(encoded) <= MAX_CONTRACT_REQUEST_BODY_BYTES


def test_review_snapshot_and_renderer_projection_reuse_stage_bounds():
    with pytest.raises(ValidationError):
        ApprovalContentSnapshot(
            content_id=1,
            title="Too large",
            body="x" * (MAX_STAGE_BODY_BYTES + 1),
            content_type=ContentType.LONG_ARTICLE,
        )
    with pytest.raises(ValidationError, match="asset meta"):
        ApprovalReviewAsset(
            asset_id=1,
            asset_type=AssetType.IMAGE,
            source=AssetSource.AI_GENERATED,
            vaulted_path=f"vault://sha256/{DIGEST}",
            sha256=DIGEST,
            size_bytes=1,
            storage_suffix=".jpg",
            meta={"payload": "x" * MAX_ASSET_META_BYTES},
        )

    renderer = RendererContract(
        renderer_id="bounded.renderer",
        contract_version="1",
        adapter_version="1",
        platform=Platform.ZHIHU,
        publisher_kind=PublisherKind.ZHIHU_CLI,
        requires_external_account_id=True,
    )
    with pytest.raises(ValidationError, match="renderer payload"):
        RendererBinding.from_projection(
            renderer=renderer,
            payload={"body": "x" * MAX_RENDERER_PAYLOAD_BYTES},
        )

    with pytest.raises(ValidationError):
        PerformanceReviewResponse(
            review_id="bounded-review",
            reviewed_at=UTC_NOW,
            totals=PerformanceTotals(
                jobs_reviewed=0,
                jobs_with_metrics=0,
                likes=0,
                comments=0,
                shares=0,
                views=0,
            ),
            findings=["x" * 1001],
        )


def test_contract_integers_have_a_finite_wire_bound():
    with pytest.raises(ValidationError):
        PlanPublicationRequest(content_id=MAX_SIGNED_64 + 1, account_ids=[1])
    with pytest.raises(ValidationError):
        ApprovalReviewAsset(
            asset_id=1,
            asset_type=AssetType.IMAGE,
            source=AssetSource.AI_GENERATED,
            vaulted_path=f"vault://sha256/{DIGEST}",
            sha256=DIGEST,
            size_bytes=MAX_SIGNED_64 + 1,
            storage_suffix=".jpg",
        )
    with pytest.raises(ValidationError):
        MetricSnapshot(
            collected_at=UTC_NOW,
            source="manual",
            views=MAX_SIGNED_64 + 1,
        )
    with pytest.raises(ValidationError):
        RendererAssetRule(
            asset_type=AssetType.IMAGE,
            min_count=0,
            max_count=257,
        )


def test_near_maximum_approval_review_fits_the_shared_response_envelope():
    renderer = RendererContract(
        renderer_id="bounded.renderer",
        contract_version="1",
        adapter_version="1",
        platform=Platform.ZHIHU,
        publisher_kind=PublisherKind.ZHIHU_CLI,
        requires_external_account_id=True,
        asset_rules=[
            RendererAssetRule(
                asset_type=AssetType.IMAGE,
                min_count=0,
                max_count=256,
            )
        ],
    )
    execution = RendererBinding.from_projection(
        renderer=renderer,
        payload={"body": "p" * (MAX_RENDERER_PAYLOAD_BYTES - 11)},
    )
    review = ApprovalReviewResponse(
        approval_id="approval-max",
        plan_id="plan-max",
        state="pending",
        plan_digest=DIGEST,
        content_digest=DIGEST,
        content=ApprovalContentSnapshot(
            content_id=MAX_SIGNED_64,
            title="Maximum bounded review",
            body="\x01" * MAX_STAGE_BODY_BYTES,
            content_type=ContentType.LONG_ARTICLE,
            extra={"payload": "e" * 64_000},
            assets=[
                ApprovalReviewAsset(
                    asset_id=index,
                    asset_type=AssetType.IMAGE,
                    source=AssetSource.AI_GENERATED,
                    vaulted_path=f"vault://sha256/{DIGEST}",
                    sha256=DIGEST,
                    size_bytes=MAX_SIGNED_64,
                    storage_suffix=".jpg",
                    meta={"payload": "m" * 16_000},
                )
                for index in range(1, 257)
            ],
        ),
        targets=[
            ApprovalReviewTarget(
                account_id=index,
                platform=Platform.ZHIHU,
                account_binding_digest=DIGEST,
                approved_external_account_id=f"zhihu:id:max-{index}",
                execution=execution,
                account_display=f"account-{index}",
            )
            for index in range(1, 17)
        ],
        planned_for=UTC_NOW,
        requested_at=UTC_NOW,
    )

    encoded = review.model_dump_json().encode("utf-8")

    assert len(encoded) > 14 * 1024 * 1024
    assert len(encoded) <= MAX_CONTRACT_RESPONSE_BODY_BYTES
