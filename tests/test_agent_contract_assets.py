"""Security and durability contracts for Agent asset-vault imports."""

from __future__ import annotations

from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path

import pytest

from ai_ops.agent_contract import assets as asset_vault_mod
from ai_ops.agent_contract.assets import (
    AssetIntegrityError,
    AssetSourceRejectedError,
    AssetTooLargeError,
    AssetVaultConfigurationError,
    AssetVaultStorageError,
    copy_verified_vaulted_asset,
    import_asset_to_vault,
    inspect_import_asset_size,
    open_verified_vaulted_asset,
    verify_vaulted_asset,
)


@pytest.fixture
def asset_roots(tmp_path: Path) -> tuple[Path, Path]:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    return import_root, tmp_path / "vault"


def test_import_streams_to_content_addressed_immutable_path(asset_roots):
    import_root, vault_root = asset_roots
    payload = b"agent-vault\x00payload"
    (import_root / "cover.png").write_bytes(payload)

    asset = import_asset_to_vault(
        "cover.png",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )

    expected_digest = hashlib.sha256(payload).hexdigest()
    assert asset.sha256 == expected_digest
    assert asset.size_bytes == len(payload)
    assert asset.vault_path == (
        vault_root.resolve()
        / "sha256"
        / expected_digest[:2]
        / expected_digest[2:4]
        / f"{expected_digest}.png"
    )
    assert asset.vault_path.read_bytes() == payload
    assert (
        verify_vaulted_asset(
            asset,
            vault_root=vault_root,
            max_bytes=1024,
        )
        == asset
    )


@pytest.mark.parametrize("source_kind", ["traversal", "absolute"])
def test_import_rejects_paths_outside_the_explicit_root_without_echoing_them(
    asset_roots,
    source_kind,
):
    import_root, vault_root = asset_roots
    outside = import_root.parent / "outside-secret-name.txt"
    outside.write_bytes(b"not authorized")
    source = Path("..") / outside.name if source_kind == "traversal" else outside

    with pytest.raises(AssetSourceRejectedError) as captured:
        import_asset_to_vault(
            source,
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert captured.value.code == "asset_source_rejected"
    assert outside.name not in str(captured.value)
    assert str(outside) not in str(captured.value)


def test_import_rejects_file_and_directory_symlinks(asset_roots):
    import_root, vault_root = asset_roots
    real_dir = import_root / "real"
    real_dir.mkdir()
    (real_dir / "asset.bin").write_bytes(b"data")
    file_link = import_root / "file-link.bin"
    directory_link = import_root / "directory-link"
    try:
        file_link.symlink_to(real_dir / "asset.bin")
        directory_link.symlink_to(real_dir, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    for source in (file_link.name, directory_link / "asset.bin"):
        with pytest.raises(AssetSourceRejectedError):
            import_asset_to_vault(
                source,
                import_root=import_root,
                vault_root=vault_root,
                max_bytes=1024,
            )


@pytest.mark.parametrize("vault_location", ["inside-import", "contains-import"])
def test_import_and_vault_roots_must_be_disjoint(tmp_path, vault_location):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "asset.bin").write_bytes(b"payload")
    vault_root = import_root / "vault" if vault_location == "inside-import" else tmp_path

    with pytest.raises(AssetVaultConfigurationError, match="must not overlap"):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )


def test_import_rejects_directory_and_fifo_without_blocking(asset_roots):
    import_root, vault_root = asset_roots
    directory = import_root / "not-a-file"
    directory.mkdir()

    with pytest.raises(AssetSourceRejectedError):
        import_asset_to_vault(
            directory.name,
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    if not hasattr(os, "mkfifo"):
        return
    fifo = import_root / "named-pipe"
    os.mkfifo(fifo)
    with pytest.raises(AssetSourceRejectedError):
        import_asset_to_vault(
            fifo.name,
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )


def test_import_rejects_device_file_when_platform_exposes_one(asset_roots):
    _, vault_root = asset_roots
    device = Path(os.devnull)
    if not device.is_absolute() or not device.exists() or device.parent.is_symlink():
        pytest.skip("no filesystem-backed device is available on this platform")

    with pytest.raises(AssetSourceRejectedError):
        import_asset_to_vault(
            device.name,
            import_root=device.parent,
            vault_root=vault_root,
            max_bytes=1024,
        )


def test_import_enforces_maximum_bytes_before_creating_a_temp_file(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "large.bin").write_bytes(b"12345")

    with pytest.raises(AssetTooLargeError) as captured:
        import_asset_to_vault(
            "large.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=4,
        )

    assert captured.value.code == "asset_too_large"
    assert not list(vault_root.glob(".asset-*.tmp"))


def test_preflight_securely_reports_size_without_creating_a_vault(asset_roots):
    import_root, vault_root = asset_roots
    payload = b"preflight-only"
    source = import_root / "asset.bin"
    source.write_bytes(payload)

    assert inspect_import_asset_size(
        source.name,
        import_root=import_root,
        max_bytes=len(payload),
    ) == len(payload)
    assert source.read_bytes() == payload
    assert not vault_root.exists()


def test_preflight_rejects_oversized_and_symlink_sources(asset_roots):
    import_root, _ = asset_roots
    source = import_root / "asset.bin"
    source.write_bytes(b"12345")

    with pytest.raises(AssetTooLargeError):
        inspect_import_asset_size(source.name, import_root=import_root, max_bytes=4)

    symlink = import_root / "asset-link.bin"
    try:
        symlink.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(AssetSourceRejectedError):
        inspect_import_asset_size(symlink.name, import_root=import_root, max_bytes=10)


def test_import_and_preflight_fail_closed_without_secure_dir_fd_primitives(
    asset_roots,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    monkeypatch.setattr(asset_vault_mod, "_supports_secure_dir_fd", lambda: False)

    with pytest.raises(AssetVaultConfigurationError, match="primitives"):
        inspect_import_asset_size(
            "asset.bin",
            import_root=import_root,
            max_bytes=1024,
        )
    with pytest.raises(AssetVaultConfigurationError, match="primitives"):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert not vault_root.exists()


def test_import_enforces_private_owned_vault_directory_modes(asset_roots):
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX ownership is unavailable")
    import_root, vault_root = asset_roots
    payload = b"private-vault"
    (import_root / "asset.bin").write_bytes(payload)
    vault_root.mkdir(mode=0o777)
    vault_root.chmod(0o777)
    sha256_root = vault_root / "sha256"
    sha256_root.mkdir(mode=0o777)
    sha256_root.chmod(0o777)

    asset = import_asset_to_vault(
        "asset.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )

    for directory in (
        vault_root,
        sha256_root,
        asset.vault_path.parent.parent,
        asset.vault_path.parent,
    ):
        directory_stat = directory.stat()
        assert directory_stat.st_uid == os.getuid()
        assert directory_stat.st_mode & 0o777 == 0o700


def test_import_rejects_a_vault_root_not_owned_by_the_current_uid(
    asset_roots,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    actual_uid = os.getuid()
    monkeypatch.setattr(asset_vault_mod, "_current_uid", lambda: actual_uid + 1)

    with pytest.raises(AssetVaultConfigurationError):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )


def test_import_fails_closed_when_private_vault_mode_cannot_be_enforced(
    asset_roots,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    vault_root.mkdir(mode=0o777)
    vault_root.chmod(0o777)

    def reject_fchmod(_fd, _mode):
        raise PermissionError("simulated chmod rejection")

    monkeypatch.setattr(os, "fchmod", reject_fchmod)

    with pytest.raises(AssetVaultConfigurationError) as captured:
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert "simulated" not in str(captured.value)


def test_import_rejects_a_symlinked_digest_directory(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    vault_root.mkdir(mode=0o700)
    outside = vault_root.parent / "outside-digest"
    outside.mkdir()
    try:
        (vault_root / "sha256").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(AssetVaultStorageError):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert not [path for path in outside.rglob("*") if path.is_file()]
    assert not list(vault_root.glob(".asset-*.tmp"))


def test_verify_reenforces_private_digest_directory_modes(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    asset = import_asset_to_vault(
        "asset.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    asset.vault_path.parent.chmod(0o777)

    assert verify_vaulted_asset(asset, vault_root=vault_root, max_bytes=1024) == asset
    assert asset.vault_path.parent.stat().st_mode & 0o777 == 0o700


def test_verify_detects_changed_bytes_and_size(asset_roots):
    import_root, vault_root = asset_roots
    source = import_root / "mutable.bin"
    source.write_bytes(b"original")
    asset = import_asset_to_vault(
        source,
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )

    asset.vault_path.chmod(0o600)
    asset.vault_path.write_bytes(b"tampered")

    with pytest.raises(AssetIntegrityError) as captured:
        verify_vaulted_asset(asset, vault_root=vault_root, max_bytes=1024)

    assert captured.value.code == "asset_integrity_failed"
    assert str(asset.vault_path) not in str(captured.value)


def test_verify_rejects_noncanonical_path_even_with_a_valid_identity(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")
    asset = import_asset_to_vault(
        "asset.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    outside = import_root.parent / "vault-path-escape.bin"
    outside.write_bytes(asset.vault_path.read_bytes())

    with pytest.raises(AssetIntegrityError) as captured:
        verify_vaulted_asset(
            replace(asset, vault_path=outside),
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert outside.name not in str(captured.value)


def test_open_verified_asset_keeps_the_hashed_inode_across_path_replacement(asset_roots):
    import_root, vault_root = asset_roots
    payload = b"approved bytes"
    (import_root / "asset.bin").write_bytes(payload)
    asset = import_asset_to_vault(
        "asset.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    opened = open_verified_vaulted_asset(asset, vault_root=vault_root, max_bytes=1024)
    replacement = import_root / "replacement-secret.bin"
    replacement.write_bytes(b"must never be streamed")
    try:
        try:
            asset.vault_path.unlink()
            asset.vault_path.symlink_to(replacement)
        except (NotImplementedError, OSError):
            pytest.skip("symlinks are unavailable on this platform")

        assert opened.handle.read() == payload
    finally:
        opened.close()


def test_single_pass_copy_detects_path_replacement_during_source_read(
    asset_roots,
    tmp_path,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    payload = b"approved bytes copied once"
    replacement_payload = b"unapproved path replacement"
    (import_root / "asset.bin").write_bytes(payload)
    asset = import_asset_to_vault(
        "asset.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    real_secure_open = asset_vault_mod._open_regular_beneath

    def replace_path_after_secure_open(root, relative):
        source_fd, source_stat = real_secure_open(root, relative)
        asset.vault_path.unlink()
        asset.vault_path.write_bytes(replacement_payload)
        return source_fd, source_stat

    monkeypatch.setattr(
        asset_vault_mod,
        "_open_regular_beneath",
        replace_path_after_secure_open,
    )
    destination = tmp_path / "execution-copy.bin"
    destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with pytest.raises(AssetIntegrityError):
            copy_verified_vaulted_asset(
                asset,
                destination_fd=destination_fd,
                vault_root=vault_root,
                max_bytes=1024,
            )
    finally:
        os.close(destination_fd)

    # The destination is never exposed after rejection even though it was fed
    # from the approved open inode; the caller owns and discards this temp file.
    assert destination.read_bytes() == payload
    assert asset.vault_path.read_bytes() == replacement_payload


def test_identical_content_reuses_one_existing_digest_file(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "first.bin").write_bytes(b"same bytes")
    (import_root / "second.bin").write_bytes(b"same bytes")

    first = import_asset_to_vault(
        "first.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    first_inode = first.vault_path.stat().st_ino
    second = import_asset_to_vault(
        "second.bin",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )

    assert second == first
    assert second.vault_path.stat().st_ino == first_inode
    assert [path for path in vault_root.rglob("*") if path.is_file()] == [first.vault_path]
    assert not list(vault_root.glob(".asset-*.tmp"))


def test_storage_suffix_is_normalized_without_entering_the_public_identity(asset_roots):
    import_root, vault_root = asset_roots
    (import_root / "cover.JPEG").write_bytes(b"jpeg-like-payload")

    asset = import_asset_to_vault(
        "cover.JPEG",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )

    assert asset.vault_path.name == f"{asset.sha256}.jpeg"
    assert verify_vaulted_asset(asset, vault_root=vault_root, max_bytes=1024) == asset


def test_atomic_commit_failure_leaves_no_temp_or_partial_file(
    asset_roots,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"payload")

    def fail_link(_source, _destination, **_kwargs):
        raise OSError(errno.EIO, "simulated path-bearing storage failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(AssetVaultStorageError) as captured:
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert captured.value.code == "asset_vault_storage_failed"
    assert "simulated" not in str(captured.value)
    assert not list(vault_root.glob(".asset-*.tmp"))
    assert not [path for path in vault_root.rglob("*") if path.is_file()]


@pytest.mark.parametrize("replacement_point", ["temp-before-link", "destination-after-link"])
def test_commit_rejects_path_replacement_and_cleans_all_entries(
    asset_roots,
    monkeypatch,
    replacement_point,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"approved-payload")
    real_link = os.link

    def replace_during_link(
        source,
        destination,
        *,
        src_dir_fd,
        dst_dir_fd,
        follow_symlinks,
    ):
        if replacement_point == "temp-before-link":
            os.unlink(source, dir_fd=src_dir_fd)
            replacement_fd = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=src_dir_fd,
            )
            try:
                os.write(replacement_fd, b"unapproved-temp")
            finally:
                os.close(replacement_fd)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if replacement_point == "destination-after-link":
            os.unlink(destination, dir_fd=dst_dir_fd)
            replacement_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(replacement_fd, b"unapproved-destination")
            finally:
                os.close(replacement_fd)

    monkeypatch.setattr(os, "link", replace_during_link)

    with pytest.raises(AssetVaultStorageError, match="verification"):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert not list(vault_root.glob(".asset-*.tmp"))
    assert not [path for path in vault_root.rglob("*") if path.is_file()]


def test_commit_rehashes_new_destination_and_removes_mutated_inode(
    asset_roots,
    monkeypatch,
):
    import_root, vault_root = asset_roots
    (import_root / "asset.bin").write_bytes(b"approved-payload")
    real_link = os.link

    def mutate_after_link(
        source,
        destination,
        *,
        src_dir_fd,
        dst_dir_fd,
        follow_symlinks,
    ):
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        os.chmod(source, 0o600, dir_fd=src_dir_fd, follow_symlinks=False)
        replacement_fd = os.open(
            source,
            os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
            dir_fd=src_dir_fd,
        )
        try:
            os.write(replacement_fd, b"mutated-payload!")
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(os, "link", mutate_after_link)

    with pytest.raises(AssetVaultStorageError, match="verification"):
        import_asset_to_vault(
            "asset.bin",
            import_root=import_root,
            vault_root=vault_root,
            max_bytes=1024,
        )

    assert not list(vault_root.glob(".asset-*.tmp"))
    assert not [path for path in vault_root.rglob("*") if path.is_file()]
