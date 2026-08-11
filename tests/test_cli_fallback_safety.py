"""Offline safety contracts for legacy browser-oriented CLI fallbacks."""
from __future__ import annotations

import json

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import xhs_skills as xhs_module
from ai_ops.publishers.xhs_skills import XhsSkillsPublisher


class _FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"private stdout", b"private stderr"


@pytest.mark.asyncio
async def test_xhs_cli_exit_zero_without_post_identity_remains_unknown(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "external_sau_path", tmp_path / "social-auto-upload")
    monkeypatch.setattr(settings, "sau_cli_timeout_seconds", 5)
    script = tmp_path / "XiaohongshuSkills/scripts/publish_pipeline.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake; never executed", encoding="utf-8")
    image = tmp_path / "private.jpg"
    image.write_bytes(b"fake")
    calls: list[tuple[object, ...]] = []

    async def fake_create(*args, **kwargs):
        del kwargs
        calls.append(args)
        return _FakeProcess()

    monkeypatch.setattr(xhs_module.asyncio, "create_subprocess_exec", fake_create)
    content = PublishContent(
        title="private title",
        body="private body",
        content_type=ContentType.IMAGE_TEXT,
        images=[str(image)],
    )

    result = await XhsSkillsPublisher().publish(1, {}, content)

    assert calls
    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    serialized = json.dumps(result.raw_response, ensure_ascii=False)
    assert content.title not in serialized
    assert content.body not in serialized
    assert str(image) not in serialized
    assert "cmd" not in result.raw_response
    assert "stdout" not in result.raw_response


@pytest.mark.asyncio
async def test_xhs_cli_spawn_failure_is_safe_for_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "external_sau_path", tmp_path / "social-auto-upload")
    script = tmp_path / "XiaohongshuSkills/scripts/publish_pipeline.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake; never executed", encoding="utf-8")

    async def fail_create(*args, **kwargs):
        del args, kwargs
        raise OSError("not executable")

    monkeypatch.setattr(xhs_module.asyncio, "create_subprocess_exec", fail_create)
    result = await XhsSkillsPublisher().publish(
        1,
        {},
        PublishContent(
            title="title",
            body="body",
            content_type=ContentType.IMAGE_TEXT,
            images=[str(tmp_path / "image.jpg")],
        ),
    )

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.effect_applied is False
