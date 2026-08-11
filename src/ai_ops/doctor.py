"""Read-only installation and runtime diagnostics for ai-ops-auto.

The doctor deliberately does not migrate a database, create a SQLite file,
launch a browser, execute an adapter binary, or contact a publishing platform.
It only inspects local configuration/resources and opens the configured
database for a read-only query.

Exit policy is deterministic: required check failures return 1, otherwise 0.
Callers may opt into ``strict`` mode, where warnings also return 1.  CLI usage
errors remain the CLI framework's responsibility (normally exit code 2).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from .config import settings


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
    missing_templates = [
        name for name in required_templates if not (templates / name).is_file()
    ]
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
            table_names = set(inspect(connection).get_table_names())
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
        active_sidecar = (
            _sqlite_mutable_sidecar(sqlite_path) if sqlite_path is not None else None
        )
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
    required_tables = {
        "topics",
        "articles",
        "assets",
        "accounts",
        "publish_jobs",
        "metrics",
        "resume_profiles",
        "job_postings",
        "job_matches",
        "applications",
        "job_accounts",
    }
    missing_tables = sorted(required_tables - table_names)
    if len(code_heads) != 1:
        schema = _schema_skipped("a unique code migration head is unavailable")
    elif db_heads != code_heads or missing_tables:
        schema = _result(
            "database.schema",
            CheckOutcome.FAIL,
            "database schema is not at the unique code migration head",
            remediation="back up the database and run `ai-ops init-db`",
            details={
                "db_heads": list(db_heads),
                "code_heads": list(code_heads),
                "missing_core_tables": missing_tables,
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
    if data_dir.exists() and data_dir.is_dir() and os.access(
        data_dir,
        os.R_OK | os.W_OK | os.X_OK,
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

    host = str(getattr(config, "api_host", "127.0.0.1"))
    has_api_key = bool(str(getattr(config, "api_key", "")))
    loopback = _is_loopback_host(host)
    if not loopback and not has_api_key:
        api = _result(
            "security.api_auth",
            CheckOutcome.FAIL,
            "API is non-loopback while API_KEY is empty",
            remediation="set API_KEY or bind API_HOST to a loopback address",
            details={"loopback": False, "api_key_configured": False},
        )
    else:
        api = _result(
            "security.api_auth",
            CheckOutcome.PASS,
            "API binding and authentication configuration is safe for its scope",
            details={"loopback": loopback, "api_key_configured": has_api_key},
        )
    return [fernet, api]


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
        if float(config.job_running_timeout_seconds) <= float(
            config.job_execution_timeout_seconds
        ):
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


def _browser_check(config: Any, module_probe: ModuleProbe, executable_probe: ExecutableProbe) -> DoctorCheck:
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
        _chrome_available(executable_probe)
        if engine == "playwright_chrome_channel"
        else False
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


def _adapter_checks(config: Any, executable_probe: ExecutableProbe) -> list[DoctorCheck]:
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
    return [zhihu, youtube, sau, xhs, mpt, funclip]


def run_doctor(
    config: Any | None = None,
    *,
    resource_roots: Sequence[Path] | None = None,
    package_root: Path | None = None,
    module_probe: ModuleProbe = _module_available,
    executable_probe: ExecutableProbe = shutil.which,
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
    checks.extend(_adapter_checks(active, executable_probe))
    return DoctorReport(tuple(checks))


__all__ = [
    "CheckOutcome",
    "CheckSeverity",
    "DoctorCheck",
    "DoctorReport",
    "run_doctor",
]
