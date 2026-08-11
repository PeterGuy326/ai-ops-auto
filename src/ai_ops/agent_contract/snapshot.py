"""Immutable content snapshots shared by approval and worker execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ..core.enums import AssetSource, AssetType, ContentType
from ..core.schemas import PublishContent
from .assets import (
    AssetTooLargeError,
    AssetVaultConfigurationError,
    VaultedAsset,
    verify_vaulted_asset,
)
from .digest import canonical_sha256
from .schemas import (
    MAX_ASSET_META_BYTES,
    MAX_ASSET_META_DEPTH,
    MAX_ASSET_META_ITEMS,
    MAX_SIGNED_64,
    MAX_STAGE_ASSETS,
    MAX_STAGE_BODY_BYTES,
    MAX_STAGE_EXTRA_BYTES,
    MAX_STAGE_EXTRA_DEPTH,
    MAX_STAGE_EXTRA_ITEMS,
    ApprovalContentSnapshot,
    ApprovalReviewAsset,
    _validate_json_limits,
)


class StoredApprovalAsset(BaseModel):
    """Private persisted asset projection; ``storage_path`` never crosses API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: int = Field(gt=0, le=MAX_SIGNED_64)
    asset_type: AssetType
    source: AssetSource
    storage_path: str = Field(min_length=1, max_length=4096)
    vaulted_path: str = Field(
        pattern=r"^vault://sha256/[0-9a-f]{64}$",
        max_length=128,
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=MAX_SIGNED_64)
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

    @model_validator(mode="after")
    def _logical_uri_matches_digest(self):
        if self.vaulted_path != f"vault://sha256/{self.sha256}":
            raise ValueError("vaulted_path must identify sha256")
        if Path(self.storage_path).name != f"{self.sha256}{self.storage_suffix}":
            raise ValueError("storage_path must match sha256 and storage_suffix")
        return self


class StoredApprovalContent(BaseModel):
    """Private payload stored on PublicationPlan and consumed by its jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: int = Field(gt=0, le=MAX_SIGNED_64)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=MAX_STAGE_BODY_BYTES)
    content_type: ContentType
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    assets: list[StoredApprovalAsset] = Field(default_factory=list, max_length=MAX_STAGE_ASSETS)

    @field_validator("body")
    @classmethod
    def _body_is_bounded(cls, value: str) -> str:
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

    @model_validator(mode="after")
    def _asset_ids_are_unique(self):
        if len({asset.asset_id for asset in self.assets}) != len(self.assets):
            raise ValueError("snapshot asset IDs must be unique")
        return self


def _asset_record(asset: Any) -> VaultedAsset:
    if (
        getattr(asset, "storage_kind", None) != "agent_vault_v1"
        or not isinstance(getattr(asset, "content_sha256", None), str)
        or not isinstance(getattr(asset, "size_bytes", None), int)
    ):
        raise ValueError("content asset is not stored in the Agent vault")
    return VaultedAsset(
        sha256=asset.content_sha256,
        size_bytes=asset.size_bytes,
        vault_path=Path(asset.local_path),
    )


def build_stored_content_snapshot(
    article: Any,
    *,
    vault_root: str | Path,
    max_bytes: int,
    max_total_bytes: int | None = None,
) -> StoredApprovalContent:
    """Build and verify the exact snapshot represented by a mutable Article."""

    snapshot = build_stored_content_metadata_snapshot(article)
    return verify_stored_content_snapshot(
        snapshot,
        vault_root=vault_root,
        max_bytes=max_bytes,
        max_total_bytes=max_total_bytes,
    )


def build_stored_content_metadata_snapshot(article: Any) -> StoredApprovalContent:
    """Build the durable ORM projection without reading every asset byte."""

    assets: list[StoredApprovalAsset] = []
    for asset in sorted(article.assets, key=lambda item: item.id or 0):
        if not isinstance(asset.id, int) or asset.id <= 0:
            raise ValueError("content asset has no stable identifier")
        record = _asset_record(asset)
        storage_suffix = record.vault_path.suffix.lower()
        assets.append(
            StoredApprovalAsset(
                asset_id=asset.id,
                asset_type=asset.asset_type,
                source=asset.source,
                storage_path=str(record.vault_path),
                vaulted_path=f"vault://sha256/{record.sha256}",
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                storage_suffix=storage_suffix,
                meta=asset.meta or {},
            )
        )
    return StoredApprovalContent(
        content_id=article.id,
        title=article.title,
        body=article.body,
        content_type=article.content_type,
        extra=article.extra or {},
        assets=assets,
    )


def parse_stored_content_snapshot(value: Any) -> StoredApprovalContent:
    """Strictly parse one private database JSON snapshot."""

    return StoredApprovalContent.model_validate(value)


def content_snapshot_digest_payload(
    snapshot: StoredApprovalContent | ApprovalContentSnapshot,
) -> dict[str, Any]:
    """Return exactly the safe fields covered by human content approval."""

    return {
        "title": snapshot.title,
        "body": snapshot.body,
        "content_type": snapshot.content_type,
        "extra": snapshot.extra,
        "assets": [
            {
                "asset_type": asset.asset_type,
                "source": asset.source,
                "vaulted_path": asset.vaulted_path,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "storage_suffix": asset.storage_suffix,
                "meta": asset.meta,
            }
            for asset in snapshot.assets
        ],
    }


def approval_content_digest(
    snapshot: StoredApprovalContent | ApprovalContentSnapshot,
) -> str:
    """Digest a private snapshot or its public human-review projection."""

    return canonical_sha256(content_snapshot_digest_payload(snapshot))


def stored_content_digest(snapshot: StoredApprovalContent) -> str:
    """Internal alias for the same digest exposed to human review clients."""

    return approval_content_digest(snapshot)


def verify_stored_content_snapshot(
    snapshot: StoredApprovalContent,
    *,
    vault_root: str | Path,
    max_bytes: int,
    max_total_bytes: int | None = None,
) -> StoredApprovalContent:
    """Re-hash every approved asset before scheduling or external execution."""

    validate_stored_content_total(snapshot, max_total_bytes=max_total_bytes)
    for asset in snapshot.assets:
        verify_vaulted_asset(
            VaultedAsset(
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                vault_path=Path(asset.storage_path),
            ),
            vault_root=vault_root,
            max_bytes=max_bytes,
        )
    return snapshot


def validate_stored_content_total(
    snapshot: StoredApprovalContent,
    *,
    max_total_bytes: int | None,
) -> int:
    """Fail closed before I/O when a plan's aggregate asset budget is excessive."""

    if max_total_bytes is None:
        return sum(asset.size_bytes for asset in snapshot.assets)
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes <= 0
    ):
        raise AssetVaultConfigurationError("asset total size limit is invalid")
    total = sum(asset.size_bytes for asset in snapshot.assets)
    if total > max_total_bytes:
        raise AssetTooLargeError("content assets exceed the configured total size limit")
    return total


def public_content_snapshot(snapshot: StoredApprovalContent) -> ApprovalContentSnapshot:
    """Remove host paths while preserving the complete approved projection."""

    return ApprovalContentSnapshot(
        content_id=snapshot.content_id,
        title=snapshot.title,
        body=snapshot.body,
        content_type=snapshot.content_type,
        extra=snapshot.extra,
        assets=[
            ApprovalReviewAsset(
                asset_id=asset.asset_id,
                asset_type=asset.asset_type,
                source=asset.source,
                vaulted_path=asset.vaulted_path,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                storage_suffix=asset.storage_suffix,
                meta=asset.meta,
            )
            for asset in snapshot.assets
        ],
    )


def publish_content_from_snapshot(snapshot: StoredApprovalContent) -> PublishContent:
    """Build a publisher payload without consulting the mutable Article row."""

    images = [
        asset.storage_path for asset in snapshot.assets if asset.asset_type == AssetType.IMAGE
    ]
    videos = [
        asset.storage_path for asset in snapshot.assets if asset.asset_type == AssetType.VIDEO
    ]
    tags = snapshot.extra.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise ValueError("approved content tags are invalid")
    return PublishContent(
        title=snapshot.title,
        body=snapshot.body,
        content_type=snapshot.content_type,
        images=images,
        videos=videos,
        tags=tags,
        extra=snapshot.extra,
        exact_approval=True,
    )


__all__ = [
    "StoredApprovalAsset",
    "StoredApprovalContent",
    "approval_content_digest",
    "build_stored_content_metadata_snapshot",
    "build_stored_content_snapshot",
    "content_snapshot_digest_payload",
    "parse_stored_content_snapshot",
    "public_content_snapshot",
    "publish_content_from_snapshot",
    "stored_content_digest",
    "validate_stored_content_total",
    "verify_stored_content_snapshot",
]
