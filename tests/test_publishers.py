"""publisher registry 路由 + fallback 行为单测。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import stat
import threading

import pytest

from ai_ops.agent_contract import assets as asset_vault_mod
from ai_ops.agent_contract.assets import import_asset_to_vault
from ai_ops.agent_contract.digest import canonical_sha256
from ai_ops.core.enums import AccountHealth, AssetType, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import ApprovedAssetExecution, PublishContent, PublishResult
from ai_ops.publishers.base import (
    AgentContractAssetRule,
    AgentContractRendererDescriptor,
    AgentContractRendererUnavailable,
    PublisherBase,
)
from ai_ops.publishers.plugin_sdk import (
    PUBLISHER_PLUGIN_API_VERSION,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
    instantiate_validated_publisher,
)
from ai_ops.publishers.registry import PublisherRegistry
from ai_ops.scheduler import worker as worker_mod


class _AlwaysFail(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    async def login(self, account_id, credential):
        return False

    async def publish(self, account_id, credential, content):
        return PublishResult(success=False, error="模拟失败")

    async def health_check(self, account_id, credential):
        return AccountHealth.DEGRADED


class _AlwaysOk(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.XHS_TOOLKIT

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        return PublishResult(success=True, platform_post_id="x1", platform_url="https://xhs/x1")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


class _Uncertain(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    async def login(self, account_id, credential):
        return False

    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=False,
            outcome_uncertain=True,
            error="platform outcome unknown",
        )

    async def health_check(self, account_id, credential):
        return AccountHealth.UNKNOWN


class _KnownPartialEffect(_Uncertain):
    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=False,
            effect_applied=True,
            platform_post_id="already-created",
            error="post created but follow-up failed",
        )


class _RaisesUnexpectedly(_Uncertain):
    async def publish(self, account_id, credential, content):
        raise RuntimeError("exception text may contain a secret")


class _ThirdPartyLeaksRaw(_Uncertain):
    kind = "fixture_third_party"

    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=False,
            error="token=must-not-leak",
            raw_response={"token": "must-not-leak", "response": "private"},
        )


class _ThirdPartyNotImplemented(_Uncertain):
    kind = "fixture_third_party"

    async def publish(self, account_id, credential, content):
        raise NotImplementedError("cookie=must-not-leak")


class _ThirdPartySystemExit(_Uncertain):
    kind = "fixture_third_party"

    async def publish(self, account_id, credential, content):
        raise SystemExit("token=must-not-leak")


def _third_party_factory(publisher_type):
    plugin = PublisherPlugin(
        manifest=PublisherPluginManifest(
            plugin_id="fixture.publisher",
            plugin_version="1.0.0",
            api_version=PUBLISHER_PLUGIN_API_VERSION,
            platform=Platform.XIAOHONGSHU,
            publisher_kind="fixture_third_party",
            adapter_version="1",
            capabilities=(
                PublisherPluginCapability.HEALTH_CHECK,
                PublisherPluginCapability.LOGIN,
                PublisherPluginCapability.PUBLISH,
            ),
        ),
        factory=publisher_type,
    )
    return lambda: instantiate_validated_publisher(
        "fixture-ai-ops:fixture.publisher",
        plugin,
    )


class _ExactAssetReader(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.XHS_TOOLKIT
    agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
        renderer_id="test.path-free-image",
        contract_version="1",
        adapter_version="test-1",
        platform=platform,
        publisher_kind=kind,
        asset_rules=(AgentContractAssetRule(asset_type=AssetType.IMAGE, min_count=1),),
    )
    observed: dict[str, object] = {}

    def render_agent_contract_payload(self, content):
        return {
            "title": content.title,
            "body": content.body,
            "image_slots": [
                {"asset_type": "image", "index": index} for index, _ in enumerate(content.images)
            ],
        }

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        path = Path(content.images[0])
        type(self).observed = {
            "path": path,
            "payload": path.read_bytes(),
            "file_mode": stat.S_IMODE(path.stat().st_mode),
            "directory_mode": stat.S_IMODE(path.parent.stat().st_mode),
            "approved_assets": list(content.approved_assets),
        }
        return PublishResult(success=True, platform_post_id="exact-1")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


class _ExactBlockingAssetReader(_ExactAssetReader):
    entered: asyncio.Event | None = None

    async def publish(self, account_id, credential, content):
        type(self).observed = {"path": Path(content.images[0])}
        assert type(self).entered is not None
        type(self).entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled Publisher unexpectedly resumed")


def _exact_asset_content(asset) -> PublishContent:
    content = PublishContent(
        title="approved title",
        body="approved body",
        content_type=ContentType.IMAGE_TEXT,
        images=[str(asset.vault_path)],
        exact_approval=True,
        approved_publisher_kind=PublisherKind.XHS_TOOLKIT.value,
        approved_assets=[
            ApprovedAssetExecution(
                asset_type=AssetType.IMAGE,
                storage_path=str(asset.vault_path),
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                storage_suffix=asset.vault_path.suffix,
            )
        ],
    )
    content.approved_renderer_payload_digest = canonical_sha256(
        _ExactAssetReader().agent_contract_digest_material(content)
    )
    return content


def test_agent_contract_renderer_is_fail_closed_by_default():
    publisher = _AlwaysFail()
    content = PublishContent(title="t", body="b", content_type="image_text")

    assert publisher.supports_agent_contract_renderer is False
    assert publisher.agent_contract_renderer_descriptor is None
    with pytest.raises(AgentContractRendererUnavailable):
        publisher.render_agent_contract_payload(content)
    with pytest.raises(AgentContractRendererUnavailable):
        publisher.agent_contract_digest_material(content)


def test_registry_priority_order():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    reg.register(Platform.XIAOHONGSHU, _AlwaysFail, priority=10)

    pubs = reg.resolve(Platform.XIAOHONGSHU)
    # priority 10 排在 20 之前
    assert pubs[0].kind == PublisherKind.SOCIAL_AUTO_UPLOAD
    assert pubs[1].kind == PublisherKind.XHS_TOOLKIT


def test_registry_supported_platforms():
    reg = PublisherRegistry()
    reg.register(Platform.DOUYIN, _AlwaysOk)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk)
    assert set(reg.supported_platforms()) == {Platform.DOUYIN, Platform.XIAOHONGSHU}


def test_fallback_chain_simulated():
    """模拟 worker 的 fallback 逻辑：第一个失败，第二个成功。"""
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysFail, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)

    async def run():
        content = PublishContent(title="t", body="b", content_type="image_text")
        for pub in reg.resolve(Platform.XIAOHONGSHU):
            res = await pub.publish(1, {}, content)
            if res.success:
                return res
        return None

    res = asyncio.run(run())
    assert res is not None and res.success
    assert res.platform_post_id == "x1"


def test_worker_fallback_stops_on_uncertain_outcome(monkeypatch):
    """A second Publisher could duplicate a write that the first cannot confirm."""
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _Uncertain, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.outcome_uncertain is True


def test_worker_fallback_stops_after_known_partial_effect(monkeypatch):
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _KnownPartialEffect, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.effect_applied is True
    assert result.platform_post_id == "already-created"


def test_worker_unexpected_exception_is_unknown_and_stops_fallback(monkeypatch):
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _RaisesUnexpectedly, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["exception_type"] == "RuntimeError"
    assert "secret" not in result.model_dump_json()


def test_third_party_publish_result_drops_arbitrary_raw_and_error_text():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _third_party_factory(_ThirdPartyLeaksRaw))

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {"token": "credential"},
            PublishContent(title="t", body="b", content_type="image_text"),
            registry=reg,
            receipt_writer=lambda **_: None,
        )
    )

    assert result.error == "Publisher plugin reported a failure"
    assert result.raw_response == {"publisher_kind": "fixture_third_party"}
    assert "must-not-leak" not in result.model_dump_json()


def test_third_party_not_implemented_exception_text_is_redacted():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _third_party_factory(_ThirdPartyNotImplemented))

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {"cookie": "credential"},
            PublishContent(title="t", body="b", content_type="image_text"),
            registry=reg,
            receipt_writer=lambda **_: None,
        )
    )

    assert result.raw_response["error_code"] == "publish_not_implemented"
    assert result.raw_response["exception_type"] == "NotImplementedError"
    assert "must-not-leak" not in result.model_dump_json()


def test_third_party_system_exit_is_uncertain_and_does_not_stop_worker():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _third_party_factory(_ThirdPartySystemExit))

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {"token": "credential"},
            PublishContent(title="t", body="b", content_type="image_text"),
            registry=reg,
            receipt_writer=lambda **_: None,
        )
    )

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["exception_type"] == "SystemExit"
    assert "must-not-leak" not in result.model_dump_json()


def test_custom_receipt_writer_failure_does_not_erase_confirmed_result():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk)

    def broken_receipt_writer(**kwargs):
        raise OSError("isolated receipt store unavailable")

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
            registry=reg,
            receipt_writer=broken_receipt_writer,
        )
    )

    assert result.success is True
    assert result.platform_post_id == "x1"


def test_legacy_publish_content_bypasses_exact_asset_materialization():
    content = PublishContent(
        title="legacy",
        body="body",
        content_type=ContentType.IMAGE_TEXT,
        images=["/a/legacy/path-that-does-not-exist.jpg"],
    )

    with worker_mod._materialized_exact_assets(content) as execution_content:
        assert execution_content is content
        assert execution_content.images == content.images


def test_exact_asset_is_private_read_only_and_removed_after_publisher(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    payload = b"approved-image-payload"
    (import_root / "approved.jpg").write_bytes(payload)
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    _ExactAssetReader.observed = {}
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    result = asyncio.run(
        worker_mod._try_publishers_with_materialized_assets(
            Platform.XIAOHONGSHU,
            1,
            {},
            _exact_asset_content(asset),
            registry=registry,
            receipt_writer=lambda **_: None,
        )
    )

    execution_path = _ExactAssetReader.observed["path"]
    assert isinstance(execution_path, Path)
    assert result.success is True
    assert execution_path != asset.vault_path
    assert execution_path.is_relative_to(vault_root.resolve())
    assert execution_path.suffix == ".jpg"
    assert _ExactAssetReader.observed["payload"] == payload
    assert _ExactAssetReader.observed["file_mode"] == 0o400
    assert _ExactAssetReader.observed["directory_mode"] == 0o700
    assert _ExactAssetReader.observed["approved_assets"] == []
    assert not execution_path.exists()
    assert not execution_path.parent.exists()
    assert asset.vault_path.read_bytes() == payload


def test_exact_asset_materialization_keeps_the_event_loop_responsive(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "approved.jpg").write_bytes(b"approved-image-payload")
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    real_copy = worker_mod._copy_verified_asset_to_execution_file
    copy_started = threading.Event()
    release_copy = threading.Event()

    def blocking_copy(*args, **kwargs):
        copy_started.set()
        if not release_copy.wait(timeout=1):
            raise AssertionError("event loop did not run while exact asset copy blocked")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(
        worker_mod,
        "_copy_verified_asset_to_execution_file",
        blocking_copy,
    )
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    async def exercise() -> PublishResult:
        asyncio.get_running_loop().call_later(0.05, release_copy.set)
        return await asyncio.wait_for(
            worker_mod._try_publishers_with_materialized_assets(
                Platform.XIAOHONGSHU,
                1,
                {},
                _exact_asset_content(asset),
                registry=registry,
                receipt_writer=lambda **_: None,
            ),
            timeout=1,
        )

    result = asyncio.run(exercise())

    assert copy_started.is_set()
    assert result.success is True
    assert not list(vault_root.glob(".agent-execution-*"))


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX vault ownership only")
def test_exact_asset_materialization_rejects_insecure_vault_root_permissions(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "approved.jpg").write_bytes(b"approved-image-payload")
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    vault_root.chmod(0o755)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    _ExactAssetReader.observed = {}
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    with pytest.raises(worker_mod.ExactAssetMaterializationError):
        asyncio.run(
            worker_mod._try_publishers_with_materialized_assets(
                Platform.XIAOHONGSHU,
                1,
                {},
                _exact_asset_content(asset),
                registry=registry,
                receipt_writer=lambda **_: None,
            )
        )

    assert _ExactAssetReader.observed == {}
    assert not list(vault_root.glob(".agent-execution-*"))


def test_cancellation_during_exact_materialization_waits_for_cleanup(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "approved.jpg").write_bytes(b"approved-image-payload")
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    real_copy = worker_mod._copy_verified_asset_to_execution_file
    copy_started = threading.Event()
    release_copy = threading.Event()
    _ExactAssetReader.observed = {}

    def blocking_copy(*args, **kwargs):
        copy_started.set()
        if not release_copy.wait(timeout=1):
            raise AssertionError("test did not release exact asset copy")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(
        worker_mod,
        "_copy_verified_asset_to_execution_file",
        blocking_copy,
    )
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    async def exercise() -> None:
        task = asyncio.create_task(
            worker_mod._try_publishers_with_materialized_assets(
                Platform.XIAOHONGSHU,
                1,
                {},
                _exact_asset_content(asset),
                registry=registry,
                receipt_writer=lambda **_: None,
            )
        )
        while not copy_started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_copy.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(exercise())
    finally:
        release_copy.set()

    assert _ExactAssetReader.observed == {}
    assert not list(vault_root.glob(".agent-execution-*"))


def test_exact_asset_materialization_reads_each_source_once(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    approved_payload = b"approved-before-path-replacement"
    (import_root / "approved.jpg").write_bytes(approved_payload)
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    real_copy = worker_mod.copy_verified_vaulted_asset
    real_open_fd = asset_vault_mod._open_vaulted_asset_fd
    real_read = asset_vault_mod.os.read
    copy_calls = 0
    source_bytes_read = 0
    source_fds: set[int] = set()

    def count_copy(*args, **kwargs):
        nonlocal copy_calls
        copy_calls += 1
        return real_copy(*args, **kwargs)

    def track_source_fd(*args, **kwargs):
        opened = real_open_fd(*args, **kwargs)
        source_fds.add(opened.file_fd)
        return opened

    def count_source_bytes(file_fd, max_bytes):
        nonlocal source_bytes_read
        chunk = real_read(file_fd, max_bytes)
        if file_fd in source_fds:
            source_bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(worker_mod, "copy_verified_vaulted_asset", count_copy)
    monkeypatch.setattr(asset_vault_mod, "_open_vaulted_asset_fd", track_source_fd)
    monkeypatch.setattr(asset_vault_mod.os, "read", count_source_bytes)
    _ExactAssetReader.observed = {}
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    result = asyncio.run(
        worker_mod._try_publishers_with_materialized_assets(
            Platform.XIAOHONGSHU,
            1,
            {},
            _exact_asset_content(asset),
            registry=registry,
            receipt_writer=lambda **_: None,
        )
    )

    assert result.success is True
    assert copy_calls == 1
    assert source_bytes_read == len(approved_payload)
    assert _ExactAssetReader.observed["payload"] == approved_payload
    assert asset.vault_path.read_bytes() == approved_payload


def test_exact_asset_manifest_mismatch_fails_before_publisher(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    payload = b"approved-image-payload"
    (import_root / "approved.jpg").write_bytes(payload)
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    content = _exact_asset_content(asset).model_copy(
        update={
            "images": [
                str(asset.vault_path.with_name(f"{hashlib.sha256(b'tampered').hexdigest()}.jpg"))
            ]
        }
    )
    _ExactAssetReader.observed = {}
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactAssetReader)

    with pytest.raises(worker_mod.ExactAssetMaterializationError):
        asyncio.run(
            worker_mod._try_publishers_with_materialized_assets(
                Platform.XIAOHONGSHU,
                1,
                {},
                content,
                registry=registry,
                receipt_writer=lambda **_: None,
            )
        )

    assert _ExactAssetReader.observed == {}
    assert not list(vault_root.glob(".agent-execution-*"))


def test_exact_asset_execution_copy_is_removed_when_publisher_is_cancelled(
    tmp_path: Path,
    monkeypatch,
):
    import_root = tmp_path / "imports"
    import_root.mkdir()
    (import_root / "approved.jpg").write_bytes(b"approved-image-payload")
    vault_root = tmp_path / "vault"
    asset = import_asset_to_vault(
        "approved.jpg",
        import_root=import_root,
        vault_root=vault_root,
        max_bytes=1024,
    )
    monkeypatch.setattr(worker_mod.settings, "agent_asset_vault_root", vault_root)
    monkeypatch.setattr(worker_mod.settings, "agent_asset_max_bytes", 1024)
    real_cleanup = worker_mod._cleanup_exact_asset_temporary_directory
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def blocking_cleanup(temporary):
        cleanup_started.set()
        if not release_cleanup.wait(timeout=1):
            raise AssertionError("test did not release exact asset cleanup")
        real_cleanup(temporary)

    monkeypatch.setattr(
        worker_mod,
        "_cleanup_exact_asset_temporary_directory",
        blocking_cleanup,
    )
    registry = PublisherRegistry()
    registry.register(Platform.XIAOHONGSHU, _ExactBlockingAssetReader)

    async def cancel_after_entry() -> Path:
        _ExactBlockingAssetReader.entered = asyncio.Event()
        task = asyncio.create_task(
            worker_mod._try_publishers_with_materialized_assets(
                Platform.XIAOHONGSHU,
                1,
                {},
                _exact_asset_content(asset),
                registry=registry,
                receipt_writer=lambda **_: None,
            )
        )
        await _ExactBlockingAssetReader.entered.wait()
        execution_path = _ExactBlockingAssetReader.observed["path"]
        assert isinstance(execution_path, Path)
        assert execution_path.exists()
        task.cancel()
        while not cleanup_started.is_set():
            await asyncio.sleep(0.001)
        # A second cancellation must not detach the still-running cleanup
        # thread and return while approved execution copies remain on disk.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert execution_path.exists()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return execution_path

    try:
        execution_path = asyncio.run(cancel_after_entry())
    finally:
        release_cleanup.set()

    assert not execution_path.exists()
    assert not execution_path.parent.exists()
