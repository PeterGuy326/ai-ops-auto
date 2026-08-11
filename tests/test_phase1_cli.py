"""CLI contracts for the five-minute diagnostic and offline demo path."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.config import settings
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


def test_doctor_reports_malformed_agent_principals_as_invalid_configuration(tmp_path):
    environment = os.environ.copy()
    environment["AGENT_PRINCIPALS"] = "not-json"

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
    assert json.loads(result.stdout)["error"]["code"] == "invalid_configuration"


def test_runtime_command_redacts_all_settings_when_configuration_is_invalid(tmp_path):
    secrets = {
        "API_KEY": "legacy-secret-must-never-enter-traceback",
        "OPENAI_API_KEY": "provider-secret-must-never-enter-traceback",
    }
    environment = os.environ.copy()
    environment.update(secrets)
    environment["SCHEDULER_BACKEND"] = "not-implemented"

    result = subprocess.run(
        [sys.executable, "-m", "ai_ops.cli", "serve", "--host", "127.0.0.1"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "configuration validation failed" in output
    assert "Traceback" not in output
    for secret in secrets.values():
        assert secret not in output


def test_serve_refuses_explicit_dev_bypass_on_non_loopback(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "agent_principals", [])
    monkeypatch.setattr(settings, "legacy_dev_auth_bypass", True)

    def must_not_start(*_args, **_kwargs):
        raise AssertionError("uvicorn must not start")

    monkeypatch.setattr("uvicorn.run", must_not_start)

    result = CliRunner().invoke(cli.app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 1
    assert "may only bind to a loopback host" in result.output


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
