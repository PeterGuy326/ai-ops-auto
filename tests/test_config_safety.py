import pytest
from pydantic import ValidationError

from ai_ops.config import Settings


def test_running_timeout_must_exceed_execution_timeout():
    with pytest.raises(ValidationError, match="JOB_RUNNING_TIMEOUT_SECONDS"):
        Settings(
            _env_file=None,
            job_execution_timeout_seconds=60,
            job_running_timeout_seconds=60,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduler_poll_seconds", 0),
        ("scheduler_max_concurrency", 0),
        ("scheduler_max_concurrency", 101),
        ("metrics_task_collection_timeout_seconds", 0),
        ("metrics_task_lease_seconds", 1),
        ("metrics_task_max_attempts", 0),
        ("metrics_task_max_attempts", 21),
        ("metrics_task_max_concurrency", 0),
        ("metrics_task_account_lock_timeout_seconds", -1),
        ("metrics_task_account_lock_timeout_seconds", 61),
        ("job_execution_timeout_seconds", 0),
        ("sau_cli_timeout_seconds", 0),
        ("sau_cli_timeout_seconds", 7201),
    ],
)
def test_scheduler_safety_bounds_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_zhihu_cli_is_canary_off_by_default():
    config = Settings(_env_file=None)

    assert config.zhihu_cli_enabled is False
    assert config.zhihu_cli_bin == "zhihu"
    assert config.zhihu_cli_max_content_bytes == 60_000


def test_metrics_task_lease_must_cover_collection_and_finalize_margin():
    with pytest.raises(ValidationError, match="METRICS_TASK_LEASE_SECONDS"):
        Settings(
            _env_file=None,
            metrics_task_collection_timeout_seconds=300,
            metrics_task_lease_seconds=300,
        )

    with pytest.raises(ValidationError, match="finalization margin"):
        Settings(
            _env_file=None,
            metrics_task_collection_timeout_seconds=120,
            metrics_task_lease_seconds=150,
        )


def test_metrics_task_recovery_has_bounded_defaults():
    config = Settings(_env_file=None)

    assert config.metrics_task_collection_timeout_seconds == 120
    assert config.metrics_task_lease_seconds == 300
    assert config.metrics_task_max_attempts == 5
    assert config.metrics_task_retry_base_seconds == 300
    assert config.metrics_task_max_concurrency == 4
    assert config.metrics_task_account_lock_timeout_seconds == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("zhihu_cli_timeout_seconds", 0),
        ("zhihu_cli_timeout_seconds", 1801),
        ("zhihu_cli_max_content_bytes", 100),
    ],
)
def test_zhihu_cli_safety_bounds_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_youtube_cli_is_canary_off_by_default():
    config = Settings(_env_file=None)

    assert config.youtube_uploader_enabled is False
    assert config.youtube_uploader_bin == "youtubeuploader"


def test_uncalibrated_browser_stubs_are_off_by_default():
    config = Settings(_env_file=None)

    assert config.baijiahao_publisher_enabled is False
    assert config.sohuhao_publisher_enabled is False


def test_publisher_plugins_are_deny_by_default():
    config = Settings(_env_file=None)

    assert config.publisher_plugin_allowlist == ()


def test_publisher_plugin_allowlist_is_canonical_and_sorted():
    config = Settings(
        _env_file=None,
        publisher_plugin_allowlist=(
            "Zed_Plugin:zed.publisher",
            "acme.plugin:acme.publisher",
        ),
    )

    assert config.publisher_plugin_allowlist == (
        "acme-plugin:acme.publisher",
        "zed-plugin:zed.publisher",
    )


@pytest.mark.parametrize(
    "allowlist",
    [
        ("*",),
        ("bare-entry-point",),
        ("bad path:plugin",),
        ("dist:",),
        ("dist:UPPER",),
        ("dist:plugin", "DIST:plugin"),
        tuple(f"dist-{index}:plugin-{index}" for index in range(33)),
    ],
)
def test_publisher_plugin_allowlist_rejects_unsafe_selection(allowlist):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, publisher_plugin_allowlist=allowlist)


def test_enabled_youtube_timeout_must_leave_worker_cleanup_window():
    with pytest.raises(ValidationError, match="YOUTUBE_UPLOADER_TIMEOUT_SECONDS"):
        Settings(
            _env_file=None,
            youtube_uploader_enabled=True,
            youtube_uploader_timeout_seconds=1800,
            job_execution_timeout_seconds=1800,
        )


def test_disabled_youtube_timeout_does_not_block_unrelated_worker_config():
    config = Settings(
        _env_file=None,
        youtube_uploader_enabled=False,
        youtube_uploader_timeout_seconds=1800,
        job_execution_timeout_seconds=1800,
    )

    assert config.youtube_uploader_enabled is False


def test_github_pages_assets_and_lock_have_safe_defaults():
    config = Settings(_env_file=None)

    assert config.github_pages_asset_root.name == "assets"
    assert config.github_pages_max_image_bytes == 20 * 1024 * 1024
    assert config.github_pages_max_total_image_bytes == 100 * 1024 * 1024
    assert config.github_pages_lock_timeout_seconds == 900
    assert config.github_pages_gh_verify_enabled is False
    assert config.github_pages_repository == ""
    assert config.github_pages_gh_bin == "gh"
    assert config.github_pages_gh_version == "2.97.0"
    assert config.github_pages_gh_sha256 == ""
    assert config.github_pages_gh_token.get_secret_value() == ""
    assert config.github_pages_deploy_timeout_seconds == 600
    assert config.github_pages_verify_poll_seconds == 5
    assert config.github_pages_readback_timeout_seconds == 120
    assert config.github_pages_readback_request_timeout_seconds == 10
    assert config.github_pages_readback_max_response_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("github_pages_max_image_bytes", 100),
        ("github_pages_max_total_image_bytes", 100),
        ("github_pages_lock_timeout_seconds", 0),
        ("github_pages_lock_timeout_seconds", 7201),
        ("github_pages_deploy_timeout_seconds", 0),
        ("github_pages_deploy_timeout_seconds", 3601),
        ("github_pages_verify_poll_seconds", 0),
        ("github_pages_verify_poll_seconds", 61),
        ("github_pages_readback_timeout_seconds", 0),
        ("github_pages_readback_timeout_seconds", 601),
        ("github_pages_readback_request_timeout_seconds", 0),
        ("github_pages_readback_request_timeout_seconds", 61),
        ("github_pages_readback_max_response_bytes", 1023),
        ("github_pages_readback_max_response_bytes", 10 * 1024 * 1024 + 1),
    ],
)
def test_github_pages_asset_and_lock_bounds_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "repository",
    [
        "owner",
        "owner/repo/extra",
        " owner/repo",
        "owner/repo ",
        "https://github.com/owner/repo",
        "-owner/repo",
        "owner-/repo",
        "owner--name/repo",
        "owner/..",
        "owner/repo.git",
        "owner/-repo",
        "owner/.repo",
    ],
)
def test_github_pages_repository_rejects_ambiguous_values(repository):
    with pytest.raises(ValidationError, match="GITHUB_PAGES_REPOSITORY"):
        Settings(_env_file=None, github_pages_repository=repository)


def test_github_pages_repository_accepts_exact_owner_repo():
    config = Settings(_env_file=None, github_pages_repository="PeterGuy326/ai-ops-auto")

    assert config.github_pages_repository == "PeterGuy326/ai-ops-auto"


def test_github_pages_gh_verification_requires_repository():
    with pytest.raises(ValidationError, match="GITHUB_PAGES_REPOSITORY is required"):
        Settings(_env_file=None, github_pages_gh_verify_enabled=True)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "http://owner.github.io/site",
        "https://custom.example/site",
        "https://other.github.io/site",
        "https://owner.github.io/site?preview=1",
        " https://owner.github.io/site",
        "https://owner.github.io/site\n",
        "\x00https://owner.github.io/site",
        "https://own\ter.github.io/site",
    ],
)
def test_github_pages_gh_verification_requires_canonical_owner_base_url(base_url):
    with pytest.raises(ValidationError, match="GITHUB_PAGES_BASE_URL"):
        Settings(
            _env_file=None,
            github_pages_gh_verify_enabled=True,
            github_pages_repository="owner/site",
            github_pages_base_url=base_url,
            github_pages_gh_sha256="a" * 64,
        )


def test_github_pages_gh_verification_accepts_canonical_owner_base_url():
    config = Settings(
        _env_file=None,
        github_pages_gh_verify_enabled=True,
        github_pages_repository="owner/site",
        github_pages_base_url="https://owner.github.io/site/",
        github_pages_gh_sha256="a" * 64,
    )

    assert config.github_pages_gh_verify_enabled is True


def test_github_pages_gh_verification_requires_binary_digest_pin():
    with pytest.raises(ValidationError, match="GITHUB_PAGES_GH_SHA256 is required"):
        Settings(
            _env_file=None,
            github_pages_gh_verify_enabled=True,
            github_pages_repository="owner/site",
            github_pages_base_url="https://owner.github.io/site",
        )


def test_github_pages_gh_version_is_a_source_audited_exact_pin():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            github_pages_repository="owner/repo",
            github_pages_gh_verify_enabled=True,
            github_pages_gh_version="2.98.0",
        )


def test_github_pages_gh_binary_is_a_fixed_contract():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, github_pages_gh_bin="/tmp/unreviewed-gh")


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "sha256:a" + "a" * 57])
def test_github_pages_gh_digest_pin_rejects_noncanonical_values(digest):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, github_pages_gh_sha256=digest)


def test_github_pages_token_is_secret_typed_and_not_rendered():
    config = Settings(_env_file=None, github_pages_gh_token="do-not-render-this-token")

    assert config.github_pages_gh_token.get_secret_value() == "do-not-render-this-token"
    assert "do-not-render-this-token" not in repr(config)
    assert "do-not-render-this-token" not in str(config.model_dump()["github_pages_gh_token"])
