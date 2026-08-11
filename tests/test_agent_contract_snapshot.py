from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_ops.agent_contract.assets import (
    AssetIntegrityError,
    AssetTooLargeError,
    import_asset_to_vault,
)
from ai_ops.agent_contract.snapshot import (
    approval_content_digest,
    build_stored_content_snapshot,
    parse_stored_content_snapshot,
    public_content_snapshot,
    publish_content_from_snapshot,
    stored_content_digest,
    verify_stored_content_snapshot,
)
from ai_ops.core.enums import AssetSource, AssetType, ContentType


def _article_with_vaulted_image(tmp_path: Path):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    vault_root = tmp_path / "vault"
    (import_root / "cover.bin").write_bytes(b"approved-image-bytes")
    vaulted = import_asset_to_vault(
        "cover.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    asset = SimpleNamespace(
        id=9,
        asset_type=AssetType.IMAGE,
        source=AssetSource.AI_GENERATED,
        local_path=str(vaulted.vault_path),
        content_sha256=vaulted.sha256,
        size_bytes=vaulted.size_bytes,
        storage_kind="agent_vault_v1",
        meta={"role": "cover"},
    )
    article = SimpleNamespace(
        id=3,
        title="Reviewed title",
        body="Reviewed body",
        content_type=ContentType.LONG_ARTICLE,
        extra={"tags": ["one", "two"]},
        assets=[asset],
    )
    return article, vault_root, vaulted


def test_snapshot_is_strict_public_and_publisher_ready(tmp_path):
    article, vault_root, vaulted = _article_with_vaulted_image(tmp_path)

    snapshot = build_stored_content_snapshot(
        article,
        vault_root=vault_root,
        max_bytes=1024,
    )
    replay = parse_stored_content_snapshot(snapshot.model_dump(mode="json"))
    review = public_content_snapshot(replay)
    content = publish_content_from_snapshot(replay)

    assert stored_content_digest(replay) == stored_content_digest(snapshot)
    assert approval_content_digest(review) == stored_content_digest(snapshot)
    assert review.assets[0].vaulted_path == f"vault://sha256/{vaulted.sha256}"
    assert "storage_path" not in review.model_dump(mode="json")["assets"][0]
    assert content.images == [str(vaulted.vault_path)]
    assert content.body == "Reviewed body"
    assert content.tags == ["one", "two"]
    assert content.exact_approval is True


def test_snapshot_digest_binds_body_asset_order_and_bytes(tmp_path):
    article, vault_root, vaulted = _article_with_vaulted_image(tmp_path)
    first = build_stored_content_snapshot(
        article,
        vault_root=vault_root,
        max_bytes=1024,
    )

    article.body = "changed after review"
    changed = build_stored_content_snapshot(
        article,
        vault_root=vault_root,
        max_bytes=1024,
    )
    assert stored_content_digest(first) != stored_content_digest(changed)

    vaulted.vault_path.chmod(0o600)
    vaulted.vault_path.write_bytes(b"replacement-image-bytes")
    with pytest.raises(AssetIntegrityError):
        verify_stored_content_snapshot(
            first,
            vault_root=vault_root,
            max_bytes=1024,
        )


def test_snapshot_total_asset_budget_fails_before_hashing(tmp_path):
    article, vault_root, vaulted = _article_with_vaulted_image(tmp_path)
    snapshot = build_stored_content_snapshot(
        article,
        vault_root=vault_root,
        max_bytes=1024,
    )
    vaulted.vault_path.unlink()

    with pytest.raises(AssetTooLargeError) as captured:
        verify_stored_content_snapshot(
            snapshot,
            vault_root=vault_root,
            max_bytes=1024,
            max_total_bytes=1,
        )

    assert captured.value.code == "asset_too_large"


def test_snapshot_parser_rejects_unknown_private_fields(tmp_path):
    article, vault_root, _ = _article_with_vaulted_image(tmp_path)
    snapshot = build_stored_content_snapshot(
        article,
        vault_root=vault_root,
        max_bytes=1024,
    ).model_dump(mode="json")
    snapshot["unexpected"] = "not allowed"

    with pytest.raises(ValueError):
        parse_stored_content_snapshot(snapshot)
