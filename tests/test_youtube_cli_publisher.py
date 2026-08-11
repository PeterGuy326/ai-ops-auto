"""Offline contract tests for the youtubeuploader v1.25.5 adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers.base import AgentContractRendererUnavailable
from ai_ops.publishers.registry import build_default_registry
from ai_ops.publishers.youtube_cli import (
    YoutubeUploaderPublisher,
    _CommandResult,
    project_youtube_agent_payload,
)
from ai_ops.runtime.receipts import read_publish_receipt


@pytest.fixture
def youtube_settings(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    assets = tmp_path / "data"
    assets.mkdir()
    monkeypatch.setattr(settings, "youtube_uploader_profile_root", profiles)
    monkeypatch.setattr(settings, "youtube_uploader_asset_root", assets)
    monkeypatch.setattr(settings, "agent_asset_vault_root", assets)
    monkeypatch.setattr(settings, "youtube_uploader_bin", "youtubeuploader")
    monkeypatch.setattr(settings, "youtube_uploader_timeout_seconds", 1)
    monkeypatch.setattr(settings, "youtube_uploader_max_video_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "control-data")
    return profiles, assets


def _content(video: str, *, privacy: str = "private") -> PublishContent:
    return PublishContent(
        title="AI 视频标题",
        body="未公开的正文；$(不会进 argv)",
        content_type=ContentType.VIDEO,
        videos=[video],
        tags=["agent", "自动化"],
        extra={
            "youtube_privacy": privacy,
            "youtube_category_id": "28",
            "youtube_contains_synthetic_media": True,
        },
    )


def _prepare(pub: YoutubeUploaderPublisher, account_id: int, assets):
    home = pub._account_home(account_id, create=True)
    secrets = home / "client_secrets.json"
    token = home / "request.token"
    secrets.write_text(
        json.dumps({"installed": {"client_id": "client-id", "client_secret": "redacted"}}),
        encoding="utf-8",
    )
    token.write_text(
        json.dumps({"access_token": "access-redacted", "refresh_token": "refresh-redacted"}),
        encoding="utf-8",
    )
    secrets.chmod(0o600)
    token.chmod(0o600)
    video = assets / "clip.mp4"
    video.write_bytes(b"fake-local-video")
    return home, secrets, token, video


def test_agent_projection_is_path_free_but_exact_renderer_opt_in_is_paused():
    content = _content("/private/agent-vault/approved-video.mp4").model_copy(
        update={"exact_approval": True}
    )
    publisher = YoutubeUploaderPublisher()

    projection = project_youtube_agent_payload(content)
    descriptor = publisher.agent_contract_renderer_descriptor

    assert publisher.supports_agent_contract_renderer is False
    assert descriptor is None
    with pytest.raises(AgentContractRendererUnavailable):
        publisher.agent_contract_digest_material(content)
    assert projection["media"] == {"asset_type": "video", "index": 0}
    assert projection["metadata"] == {
        "title": "AI 视频标题",
        "description": "未公开的正文；$(不会进 argv)",
        "tags": ["agent", "自动化"],
        "privacyStatus": "private",
        "language": "zh-Hans",
        "madeForKids": False,
        "containsSyntheticMedia": True,
        "categoryId": "28",
    }
    assert "/private/agent-vault" not in json.dumps(projection, ensure_ascii=False)


@pytest.mark.parametrize(
    "updates",
    [
        {"images": ["/vault/cover.jpg"]},
        {"videos": []},
        {"videos": ["/vault/one.mp4", "/vault/two.mp4"]},
        {"extra": {"youtube_privacy": "private", "unknown": True}},
    ],
)
def test_agent_contract_renderer_rejects_unaccounted_youtube_fields(updates):
    content = _content("/vault/video.mp4").model_copy(update={"exact_approval": True, **updates})

    with pytest.raises(AgentContractRendererUnavailable):
        project_youtube_agent_payload(content)


@pytest.mark.parametrize(
    "updates",
    [
        {"tags": [f"tag-{value}" for value in range(51)]},
        {"tags": ["界" * 100, "界" * 100]},
        {"tags": [""]},
        {"extra": {"youtube_category_id": "0001"}},
        {"extra": {"youtube_category_id": "1234"}},
    ],
)
def test_legacy_projection_fields_are_bounded_without_reenabling_exact(updates):
    content = _content("/vault/video.mp4").model_copy(update=updates)

    with pytest.raises(AgentContractRendererUnavailable):
        project_youtube_agent_payload(content)
    assert YoutubeUploaderPublisher.agent_contract_renderer_descriptor is None


def test_receipt_drives_success_and_sensitive_metadata_stays_out_of_argv(
    youtube_settings, monkeypatch
):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    home, secrets, token, video = _prepare(pub, 4, assets)
    calls: list[tuple[str, ...]] = []
    seen_metadata: dict = {}

    async def version_ready(account_id):
        return True, "v1.25.5"

    async def fake_run(account_id, *args, timeout=None):
        calls.append(args)
        meta_path = next(value.split("=", 1)[1] for value in args if value.startswith("-metaJSON="))
        receipt_path = next(
            value.split("=", 1)[1] for value in args if value.startswith("-metaJSONout=")
        )
        seen_metadata.update(json.loads(open(meta_path, encoding="utf-8").read()))
        with open(receipt_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "id": "abc123DEF45",
                    "status": {"privacyStatus": "private"},
                    "snippet": {"title": "AI 视频标题"},
                },
                handle,
            )
        return _CommandResult(started=True, returncode=0)

    monkeypatch.setattr(pub, "_audited_version_ready", version_ready)
    monkeypatch.setattr(pub, "_run", fake_run)

    content = _content(str(video))
    content.job_id = 42
    content.operation_id = "c" * 32
    result = asyncio.run(pub.publish(4, {"access_token": "must-not-be-used"}, content))

    assert result.success is True
    assert result.effect_applied is True
    assert result.platform_post_id == "abc123DEF45"
    assert result.platform_url == "https://www.youtube.com/watch?v=abc123DEF45"
    argv = calls[0]
    assert f"-filename={video}" in argv
    assert f"-secrets={secrets}" in argv
    assert f"-cache={token}" in argv
    assert "-oAuthPort=-1" in argv
    assert "-quiet=true" in argv
    assert "-notify=false" in argv
    assert "-sendFilename=false" in argv
    assert content.title not in repr(argv)
    assert content.body not in repr(argv)
    assert "must-not-be-used" not in repr(argv)
    assert seen_metadata["containsSyntheticMedia"] is True
    assert seen_metadata["privacyStatus"] == "private"
    assert any(tag.startswith("aiops_") for tag in seen_metadata["tags"])
    assert "access-redacted" not in result.model_dump_json()
    evidence_path = home / "recovery" / f"{content.operation_id}.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["job_id"] == 42
    assert evidence["video_id"] == "abc123DEF45"
    assert evidence["outcome"] == "confirmed"
    assert content.title not in evidence_path.read_text(encoding="utf-8")
    durable = read_publish_receipt(42, content.operation_id)
    assert durable is not None
    assert durable["platform_post_id"] == "abc123DEF45"


def test_exact_approval_is_paused_before_upload_without_channel_identity(
    youtube_settings,
    monkeypatch,
):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    _, _, _, video = _prepare(pub, 8, assets)

    async def version_ready(account_id):
        return True, "v1.25.5"

    async def must_not_upload(*args, **kwargs):
        raise AssertionError("upload subprocess must not start")

    monkeypatch.setattr(pub, "_audited_version_ready", version_ready)
    monkeypatch.setattr(pub, "_run", must_not_upload)
    content = _content(str(video)).model_copy(
        update={
            "title": "  Human-approved title  ",
            "exact_approval": True,
            "operation_id": "e" * 32,
        }
    )

    result = asyncio.run(pub.publish(8, {}, content))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert "agent contract rendering" in (result.error or "").lower()


def test_valid_receipt_after_nonzero_exit_is_confirmed_partial(youtube_settings, monkeypatch):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    _, _, _, video = _prepare(pub, 1, assets)

    async def ready(account_id):
        return True, "v1.25.5"

    async def fake_run(account_id, *args, timeout=None):
        receipt = next(
            value.split("=", 1)[1] for value in args if value.startswith("-metaJSONout=")
        )
        with open(receipt, "w", encoding="utf-8") as handle:
            json.dump({"id": "abc123DEF45", "status": {"privacyStatus": "private"}}, handle)
        return _CommandResult(started=True, returncode=1)

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", fake_run)

    result = asyncio.run(pub.publish(1, {}, _content(str(video))))

    assert result.success is True
    assert result.effect_applied is True
    assert result.raw_response["outcome"] == "published_partial"
    assert result.raw_response["needs_reconciliation"] is True


def test_receipt_privacy_mismatch_is_known_effect_without_retry(youtube_settings, monkeypatch):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    _, _, _, video = _prepare(pub, 1, assets)

    async def ready(account_id):
        return True, "v1.25.5"

    async def fake_run(account_id, *args, timeout=None):
        receipt = next(
            value.split("=", 1)[1] for value in args if value.startswith("-metaJSONout=")
        )
        with open(receipt, "w", encoding="utf-8") as handle:
            json.dump({"id": "abc123DEF45", "status": {"privacyStatus": "private"}}, handle)
        return _CommandResult(started=True, returncode=0)

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", fake_run)

    result = asyncio.run(pub.publish(1, {}, _content(str(video), privacy="public")))

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.platform_post_id == "abc123DEF45"
    assert result.raw_response["outcome"] == "published_partial"


@pytest.mark.parametrize("returncode", [0, 1])
def test_started_upload_without_receipt_is_uncertain(youtube_settings, monkeypatch, returncode):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    _, _, _, video = _prepare(pub, 1, assets)

    async def ready(account_id):
        return True, "v1.25.5"

    async def fake_run(account_id, *args, timeout=None):
        return _CommandResult(started=True, returncode=returncode)

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", fake_run)

    result = asyncio.run(pub.publish(1, {}, _content(str(video))))

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.raw_response["outcome"] == "unknown"


def test_deleted_receipt_stays_unknown_and_writes_redacted_recovery_evidence(
    youtube_settings, monkeypatch
):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    home, _, _, video = _prepare(pub, 3, assets)

    async def ready(account_id):
        return True, "v1.25.5"

    async def delete_receipt(account_id, *args, timeout=None):
        receipt = Path(
            next(value.split("=", 1)[1] for value in args if value.startswith("-metaJSONout="))
        )
        receipt.unlink()
        return _CommandResult(started=True, returncode=0)

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", delete_receipt)

    result = asyncio.run(pub.publish(3, {}, _content(str(video))))

    assert result.success is False
    assert result.outcome_uncertain is True
    evidence_files = list((home / "recovery").glob("*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["outcome"] == "unknown_after_upload"
    assert evidence["reconciliation_tag"].startswith("aiops_")
    assert "AI 视频标题" not in evidence_files[0].read_text(encoding="utf-8")


def test_recovery_evidence_failure_never_masks_the_platform_outcome(tmp_path):
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("block recovery mkdir", encoding="utf-8")

    YoutubeUploaderPublisher._write_recovery_evidence(
        not_a_directory,
        "attempt123",
        None,
        outcome="unknown_after_upload",
    )


def test_missing_token_fails_before_write(youtube_settings, monkeypatch):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    home = pub._account_home(1, create=True)
    (home / "client_secrets.json").write_text(
        json.dumps({"installed": {"client_id": "id"}}), encoding="utf-8"
    )
    video = assets / "clip.mp4"
    video.write_bytes(b"video")

    async def ready(account_id):
        return True, "v1.25.5"

    async def must_not_run(*args, **kwargs):
        raise AssertionError("upload subprocess must not start")

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", must_not_run)

    result = asyncio.run(pub.publish(1, {}, _content(str(video))))
    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False


def test_cancel_keeps_redacted_recovery_evidence(youtube_settings, monkeypatch):
    _, assets = youtube_settings
    pub = YoutubeUploaderPublisher()
    home, _, _, video = _prepare(pub, 2, assets)

    async def ready(account_id):
        return True, "v1.25.5"

    async def cancel_after_receipt(account_id, *args, timeout=None):
        receipt = next(
            value.split("=", 1)[1] for value in args if value.startswith("-metaJSONout=")
        )
        with open(receipt, "w", encoding="utf-8") as handle:
            json.dump({"id": "abc123DEF45", "status": {"privacyStatus": "private"}}, handle)
        raise asyncio.CancelledError

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", cancel_after_receipt)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pub.publish(2, {}, _content(str(video))))

    evidence_files = list((home / "recovery").glob("*.json"))
    assert len(evidence_files) == 1
    evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
    assert evidence["video_id"] == "abc123DEF45"
    assert "AI 视频标题" not in evidence_files[0].read_text(encoding="utf-8")


def test_feature_flag_registers_only_audited_youtube_cli(youtube_settings, monkeypatch):
    monkeypatch.setattr(settings, "youtube_uploader_enabled", True)
    kinds = [publisher.kind for publisher in build_default_registry().resolve(Platform.YOUTUBE)]

    assert kinds == [PublisherKind.YOUTUBE_UPLOADER]


def test_version_gate_accepts_only_the_audited_release(youtube_settings, tmp_path, monkeypatch):
    fake = tmp_path / "youtubeuploader"
    fake.write_text(
        "#!/bin/sh\necho 'Youtubeuploader version: 1.25.4'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "youtube_uploader_bin", str(fake))
    pub = YoutubeUploaderPublisher()

    ready, reason = asyncio.run(pub._audited_version_ready(1))

    assert ready is False
    assert "v1.25.5" in reason
