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
