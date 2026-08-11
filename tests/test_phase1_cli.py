"""CLI contracts for the five-minute diagnostic and offline demo path."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.doctor import CheckOutcome, DoctorCheck, DoctorReport


def test_doctor_json_and_strict_exit_policy(monkeypatch):
    report = DoctorReport(
        (
            DoctorCheck("core", CheckOutcome.PASS, "ready"),
            DoctorCheck("optional", CheckOutcome.WARN, "not installed"),
        )
    )
    monkeypatch.setattr("ai_ops.doctor.run_doctor", lambda: report)
    runner = CliRunner()

    default = runner.invoke(cli.app, ["doctor", "--json"])
    strict = runner.invoke(cli.app, ["doctor", "--json", "--strict"])

    assert default.exit_code == 0
    assert json.loads(default.stdout) == report.to_dict(strict=False)
    assert strict.exit_code == 1
    assert json.loads(strict.stdout) == report.to_dict(strict=True)


def test_doctor_reports_invalid_settings_as_redacted_json(tmp_path):
    secret = "must-not-appear-in-doctor-output"
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": f"postgresql://user:{secret}@private.invalid/db",
            "SCHEDULER_BACKEND": "not-implemented",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "ai_ops.cli", "doctor", "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_configuration"
    assert secret not in result.stdout


def test_demo_json_runs_the_complete_synthetic_chain():
    result = CliRunner().invoke(cli.app, ["demo", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["notice"] == "SYNTHETIC — NO EXTERNAL ACTION"
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["external_calls"] == 0
    assert payload["credentials_used"] is False
    assert payload["review"]["passed"] is True
    assert payload["storage"]["cleanup_performed"] is True


def test_demo_cli_refuses_existing_database_without_traceback(tmp_path):
    database = tmp_path / "operator.sqlite3"
    original = b"existing operator data"
    database.write_bytes(original)

    result = CliRunner().invoke(
        cli.app,
        ["demo", "--json", "--database", str(database)],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "demo_version": "offline-demo-v1",
        "error": {
            "code": "database_exists",
            "message": "演示数据库已存在；请选择一个尚不存在的路径",
        },
        "exit_code": 1,
        "ok": False,
    }
    assert "Traceback" not in result.output
    assert database.read_bytes() == original


def test_demo_cli_refuses_dangling_database_symlink(tmp_path):
    database = tmp_path / "requested.sqlite3"
    target = tmp_path / "must-not-be-created.sqlite3"
    database.symlink_to(target)

    result = CliRunner().invoke(
        cli.app,
        ["demo", "--json", "--database", str(database)],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "database_exists"
    assert database.is_symlink()
    assert not target.exists()


def test_demo_generic_json_error_is_stable_and_redacted(monkeypatch):
    secret = "must-not-leak-from-demo-error"

    async def fail_demo(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("ai_ops.demo.run_offline_demo", fail_demo)

    result = CliRunner().invoke(cli.app, ["demo", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "demo_version": "offline-demo-v1",
        "error": {"code": "demo_failed", "message": "离线演示失败（RuntimeError）"},
        "exit_code": 1,
        "ok": False,
    }
    assert secret not in result.stdout


def test_demo_failed_review_has_the_same_top_level_exit_contract(monkeypatch):
    from ai_ops.scheduler import worker as worker_module

    monkeypatch.setattr(
        worker_module,
        "_persist_initial_metrics",
        lambda *_args, **_kwargs: None,
    )

    result = CliRunner().invoke(cli.app, ["demo", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert payload["review"]["passed"] is False


def test_demo_explicit_database_retention_and_cleanup_flags(tmp_path):
    retained_path = tmp_path / "retained.sqlite3"
    cleaned_path = tmp_path / "cleaned.sqlite3"
    runner = CliRunner()

    retained = runner.invoke(
        cli.app,
        ["demo", "--json", "--database", str(retained_path)],
    )
    cleaned = runner.invoke(
        cli.app,
        [
            "demo",
            "--json",
            "--database",
            str(cleaned_path),
            "--no-keep-data",
        ],
    )

    assert retained.exit_code == 0, retained.output
    assert retained_path.is_file()
    assert json.loads(retained.stdout)["storage"]["retained"] is True
    assert cleaned.exit_code == 0, cleaned.output
    assert not cleaned_path.exists()
    assert json.loads(cleaned.stdout)["storage"]["cleanup_performed"] is True
