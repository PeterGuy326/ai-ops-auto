"""Real smoke tests for user-facing database initialization entrypoints."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.core import db as db_mod
from ai_ops.core.models import ApprovalRequest, Asset, Base


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def _assert_database_at_head(url: str) -> None:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revision == db_mod.get_code_alembic_head()
        inspector = inspect(engine)
        assert {"publish_jobs", "metrics", "metrics_collection_tasks"} <= set(
            inspector.get_table_names()
        )
        assert "collection_task_id" in {
            column["name"] for column in inspector.get_columns("metrics")
        }
    finally:
        engine.dispose()


def test_runtime_sqlite_connections_enforce_declared_foreign_keys():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    db_mod.enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                ApprovalRequest.__table__.insert().values(
                    plan_id=999,
                    plan_digest="a" * 64,
                    status="pending",
                    requested_by="agent",
                    requested_by_type="agent",
                )
            )
    engine.dispose()


@pytest.mark.parametrize(
    ("content_sha256", "size_bytes", "storage_kind"),
    [
        ("a" * 64, 7, None),
        (None, 7, "agent_vault_v1"),
        ("a" * 64, None, "agent_vault_v1"),
        ("a" * 64, 7, "unsupported_vault"),
    ],
)
def test_runtime_metadata_rejects_partial_or_unsupported_asset_vault_rows(
    content_sha256,
    size_bytes,
    storage_kind,
):
    """Base.create_all must enforce the same fail-closed rule as Alembic."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    Asset.__table__.insert().values(
                        asset_type="image",
                        source="user_upload",
                        local_path="legacy/cover.png",
                        content_sha256=content_sha256,
                        size_bytes=size_bytes,
                        storage_kind=storage_kind,
                        meta={},
                    )
                )
    finally:
        engine.dispose()


def test_ai_ops_init_db_really_migrates_empty_database(tmp_path, monkeypatch):
    db_url = _sqlite_url(tmp_path / "cli.db")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(db_mod.settings, "database_url", db_url)
    existing_logger = logging.getLogger("ai_ops.safe_init_regression")
    existing_logger.disabled = False

    result = CliRunner().invoke(cli.app, ["init-db"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().endswith("OK: db initialized")
    assert existing_logger.disabled is False
    _assert_database_at_head(db_url)


def test_init_db_script_defaults_to_real_alembic_path(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    db_url = _sqlite_url(tmp_path / "script.db")
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "init_db.py")],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Alembic head" in result.stdout
    _assert_database_at_head(db_url)


def test_docker_entrypoint_uses_safe_initializer(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    db_url = _sqlite_url(tmp_path / "entrypoint.db")
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env.pop("SKIP_MIGRATIONS", None)

    result = subprocess.run(
        ["bash", str(repo_root / "docker-entrypoint.sh"), "true"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running: ai-ops init-db" in result.stdout
    assert "handing off to CMD: true" in result.stdout
    _assert_database_at_head(db_url)


def test_ai_ops_init_db_refuses_unknown_unversioned_database(tmp_path, monkeypatch):
    db_url = _sqlite_url(tmp_path / "unsafe.db")
    engine = create_engine(db_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE customer_data (id INTEGER PRIMARY KEY)"))
    finally:
        engine.dispose()
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(db_mod.settings, "database_url", db_url)

    result = CliRunner().invoke(cli.app, ["init-db"])

    assert result.exit_code == 1
    assert "initialization refused or failed" in result.output
    assert db_url not in result.output
    engine = create_engine(db_url, future=True)
    try:
        assert set(inspect(engine).get_table_names()) == {"customer_data"}
    finally:
        engine.dispose()
