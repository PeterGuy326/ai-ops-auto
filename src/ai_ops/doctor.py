"""Read-only installation and runtime diagnostics for ai-ops-auto.

The doctor deliberately does not migrate a database, create a SQLite file,
launch a browser, or contact a publishing platform. It only inspects local
configuration/resources and opens the configured database for a read-only
query. The sole adapter execution is an opt-in, fixed ``gh --version`` probe;
it receives a minimal environment without credentials and never performs API
or login discovery.

Exit policy is deterministic: required check failures return 1, otherwise 0.
Callers may opt into ``strict`` mode, where warnings also return 1.  CLI usage
errors remain the CLI framework's responsibility (normally exit code 2).
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from .config import (
    HUMAN_APPROVAL_SCOPES,
    SCOPE_APPROVAL_REQUEST,
    SCOPE_CONTENT_STAGE,
    SCOPE_PLAN_CREATE,
    SCOPE_SCHEDULE_CREATE,
    canonical_github_pages_base,
    settings,
    valid_github_pages_repository,
)


class CheckOutcome(StrEnum):
    """Stable machine-readable result of one doctor check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class CheckSeverity(StrEnum):
    """Severity is separate from outcome for downstream presentation."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_SEVERITY_BY_OUTCOME = {
    CheckOutcome.PASS: CheckSeverity.INFO,
    CheckOutcome.WARN: CheckSeverity.WARNING,
    CheckOutcome.FAIL: CheckSeverity.ERROR,
    CheckOutcome.SKIP: CheckSeverity.INFO,
}


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One stable, JSON-safe diagnostic result."""

    check_id: str
    outcome: CheckOutcome
    summary: str
    remediation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> CheckSeverity:
        return _SEVERITY_BY_OUTCOME[self.outcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "outcome": self.outcome.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "remediation": self.remediation,
            "details": _json_safe(self.details),
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Ordered doctor results plus deterministic rendering/exit semantics."""

    checks: tuple[DoctorCheck, ...]
    schema_version: int = 1

    def counts(self) -> dict[str, int]:
        return {
            outcome.value: sum(check.outcome == outcome for check in self.checks)
            for outcome in CheckOutcome
        }

    def exit_code_for(self, *, strict: bool = False) -> int:
        if any(check.outcome == CheckOutcome.FAIL for check in self.checks):
            return 1
        if strict and any(check.outcome == CheckOutcome.WARN for check in self.checks):
            return 1
        return 0

    @property
    def exit_code(self) -> int:
        return self.exit_code_for()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self, *, strict: bool = False) -> dict[str, Any]:
        exit_code = self.exit_code_for(strict=strict)
        return {
            "schema_version": self.schema_version,
            "ok": exit_code == 0,
            "strict": strict,
            "exit_code": exit_code,
            "summary": self.counts(),
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self, *, strict: bool = False, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(strict=strict),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )

    def render_human(self, *, strict: bool = False) -> str:
        lines = ["ai-ops doctor"]
        for check in self.checks:
            label = check.outcome.value.upper().ljust(4)
            lines.append(f"{label} {check.check_id}: {check.summary}")
            if check.remediation:
                lines.append(f"     fix: {check.remediation}")
        counts = self.counts()
        lines.append(
            "summary: "
            f"{counts['pass']} passed, {counts['warn']} warnings, "
            f"{counts['fail']} failed, {counts['skip']} skipped; "
            f"exit={self.exit_code_for(strict=strict)}"
        )
        return "\n".join(lines)


ModuleProbe = Callable[[str], bool]
ExecutableProbe = Callable[[str], str | None]
CommandProbe = Callable[[Sequence[str]], tuple[int, str]]
FileDigestProbe = Callable[[str], str | None]

_GH_VERSION_PATTERN = re.compile(
    r"^gh version (?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n]+\))?$"
)
_GH_PROBE_MAX_OUTPUT_BYTES = 4096
_GH_PROBE_TIMEOUT_SECONDS = 5.0


def _local_file_sha256(value: str) -> str | None:
    try:
        path = Path(value).resolve(strict=True)
        metadata = path.stat()
        if not path.is_file() or metadata.st_size > 512 * 1024 * 1024:
            return None
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _stop_local_probe_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the probe and descendants."""

    try:
        if os.name == "posix" and process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        pass


def _local_command_probe(argv: Sequence[str]) -> tuple[int, str]:
    """Run one fixed local metadata probe without forwarding ambient secrets."""

    with tempfile.TemporaryDirectory(prefix="ai-ops-doctor-gh-") as isolated_root:
        root = Path(isolated_root)
        safe_env = {
            "DO_NOT_TRACK": "true",
            "GH_CONFIG_DIR": str(root / "gh-config"),
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PROMPT_DISABLED": "1",
            "GH_TELEMETRY": "false",
            "HOME": str(root / "home"),
            "LANG": "C",
            "LC_ALL": "C",
            "NO_COLOR": "1",
            "PATH": os.environ.get("PATH", os.defpath),
            "TMP": str(root / "tmp"),
            "TEMP": str(root / "tmp"),
            "TMPDIR": str(root / "tmp"),
            "USERPROFILE": str(root / "home"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
        for directory in (
            root / "gh-config",
            root / "home",
            root / "tmp",
            root / "xdg-cache",
            root / "xdg-config",
            root / "xdg-data",
            root / "xdg-state",
        ):
            directory.mkdir()
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                safe_env[name] = value
        popen_kwargs: dict[str, Any] = {
            "cwd": root,
            "env": safe_env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed probed gh executable
                tuple(argv),
                **popen_kwargs,
            )
        except OSError:
            return -1, ""

        output = bytearray()
        overflow = threading.Event()
        read_failed = threading.Event()
        drained = threading.Event()

        def drain_output() -> None:
            try:
                assert process.stdout is not None
                while chunk := process.stdout.read(4096):
                    remaining = _GH_PROBE_MAX_OUTPUT_BYTES + 1 - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(output) > _GH_PROBE_MAX_OUTPUT_BYTES:
                        overflow.set()
            except (OSError, ValueError):
                read_failed.set()
            finally:
                drained.set()

        reader = threading.Thread(target=drain_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + _GH_PROBE_TIMEOUT_SECONDS
        parent_exited_at: float | None = None
        failed = False
        while not drained.wait(0.01):
            now = time.monotonic()
            if overflow.is_set() or read_failed.is_set() or now >= deadline:
                failed = True
                break
            if process.poll() is not None:
                parent_exited_at = parent_exited_at or now
                # A normal `gh --version` has no descendant retaining stdout.
                if now - parent_exited_at >= 0.1:
                    failed = True
                    break

        if overflow.is_set() or read_failed.is_set():
            failed = True
        if failed:
            _stop_local_probe_tree(process)
            reader.join(timeout=1)
            return -1, ""
        try:
            return_code = process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            _stop_local_probe_tree(process)
            return -1, ""
        reader.join(timeout=1)
        if reader.is_alive() or len(output) > _GH_PROBE_MAX_OUTPUT_BYTES:
            _stop_local_probe_tree(process)
            return -1, ""
        return return_code, bytes(output).decode("utf-8", errors="replace")


_REQUIRED_CORE_TABLES = frozenset(
    {
        "topics",
        "articles",
        "assets",
        "accounts",
        "publication_plans",
        "approval_requests",
        "agent_operations",
        "publish_jobs",
        "metrics_collection_tasks",
        "metrics",
        "resume_profiles",
        "job_postings",
        "job_matches",
        "applications",
        "job_accounts",
    }
)
_AGENT_CONTRACT_REQUIRED_COLUMNS = {
    "assets": {"content_sha256", "size_bytes", "storage_kind"},
    "publication_plans": {
        "article_id",
        "state",
        "content_digest",
        "plan_digest",
        "content_snapshot",
        "targets",
        "planned_for",
        "created_by",
        "created_by_type",
        "created_at",
        "updated_at",
    },
    "approval_requests": {
        "plan_id",
        "plan_digest",
        "status",
        "requested_by",
        "requested_by_type",
        "requested_at",
        "decided_by",
        "decided_by_type",
        "decided_at",
        "decision_reason",
        "expires_at",
        "updated_at",
    },
    "agent_operations": {
        "principal_id",
        "principal_type",
        "operation",
        "idempotency_key",
        "request_digest",
        "response_status_code",
        "response_json",
        "lease_token",
        "lease_expires_at",
        "created_at",
        "updated_at",
    },
    "publish_jobs": {"plan_id", "approved_planned_for"},
    "metrics_collection_tasks": {
        "id",
        "job_id",
        "interval_index",
        "window_seconds",
        "due_at",
        "collection_deadline_at",
        "next_attempt_at",
        "status",
        "attempts",
        "max_attempts",
        "lease_token",
        "lease_expires_at",
        "last_error",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    },
    "metrics": {"agent_operation_id", "collection_task_id"},
}
_AGENT_CONTRACT_REQUIRED_FOREIGN_KEYS = {
    ("publication_plans", ("article_id",), "articles", ("id",)),
    ("approval_requests", ("plan_id",), "publication_plans", ("id",)),
    ("publish_jobs", ("plan_id",), "publication_plans", ("id",)),
    ("metrics_collection_tasks", ("job_id",), "publish_jobs", ("id",)),
    ("metrics", ("agent_operation_id",), "agent_operations", ("id",)),
    (
        "metrics",
        ("collection_task_id", "job_id"),
        "metrics_collection_tasks",
        ("id", "job_id"),
    ),
}
_AGENT_CONTRACT_REQUIRED_UNIQUES = {
    ("agent_operations", ("principal_id", "operation", "idempotency_key")),
    ("publish_jobs", ("plan_id", "account_id")),
    ("metrics_collection_tasks", ("job_id", "interval_index")),
    ("metrics_collection_tasks", ("job_id", "window_seconds")),
    ("metrics_collection_tasks", ("id", "job_id")),
    ("metrics", ("agent_operation_id",)),
    ("metrics", ("collection_task_id",)),
}
_AGENT_CONTRACT_REQUIRED_CHECKS = {
    "ck_assets_vault_metadata_complete",
    "ck_publication_plans_state",
    "ck_publication_plans_content_digest_sha256",
    "ck_publication_plans_plan_digest_sha256",
    "ck_approval_requests_status",
    "ck_approval_requests_plan_digest_sha256",
    "ck_approval_requests_decider_identity",
    "ck_approval_requests_decision_complete",
    "ck_agent_operations_request_digest_sha256",
    "ck_agent_operations_response_status_code",
    "ck_agent_operations_response_complete",
    "ck_agent_operations_lease_complete",
    "ck_agent_operations_completed_not_leased",
    "ck_publish_jobs_contract_planned_for",
    "ck_metrics_collection_tasks_status",
    "ck_metrics_collection_tasks_window",
    "ck_metrics_collection_tasks_attempts",
    "ck_metrics_collection_tasks_lifecycle",
    "ck_metrics_single_ledger_owner",
    "ck_metrics_collection_task_source",
}
_AGENT_CONTRACT_REQUIRED_INDEXES = {
    (
        "metrics_collection_tasks",
        "ix_metrics_collection_tasks_due",
        ("status", "next_attempt_at", "id"),
    ),
    (
        "metrics_collection_tasks",
        "ix_metrics_collection_tasks_expired_lease",
        ("status", "lease_expires_at", "id"),
    ),
    (
        "metrics_collection_tasks",
        "ix_metrics_collection_tasks_deadline",
        ("status", "collection_deadline_at", "id"),
    ),
    (
        "metrics",
        "ix_metrics_job_collected_id",
        ("job_id", "collected_at", "id"),
    ),
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _result(
    check_id: str,
    outcome: CheckOutcome,
    summary: str,
    *,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        check_id=check_id,
        outcome=outcome,
        summary=summary,
        remediation=remediation,
        details=details or {},
    )


def _default_resource_roots() -> tuple[Path, ...]:
    source_root = Path(__file__).resolve().parents[2]
    installed_root = Path(sys.prefix) / "share" / "ai-ops-auto"
    return tuple(dict.fromkeys((source_root, installed_root)))


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _migration_bundle(root: Path) -> bool:
    versions = root / "alembic" / "versions"
    return (
        (root / "alembic.ini").is_file()
        and (root / "alembic" / "env.py").is_file()
        and versions.is_dir()
        and any(path.is_file() for path in versions.glob("*.py"))
    )


def _migration_heads(root: Path) -> tuple[str, ...]:
    """Read the literal Alembic revision graph without importing migration code."""
    revisions: dict[str, tuple[str, ...]] = {}
    for path in sorted((root / "alembic" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        revision = _migration_literal(tree, "revision")
        down_revision = _migration_literal(tree, "down_revision")
        if not isinstance(revision, str) or not revision or revision in revisions:
            raise ValueError("migration revision identifiers must be unique string literals")
        if down_revision is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down_revision, str):
            parents = (down_revision,)
        elif isinstance(down_revision, (tuple, list)) and all(
            isinstance(parent, str) and parent for parent in down_revision
        ):
            parents = tuple(down_revision)
        else:
            raise ValueError("migration down_revision must be a literal string sequence or None")
        revisions[revision] = parents

    referenced = {parent for parents in revisions.values() for parent in parents}
    if referenced - revisions.keys():
        raise ValueError("migration graph references a missing parent")
    children: dict[str, set[str]] = {revision: set() for revision in revisions}
    indegree = {revision: len(parents) for revision, parents in revisions.items()}
    for child, parents in revisions.items():
        for parent in parents:
            children[parent].add(child)
    ready = [revision for revision, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        revision = ready.pop()
        visited += 1
        for child in children[revision]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(revisions):
        raise ValueError("migration graph contains a cycle")
    return tuple(sorted(revisions.keys() - referenced))


def _migration_literal(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    raise ValueError(f"migration is missing literal {name}")


def _resource_check(
    roots: Sequence[Path],
    package_root: Path,
) -> tuple[DoctorCheck, tuple[str, ...]]:
    migration_roots = [root for root in roots if _migration_bundle(root)]
    prompt_roots = [
        root
        for root in roots
        if all(
            any(path.is_file() for path in (root / "prompts" / group).glob("*.md"))
            for group in ("accounts", "platform_style", "topics")
        )
    ]
    templates = package_root / "api" / "templates"
    required_templates = (
        "base.html",
        "dashboard.html",
        "login.html",
        "list.html",
        "article_detail.html",
        "account_detail.html",
    )
    missing_templates = [name for name in required_templates if not (templates / name).is_file()]
    ui_ready = not missing_templates

    heads: tuple[str, ...] = ()
    graph_valid = False
    if migration_roots:
        try:
            heads = _migration_heads(migration_roots[0])
            graph_valid = len(heads) == 1
        except Exception:
            graph_valid = False

    details = {
        "migration_bundle": bool(migration_roots),
        "migration_graph_valid": graph_valid,
        "migration_head_count": len(heads),
        "prompt_bundle": bool(prompt_roots),
        "ui_templates": ui_ready,
        "missing_ui_templates": missing_templates,
    }
    if migration_roots and prompt_roots and ui_ready and graph_valid:
        return (
            _result(
                "resources.packaged",
                CheckOutcome.PASS,
                "migration, prompt, and UI resources are available",
                details=details,
            ),
            heads,
        )
    return (
        _result(
            "resources.packaged",
            CheckOutcome.FAIL,
            "required packaged resources are missing or the migration graph is invalid",
            remediation="reinstall ai-ops-auto from a complete wheel or source checkout",
            details=details,
        ),
        heads,
    )


def _database_checks(config: Any, code_heads: tuple[str, ...]) -> list[DoctorCheck]:
    database_url = str(getattr(config, "database_url", ""))
    try:
        parsed = make_url(database_url)
    except Exception:
        failed = _result(
            "database.connectivity",
            CheckOutcome.FAIL,
            "database configuration is not a valid SQLAlchemy URL",
            remediation="set DATABASE_URL to a supported SQLite or PostgreSQL URL",
        )
        return [failed, _schema_skipped("database connectivity failed")]

    dialect = parsed.get_backend_name()
    database_name = parsed.database or ""
    if dialect not in {"sqlite", "postgresql"}:
        failed = _result(
            "database.connectivity",
            CheckOutcome.FAIL,
            "configured database dialect is unsupported",
            remediation="use a documented SQLite or PostgreSQL DATABASE_URL",
            details={"dialect": dialect},
        )
        return [failed, _schema_skipped("database dialect is unsupported")]

    sqlite_path: Path | None = None
    sqlite_uri: str | None = None
    if dialect == "sqlite":
        # SQLite file URIs have an extra decoding layer. A path preflight and
        # the subsequent SQLite open can therefore address different files.
        # Reject URI mode and build one canonical read-only URI ourselves.
        uri_mode = str(parsed.query.get("uri", "")).lower() in {"1", "true", "yes"}
        if not database_name or database_name == ":memory:":
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "ephemeral SQLite databases cannot be diagnosed across processes",
                remediation="configure a file-backed SQLite database and run `ai-ops init-db`",
                details={"dialect": dialect},
            )
            return [failed, _schema_skipped("SQLite storage is ephemeral")]
        if database_name.startswith("file:") or uri_mode:
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "SQLite file URI mode is refused by the zero-write diagnostic",
                remediation="use a standard sqlite:///path/to/database URL",
                details={"dialect": dialect},
            )
            return [failed, _schema_skipped("SQLite URI mode was refused")]

        candidate = Path(database_name).expanduser()
        if not candidate.exists():
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "configured SQLite database does not exist",
                remediation="run `ai-ops init-db`, then run doctor again",
                details={"dialect": dialect},
            )
            return [failed, _schema_skipped("database does not exist")]
        try:
            sqlite_path = candidate.resolve(strict=True)
        except OSError:
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "configured SQLite path cannot be resolved safely",
                remediation="fix the SQLite file path and permissions",
                details={"dialect": dialect},
            )
            return [failed, _schema_skipped("SQLite path resolution failed")]
        if not sqlite_path.is_file():
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "configured SQLite path is not a regular database file",
                remediation="set DATABASE_URL to an initialized SQLite file",
                details={"dialect": dialect},
            )
            return [failed, _schema_skipped("SQLite path is not a file")]
        active_sidecar = _sqlite_mutable_sidecar(sqlite_path)
        if active_sidecar is not None:
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "active SQLite recovery sidecar prevents a safe zero-write schema probe",
                remediation="stop SQLite writers and checkpoint/recover the database, then rerun doctor",
                details={
                    "dialect": dialect,
                    "active_sidecar": active_sidecar,
                    "active_wal": active_sidecar == "wal",
                },
            )
            return [failed, _schema_skipped("an active SQLite recovery sidecar was detected")]
        sqlite_uri = f"{sqlite_path.as_uri()}?mode=ro&immutable=1"

    connect_args: dict[str, Any] = {}
    if dialect == "postgresql":
        connect_args["connect_timeout"] = 5

    engine = None
    try:
        if dialect == "sqlite":
            assert sqlite_uri is not None
            engine = create_engine(
                "sqlite+pysqlite://",
                future=True,
                poolclass=NullPool,
                creator=lambda: sqlite3.connect(
                    sqlite_uri,
                    uri=True,
                    check_same_thread=False,
                ),
            )
        else:
            engine = create_engine(
                database_url,
                future=True,
                poolclass=NullPool,
                connect_args=connect_args,
            )
        with engine.connect() as connection:
            if dialect == "postgresql":
                # Keep every catalog/schema query in one server-enforced
                # read-only transaction. This must be the first statement in
                # the transaction; even a compromised inspector call then
                # cannot mutate the configured database.
                connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SELECT 1"))
            database_inspector = inspect(connection)
            table_names = set(database_inspector.get_table_names())
            if "alembic_version" in table_names:
                db_heads = tuple(
                    sorted(
                        str(value)
                        for value in connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars()
                    )
                )
            else:
                db_heads = ()
            inspected_agent_tables = set(_AGENT_CONTRACT_REQUIRED_COLUMNS).intersection(table_names)
            columns_by_table = {
                table_name: {
                    str(column["name"]) for column in database_inspector.get_columns(table_name)
                }
                for table_name in inspected_agent_tables
            }
            observed_foreign_keys = {
                (
                    table_name,
                    tuple(str(name) for name in foreign_key.get("constrained_columns") or ()),
                    str(foreign_key.get("referred_table") or ""),
                    tuple(str(name) for name in foreign_key.get("referred_columns") or ()),
                )
                for table_name in inspected_agent_tables
                for foreign_key in database_inspector.get_foreign_keys(table_name)
            }
            observed_uniques = {
                (
                    table_name,
                    tuple(str(name) for name in unique.get("column_names") or ()),
                )
                for table_name in inspected_agent_tables
                for unique in database_inspector.get_unique_constraints(table_name)
            }
            observed_checks = {
                str(constraint.get("name"))
                for table_name in inspected_agent_tables
                for constraint in database_inspector.get_check_constraints(table_name)
                if constraint.get("name")
            }
            observed_indexes = {
                (
                    table_name,
                    str(index.get("name") or ""),
                    tuple(str(name) for name in index.get("column_names") or ()),
                )
                for table_name in inspected_agent_tables
                for index in database_inspector.get_indexes(table_name)
            }
        active_sidecar = _sqlite_mutable_sidecar(sqlite_path) if sqlite_path is not None else None
        if active_sidecar is not None:
            failed = _result(
                "database.connectivity",
                CheckOutcome.FAIL,
                "SQLite recovery state changed during the zero-write schema probe",
                remediation="stop SQLite writers and checkpoint/recover the database, then rerun doctor",
                details={
                    "dialect": dialect,
                    "active_sidecar": active_sidecar,
                    "active_wal": active_sidecar == "wal",
                },
            )
            return [failed, _schema_skipped("SQLite changed during the probe")]
    except Exception as exc:
        failed = _result(
            "database.connectivity",
            CheckOutcome.FAIL,
            "database connection or read-only probe failed",
            remediation="verify the database service, driver, permissions, and DATABASE_URL",
            details={"dialect": dialect, "error_type": type(exc).__name__},
        )
        return [failed, _schema_skipped("database connectivity failed")]
    finally:
        if engine is not None:
            engine.dispose()

    connected = _result(
        "database.connectivity",
        CheckOutcome.PASS,
        "database accepted a read-only query",
        details={"dialect": dialect},
    )
    missing_tables = sorted(_REQUIRED_CORE_TABLES - table_names)
    missing_columns = {
        table_name: sorted(required - columns_by_table.get(table_name, set()))
        for table_name, required in _AGENT_CONTRACT_REQUIRED_COLUMNS.items()
        if table_name in table_names and required - columns_by_table.get(table_name, set())
    }
    missing_foreign_keys = sorted(_AGENT_CONTRACT_REQUIRED_FOREIGN_KEYS - observed_foreign_keys)
    missing_uniques = sorted(_AGENT_CONTRACT_REQUIRED_UNIQUES - observed_uniques)
    missing_checks = sorted(_AGENT_CONTRACT_REQUIRED_CHECKS - observed_checks)
    missing_indexes = sorted(_AGENT_CONTRACT_REQUIRED_INDEXES - observed_indexes)
    has_shape_drift = bool(
        missing_columns
        or missing_foreign_keys
        or missing_uniques
        or missing_checks
        or missing_indexes
    )
    if len(code_heads) != 1:
        schema = _schema_skipped("a unique code migration head is unavailable")
    elif db_heads != code_heads or missing_tables or has_shape_drift:
        schema = _result(
            "database.schema",
            CheckOutcome.FAIL,
            "database migration marker or critical schema shape does not match code",
            remediation="back up the database and run `ai-ops init-db`",
            details={
                "db_heads": list(db_heads),
                "code_heads": list(code_heads),
                "missing_core_tables": missing_tables,
                "missing_columns": missing_columns,
                "missing_foreign_keys": [list(item) for item in missing_foreign_keys],
                "missing_unique_constraints": [list(item) for item in missing_uniques],
                "missing_check_constraints": missing_checks,
                "missing_indexes": [list(item) for item in missing_indexes],
            },
        )
    else:
        schema = _result(
            "database.schema",
            CheckOutcome.PASS,
            "database schema is at the unique migration head",
            details={"head": code_heads[0]},
        )
    return [connected, schema]


def _sqlite_mutable_sidecar(database_path: Path) -> str | None:
    """Inspect recovery sidecars without opening SQLite or creating shared memory."""
    for kind in ("wal", "journal"):
        sidecar = Path(f"{database_path}-{kind}")
        try:
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                return kind
        except OSError:
            # A racing or unreadable sidecar cannot prove a safe snapshot.
            return kind
    return None


def _schema_skipped(reason: str) -> DoctorCheck:
    return _result(
        "database.schema",
        CheckOutcome.SKIP,
        f"schema check skipped because {reason}",
        remediation="resolve the prerequisite failure and rerun doctor",
    )


def _runtime_check(config: Any) -> list[DoctorCheck]:
    python_ok = sys.version_info >= (3, 11)
    python_check = _result(
        "runtime.python",
        CheckOutcome.PASS if python_ok else CheckOutcome.FAIL,
        f"Python {sys.version_info.major}.{sys.version_info.minor} is "
        + ("supported" if python_ok else "unsupported"),
        remediation=None if python_ok else "install Python 3.11 or newer",
        details={"major": sys.version_info.major, "minor": sys.version_info.minor},
    )

    data_dir = Path(getattr(config, "data_dir", Path("./data"))).expanduser()
    if (
        data_dir.exists()
        and data_dir.is_dir()
        and os.access(
            data_dir,
            os.R_OK | os.W_OK | os.X_OK,
        )
    ):
        data_check = _result(
            "runtime.data_dir",
            CheckOutcome.PASS,
            "data directory exists and is readable/writable",
        )
    elif not data_dir.exists():
        data_check = _result(
            "runtime.data_dir",
            CheckOutcome.WARN,
            "data directory does not exist yet",
            remediation="run `ai-ops init-db` to create runtime storage",
        )
    else:
        data_check = _result(
            "runtime.data_dir",
            CheckOutcome.FAIL,
            "data directory is not a readable/writable directory",
            remediation="fix DATA_DIR type and permissions",
        )
    return [python_check, data_check]


def _publication_safety_check(config: Any) -> DoctorCheck:
    live_flags = {
        "auto_publish": bool(getattr(config, "auto_publish_enabled", False)),
        "github_pages_live": not bool(getattr(config, "github_pages_dry_run", True)),
        "zhihu_cli": bool(getattr(config, "zhihu_cli_enabled", False)),
        "youtube_uploader": bool(getattr(config, "youtube_uploader_enabled", False)),
        "publisher_plugins": bool(getattr(config, "publisher_plugin_allowlist", ())),
        "baijiahao_stub": bool(getattr(config, "baijiahao_publisher_enabled", False)),
        "sohuhao_stub": bool(getattr(config, "sohuhao_publisher_enabled", False)),
    }
    enabled = sorted(name for name, value in live_flags.items() if value)
    policy_problems: list[str] = []
    policy_bounds = {
        "publish_min_interval_seconds": (60, 604_800),
        "publish_max_per_day": (1, 50),
        "nurture_days": (0, 365),
        "publish_jitter_seconds": (0, 86_400),
    }
    for name, (minimum, maximum) in policy_bounds.items():
        try:
            value = float(getattr(config, name))
            if value < minimum or value > maximum:
                policy_problems.append(f"{name} is outside safe bounds")
        except (AttributeError, TypeError, ValueError):
            policy_problems.append(f"{name} is invalid")
    if policy_problems:
        return _result(
            "safety.publication",
            CheckOutcome.FAIL,
            "publication policy has unsafe or invalid bounds",
            remediation="restore documented publish interval, quota, nurture, and jitter limits",
            details={"problems": policy_problems, "enabled_capabilities": enabled},
        )
    if not enabled:
        return _result(
            "safety.publication",
            CheckOutcome.PASS,
            "automatic and experimental live publication stays deny-by-default",
            details={"live_capability_count": 0},
        )
    return _result(
        "safety.publication",
        CheckOutcome.WARN,
        "one or more write-capable settings are intentionally enabled",
        remediation="confirm canary scope and account policy before starting the worker",
        details={"enabled_capabilities": enabled},
    )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _security_checks(config: Any) -> list[DoctorCheck]:
    key = str(getattr(config, "fernet_key", ""))
    if not key:
        fernet = _result(
            "security.credential_key",
            CheckOutcome.WARN,
            "FERNET_KEY is not configured; account credentials cannot be stored",
            remediation="run `ai-ops gen-fernet-key` and inject FERNET_KEY securely",
        )
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode())
            fernet = _result(
                "security.credential_key",
                CheckOutcome.PASS,
                "FERNET_KEY has a valid format",
            )
        except Exception:
            fernet = _result(
                "security.credential_key",
                CheckOutcome.FAIL,
                "FERNET_KEY is configured but invalid",
                remediation="replace it with output from `ai-ops gen-fernet-key`",
            )

    principals = tuple(getattr(config, "agent_principals", ()) or ())
    host = str(getattr(config, "api_host", "127.0.0.1"))
    has_api_key = bool(str(getattr(config, "api_key", "")))
    dev_bypass_requested = bool(getattr(config, "legacy_dev_auth_bypass", False))
    loopback = _is_loopback_host(host)
    dev_bypass_effective = dev_bypass_requested and not has_api_key and not principals
    api_details = {
        "loopback": loopback,
        "api_key_configured": has_api_key,
        "configured_principals": len(principals),
        "dev_bypass_requested": dev_bypass_requested,
        "dev_bypass_effective": dev_bypass_effective,
    }
    if dev_bypass_effective and not loopback:
        api = _result(
            "security.api_auth",
            CheckOutcome.FAIL,
            "legacy anonymous mode is requested on a non-loopback API binding",
            remediation="disable LEGACY_DEV_AUTH_BYPASS or bind API_HOST to loopback",
            details=api_details,
        )
    elif dev_bypass_effective:
        api = _result(
            "security.api_auth",
            CheckOutcome.WARN,
            "legacy protected routes are anonymously accessible on loopback",
            remediation="set API_KEY and disable LEGACY_DEV_AUTH_BYPASS outside local development",
            details=api_details,
        )
    elif principals and not has_api_key:
        api = _result(
            "security.api_auth",
            CheckOutcome.WARN,
            "legacy protected routes fail closed because API_KEY is empty",
            remediation="set API_KEY if the legacy API or UI must remain available",
            details=api_details,
        )
    else:
        api = _result(
            "security.api_auth",
            CheckOutcome.PASS,
            (
                "legacy protected routes require API_KEY authentication"
                if has_api_key
                else "all protected routes fail closed while no identity is configured"
            ),
            details=api_details,
        )

    required_operational_scopes = {
        SCOPE_CONTENT_STAGE,
        SCOPE_PLAN_CREATE,
        SCOPE_APPROVAL_REQUEST,
        SCOPE_SCHEDULE_CREATE,
    }
    invalid_approval_principals = 0
    human_approver_ids: set[str] = set()
    operational_caller_ids: set[str] = set()
    operational_scope_coverage: set[str] = set()
    for principal in principals:
        principal_id = str(getattr(principal, "principal_id", ""))
        principal_type = str(getattr(principal, "type", ""))
        scopes = set(getattr(principal, "scopes", ()) or ())
        approval_scopes = HUMAN_APPROVAL_SCOPES.intersection(scopes)
        if approval_scopes and principal_type != "human":
            invalid_approval_principals += 1
        if principal_type == "human" and HUMAN_APPROVAL_SCOPES.issubset(scopes):
            human_approver_ids.add(principal_id)
        caller_scopes = required_operational_scopes.intersection(scopes)
        if principal_type != "human" and caller_scopes:
            operational_caller_ids.add(principal_id)
            operational_scope_coverage.update(caller_scopes)
    missing_operational_scopes = sorted(required_operational_scopes - operational_scope_coverage)
    has_independent_pair = (
        any(
            caller_id != approver_id
            for caller_id in operational_caller_ids
            for approver_id in human_approver_ids
        )
        and not missing_operational_scopes
    )
    principal_details = {
        "configured_principals": len(principals),
        "human_approvers": len(human_approver_ids),
        "operational_callers": len(operational_caller_ids),
        "operational_scope_coverage": sorted(operational_scope_coverage),
        "missing_operational_scopes": missing_operational_scopes,
        "independent_pair": has_independent_pair,
    }
    if invalid_approval_principals:
        agent_contract = _result(
            "security.agent_contract",
            CheckOutcome.FAIL,
            "a non-human principal has a human approval scope",
            remediation="remove approval:read and approval:decide from every agent/service principal",
            details=principal_details,
        )
    elif not principals:
        agent_contract = _result(
            "security.agent_contract",
            CheckOutcome.WARN,
            "Agent contract v1 is disabled because no principals are configured",
            remediation="provision separate Agent and human bearer principals when enabling /v1",
            details=principal_details,
        )
    elif not has_independent_pair:
        agent_contract = _result(
            "security.agent_contract",
            CheckOutcome.WARN,
            "Agent contract v1 lacks a complete independent caller/approver workflow",
            remediation=(
                "cover content:stage, plan:create, approval:request, and schedule:create "
                "with non-human principals; give a separate human both approval:read "
                "and approval:decide"
            ),
            details=principal_details,
        )
    else:
        agent_contract = _result(
            "security.agent_contract",
            CheckOutcome.PASS,
            "Agent contract v1 has separate operational and human approval identities",
            details=principal_details,
        )
    return [fernet, api, agent_contract]


def _scheduler_check(config: Any, module_probe: ModuleProbe) -> DoctorCheck:
    problems: list[str] = []
    backend = str(getattr(config, "scheduler_backend", ""))
    if backend != "apscheduler":
        problems.append("unsupported backend")
    if not module_probe("apscheduler"):
        problems.append("APScheduler package unavailable")
    timezone_name = str(getattr(config, "scheduler_timezone", ""))
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append("invalid timezone")

    numeric_positive = (
        "scheduler_poll_seconds",
        "scheduler_max_concurrency",
        "job_retry_base_seconds",
        "job_execution_timeout_seconds",
        "job_running_timeout_seconds",
    )
    for name in numeric_positive:
        try:
            if float(getattr(config, name)) <= 0:
                problems.append(f"{name} must be positive")
        except (AttributeError, TypeError, ValueError):
            problems.append(f"{name} is invalid")
    try:
        if float(config.job_running_timeout_seconds) <= float(config.job_execution_timeout_seconds):
            problems.append("running timeout must exceed execution timeout")
    except (AttributeError, TypeError, ValueError):
        pass

    if problems:
        return _result(
            "scheduler.configuration",
            CheckOutcome.FAIL,
            "scheduler configuration has core blockers",
            remediation="fix scheduler environment values and rerun doctor",
            details={"problems": problems},
        )
    return _result(
        "scheduler.configuration",
        CheckOutcome.PASS,
        "scheduler backend, timezone, concurrency, and timeout ordering are valid",
        details={"backend": backend, "timezone_valid": True},
    )


def _configured_endpoint_valid(value: str) -> bool:
    if not value:
        return True
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _configured_proxy_valid(value: str) -> bool:
    if not value:
        return True
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https", "socks5"} and bool(parsed.hostname)
    except ValueError:
        return False


def _chrome_available(executable_probe: ExecutableProbe) -> bool:
    if any(executable_probe(name) for name in ("google-chrome", "google-chrome-stable", "chrome")):
        return True
    candidates = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    )
    return any(path.is_file() and os.access(path, os.X_OK) for path in candidates)


def _browser_check(
    config: Any, module_probe: ModuleProbe, executable_probe: ExecutableProbe
) -> DoctorCheck:
    engine = str(getattr(config, "browser_engine", ""))
    modules = {
        "playwright_chromium": "playwright",
        "playwright_chrome_channel": "playwright",
        "patchright": "patchright",
        "camoufox": "camoufox",
    }
    if engine not in modules:
        return _result(
            "browser.runtime",
            CheckOutcome.WARN,
            "configured browser engine is unsupported",
            remediation="choose a documented BROWSER_ENGINE value",
        )

    module_ready = module_probe(modules[engine])
    artifact_verified = (
        _chrome_available(executable_probe) if engine == "playwright_chrome_channel" else False
    )
    cdp_url = str(getattr(config, "browser_cdp_url", ""))
    endpoint_valid = _configured_endpoint_valid(cdp_url)
    proxy = str(getattr(config, "browser_proxy", ""))
    proxy_valid = _configured_proxy_valid(proxy)
    details = {
        "engine": engine,
        "python_module_available": module_ready,
        "browser_artifact_verified": artifact_verified,
        "cdp_configured": bool(cdp_url),
        "cdp_syntax_valid": endpoint_valid,
        "proxy_configured": bool(proxy),
        "proxy_syntax_valid": proxy_valid,
        "probed_online": False,
    }
    if not endpoint_valid or not proxy_valid:
        return _result(
            "browser.runtime",
            CheckOutcome.WARN,
            "configured browser endpoint or proxy syntax is invalid",
            remediation="fix or clear BROWSER_CDP_URL/BROWSER_PROXY",
            details=details,
        )
    if cdp_url:
        return _result(
            "browser.runtime",
            CheckOutcome.WARN,
            "configured CDP endpoint syntax is valid but reachability was not probed",
            remediation="verify the trusted CDP endpoint before browser publishing",
            details=details,
        )
    if not module_ready or not artifact_verified:
        return _result(
            "browser.runtime",
            CheckOutcome.WARN,
            "browser integration exists but a runnable artifact was not statically verified",
            remediation="install the selected browser extra/runtime before browser publishing",
            details=details,
        )
    return _result(
        "browser.runtime",
        CheckOutcome.PASS,
        "selected browser integration and local Chrome executable are available",
        details=details,
    )


def _binary_adapter_check(
    check_id: str,
    *,
    enabled: bool,
    binary: str,
    label: str,
    executable_probe: ExecutableProbe,
) -> DoctorCheck:
    if not enabled:
        return _result(check_id, CheckOutcome.SKIP, f"{label} is disabled")
    available = bool(binary and executable_probe(binary))
    if available:
        return _result(
            check_id,
            CheckOutcome.PASS,
            f"{label} executable is available; version/login were not probed",
            details={"enabled": True, "executable_available": True, "probed_online": False},
        )
    return _result(
        check_id,
        CheckOutcome.WARN,
        f"{label} is enabled but its executable is unavailable",
        remediation=f"install the audited {label} binary or disable its feature flag",
        details={"enabled": True, "executable_available": False, "probed_online": False},
    )


def _github_pages_gh_check(
    config: Any,
    *,
    executable_probe: ExecutableProbe,
    command_probe: CommandProbe,
    file_digest_probe: FileDigestProbe,
) -> DoctorCheck:
    enabled = bool(getattr(config, "github_pages_gh_verify_enabled", False))
    token = getattr(config, "github_pages_gh_token", "")
    reveal = getattr(token, "get_secret_value", None)
    token_value = reveal() if callable(reveal) else token
    token_configured = bool(str(token_value).strip())
    base_details = {
        "enabled": enabled,
        "token_configured": token_configured,
        "probed_online": False,
    }
    if not enabled:
        return _result(
            "adapter.github_pages_gh",
            CheckOutcome.SKIP,
            "GitHub Pages gh verification is disabled",
            details=base_details,
        )

    repository = str(getattr(config, "github_pages_repository", ""))
    binary = str(getattr(config, "github_pages_gh_bin", "gh"))
    expected_version = str(getattr(config, "github_pages_gh_version", "2.97.0"))
    expected_digest = str(getattr(config, "github_pages_gh_sha256", ""))
    problems: list[str] = []
    if binary != "gh":
        problems.append("gh executable contract is not fixed to the audited basename")
    if expected_version != "2.97.0":
        problems.append("gh version contract is not the approved release contract")
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        problems.append("gh executable SHA-256 approval pin is missing or malformed")
    if not valid_github_pages_repository(repository):
        problems.append("GitHub repository is missing or not exact owner/repo syntax")
    base_url = str(getattr(config, "github_pages_base_url", ""))
    base_url_approved = canonical_github_pages_base(base_url, repository=repository) is not None
    if not base_url_approved:
        problems.append("GitHub Pages base URL is not the canonical owner github.io HTTPS boundary")
    if not token_configured:
        problems.append("project-scoped GitHub Pages token is not configured")
    if problems:
        return _result(
            "adapter.github_pages_gh",
            CheckOutcome.FAIL,
            "GitHub Pages gh verification configuration is incomplete or unsafe",
            remediation=(
                "configure exact owner/repo, the approved gh contract, and a project-scoped "
                "Pages:read token, or disable GITHUB_PAGES_GH_VERIFY_ENABLED"
            ),
            details={
                **base_details,
                "base_url_approved": base_url_approved,
                "problems": problems,
                "version_probed": False,
            },
        )

    executable = executable_probe(binary)
    if not executable:
        return _result(
            "adapter.github_pages_gh",
            CheckOutcome.FAIL,
            "GitHub Pages gh verification is enabled but gh is unavailable",
            remediation="install the approved gh 2.97.0 binary or disable Pages verification",
            details={
                **base_details,
                "executable_available": False,
                "version_probed": False,
                "expected_version": expected_version,
            },
        )

    observed_digest = file_digest_probe(executable)
    if observed_digest != expected_digest:
        return _result(
            "adapter.github_pages_gh",
            CheckOutcome.FAIL,
            "installed gh does not match the approved executable SHA-256",
            remediation="verify the official gh 2.97.0 artifact and update the deployment digest pin",
            details={
                **base_details,
                "base_url_approved": base_url_approved,
                "executable_available": True,
                "binary_digest_matches": False,
                "version_probed": False,
                "expected_version": expected_version,
            },
        )

    try:
        return_code, output = command_probe((executable, "--version"))
    except (Exception, SystemExit):
        return_code, output = -1, ""
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    match = _GH_VERSION_PATTERN.fullmatch(first_line)
    observed_version = match.group("version") if match else None
    details = {
        **base_details,
        "executable_available": True,
        "version_probed": True,
        "expected_version": expected_version,
        "observed_version": observed_version,
        "base_url_approved": base_url_approved,
        "binary_digest_matches": True,
    }
    if return_code != 0 or observed_version != expected_version:
        return _result(
            "adapter.github_pages_gh",
            CheckOutcome.FAIL,
            "installed gh does not satisfy the pinned version contract",
            remediation="install gh 2.97.0 exactly, then rerun doctor",
            details=details,
        )
    return _result(
        "adapter.github_pages_gh",
        CheckOutcome.PASS,
        "approved SHA-256 and self-reported gh 2.97.0 match; authentication and Pages were not probed",
        details=details,
    )


def _local_adapter_check(
    check_id: str,
    *,
    label: str,
    root: Path,
    required_file: Path,
    endpoint: str = "",
    configured_extra: bool = False,
) -> DoctorCheck:
    if endpoint:
        valid = _configured_endpoint_valid(endpoint)
        return _result(
            check_id,
            # External engines remain optional even when partially configured.
            # A malformed endpoint blocks that capability, not the control
            # plane itself, so default doctor mode reports a warning.  Strict
            # mode lets operators promote it to a non-zero exit when desired.
            CheckOutcome.WARN,
            (
                f"{label} HTTP adapter is configured but reachability was not probed"
                if valid
                else f"{label} endpoint syntax is invalid"
            ),
            remediation=None if valid else f"fix or clear the {label} endpoint setting",
            details={"mode": "http", "endpoint_syntax_valid": valid, "probed_online": False},
        )
    configured = configured_extra or root.exists()
    if not configured:
        return _result(check_id, CheckOutcome.SKIP, f"{label} is not configured")
    ready = root.is_dir() and required_file.is_file()
    return _result(
        check_id,
        CheckOutcome.PASS if ready else CheckOutcome.WARN,
        (
            f"{label} local entrypoint is present; version/login were not probed"
            if ready
            else f"{label} local adapter is incomplete"
        ),
        remediation=None if ready else f"install {label} completely or clear its local settings",
        details={
            "mode": "local",
            "root_available": root.is_dir(),
            "entry_available": required_file.is_file(),
            "version_probed": False,
            "login_probed": False,
            "probed_online": False,
        },
    )


def _isolated_python_adapter_check(
    check_id: str,
    *,
    label: str,
    root: Path,
    entry_relative: Path,
    configured_python: str,
    endpoint: str = "",
) -> DoctorCheck:
    if endpoint:
        return _local_adapter_check(
            check_id,
            label=label,
            root=root,
            required_file=root / entry_relative,
            endpoint=endpoint,
        )

    normalized_root = Path(os.path.abspath(root.expanduser()))
    configured = normalized_root.exists() or bool(configured_python.strip())
    if not configured:
        return _result(check_id, CheckOutcome.SKIP, f"{label} is not configured")

    entry = normalized_root / entry_relative
    problems: list[str] = []
    if not normalized_root.is_dir():
        problems.append("repository root unavailable")
    if not entry.is_file() or entry.is_symlink():
        problems.append("entrypoint unavailable or unsafe")

    python_ready = False
    venv_ready = False
    python_inside_root = False
    if not configured_python.strip():
        problems.append("isolated Python is not configured")
    else:
        python = Path(os.path.abspath(os.path.expanduser(configured_python.strip())))
        try:
            python.relative_to(normalized_root)
            python_inside_root = True
        except ValueError:
            problems.append("isolated Python is outside the repository root")
        python_ready = python.is_file() and os.access(python, os.X_OK)
        if not python_ready:
            problems.append("isolated Python is unavailable or not executable")
        venv_ready = (python.parent.parent / "pyvenv.cfg").is_file()
        if not venv_ready:
            problems.append("isolated Python has no pyvenv.cfg boundary")

    details = {
        "mode": "local",
        "root_available": normalized_root.is_dir(),
        "entry_available": entry.is_file() and not entry.is_symlink(),
        "python_configured": bool(configured_python.strip()),
        "python_inside_root": python_inside_root,
        "python_executable": python_ready,
        "venv_boundary": venv_ready,
    }
    if problems:
        return _result(
            check_id,
            CheckOutcome.WARN,
            f"{label} local runtime boundary is incomplete",
            remediation=f"configure a complete repository-local {label} virtual environment",
            details={**details, "problems": problems},
        )
    return _result(
        check_id,
        CheckOutcome.PASS,
        f"{label} local runtime boundary is present; execution was not probed",
        details={**details, "execution_probed": False, "probed_online": False},
    )


def _adapter_checks(
    config: Any,
    executable_probe: ExecutableProbe,
    command_probe: CommandProbe = _local_command_probe,
    file_digest_probe: FileDigestProbe = _local_file_sha256,
) -> list[DoctorCheck]:
    zhihu = _binary_adapter_check(
        "adapter.zhihu_cli",
        enabled=bool(getattr(config, "zhihu_cli_enabled", False)),
        binary=str(getattr(config, "zhihu_cli_bin", "zhihu")),
        label="Zhihu CLI",
        executable_probe=executable_probe,
    )
    youtube = _binary_adapter_check(
        "adapter.youtube_uploader",
        enabled=bool(getattr(config, "youtube_uploader_enabled", False)),
        binary=str(getattr(config, "youtube_uploader_bin", "youtubeuploader")),
        label="YouTube uploader",
        executable_probe=executable_probe,
    )
    github_pages_gh = _github_pages_gh_check(
        config,
        executable_probe=executable_probe,
        command_probe=command_probe,
        file_digest_probe=file_digest_probe,
    )

    sau_root = Path(getattr(config, "external_sau_path", "./external/social-auto-upload"))
    sau = _local_adapter_check(
        "adapter.social_auto_upload",
        label="social-auto-upload",
        root=sau_root,
        required_file=sau_root / "sau_cli.py",
        endpoint=str(getattr(config, "external_sau_url", "")),
    )

    xhs_root = sau_root.parent / "XiaohongshuSkills"
    xhs = _local_adapter_check(
        "adapter.xhs_skills",
        label="XiaohongshuSkills",
        root=xhs_root,
        required_file=xhs_root / "scripts" / "publish_pipeline.py",
    )

    mpt_root = Path(getattr(config, "external_mpt_path", "./external/MoneyPrinterTurbo"))
    mpt_python = str(getattr(config, "mpt_python", ""))
    mpt = _isolated_python_adapter_check(
        "adapter.money_printer_turbo",
        label="MoneyPrinterTurbo",
        root=mpt_root,
        entry_relative=Path("main.py"),
        configured_python=mpt_python,
        endpoint=str(getattr(config, "external_mpt_url", "")),
    )

    funclip_root = Path(getattr(config, "funclip_path", "./external/FunClip"))
    funclip_python = str(getattr(config, "funclip_python", ""))
    funclip = _isolated_python_adapter_check(
        "adapter.funclip",
        label="FunClip",
        root=funclip_root,
        entry_relative=Path("funclip/videoclipper.py"),
        configured_python=funclip_python,
    )
    return [zhihu, youtube, github_pages_gh, sau, xhs, mpt, funclip]


def _publisher_plugin_selection_check(
    config: Any,
    *,
    entry_points: Sequence[Any] | None = None,
) -> DoctorCheck:
    """Inspect Publisher entry-point metadata without importing plugin code."""

    from .publishers.plugin_sdk import (
        PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
        inspect_publisher_plugins,
        safe_plugin_exception_type,
    )

    enabled = tuple(getattr(config, "publisher_plugin_allowlist", ()) or ())
    try:
        inventory = inspect_publisher_plugins(enabled, entry_points=entry_points)
    except (Exception, SystemExit) as exc:
        return _result(
            "plugins.selection",
            CheckOutcome.FAIL if enabled else CheckOutcome.WARN,
            "Publisher plugin metadata inventory failed",
            remediation="repair Python package metadata and rerun doctor",
            details={
                "code_loaded": False,
                "enabled_count": len(enabled),
                "entry_point_group": PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
                "exception_type": safe_plugin_exception_type(exc),
            },
        )
    details = {
        "code_loaded": False,
        "enabled_selectors": list(inventory.enabled_selectors),
        "entry_point_group": PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
        "installed_count": len(inventory.entries),
        "missing_enabled": list(inventory.missing_enabled),
        "duplicate_enabled": list(inventory.duplicate_enabled),
        "invalid_enabled": list(inventory.invalid_enabled),
    }
    if inventory.missing_enabled or inventory.duplicate_enabled or inventory.invalid_enabled:
        return _result(
            "plugins.selection",
            CheckOutcome.FAIL,
            "one or more enabled Publisher plugin selectors are unavailable, ambiguous, or invalid",
            remediation=(
                "install one pinned distribution with valid metadata per selector or remove it "
                "from the allowlist"
            ),
            details=details,
        )
    if not enabled:
        return _result(
            "plugins.selection",
            CheckOutcome.SKIP,
            "no third-party Publisher plugins are enabled; plugin code was not loaded",
            details=details,
        )
    return _result(
        "plugins.selection",
        CheckOutcome.WARN,
        "trusted Publisher plugins are selected but were not imported by top-level doctor",
        remediation="run `ai-ops plugins doctor` before starting a publishing worker",
        details=details,
    )


def run_doctor(
    config: Any | None = None,
    *,
    resource_roots: Sequence[Path] | None = None,
    package_root: Path | None = None,
    module_probe: ModuleProbe = _module_available,
    executable_probe: ExecutableProbe = shutil.which,
    command_probe: CommandProbe = _local_command_probe,
    file_digest_probe: FileDigestProbe = _local_file_sha256,
    plugin_entry_points: Sequence[Any] | None = None,
) -> DoctorReport:
    """Run the fixed-order, side-effect-free doctor checks."""

    active = settings if config is None else config
    roots = tuple(resource_roots) if resource_roots is not None else _default_resource_roots()
    package = package_root or Path(__file__).resolve().parent
    resources, code_heads = _resource_check(roots, package)

    checks: list[DoctorCheck] = []
    checks.extend(_runtime_check(active))
    checks.append(resources)
    checks.extend(_database_checks(active, code_heads))
    checks.append(_publication_safety_check(active))
    checks.extend(_security_checks(active))
    checks.append(_scheduler_check(active, module_probe))
    checks.append(_browser_check(active, module_probe, executable_probe))
    checks.extend(_adapter_checks(active, executable_probe, command_probe, file_digest_probe))
    checks.append(_publisher_plugin_selection_check(active, entry_points=plugin_entry_points))
    return DoctorReport(tuple(checks))


__all__ = [
    "CheckOutcome",
    "CheckSeverity",
    "DoctorCheck",
    "DoctorReport",
    "run_doctor",
]
