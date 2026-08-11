"""Behavior contracts for the isolated five-minute offline demo."""

from __future__ import annotations

import asyncio
from pathlib import Path
import socket

import pytest
from sqlalchemy import URL, create_engine, func, select

from ai_ops.config import settings
from ai_ops.core.models import Metrics, PublishJob
from ai_ops.demo import FakePublisher, run_offline_demo
from ai_ops.demo.backends import FAKE_PUBLISHER_KIND
from ai_ops.publishers.registry import default_registry


def _forbid_network(monkeypatch) -> None:
    def fail_connect(*args, **kwargs):
        raise AssertionError("offline demo attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)


def _without_storage(summary) -> dict:
    payload = summary.model_dump(mode="json")
    payload.pop("storage")
    return payload


def test_offline_demo_runs_full_chain_without_network_or_credentials(monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(
        default_registry,
        "resolve",
        lambda platform: (_ for _ in ()).throw(
            AssertionError("offline demo touched the production publisher registry")
        ),
    )
    import ai_ops.notify as notify_module
    from ai_ops.scheduler import metrics as metrics_module

    def fail_external_hook(*args, **kwargs):
        raise AssertionError("offline demo touched a production side-effect hook")

    monkeypatch.setattr(notify_module, "publish_success", fail_external_hook)
    monkeypatch.setattr(notify_module, "publish_failed", fail_external_hook)
    monkeypatch.setattr(metrics_module, "schedule_after_publish", fail_external_hook)

    summary = asyncio.run(run_offline_demo())

    assert summary.synthetic is True
    assert summary.ok is True
    assert summary.exit_code == 0
    assert summary.notice == "SYNTHETIC — NO EXTERNAL ACTION"
    assert summary.offline is True
    assert summary.external_calls == 0
    assert summary.credentials_used is False
    assert summary.review.passed is True
    assert [stage.name for stage in summary.stages] == [
        "ingest",
        "review",
        "dry-run plan",
        "durable job",
        "fake publish",
        "fake metrics",
        "final review",
    ]
    assert summary.stages[2].details == {"external_calls": 0, "jobs_created": 0}
    assert summary.stages[3].details["survived_reopen"] is True
    assert summary.review.row_counts == {
        "topics": 1,
        "accounts": 1,
        "articles": 1,
        "publish_jobs": 1,
        "metrics": 2,
    }
    assert summary.storage.retained is False
    assert summary.storage.cleanup_performed is True
    assert summary.storage.database_path is None
    assert "离线演示：PASS" in summary.to_human_text()


def test_explicit_demo_database_is_durable_and_repeatable(tmp_path, monkeypatch):
    _forbid_network(monkeypatch)
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"

    first = asyncio.run(run_offline_demo(first_path))
    second = asyncio.run(run_offline_demo(second_path))

    assert first_path.is_file()
    assert second_path.is_file()
    assert first_path.stat().st_mode & 0o077 == 0
    assert second_path.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob(".ai-ops-offline-demo-*"))
    assert first.storage.database_path == str(first_path)
    assert first.storage.retained is True
    assert _without_storage(first) == _without_storage(second)

    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(first_path)),
        future=True,
    )
    try:
        with engine.connect() as connection:
            jobs = connection.scalar(select(func.count()).select_from(PublishJob))
            metrics = connection.scalar(select(func.count()).select_from(Metrics))
        assert jobs == 1
        assert metrics == 2
    finally:
        engine.dispose()


def test_demo_refuses_to_overwrite_an_existing_database(tmp_path):
    database = tmp_path / "existing.sqlite3"
    original = b"operator data must survive"
    database.write_bytes(original)

    with pytest.raises(FileExistsError):
        asyncio.run(run_offline_demo(database))

    assert database.read_bytes() == original


@pytest.mark.parametrize("suffix", ["-journal", "-shm", "-wal"])
def test_demo_refuses_and_preserves_preexisting_sidecars(tmp_path, suffix):
    database = tmp_path / "operator.sqlite3"
    sidecar = Path(f"{database}{suffix}")
    original = f"operator-owned{suffix}".encode()
    sidecar.write_bytes(original)

    with pytest.raises(FileExistsError):
        asyncio.run(run_offline_demo(database, keep_data=False))

    assert not database.exists()
    assert sidecar.read_bytes() == original
    assert not list(tmp_path.glob(".ai-ops-offline-demo-*"))


def test_demo_refuses_dangling_sidecar_symlink(tmp_path):
    database = tmp_path / "operator.sqlite3"
    missing_target = tmp_path / "missing-sidecar-target"
    sidecar = Path(f"{database}-wal")
    sidecar.symlink_to(missing_target)

    with pytest.raises(FileExistsError):
        asyncio.run(run_offline_demo(database, keep_data=False))

    assert sidecar.is_symlink()
    assert not missing_target.exists()


def test_explicit_demo_database_can_be_cleaned_safely(tmp_path):
    database = tmp_path / "clean-me.sqlite3"

    summary = asyncio.run(run_offline_demo(database, keep_data=False))

    assert not database.exists()
    assert tmp_path.is_dir()
    assert summary.storage.retained is False
    assert summary.storage.cleanup_performed is True


def test_demo_reports_cleanup_failure_instead_of_claiming_success(tmp_path, monkeypatch):
    from ai_ops.demo import runner as demo_runner

    original_rmtree = demo_runner.shutil.rmtree

    def preserve_demo_workdir(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.parent == tmp_path and candidate.name.startswith(
            ".ai-ops-offline-demo-"
        ):
            return None
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(demo_runner.shutil, "rmtree", preserve_demo_workdir)
    summary = asyncio.run(
        run_offline_demo(tmp_path / "requested.sqlite3", keep_data=False)
    )

    assert summary.storage.cleanup_performed is False
    assert summary.storage.retained is True
    assert summary.storage.database_path is not None
    assert Path(summary.storage.database_path).is_file()
    assert not (tmp_path / "requested.sqlite3").exists()


def test_demo_does_not_touch_configured_data_dir(tmp_path, monkeypatch):
    configured_data_dir = tmp_path / "production-data-must-not-exist"
    monkeypatch.setattr(settings, "data_dir", configured_data_dir)

    summary = asyncio.run(run_offline_demo())

    assert summary.review.passed is True
    assert settings.data_dir == configured_data_dir
    assert not configured_data_dir.exists()


def test_demo_worker_policy_is_deterministic_not_production_config(monkeypatch):
    monkeypatch.setattr(settings, "publish_max_per_day", 0)
    monkeypatch.setattr(settings, "nurture_days", 100_000)
    monkeypatch.setattr(settings, "job_execution_timeout_seconds", 1)
    monkeypatch.setattr(settings, "account_operation_lock_timeout_seconds", 1)

    summary = asyncio.run(run_offline_demo())

    assert summary.review.passed is True
    assert summary.review.job_status == "success"


def test_demo_error_paths_do_not_use_global_exception_telemetry(monkeypatch):
    from ai_ops.scheduler import worker as worker_module

    def fail_metrics(*args, **kwargs):
        raise RuntimeError("forced local-only demo failure")

    def fail_if_reported(*args, **kwargs):
        raise AssertionError("offline demo touched global exception telemetry")

    monkeypatch.setattr(worker_module, "_persist_initial_metrics", fail_metrics)
    monkeypatch.setattr(worker_module, "capture_exception", fail_if_reported)

    summary = asyncio.run(run_offline_demo())

    assert summary.offline is True
    assert summary.external_calls == 0
    assert summary.review.passed is False
    assert summary.ok is False
    assert summary.exit_code == 1


def test_concurrent_demos_keep_database_lock_and_receipt_state_isolated(
    tmp_path,
    monkeypatch,
):
    _forbid_network(monkeypatch)

    async def run_both():
        return await asyncio.gather(
            run_offline_demo(tmp_path / "concurrent-a.sqlite3"),
            run_offline_demo(tmp_path / "concurrent-b.sqlite3"),
        )

    first, second = asyncio.run(run_both())

    assert first.review.passed is True
    assert second.review.passed is True
    assert _without_storage(first) == _without_storage(second)


def test_fake_backend_rejects_credentials_and_is_not_in_default_registry():
    publisher = FakePublisher()

    with pytest.raises(ValueError, match="do not accept credentials"):
        asyncio.run(publisher.login(1, {"token": "must-not-be-used"}))

    registered_kinds = {
        default_registry.kind_value(candidate)
        for platform in default_registry.supported_platforms()
        for candidate in default_registry.resolve(platform)
    }
    assert FAKE_PUBLISHER_KIND not in registered_kinds
