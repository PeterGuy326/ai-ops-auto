"""Read-only inventory and explicit validation commands for Publisher plugins."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import json
import os

import typer

from .publishers.plugin_sdk import (
    PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
    inspect_publisher_plugins,
    safe_plugin_exception_type,
    validate_enabled_publisher_plugins,
)


plugin_app = typer.Typer(
    help="Inspect and validate trusted Publisher plugins.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _allowlist() -> tuple[str, ...]:
    from .config import settings

    return tuple(settings.publisher_plugin_allowlist)


def _emit_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _unexpected_failure(*, as_json: bool, code: str, exc: BaseException) -> None:
    exception_type = safe_plugin_exception_type(exc)
    payload = {
        "entry_point_group": PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
        "error": {"code": code, "exception_type": exception_type},
        "exit_code": 1,
        "ok": False,
        "schema_version": 1,
    }
    if as_json:
        _emit_json(payload)
    else:
        typer.echo(f"ERROR: {code} ({exception_type})", err=True)
    raise typer.Exit(code=1) from None


def _validate_plugins_without_third_party_output():
    """Keep plugin prints from corrupting either human or machine CLI output."""

    # Plugins are trusted in-process code rather than a sandbox boundary, but
    # their normal Python stdout/stderr must not corrupt this command's stable
    # output contract or echo accidental credentials.  Never retain the bytes.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            return validate_enabled_publisher_plugins(_allowlist())


@plugin_app.command("list")
def list_plugins(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output one stable JSON inventory document.",
    ),
) -> None:
    """List installed entry-point metadata without importing plugin code."""

    try:
        inventory = inspect_publisher_plugins(_allowlist())
    except (Exception, SystemExit) as exc:
        _unexpected_failure(as_json=as_json, code="plugin_inventory_failed", exc=exc)
        return
    if as_json:
        _emit_json(inventory.to_dict())
    else:
        typer.echo(f"Publisher plugins ({PUBLISHER_PLUGIN_ENTRY_POINT_GROUP})")
        if not inventory.entries:
            typer.echo("no installed plugins discovered")
        for entry in inventory.entries:
            status = str(entry.to_dict()["status"]).upper()
            version = entry.distribution_version or "unknown"
            typer.echo(f"{status.ljust(9)} {entry.selector} distribution_version={version}")
        if inventory.missing_enabled:
            typer.echo("ERROR: one or more enabled selectors are not installed", err=True)
        if inventory.duplicate_enabled:
            typer.echo("ERROR: one or more enabled selectors are ambiguous", err=True)
        if inventory.invalid_enabled:
            typer.echo("ERROR: one or more enabled selectors have invalid metadata", err=True)
        typer.echo(
            f"summary: {len(inventory.entries)} installed, "
            f"{len(inventory.enabled_selectors)} enabled; exit={inventory.exit_code}"
        )
    if inventory.exit_code:
        raise typer.Exit(code=inventory.exit_code)


@plugin_app.command("doctor")
def doctor_plugins(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output one stable JSON validation document.",
    ),
) -> None:
    """Load and validate only plugins explicitly authorized in the allowlist."""

    try:
        report = _validate_plugins_without_third_party_output()
    except (Exception, SystemExit) as exc:
        _unexpected_failure(as_json=as_json, code="plugin_validation_failed", exc=exc)
        return
    if as_json:
        _emit_json(report.to_dict())
    else:
        typer.echo(f"Publisher plugin doctor ({PUBLISHER_PLUGIN_ENTRY_POINT_GROUP})")
        if not report.enabled_selectors:
            typer.echo("SKIP no Publisher plugins are enabled; third-party code was not loaded")
        for plugin in report.loaded:
            manifest = plugin.plugin.manifest
            typer.echo(
                f"PASS {plugin.selector}: {manifest.platform.value}/"
                f"{manifest.publisher_kind} plugin_version={manifest.plugin_version}"
            )
        for error in report.errors:
            suffix = f" ({error.exception_type})" if error.exception_type else ""
            typer.echo(f"FAIL {error.selector}: {error.code.value}{suffix}")
        typer.echo(
            f"summary: {len(report.loaded)} valid, {len(report.errors)} invalid; "
            f"exit={report.exit_code}"
        )
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


__all__ = ["plugin_app"]
