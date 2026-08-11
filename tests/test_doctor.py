from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, text

from ai_ops.core.models import Base
from ai_ops.doctor import (
    CheckOutcome,
    DoctorCheck,
    DoctorReport,
    _adapter_checks,
    _browser_check,
    _database_checks,
    _default_resource_roots,
    _migration_bundle,
    _migration_heads,
    _publication_safety_check,
    _resource_check,
    _runtime_check,
    _scheduler_check,
    _security_checks,
    run_doctor,
)


def _config(tmp_path: Path, **overrides):
    values = {
        "database_url": f"sqlite:///{tmp_path / 'doctor.db'}",
        "data_dir": tmp_path,
        "auto_publish_enabled": False,
        "github_pages_dry_run": True,
        "zhihu_cli_enabled": False,
        "zhihu_cli_bin": "zhihu",
        "youtube_uploader_enabled": False,
        "youtube_uploader_bin": "youtubeuploader",
        "baijiahao_publisher_enabled": False,
        "sohuhao_publisher_enabled": False,
        "fernet_key": Fernet.generate_key().decode(),
        "api_host": "127.0.0.1",
        "api_key": "test-api-key",
        "legacy_dev_auth_bypass": False,
        "scheduler_backend": "apscheduler",
        "scheduler_timezone": "Asia/Shanghai",
        "scheduler_poll_seconds": 15,
        "scheduler_max_concurrency": 4,
        "job_retry_base_seconds": 60,
        "job_execution_timeout_seconds": 1800,
        "job_running_timeout_seconds": 7200,
        "publish_min_interval_seconds": 14400,
        "publish_max_per_day": 2,
        "nurture_days": 7,
        "publish_jitter_seconds": 600,
        "browser_engine": "playwright_chromium",
        "browser_cdp_url": "",
        "browser_proxy": "",
        "external_sau_path": tmp_path / "missing-sau",
        "external_sau_url": "",
        "external_mpt_path": tmp_path / "missing-mpt",
        "external_mpt_url": "",
        "mpt_python": "",
        "funclip_path": tmp_path / "missing-funclip",
        "funclip_python": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _source_resource_root() -> Path:
    return next(root for root in _default_resource_roots() if _migration_bundle(root))


def _create_at_head_database(config) -> None:
    head = _migration_heads(_source_resource_root())[0]
    engine = create_engine(config.database_url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            # These feature models live in separate modules and are irrelevant
            # to the Agent-contract shape assertions below.  Doctor still
            # treats their migrated tables as core deployment resources.
            for table_name in (
                "resume_profiles",
                "job_postings",
                "job_matches",
                "applications",
                "job_accounts",
            ):
                connection.execute(
                    text(f"CREATE TABLE IF NOT EXISTS {table_name} (id INTEGER PRIMARY KEY)")
                )
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                {"head": head},
            )
    finally:
        engine.dispose()


def test_report_has_stable_json_and_deterministic_exit_policy():
    report = DoctorReport(
        (
            DoctorCheck("required", CheckOutcome.PASS, "ready"),
            DoctorCheck("optional", CheckOutcome.WARN, "not installed"),
        )
    )

    assert report.exit_code == 0
    assert report.exit_code_for(strict=True) == 1
    payload = json.loads(report.to_json(strict=True))
    assert payload["schema_version"] == 1
    assert payload["exit_code"] == 1
    assert payload["summary"] == {"fail": 0, "pass": 1, "skip": 0, "warn": 1}
    assert payload["checks"][1]["severity"] == "warning"


def test_missing_sqlite_database_fails_without_creating_file(tmp_path):
    config = _config(tmp_path)
    database_path = tmp_path / "doctor.db"

    report = run_doctor(config, module_probe=lambda _name: True)

    assert not database_path.exists()
    checks = {check.check_id: check for check in report.checks}
    assert checks["database.connectivity"].outcome == CheckOutcome.FAIL
    assert checks["database.schema"].outcome == CheckOutcome.SKIP
    assert report.exit_code == 1


def test_sqlite_file_uri_cannot_redirect_probe_to_a_decoded_target(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "must-not-be-created.sqlite3"
    encoded_target = quote(str(target), safe="")
    # This decoy made the historical Path preflight pass, while SQLite decoded
    # the URI and opened a different absolute target.
    (tmp_path / f"file:{encoded_target}").touch()
    config = _config(
        tmp_path,
        database_url=f"sqlite:///file:{encoded_target}?uri=true",
    )

    report = run_doctor(config, module_probe=lambda _name: True)

    assert report.exit_code == 1
    assert not target.exists()
    check = {item.check_id: item for item in report.checks}["database.connectivity"]
    assert check.outcome == CheckOutcome.FAIL
    assert "URI mode" in check.summary


@pytest.mark.parametrize("kind", ["wal", "journal"])
def test_active_sqlite_recovery_sidecar_is_refused_without_new_files(tmp_path, kind):
    config = _config(tmp_path)
    _create_at_head_database(config)
    database_path = tmp_path / "doctor.db"
    sidecar_path = Path(f"{database_path}-{kind}")
    shm_path = Path(f"{database_path}-shm")
    sidecar_path.write_bytes(b"active-recovery-sentinel")
    shm_path.unlink(missing_ok=True)

    report = run_doctor(config, module_probe=lambda _name: True)

    assert report.exit_code == 1
    assert sidecar_path.read_bytes() == b"active-recovery-sentinel"
    assert not shm_path.exists()
    check = {item.check_id: item for item in report.checks}["database.connectivity"]
    assert check.details["active_sidecar"] == kind


def test_sqlite_probe_preserves_database_bytes_metadata_and_directory_entries(tmp_path):
    config = _config(tmp_path)
    _create_at_head_database(config)
    database_path = tmp_path / "doctor.db"
    before_bytes = database_path.read_bytes()
    before_stat = database_path.stat()
    before_entries = sorted(path.name for path in tmp_path.iterdir())

    report = run_doctor(config, module_probe=lambda _name: True)

    after_stat = database_path.stat()
    assert report.exit_code == 0
    assert database_path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert sorted(path.name for path in tmp_path.iterdir()) == before_entries


def test_postgresql_probe_sets_transaction_read_only_before_catalog_queries(monkeypatch, tmp_path):
    statements: list[str] = []

    class FakeResult:
        def scalars(self):
            return ["head"]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            statements.append(str(statement))
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

        def dispose(self):
            return None

    class FakeInspector:
        def get_table_names(self):
            return [
                "topics",
                "articles",
                "assets",
                "accounts",
                "publication_plans",
                "approval_requests",
                "agent_operations",
                "publish_jobs",
                "metrics",
                "resume_profiles",
                "job_postings",
                "job_matches",
                "applications",
                "job_accounts",
                "alembic_version",
            ]

        def get_columns(self, table_name):
            from ai_ops.doctor import _AGENT_CONTRACT_REQUIRED_COLUMNS

            return [{"name": name} for name in _AGENT_CONTRACT_REQUIRED_COLUMNS.get(table_name, ())]

        def get_foreign_keys(self, table_name):
            from ai_ops.doctor import _AGENT_CONTRACT_REQUIRED_FOREIGN_KEYS

            return [
                {
                    "constrained_columns": constrained,
                    "referred_table": referred_table,
                    "referred_columns": referred,
                }
                for source, constrained, referred_table, referred in (
                    _AGENT_CONTRACT_REQUIRED_FOREIGN_KEYS
                )
                if source == table_name
            ]

        def get_unique_constraints(self, table_name):
            from ai_ops.doctor import _AGENT_CONTRACT_REQUIRED_UNIQUES

            return [
                {"column_names": columns}
                for source, columns in _AGENT_CONTRACT_REQUIRED_UNIQUES
                if source == table_name
            ]

        def get_check_constraints(self, table_name):
            from ai_ops.doctor import _AGENT_CONTRACT_REQUIRED_CHECKS

            return [{"name": name} for name in _AGENT_CONTRACT_REQUIRED_CHECKS]

    monkeypatch.setattr("ai_ops.doctor.create_engine", lambda *_args, **_kwargs: FakeEngine())
    monkeypatch.setattr("ai_ops.doctor.inspect", lambda _connection: FakeInspector())

    checks = _database_checks(
        _config(tmp_path, database_url="postgresql://user:secret@db.invalid/ai_ops"),
        ("head",),
    )

    assert checks[0].outcome == CheckOutcome.PASS
    assert statements[:2] == ["SET TRANSACTION READ ONLY", "SELECT 1"]


def test_database_at_head_and_core_configuration_have_no_failures(tmp_path):
    config = _config(tmp_path)
    _create_at_head_database(config)

    report = run_doctor(
        config,
        module_probe=lambda _name: True,
        executable_probe=lambda _name: "/usr/bin/tool",
    )

    checks = {check.check_id: check for check in report.checks}
    assert checks["resources.packaged"].outcome == CheckOutcome.PASS
    assert checks["database.connectivity"].outcome == CheckOutcome.PASS
    assert checks["database.schema"].outcome == CheckOutcome.PASS
    assert not [check for check in report.checks if check.outcome == CheckOutcome.FAIL]
    assert report.exit_code == 0


def test_schema_at_head_still_fails_when_a_migrated_table_is_missing(tmp_path):
    config = _config(tmp_path)
    _create_at_head_database(config)
    engine = create_engine(config.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE job_accounts"))
    finally:
        engine.dispose()

    report = run_doctor(config, module_probe=lambda _name: True)
    schema = {check.check_id: check for check in report.checks}["database.schema"]

    assert schema.outcome == CheckOutcome.FAIL
    assert schema.details["missing_core_tables"] == ["job_accounts"]


def test_schema_at_head_fails_when_agent_contract_columns_are_missing(tmp_path):
    config = _config(tmp_path)
    _create_at_head_database(config)
    engine = create_engine(config.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE agent_operations"))
            connection.execute(
                text(
                    "CREATE TABLE agent_operations ("
                    "id INTEGER PRIMARY KEY, principal_id VARCHAR(128) NOT NULL)"
                )
            )
    finally:
        engine.dispose()

    report = run_doctor(config, module_probe=lambda _name: True)
    schema = {check.check_id: check for check in report.checks}["database.schema"]

    assert schema.outcome == CheckOutcome.FAIL
    assert "response_json" in schema.details["missing_columns"]["agent_operations"]


def test_missing_packaged_resources_is_a_core_failure(tmp_path):
    empty_package = tmp_path / "empty-package"
    empty_package.mkdir()

    check, heads = _resource_check((tmp_path / "missing",), empty_package)

    assert check.check_id == "resources.packaged"
    assert check.outcome == CheckOutcome.FAIL
    assert heads == ()


def test_resource_check_requires_every_server_rendered_ui_template(tmp_path):
    package_root = tmp_path / "package"
    templates = package_root / "api" / "templates"
    templates.mkdir(parents=True)
    for name in (
        "base.html",
        "dashboard.html",
        "login.html",
        "list.html",
        "article_detail.html",
    ):
        (templates / name).write_text("template", encoding="utf-8")

    check, _heads = _resource_check((_source_resource_root(),), package_root)

    assert check.outcome == CheckOutcome.FAIL
    assert check.details["missing_ui_templates"] == ["account_detail.html"]


def test_migration_graph_probe_does_not_import_or_compile_revision_files(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (tmp_path / "alembic" / "env.py").write_text("", encoding="utf-8")
    (versions / "one.py").write_text(
        'revision: str = "one"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "two.py").write_text(
        'revision = "two"\ndown_revision: str = "one"\n',
        encoding="utf-8",
    )

    assert _migration_heads(tmp_path) == ("two",)
    assert not (versions / "__pycache__").exists()


def test_migration_graph_probe_rejects_a_cycle_hidden_by_an_independent_head(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    for name, parent in (("a", "b"), ("b", "a"), ("only-head", None)):
        (versions / f"{name}.py").write_text(
            f"revision = {name!r}\ndown_revision = {parent!r}\n",
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="cycle"):
        _migration_heads(tmp_path)


def test_live_publication_settings_are_warning_not_failure(tmp_path):
    config = _config(tmp_path, auto_publish_enabled=True, github_pages_dry_run=False)

    check = _publication_safety_check(config)

    assert check.outcome == CheckOutcome.WARN
    assert check.details["enabled_capabilities"] == ["auto_publish", "github_pages_live"]


def test_unsafe_publication_policy_is_a_core_failure(tmp_path):
    config = _config(tmp_path, publish_max_per_day=0, nurture_days=-1)

    check = _publication_safety_check(config)

    assert check.outcome == CheckOutcome.FAIL
    assert "publish_max_per_day is outside safe bounds" in check.details["problems"]
    assert "nurture_days is outside safe bounds" in check.details["problems"]


def test_data_directory_requires_search_permission(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr("ai_ops.doctor.os.access", lambda _path, mode: not (mode & 1))

    by_id = {check.check_id: check for check in _runtime_check(config)}

    assert by_id["runtime.data_dir"].outcome == CheckOutcome.FAIL


def test_invalid_scheduler_configuration_is_a_core_failure(tmp_path):
    config = _config(
        tmp_path,
        scheduler_timezone="Invalid/Timezone",
        job_execution_timeout_seconds=100,
        job_running_timeout_seconds=100,
    )

    check = _scheduler_check(config, lambda _name: True)

    assert check.outcome == CheckOutcome.FAIL
    assert "invalid timezone" in check.details["problems"]
    assert "running timeout must exceed execution timeout" in check.details["problems"]


def test_missing_selected_browser_runtime_is_only_a_warning(tmp_path):
    config = _config(tmp_path, browser_engine="camoufox")

    check = _browser_check(config, lambda _name: False, lambda _name: None)

    assert check.outcome == CheckOutcome.WARN
    assert check.details["python_module_available"] is False
    assert check.details["probed_online"] is False


def test_browser_module_without_verified_artifact_is_not_reported_ready(tmp_path):
    config = _config(tmp_path, browser_engine="playwright_chromium")

    check = _browser_check(config, lambda _name: True, lambda _name: None)

    assert check.outcome == CheckOutcome.WARN
    assert check.details["python_module_available"] is True
    assert check.details["browser_artifact_verified"] is False


def test_unprobed_cdp_and_invalid_proxy_are_visible_warnings(tmp_path):
    cdp = _browser_check(
        _config(tmp_path, browser_cdp_url="http://127.0.0.1:9333"),
        lambda _name: True,
        lambda _name: "/usr/bin/chrome",
    )
    proxy = _browser_check(
        _config(tmp_path, browser_proxy="not-a-proxy"),
        lambda _name: True,
        lambda _name: "/usr/bin/chrome",
    )

    assert cdp.outcome == CheckOutcome.WARN
    assert cdp.details["cdp_configured"] is True
    assert proxy.outcome == CheckOutcome.WARN
    assert proxy.details["proxy_syntax_valid"] is False


def test_enabled_optional_binary_adapter_missing_is_warning(tmp_path):
    config = _config(tmp_path, zhihu_cli_enabled=True)

    checks = _adapter_checks(config, lambda _name: None)
    by_id = {check.check_id: check for check in checks}

    assert by_id["adapter.zhihu_cli"].outcome == CheckOutcome.WARN
    assert by_id["adapter.youtube_uploader"].outcome == CheckOutcome.SKIP


def test_local_video_adapter_requires_repository_isolated_python(tmp_path):
    mpt_root = tmp_path / "MoneyPrinterTurbo"
    mpt_root.mkdir()
    (mpt_root / "main.py").write_text("", encoding="utf-8")
    config = _config(tmp_path, external_mpt_path=mpt_root, mpt_python="")

    check = {item.check_id: item for item in _adapter_checks(config, lambda _name: None)}[
        "adapter.money_printer_turbo"
    ]

    assert check.outcome == CheckOutcome.WARN
    assert "isolated Python is not configured" in check.details["problems"]

    python = mpt_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    python.chmod(0o700)
    (mpt_root / ".venv" / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    config.mpt_python = str(python)

    ready = {item.check_id: item for item in _adapter_checks(config, lambda _name: None)}[
        "adapter.money_printer_turbo"
    ]
    assert ready.outcome == CheckOutcome.PASS


def test_invalid_optional_adapter_endpoint_is_warning(tmp_path):
    config = _config(tmp_path, external_sau_url="http://[")

    by_id = {check.check_id: check for check in _adapter_checks(config, lambda _name: None)}

    assert by_id["adapter.social_auto_upload"].outcome == CheckOutcome.WARN
    assert by_id["adapter.social_auto_upload"].details["endpoint_syntax_valid"] is False


def test_local_adapter_pass_only_claims_static_entrypoint_presence(tmp_path):
    sau_root = tmp_path / "social-auto-upload"
    sau_root.mkdir()
    (sau_root / "sau_cli.py").write_text("", encoding="utf-8")

    by_id = {
        check.check_id: check
        for check in _adapter_checks(
            _config(tmp_path, external_sau_path=sau_root),
            lambda _name: None,
        )
    }
    check = by_id["adapter.social_auto_upload"]

    assert check.outcome == CheckOutcome.PASS
    assert "entrypoint is present" in check.summary
    assert "available" not in check.summary
    assert check.details["version_probed"] is False
    assert check.details["login_probed"] is False
    assert check.details["probed_online"] is False


def test_report_never_contains_raw_database_proxy_or_endpoint_values(tmp_path):
    secret = "never-print-this-token"
    config = _config(
        tmp_path,
        database_url=f"sqlite:///{tmp_path / secret}",
        browser_proxy=f"http://{secret}@proxy.invalid",
        external_sau_url=f"https://{secret}.internal.invalid/api",
    )

    rendered = run_doctor(config, module_probe=lambda _name: True).to_json()

    assert secret not in rendered


def test_agent_contract_security_requires_distinct_principal_ids(tmp_path):
    one_human = SimpleNamespace(
        principal_id="same-human",
        type="human",
        scopes=("approval:decide", "content:stage"),
    )
    config = _config(tmp_path, agent_principals=[one_human])

    check = {item.check_id: item for item in _security_checks(config)}["security.agent_contract"]

    assert check.outcome == CheckOutcome.WARN
    assert check.details["independent_pair"] is False


def test_agent_contract_security_accepts_distinct_caller_and_approver(tmp_path):
    caller = SimpleNamespace(
        principal_id="creator-agent",
        type="agent",
        scopes=(
            "content:stage",
            "plan:create",
            "approval:request",
            "schedule:create",
        ),
    )
    approver = SimpleNamespace(
        principal_id="human-reviewer",
        type="human",
        scopes=("approval:read", "approval:decide"),
    )
    config = _config(tmp_path, agent_principals=[caller, approver])

    check = {item.check_id: item for item in _security_checks(config)}["security.agent_contract"]

    assert check.outcome == CheckOutcome.PASS
    assert check.details["independent_pair"] is True


def test_agent_principals_with_empty_legacy_key_fail_auth_check_independently(
    tmp_path,
):
    caller = SimpleNamespace(
        principal_id="creator-agent",
        type="agent",
        scopes=(
            "content:stage",
            "plan:create",
            "approval:request",
            "schedule:create",
        ),
    )
    approver = SimpleNamespace(
        principal_id="human-reviewer",
        type="human",
        scopes=("approval:read", "approval:decide"),
    )
    config = _config(
        tmp_path,
        api_host="127.0.0.1",
        api_key="",
        agent_principals=[caller, approver],
    )

    by_id = {item.check_id: item for item in _security_checks(config)}

    api_auth = by_id["security.api_auth"]
    assert api_auth.outcome == CheckOutcome.WARN
    assert api_auth.details == {
        "loopback": True,
        "api_key_configured": False,
        "configured_principals": 2,
        "dev_bypass_requested": False,
        "dev_bypass_effective": False,
    }
    assert "fail closed" in api_auth.summary

    agent_contract = by_id["security.agent_contract"]
    assert agent_contract.outcome == CheckOutcome.PASS
    assert agent_contract.details["independent_pair"] is True


def test_empty_legacy_key_without_principals_fails_closed_by_default(tmp_path):
    config = _config(
        tmp_path,
        api_host="127.0.0.1",
        api_key="",
        agent_principals=[],
    )

    by_id = {item.check_id: item for item in _security_checks(config)}

    assert by_id["security.api_auth"].outcome == CheckOutcome.PASS
    assert "fail closed" in by_id["security.api_auth"].summary
    assert by_id["security.agent_contract"].outcome == CheckOutcome.WARN


def test_explicit_loopback_legacy_dev_bypass_is_warned(tmp_path):
    config = _config(
        tmp_path,
        api_host="127.0.0.1",
        api_key="",
        agent_principals=[],
        legacy_dev_auth_bypass=True,
    )

    check = {item.check_id: item for item in _security_checks(config)}["security.api_auth"]

    assert check.outcome == CheckOutcome.WARN
    assert check.details["dev_bypass_effective"] is True


def test_explicit_non_loopback_legacy_dev_bypass_fails(tmp_path):
    config = _config(
        tmp_path,
        api_host="0.0.0.0",
        api_key="",
        agent_principals=[],
        legacy_dev_auth_bypass=True,
    )

    check = {item.check_id: item for item in _security_checks(config)}["security.api_auth"]

    assert check.outcome == CheckOutcome.FAIL
    assert check.details["dev_bypass_effective"] is True


def test_agent_contract_security_requires_complete_workflow_scopes(tmp_path):
    caller = SimpleNamespace(
        principal_id="creator-agent",
        type="agent",
        scopes=("content:stage", "plan:create"),
    )
    approver = SimpleNamespace(
        principal_id="human-reviewer",
        type="human",
        scopes=("approval:read", "approval:decide"),
    )

    check = {
        item.check_id: item
        for item in _security_checks(_config(tmp_path, agent_principals=[caller, approver]))
    }["security.agent_contract"]

    assert check.outcome == CheckOutcome.WARN
    assert check.details["missing_operational_scopes"] == [
        "approval:request",
        "schedule:create",
    ]


def test_agent_contract_human_approver_requires_read_and_decide(tmp_path):
    caller = SimpleNamespace(
        principal_id="creator-agent",
        type="agent",
        scopes=(
            "content:stage",
            "plan:create",
            "approval:request",
            "schedule:create",
        ),
    )
    approver = SimpleNamespace(
        principal_id="human-reviewer",
        type="human",
        scopes=("approval:decide",),
    )

    check = {
        item.check_id: item
        for item in _security_checks(_config(tmp_path, agent_principals=[caller, approver]))
    }["security.agent_contract"]

    assert check.outcome == CheckOutcome.WARN
    assert check.details["human_approvers"] == 0


def test_check_order_is_stable(tmp_path):
    report = run_doctor(_config(tmp_path), module_probe=lambda _name: True)

    assert [check.check_id for check in report.checks] == [
        "runtime.python",
        "runtime.data_dir",
        "resources.packaged",
        "database.connectivity",
        "database.schema",
        "safety.publication",
        "security.credential_key",
        "security.api_auth",
        "security.agent_contract",
        "scheduler.configuration",
        "browser.runtime",
        "adapter.zhihu_cli",
        "adapter.youtube_uploader",
        "adapter.social_auto_upload",
        "adapter.xhs_skills",
        "adapter.money_printer_turbo",
        "adapter.funclip",
    ]
