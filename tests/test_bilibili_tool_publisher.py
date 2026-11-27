"""Offline contract tests for the bilitool 0.1.3 adapter.

No real bilitool, Bilibili account or network is used.  Result-semantics tests
stub the subprocess layer; the argv/isolation tests spawn a fake ``bilitool``
shell script from a temporary account venv so the real ``exec`` path runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers.base import AgentContractRendererUnavailable
from ai_ops.publishers.bilibili_tool import (
    BilibiliToolPublisher,
    _CommandResult,
    _parse_bvid,
    _validated_metadata,
    project_bilibili_agent_payload,
)
from ai_ops.runtime.receipts import read_publish_receipt

BVID = "BV1xx411c7mD"
# Rendered exactly as upstream's logging format with f"upload success!\tbvid:{bvid}".
SUCCESS_LINE = f"[INFO] - [2025-01-15 10:00:00,123 bilitool] - upload success!\tbvid:{BVID}\n"


@pytest.fixture
def bili_settings(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    assets = tmp_path / "data"
    profiles.mkdir()
    assets.mkdir()
    monkeypatch.setattr(settings, "bilitool_profile_root", profiles)
    monkeypatch.setattr(settings, "bilitool_asset_root", assets)
    monkeypatch.setattr(settings, "agent_asset_vault_root", assets)
    monkeypatch.setattr(settings, "bilitool_bin", "bilitool")
    monkeypatch.setattr(settings, "bilitool_timeout_seconds", 1)
    monkeypatch.setattr(settings, "bilitool_max_video_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "control-data")
    return profiles, assets


def _content(video: str, **overrides) -> PublishContent:
    base = dict(
        title="AI 视频标题",
        body="视频简介",
        content_type=ContentType.VIDEO,
        videos=[video],
        tags=["AI", "自动化"],
    )
    base.update(overrides)
    return PublishContent(**base)


def _video(assets) -> str:
    path = assets / "clip.mp4"
    path.write_bytes(b"fake-local-video")
    return str(path)


async def _healthy(account_id):
    return AccountHealth.HEALTHY, ""


def _ready(pub: BilibiliToolPublisher, monkeypatch, *, health=_healthy) -> list:
    """Bypass version gate and session probe; return the argv recorder."""

    calls: list[tuple[str, ...]] = []

    async def version_ready(account_id):
        return True, "0.1.3"

    async def fake_run(account_id, *args, timeout=None):
        calls.append(args)
        return _CommandResult(started=True, returncode=0)

    monkeypatch.setattr(pub, "_audited_version_ready", version_ready)
    monkeypatch.setattr(pub, "_session_health", health)
    monkeypatch.setattr(pub, "_run", fake_run)
    return calls


# ----- pure parsing / validation -----


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (SUCCESS_LINE, BVID),
        # tqdm progress noise precedes the bvid line in a real upload.
        ("Uploading video: 100%|##| 7.43M/7.43M [00:03<00:00]\r\n" + SUCCESS_LINE, BVID),
        # Publish rejected: upstream logs one [ERROR] line and still exits 0.
        ("[ERROR] - [2025-01-15 10:00:00,001 bilitool] - 标题只能包含中文\n", None),
        # A bvid echoed in an unrelated line is not a success receipt.
        (f"[INFO] - [.. bilitool] - The {BVID} to be uploaded\n", None),
        ("upload success!\tbvid:BV1short\n", None),
        ("", None),
    ],
)
def test_bvid_only_from_upstream_success_line(stderr, expected):
    assert _parse_bvid(stderr) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"videos": ["/vault/one.mp4", "/vault/two.mp4"]},
        {"images": ["/vault/cover.jpg"]},
        {"extra": {"unknown": True}},
        {"extra": {"bilibili_copyright": True}},
        {"extra": {"biliboli_copyright": 1}},
        {"extra": {"bilibili_tid": 0}},
        {"extra": {"bilibili_tid": "17"}},
        {"extra": {"bilibili_cdn_line": "nope"}},
        {"title": "   "},
        {"title": "长" * 81},
        {"body": "长" * 2001},
        {"tags": ["ok", "  "]},
        {"tags": [f"t{index}" for index in range(11)]},
        {"tags": ["标" * 21]},
        {"title": "a\x00b"},
    ],
)
def test_invalid_content_is_rejected_before_any_subprocess(
    bili_settings, monkeypatch, overrides
):
    pub = BilibiliToolPublisher()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("no subprocess may start for invalid content")

    monkeypatch.setattr(pub, "_run", must_not_run)
    monkeypatch.setattr(pub, "_audited_version_ready", must_not_run)

    result = asyncio.run(pub.publish(1, {}, _content("/vault/video.mp4", **overrides)))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False


def test_agent_projection_is_path_free_but_exact_renderer_is_not_opted_in():
    pub = BilibiliToolPublisher()

    projection = project_bilibili_agent_payload(_content("/private/agent-vault/v.mp4"))

    assert pub.supports_agent_contract_renderer is False
    with pytest.raises(AgentContractRendererUnavailable):
        pub.agent_contract_digest_material(_content("/private/agent-vault/v.mp4"))
    assert "/private/agent-vault" not in json.dumps(projection, ensure_ascii=False)
    assert projection["title"] == "AI 视频标题"
    assert projection["tag"] == "AI,自动化"
    # Default is upstream's own default and must be stated, not inherited.
    assert projection["copyright"] == 2
    assert projection["tid"] == 138
    assert projection["cdn"] == ""


def test_metadata_validation_error_surfaces_as_renderer_unavailable():
    with pytest.raises(AgentContractRendererUnavailable):
        project_bilibili_agent_payload(_content("/v.mp4", title=""))


def test_metadata_defaults_are_explicit():
    metadata = _validated_metadata(_content("/v.mp4", extra={"bilibili_copyright": 1}))
    assert metadata == {
        "copyright": 1,
        "title": "AI 视频标题",
        "desc": "视频简介",
        "tid": 138,
        "tag": "AI,自动化",
        "source": "来源于网络",
        "cdn": "",
        "dynamic": "",
    }


# ----- publish result semantics -----


def test_success_requires_bvid_line_not_exit_code(bili_settings, monkeypatch):
    _, assets = bili_settings
    pub = BilibiliToolPublisher()
    video = _video(assets)

    async def run(account_id, *args, timeout=None):
        return _CommandResult(started=True, returncode=0, stderr=SUCCESS_LINE)

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", run)
    content = _content(video)
    content.job_id = 7
    content.operation_id = "a" * 32

    result = asyncio.run(pub.publish(7, {"SESSDATA": "must-not-be-used"}, content))

    assert result.success is True
    assert result.effect_applied is True
    assert result.platform_post_id == BVID
    assert result.platform_url == f"https://www.bilibili.com/video/{BVID}"
    assert result.raw_response["outcome"] == "confirmed"
    assert "must-not-be-used" not in result.model_dump_json()
    durable = read_publish_receipt(7, content.operation_id)
    assert durable is not None
    assert durable["platform_post_id"] == BVID


@pytest.mark.parametrize("returncode", [0, 1, None])
def test_started_without_bvid_is_unknown_even_on_exit_zero(bili_settings, monkeypatch, returncode):
    """Upstream exits 0 when Bilibili rejects the publish. No bvid means unknown."""
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        stderr = "[ERROR] - [2025-01-15 10:00:00,001 bilitool] - 标题不合规\n"
        return _CommandResult(started=True, returncode=returncode, stderr=stderr)

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", run)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.platform_post_id is None
    assert result.raw_response["write_started"] is True
    assert result.raw_response["outcome"] == "unknown"


def test_timeout_without_bvid_is_unknown(bili_settings, monkeypatch):
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        return _CommandResult(started=True, returncode=None, timed_out=True)

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", run)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False


def test_bvid_after_timeout_is_confirmed_and_never_retried(bili_settings, monkeypatch):
    """A killed process that already printed the receipt still created the archive."""
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        return _CommandResult(
            started=True, returncode=None, timed_out=True, stderr=SUCCESS_LINE
        )

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", run)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is True
    assert result.platform_post_id == BVID
    assert result.raw_response["outcome"] == "confirmed_partial"
    assert result.raw_response["needs_reconciliation"] is True


def test_unstarted_process_is_a_safe_failure_that_may_fall_back(bili_settings, monkeypatch):
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        return _CommandResult(started=False, error="bilitool 未安装或不可执行")

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", run)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.raw_response["write_started"] is False


def test_not_logged_in_fails_before_the_write_starts(bili_settings, monkeypatch):
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        raise AssertionError("upload must not start without a healthy session")

    async def expired(account_id):
        return AccountHealth.EXPIRED, "B站 CLI 登录态缺失或已失效"

    _ready(pub, monkeypatch, health=expired)
    monkeypatch.setattr(pub, "_run", run)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert "登录态" in (result.error or "")


@pytest.mark.parametrize(
    "stage,reason",
    [
        ("_audited_version_ready", "bilitool 版本 0.2.0 未经审计，仅允许 0.1.3"),
        ("_session_health", "B站 CLI 登录态缺失或已失效"),
    ],
)
def test_environment_failures_keep_the_retry_budget_and_allow_fallback(
    bili_settings, monkeypatch, stage, reason
):
    """An unprovisioned account is not a verdict on the content.

    Enabling the canary must not hurt accounts that still publish through
    social-auto-upload: the registry may fall back, and the job keeps its
    retry budget.  ``_try_publishers`` falls back only while all of
    ``success``/``effect_applied``/``outcome_uncertain`` stay false.
    """

    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def must_not_upload(account_id, *args, timeout=None):
        raise AssertionError("upload must not start")

    async def version_not_ready(account_id):
        return False, reason

    async def version_ready(account_id):
        return True, "0.1.3"

    async def logged_out(account_id):
        return AccountHealth.EXPIRED, reason

    monkeypatch.setattr(pub, "_run", must_not_upload)
    monkeypatch.setattr(
        pub, "_audited_version_ready", version_not_ready if stage == "_audited_version_ready" else version_ready
    )
    monkeypatch.setattr(pub, "_session_health", logged_out)

    result = asyncio.run(pub.publish(1, {}, _content(_video(assets))))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert result.error == reason


def test_exact_approval_fails_closed_before_any_write(bili_settings, monkeypatch):
    _, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("no subprocess may start for an exact-approval job")

    monkeypatch.setattr(pub, "_run", must_not_run)
    monkeypatch.setattr(pub, "_audited_version_ready", must_not_run)

    result = asyncio.run(
        pub.publish(1, {}, _content(_video(assets)).model_copy(update={"exact_approval": True}))
    )

    assert result.success is False
    assert result.effect_applied is False
    assert "Agent contract rendering" in (result.error or "")


def test_video_must_be_a_regular_file_inside_the_asset_root(bili_settings, monkeypatch):
    profiles, assets = bili_settings
    pub = BilibiliToolPublisher()

    async def must_not_upload(account_id, *args, timeout=None):
        raise AssertionError("an invalid video must be refused before the upload starts")

    _ready(pub, monkeypatch)
    monkeypatch.setattr(pub, "_run", must_not_upload)

    (profiles / "outside.mp4").write_bytes(b"video")
    (assets / "clip.txt").write_bytes(b"text")
    empty = assets / "empty.mp4"
    empty.write_bytes(b"")
    oversized = assets / "big.mp4"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    (assets / "real.mp4").write_bytes(b"video")
    (assets / "linked.mp4").symlink_to(assets / "real.mp4")

    cases = [
        (str(profiles / "outside.mp4"), "受支持本地视频"),
        (str(assets / "missing.mp4"), "不存在或不可读"),
        (str(assets / "clip.txt"), "受支持本地视频"),
        (str(empty), "为空或超过配置大小上限"),
        (str(oversized), "为空或超过配置大小上限"),
        (str(assets / "linked.mp4"), "符号链接"),
    ]
    for candidate, expected_error in cases:
        result = asyncio.run(pub.publish(1, {}, _content(candidate)))
        assert result.success is False, candidate
        assert result.effect_applied is False, candidate
        assert result.outcome_uncertain is False, candidate
        assert expected_error in (result.error or ""), (candidate, result.error)


# ----- session / login -----


def test_health_maps_check_markers_not_exit_codes(bili_settings, monkeypatch):
    pub = BilibiliToolPublisher()

    async def version_ready(account_id):
        return True, "0.1.3"

    monkeypatch.setattr(pub, "_audited_version_ready", version_ready)

    cases = [
        ("Current account: 某UP主\nCurrent level: 5\nStatus: 非大会员\n", AccountHealth.HEALTHY),
        (
            "There is currently no login account, some functions may not work\n",
            AccountHealth.EXPIRED,
        ),
        ("Check failed, please check the info\n", AccountHealth.UNKNOWN),
        ("Traceback (most recent call last):\n", AccountHealth.UNKNOWN),
    ]
    for stdout, expected in cases:
        # Every case above exits 0 upstream, so the exit code carries no signal.
        monkeypatch.setattr(
            pub,
            "_run",
            lambda *a, stdout=stdout, **k: _async(_CommandResult(started=True, returncode=0, stdout=stdout)),
        )
        assert asyncio.run(pub.health_check(1, {})) == expected, stdout


def _async(value):
    async def coroutine():
        return value

    return coroutine()


def test_version_gate_rejects_unaudited_builds(bili_settings, monkeypatch):
    pub = BilibiliToolPublisher()

    for stdout, expected_ok in [
        ("bilitool 0.1.3 and source code at https://github.com/timerring/bilitool\n", True),
        # `main` HEAD still reports 0.1.3 but has dropped --copyright.
        ("bilitool 0.1.4 and source code at https://github.com/timerring/bilitool\n", False),
        ("bilitool 0.1.2 and source code at https://github.com/timerring/bilitool\n", False),
        ("", False),
    ]:
        monkeypatch.setattr(
            pub,
            "_run",
            lambda *a, stdout=stdout, **k: _async(_CommandResult(started=True, returncode=0, stdout=stdout)),
        )
        ok, reason = asyncio.run(pub._audited_version_ready(1))
        assert ok is expected_ok, stdout
        if not ok:
            assert "未经审计" in reason


def test_login_imports_operator_cookie_file_and_never_starts_qr_login(bili_settings, monkeypatch):
    profiles, _ = bili_settings
    pub = BilibiliToolPublisher()
    home = profiles / "account_3"
    home.mkdir(parents=True)
    cookie_file = home / "cookie.json"
    cookie_file.write_text(json.dumps(_exported_cookie()), encoding="utf-8")

    calls: list[tuple[str, ...]] = []

    async def run(account_id, *args, timeout=None):
        calls.append(args)
        if args[0] == "--version":
            return _CommandResult(started=True, returncode=0, stdout=_VERSION_OUTPUT)
        if args[0] == "login":
            assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
            return _CommandResult(started=True, returncode=0, stdout="Login success!\n")
        return _CommandResult(started=True, returncode=0, stdout="Current account: 某UP主\n")

    monkeypatch.setattr(pub, "_run", run)

    assert asyncio.run(pub.login(3, {})) is True
    assert pub.last_login_error is None
    assert calls[1] == ("login", "-f", str(cookie_file))
    # The interactive QR path (bare `login`) must never be driven from here.
    assert all(args[0] != "login" or "-f" in args for args in calls)


def test_login_without_cookie_file_explains_the_trusted_terminal_step(bili_settings, monkeypatch):
    pub = BilibiliToolPublisher()

    async def run(account_id, *args, timeout=None):
        return _CommandResult(started=True, returncode=0, stdout=_VERSION_OUTPUT)

    monkeypatch.setattr(pub, "_run", run)

    assert asyncio.run(pub.login(3, {})) is False
    assert "bilitool login --export" in (pub.last_login_error or "")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"access_token": "t", "cookie_info": {"cookies": []}}},
        [1, 2, 3],
    ],
)
def test_login_rejects_cookie_files_that_are_not_the_exported_shape(
    bili_settings, monkeypatch, payload
):
    profiles, _ = bili_settings
    pub = BilibiliToolPublisher()
    home = profiles / "account_3"
    home.mkdir(parents=True)
    (home / "cookie.json").write_text(json.dumps(payload), encoding="utf-8")
    (home / "cookie.json").chmod(0o644)

    async def must_not_import(account_id, *args, timeout=None):
        raise AssertionError("an invalid cookie file must not be handed to upstream")

    async def version_ready(account_id):
        return True, "0.1.3"

    monkeypatch.setattr(pub, "_audited_version_ready", version_ready)
    monkeypatch.setattr(pub, "_run", must_not_import)

    assert asyncio.run(pub.login(3, {})) is False
    assert "结构无效" in (pub.last_login_error or "")


def _exported_cookie() -> dict:
    return {
        "code": 0,
        "data": {
            "access_token": "access-secret",
            "cookie_info": {
                "cookies": [
                    {"name": "SESSDATA", "value": "sess-secret"},
                    {"name": "bili_jct", "value": "jct-secret"},
                    {"name": "DedeUserID", "value": "123"},
                    {"name": "DedeUserID__ckMd5", "value": "md5"},
                    {"name": "sid", "value": "sid-secret"},
                ]
            },
        },
    }


_VERSION_OUTPUT = "bilitool 0.1.3 and source code at https://github.com/timerring/bilitool\n"


# ----- real exec path: argv, isolation and credential boundaries -----


def _install_fake_bilitool(profiles, account_id: int, body: str) -> None:
    bin_dir = profiles / f"account_{account_id}" / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "bilitool"
    binary.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    binary.chmod(0o700)


def test_real_exec_passes_every_flag_and_no_secret(bili_settings, monkeypatch):
    """Drive the real exec path with a fake bilitool that echoes its argv."""
    profiles, assets = bili_settings
    pub = BilibiliToolPublisher()
    script = """
for a in "$@"; do printf '%s\\n' "$a"; done
printf '%s\\n' "$HOME" "$FERNET_KEY" "$API_KEY"
"""
    _install_fake_bilitool(profiles, 5, script)
    monkeypatch.setenv("FERNET_KEY", "fernet-must-not-leak")
    monkeypatch.setenv("API_KEY", "apikey-must-not-leak")

    result = asyncio.run(pub._run(5, "upload", "v.mp4", "--title", "T; rm -rf /"))

    assert result.started is True
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[:4] == ["upload", "v.mp4", "--title", "T; rm -rf /"]
    # argv list form: the shell metacharacter stayed one literal argument.
    assert lines[4] == str(profiles / "account_5")
    # Control-plane secrets are not inherited across the subprocess boundary.
    assert lines[5:] == ["", ""]


def test_only_the_account_venv_binary_is_used(bili_settings, monkeypatch):
    profiles, _ = bili_settings
    pub = BilibiliToolPublisher()

    # An ambient bilitool on PATH must be ignored.
    ambient = profiles / "ambient-bin"
    ambient.mkdir(parents=True)
    (ambient / "bilitool").write_text("#!/bin/sh\necho ambient\n")
    (ambient / "bilitool").chmod(0o700)
    monkeypatch.setenv("PATH", str(ambient) + os.pathsep + os.environ.get("PATH", ""))

    assert pub._resolved_binary(1) is None
    result = asyncio.run(pub._run(1, "--version"))
    assert result.started is False
    assert "未安装" in (result.error or "")

    _install_fake_bilitool(profiles, 1, "echo account-venv\n")
    _install_fake_bilitool(profiles, 2, "echo other-account\n")
    assert asyncio.run(pub._run(1, "--version")).stdout == "account-venv\n"
    assert asyncio.run(pub._run(2, "--version")).stdout == "other-account\n"


@pytest.mark.parametrize("account_id", [0, -1])
def test_account_id_must_be_positive(bili_settings, account_id):
    pub = BilibiliToolPublisher()
    with pytest.raises(ValueError):
        pub._account_home(account_id, create=True)


def test_symlinked_account_home_is_refused(bili_settings, tmp_path):
    profiles, _ = bili_settings
    pub = BilibiliToolPublisher()
    real = tmp_path / "elsewhere"
    real.mkdir()
    (profiles / "account_1").symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError):
        pub._account_home(1, create=True)


# ----- registry -----


def test_registry_gate_and_priority_over_sau(monkeypatch):
    from ai_ops.publishers.registry import build_default_registry

    monkeypatch.setattr(settings, "bilitool_enabled", False)
    kinds_off = [
        build_default_registry().kind_value(p)
        for p in build_default_registry().resolve(Platform.BILIBILI)
    ]
    assert "bilitool" not in kinds_off
    assert "social_auto_upload" in kinds_off

    monkeypatch.setattr(settings, "bilitool_enabled", True)
    registry_on = build_default_registry()
    resolved = registry_on.resolve(Platform.BILIBILI)
    assert [registry_on.kind_value(p) for p in resolved] == [
        PublisherKind.BILITOOL.value,
        PublisherKind.SOCIAL_AUTO_UPLOAD.value,
    ]


def test_timeout_must_stay_below_the_worker_execution_timeout():
    from ai_ops.config import Settings

    with pytest.raises(ValueError, match="BILITOOL_TIMEOUT_SECONDS"):
        Settings(bilitool_enabled=True, bilitool_timeout_seconds=1800)
