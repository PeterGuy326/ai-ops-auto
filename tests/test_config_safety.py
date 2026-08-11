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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("github_pages_max_image_bytes", 100),
        ("github_pages_max_total_image_bytes", 100),
        ("github_pages_lock_timeout_seconds", 0),
        ("github_pages_lock_timeout_seconds", 7201),
    ],
)
def test_github_pages_asset_and_lock_bounds_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
