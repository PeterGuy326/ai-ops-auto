"""Contract tests for the optional pyzhihu-cli adapter.

No test imports or contacts the real upstream package.  Subprocess behavior is
faked, except for one local sleeper executable used to prove timeout cleanup.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers.registry import build_default_registry
from ai_ops.publishers.zhihu_cli import (
    ZhihuCliPublisher,
    _CommandResult,
    _parse_article_confirmation,
)
from ai_ops.runtime.receipts import read_publish_receipt


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    profile_root = tmp_path / "profiles"
    asset_root = tmp_path / "data"
    asset_root.mkdir()
    monkeypatch.setattr(settings, "zhihu_cli_profile_root", profile_root)
    monkeypatch.setattr(settings, "zhihu_cli_asset_root", asset_root)
    monkeypatch.setattr(settings, "zhihu_cli_max_content_bytes", 60_000)
    monkeypatch.setattr(settings, "zhihu_cli_max_image_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "zhihu_cli_max_total_image_bytes", 2 * 1024 * 1024)
    monkeypatch.setattr(settings, "zhihu_cli_bin", "zhihu")
    monkeypatch.setattr(settings, "zhihu_cli_timeout_seconds", 1)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "control-data")
    return profile_root, asset_root


def _content(**overrides) -> PublishContent:
    values = {
        "title": "-标题；$(不会执行)",
        "body": "第一段\n\n**加粗** `code`",
        "content_type": ContentType.LONG_ARTICLE,
        "images": [],
        "extra": {"zhihu_topic_ids": ["123", 456]},
    }
    values.update(overrides)
    return PublishContent(**values)


def test_confirmation_requires_matching_numeric_marker_and_url():
    output = (
        "\x1b[32mArticle published!  ID: 12345\x1b[0m\r\n"
        "https://zhuanlan.zhihu.com/p/12345\r\n"
    )
    assert _parse_article_confirmation(output) == (
        "12345",
        "https://zhuanlan.zhihu.com/p/12345",
    )
    assert _parse_article_confirmation(output.replace("/12345", "/99999")) is None
    assert _parse_article_confirmation("Article may have been published but no ID returned") is None


def test_account_profiles_are_isolated_and_private(cli_settings):
    pub = ZhihuCliPublisher()
    first = pub._account_home(1, create=True)
    second = pub._account_home(2, create=True)

    assert first != second
    assert first.name == "account_1"
    assert second.name == "account_2"
    assert first.stat().st_mode & 0o777 == 0o700
    assert (first / ".zhihu-cli").stat().st_mode & 0o777 == 0o700


def test_subprocess_environment_does_not_forward_application_secrets(cli_settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("FERNET_KEY", "do-not-forward-either")
    pub = ZhihuCliPublisher()

    env = pub._subprocess_env(1, create_profile=True)

    assert "OPENAI_API_KEY" not in env
    assert "FERNET_KEY" not in env
    assert env["HOME"].endswith("account_1")
    assert env["NO_COLOR"] == "1"


def test_publish_builds_argv_without_shell_and_keeps_secrets_out_of_result(
    cli_settings, monkeypatch
):
    pub = ZhihuCliPublisher()
    calls: list[tuple[str, ...]] = []

    async def ready(account_id):
        return True, "0.2.4"

    async def healthy(account_id):
        return AccountHealth.HEALTHY, ""

    async def fake_run(account_id, *args, timeout=None):
        calls.append(args)
        return _CommandResult(
            started=True,
            returncode=0,
            stdout=(
                "Article published!  ID: 7788\n"
                "https://zhuanlan.zhihu.com/p/7788\n"
            ),
        )

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_health", healthy)
    monkeypatch.setattr(pub, "_run", fake_run)

    secret = "z_c0=must-not-leak"
    content = _content(job_id=71, operation_id="d" * 32)
    result = asyncio.run(pub.publish(7, {"cookie": secret}, content))

    assert result.success is True
    assert result.platform_post_id == "7788"
    assert calls[0][:5] == ("article", "--topic", "123", "--topic", "456")
    assert "--" in calls[0]
    separator = calls[0].index("--")
    assert calls[0][separator + 1] == "-标题；$(不会执行)"
    assert "<strong>加粗</strong>" in calls[0][separator + 2]
    assert secret not in repr(calls)
    assert secret not in result.model_dump_json()
    assert "cmd" not in result.raw_response
    durable = read_publish_receipt(71, content.operation_id)
    assert durable is not None
    assert durable["platform_post_id"] == "7788"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (0, "Article may have been published but no ID returned"),
        (0, "unparseable output"),
        (1, "Failed to publish article: connection reset"),
    ],
)
def test_started_but_unconfirmed_write_is_uncertain(
    cli_settings, monkeypatch, returncode, stdout
):
    pub = ZhihuCliPublisher()

    async def ready(account_id):
        return True, "0.2.4"

    async def healthy(account_id):
        return AccountHealth.HEALTHY, ""

    async def fake_run(account_id, *args, timeout=None):
        return _CommandResult(started=True, returncode=returncode, stdout=stdout)

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_health", healthy)
    monkeypatch.setattr(pub, "_run", fake_run)

    result = asyncio.run(pub.publish(1, {}, _content()))

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.raw_response["outcome"] == "unknown"
    assert "核验" in (result.error or "")


def test_nonzero_exit_preserves_matching_candidate_identity(cli_settings, monkeypatch):
    pub = ZhihuCliPublisher()

    async def ready(account_id):
        return True, "0.2.4"

    async def healthy(account_id):
        return AccountHealth.HEALTHY, ""

    async def fake_run(account_id, *args, timeout=None):
        return _CommandResult(
            started=True,
            returncode=1,
            stdout=(
                "Article published!  ID: 7788\n"
                "https://zhuanlan.zhihu.com/p/7788\n"
            ),
        )

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_health", healthy)
    monkeypatch.setattr(pub, "_run", fake_run)

    result = asyncio.run(pub.publish(1, {}, _content()))

    assert result.success is False
    assert result.effect_applied is True
    assert result.retryable is False
    assert result.outcome_uncertain is True
    assert result.platform_post_id == "7788"
    assert result.platform_url == "https://zhuanlan.zhihu.com/p/7788"
    assert result.raw_response["outcome"] == "unknown_with_candidate_identity"


def test_preflight_failure_is_safe_for_browser_fallback(cli_settings, monkeypatch):
    pub = ZhihuCliPublisher()

    async def not_ready(account_id):
        return False, "version mismatch"

    monkeypatch.setattr(pub, "_audited_version_ready", not_ready)
    result = asyncio.run(pub.publish(1, {}, _content()))

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.error == "version mismatch"


def test_long_rendered_body_is_rejected_before_write(cli_settings, monkeypatch):
    pub = ZhihuCliPublisher()
    monkeypatch.setattr(settings, "zhihu_cli_max_content_bytes", 1024)

    async def ready(account_id):
        return True, "0.2.4"

    async def healthy(account_id):
        return AccountHealth.HEALTHY, ""

    async def must_not_run(*args, **kwargs):
        raise AssertionError("write subprocess must not start")

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_health", healthy)
    monkeypatch.setattr(pub, "_run", must_not_run)

    result = asyncio.run(pub.publish(1, {}, _content(body="x" * 2000)))
    assert result.success is False
    assert result.outcome_uncertain is False
    assert "argv" in (result.error or "")


def test_image_outside_controlled_root_is_rejected(cli_settings):
    _, asset_root = cli_settings
    outside = asset_root.parent / "outside.jpg"
    outside.write_bytes(b"not-real-but-never-opened")
    pub = ZhihuCliPublisher()

    images, error = pub._validate_images([str(outside)])

    assert images is None
    assert "受控目录" in (error or "")


def test_health_uses_online_whoami_json(cli_settings, monkeypatch):
    pub = ZhihuCliPublisher()
    cookie_file = pub._cookie_file(3)
    cookie_file.parent.mkdir(parents=True)
    cookie_file.write_text(json.dumps({"cookies": {"redacted": True}}), encoding="utf-8")

    async def ready(account_id):
        return True, "0.2.4"

    async def fake_run(account_id, *args, timeout=None):
        assert args == ("whoami", "--json")
        return _CommandResult(started=True, returncode=0, stdout='{"id":"person-id"}')

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_run", fake_run)

    assert asyncio.run(pub.health_check(3, {})) == AccountHealth.HEALTHY


def test_local_subprocess_timeout_is_reaped(cli_settings, tmp_path, monkeypatch):
    fake = tmp_path / "fake-zhihu"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(settings, "zhihu_cli_bin", str(fake))
    pub = ZhihuCliPublisher()

    result = asyncio.run(pub._run(1, "article", timeout=0.02))

    assert result.started is True
    assert result.timed_out is True
    assert result.returncode is None


def test_feature_flag_puts_cli_before_browser(cli_settings, monkeypatch):
    monkeypatch.setattr(settings, "zhihu_cli_enabled", True)
    kinds = [publisher.kind for publisher in build_default_registry().resolve(Platform.ZHIHU)]

    assert kinds[:2] == [PublisherKind.ZHIHU_CLI, PublisherKind.SOCIAL_AUTO_UPLOAD]
