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
from ai_ops.publishers.base import AgentContractRendererUnavailable
from ai_ops.publishers.registry import build_default_registry
from ai_ops.publishers.zhihu_cli import (
    ZhihuCliPublisher,
    _CommandResult,
    _parse_article_confirmation,
    _parse_whoami_external_account_id,
    normalize_zhihu_external_account_id,
    project_zhihu_agent_payload,
)
from ai_ops.runtime.receipts import read_publish_receipt


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    profile_root = tmp_path / "profiles"
    asset_root = tmp_path / "data"
    asset_root.mkdir()
    monkeypatch.setattr(settings, "zhihu_cli_profile_root", profile_root)
    monkeypatch.setattr(settings, "zhihu_cli_asset_root", asset_root)
    monkeypatch.setattr(settings, "agent_asset_vault_root", asset_root)
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


def test_agent_contract_renderer_descriptor_and_projection_are_stable_and_path_free(
    cli_settings,
):
    from PIL import Image

    _, asset_root = cli_settings
    image_paths = [asset_root / "first.jpg", asset_root / "second.jpg"]
    for path in image_paths:
        Image.new("RGB", (2, 2), color=(25, 50, 75)).save(path, format="JPEG")
    content = _content(
        images=[str(path) for path in image_paths],
        exact_approval=True,
    )
    publisher = ZhihuCliPublisher()

    projection = project_zhihu_agent_payload(content)
    material = publisher.agent_contract_digest_material(content)
    descriptor = publisher.agent_contract_renderer_descriptor

    assert publisher.supports_agent_contract_renderer is True
    assert descriptor is not None
    assert descriptor.renderer_id == "zhihu-cli.article-argv"
    assert descriptor.contract_version == (
        "4+python-markdown-3.10.3+account-id+bounds-v1+media-preflight-v1"
    )
    assert descriptor.adapter_version == "0.2.4"
    assert descriptor.requires_external_account_id is True
    assert material["renderer"]["requires_external_account_id"] is True
    assert descriptor.asset_rules[0].digest_material() == {
        "asset_type": "image",
        "min_count": 0,
        "max_count": 9,
    }
    assert projection == material["payload"]
    assert projection["topic_ids"] == ["123", "456"]
    assert projection["image_slots"] == [
        {"asset_type": "image", "index": 0},
        {"asset_type": "image", "index": 1},
    ]
    assert "<strong>加粗</strong>" in projection["body_html"]
    assert str(asset_root) not in json.dumps(material, ensure_ascii=False)


def test_agent_contract_renderer_rejects_media_that_publish_would_reject(
    cli_settings,
    monkeypatch,
):
    _, asset_root = cli_settings
    invalid_jpeg = asset_root / "invalid.jpg"
    invalid_jpeg.write_bytes(b"not-a-jpeg")
    publisher = ZhihuCliPublisher()

    with pytest.raises(AgentContractRendererUnavailable, match="media preflight"):
        publisher.agent_contract_digest_material(
            _content(images=[str(invalid_jpeg)], exact_approval=True)
        )

    from PIL import Image

    valid_jpeg = asset_root / "valid.jpg"
    Image.new("RGB", (2, 2), color=(25, 50, 75)).save(valid_jpeg, format="JPEG")
    monkeypatch.setattr(settings, "zhihu_cli_max_image_bytes", 1)
    with pytest.raises(AgentContractRendererUnavailable, match="media preflight"):
        publisher.agent_contract_digest_material(
            _content(images=[str(valid_jpeg)], exact_approval=True)
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"tags": ["unsupported"]},
        {"videos": ["/vault/video.mp4"]},
        {"content_type": ContentType.AUDIO},
        {"extra": {"zhihu_topic_ids": ["123"], "unknown": True}},
        {"extra": {"tags": []}},
    ],
)
def test_agent_contract_renderer_rejects_unaccounted_zhihu_fields(updates):
    content = _content(exact_approval=True).model_copy(update=updates)

    with pytest.raises(AgentContractRendererUnavailable):
        project_zhihu_agent_payload(content)


@pytest.mark.parametrize(
    "topic_ids",
    [
        [str(value) for value in range(1, 22)],
        ["01"],
        ["0"],
        ["1" * 33],
        ["123", "123"],
    ],
)
def test_agent_projection_bounds_topic_ids(topic_ids):
    content = _content(extra={"zhihu_topic_ids": topic_ids}, exact_approval=True)

    with pytest.raises(AgentContractRendererUnavailable):
        project_zhihu_agent_payload(content)


def test_agent_projection_bounds_rendered_html_by_configured_utf8_bytes(monkeypatch):
    monkeypatch.setattr(settings, "zhihu_cli_max_content_bytes", 100)

    with pytest.raises(AgentContractRendererUnavailable, match="content-byte limit"):
        project_zhihu_agent_payload(_content(body="界" * 40, exact_approval=True))


def test_confirmation_requires_matching_numeric_marker_and_url():
    output = (
        "\x1b[32mArticle published!  ID: 12345\x1b[0m\r\nhttps://zhuanlan.zhihu.com/p/12345\r\n"
    )
    assert _parse_article_confirmation(output) == (
        "12345",
        "https://zhuanlan.zhihu.com/p/12345",
    )
    assert _parse_article_confirmation(output.replace("/12345", "/99999")) is None
    assert _parse_article_confirmation("Article may have been published but no ID returned") is None


def test_whoami_uses_stable_id_and_never_falls_back_to_mutable_url_token():
    assert _parse_whoami_external_account_id('{"id":"person_123","url_token":"vanity"}') == (
        "zhihu:id:person_123"
    )
    assert _parse_whoami_external_account_id('{"url_token":"vanity"}') is None
    assert _parse_whoami_external_account_id('{"id":"not:canonical"}') is None
    assert _parse_whoami_external_account_id("not-json") is None
    assert normalize_zhihu_external_account_id("zhihu:id:person_123") == ("zhihu:id:person_123")
    with pytest.raises(ValueError):
        normalize_zhihu_external_account_id(" person_123 ")


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
            stdout=("Article published!  ID: 7788\nhttps://zhuanlan.zhihu.com/p/7788\n"),
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


def test_exact_approval_does_not_trim_the_reviewed_title(cli_settings, monkeypatch):
    pub = ZhihuCliPublisher()
    calls: list[tuple[str, ...]] = []

    async def ready(account_id):
        return True, "0.2.4"

    async def identity(account_id):
        return AccountHealth.HEALTHY, "", "zhihu:id:person-id"

    async def fake_run(account_id, *args, timeout=None):
        calls.append(args)
        return _CommandResult(
            started=True,
            returncode=0,
            stdout=("Article published!  ID: 7788\nhttps://zhuanlan.zhihu.com/p/7788\n"),
        )

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_identity", identity)
    monkeypatch.setattr(pub, "_run", fake_run)
    content = _content(
        title="  Human-approved title  ",
        exact_approval=True,
        approved_external_account_id="zhihu:id:person-id",
        operation_id="f" * 32,
    )
    projection = project_zhihu_agent_payload(content)

    result = asyncio.run(pub.publish(7, {}, content))

    assert result.success is True
    separator = calls[0].index("--")
    assert calls[0][separator + 1] == "  Human-approved title  "
    assert calls[0][separator + 2] == projection["body_html"]


def test_exact_approval_account_mismatch_fails_before_write_with_constant_time_compare(
    cli_settings,
    monkeypatch,
):
    from ai_ops.publishers import zhihu_cli as module

    pub = ZhihuCliPublisher()
    compared: list[tuple[str, str]] = []

    async def ready(account_id):
        return True, "0.2.4"

    async def identity(account_id):
        return AccountHealth.HEALTHY, "", "zhihu:id:currently-logged-in"

    async def must_not_write(*args, **kwargs):
        raise AssertionError("article subprocess must not start")

    def compare_digest(observed, approved):
        compared.append((observed, approved))
        return False

    monkeypatch.setattr(pub, "_audited_version_ready", ready)
    monkeypatch.setattr(pub, "_session_identity", identity)
    monkeypatch.setattr(pub, "_run", must_not_write)
    monkeypatch.setattr(module.secrets, "compare_digest", compare_digest)

    result = asyncio.run(
        pub.publish(
            7,
            {},
            _content(
                exact_approval=True,
                approved_external_account_id="zhihu:id:human-approved",
            ),
        )
    )

    assert result.success is False
    assert result.retryable is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert "写入未开始" in (result.error or "")
    assert compared == [("zhihu:id:currently-logged-in", "zhihu:id:human-approved")]


def test_interactive_login_holds_account_operation_lease(cli_settings, monkeypatch):
    from ai_ops.publishers import zhihu_cli as module

    pub = ZhihuCliPublisher()
    events: list[object] = []

    class FakeLease:
        def __init__(self, account_id, *, timeout_seconds):
            events.append(("lease", account_id, timeout_seconds))

        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("exit")

    async def locked(account_id):
        events.append(("login", account_id))
        pub.last_external_account_id = "zhihu:id:person-id"
        return True

    monkeypatch.setattr(module, "AccountOperationLease", FakeLease)
    monkeypatch.setattr(pub, "_login_interactive_locked", locked)
    monkeypatch.setattr(settings, "account_operation_lock_timeout_seconds", 23)

    assert asyncio.run(pub.login_interactive(9)) is True
    assert events == [("lease", 9, 23), "enter", ("login", 9), "exit"]
    assert pub.last_external_account_id == "zhihu:id:person-id"


def test_interactive_login_fails_safely_when_account_operation_lease_is_busy(
    cli_settings,
    monkeypatch,
):
    from ai_ops.publishers import zhihu_cli as module

    pub = ZhihuCliPublisher()

    class BusyLease:
        def __init__(self, account_id, *, timeout_seconds):
            pass

        async def __aenter__(self):
            raise module.AccountOperationLeaseTimeout("busy")

        async def __aexit__(self, exc_type, exc, traceback):
            raise AssertionError("a failed enter must not call exit")

    async def must_not_login(account_id):
        raise AssertionError("login must not start without the account lease")

    monkeypatch.setattr(module, "AccountOperationLease", BusyLease)
    monkeypatch.setattr(pub, "_login_interactive_locked", must_not_login)

    assert asyncio.run(pub.login_interactive(9)) is False
    assert "其他操作" in (pub.last_login_error or "")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (0, "Article may have been published but no ID returned"),
        (0, "unparseable output"),
        (1, "Failed to publish article: connection reset"),
    ],
)
def test_started_but_unconfirmed_write_is_uncertain(cli_settings, monkeypatch, returncode, stdout):
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
            stdout=("Article published!  ID: 7788\nhttps://zhuanlan.zhihu.com/p/7788\n"),
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
