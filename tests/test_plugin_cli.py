"""User-facing CLI contract for trusted Publisher plugins."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, Platform
from ai_ops.core.schemas import PublishResult
from ai_ops.publishers.base import PublisherBase
from ai_ops.publishers.plugin_sdk import (
    PUBLISHER_PLUGIN_API_VERSION,
    PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
)


class _Publisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = "fixture_zhihu"

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        return PublishResult(success=True, platform_post_id="fixture")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


class _EntryPoint:
    group = PUBLISHER_PLUGIN_ENTRY_POINT_GROUP
    name = "fixture.zhihu"

    def __init__(self, provider) -> None:
        self.dist = SimpleNamespace(
            metadata={"Name": "fixture-ai-ops"},
            version="1.0.0",
        )
        self.provider = provider
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if isinstance(self.provider, BaseException):
            raise self.provider
        return self.provider


class _EntryPoints(list):
    def select(self, *, group):
        return [item for item in self if item.group == group]


def _provider():
    return PublisherPlugin(
        manifest=PublisherPluginManifest(
            plugin_id="fixture.zhihu",
            plugin_version="1.0.0",
            api_version=PUBLISHER_PLUGIN_API_VERSION,
            platform=Platform.ZHIHU,
            publisher_kind="fixture_zhihu",
            adapter_version="fixture-1",
            capabilities=(
                PublisherPluginCapability.HEALTH_CHECK,
                PublisherPluginCapability.LOGIN,
                PublisherPluginCapability.PUBLISH,
            ),
        ),
        factory=_Publisher,
    )


def _install_fake_entry_points(monkeypatch, *entry_points):
    monkeypatch.setattr(
        "ai_ops.publishers.plugin_sdk.metadata.entry_points",
        lambda: _EntryPoints(entry_points),
    )


def test_plugins_list_json_is_metadata_only_even_when_selected(monkeypatch):
    entry_point = _EntryPoint(_provider)
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "list", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["code_loaded"] is False
    assert payload["plugins"][0]["status"] == "enabled"
    assert entry_point.load_calls == 0


def test_public_plugin_sdk_import_does_not_construct_default_registry():
    repo_root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from ai_ops.publishers import PublisherPluginManifest; "
                "assert PublisherPluginManifest; "
                "assert 'ai_ops.publishers.registry' not in sys.modules"
            ),
        ],
        cwd=repo_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_lazy_registry_export_survives_recursive_plugin_import():
    repo_root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    script = textwrap.dedent(
        """
        from types import SimpleNamespace

        from ai_ops.config import settings
        from ai_ops.publishers import plugin_sdk


        class EntryPoint:
            group = plugin_sdk.PUBLISHER_PLUGIN_ENTRY_POINT_GROUP
            name = "fixture.zhihu"
            dist = SimpleNamespace(
                metadata={"Name": "fixture-ai-ops"},
                version="1.0.0",
            )

            def load(self):
                # This executes while registry.default_registry is still on the
                # right-hand side of its module-level assignment.
                from ai_ops.publishers import PublisherRegistry

                assert PublisherRegistry.__name__ == "PublisherRegistry"
                return lambda: object()


        class EntryPoints(list):
            def select(self, *, group):
                return [item for item in self if item.group == group]


        plugin_sdk.metadata.entry_points = lambda: EntryPoints([EntryPoint()])
        settings.publisher_plugin_allowlist = ("fixture-ai-ops:fixture.zhihu",)

        from ai_ops.publishers import PublisherRegistry, default_registry

        assert isinstance(default_registry, PublisherRegistry)
        codes = [
            error.code.value
            for error in default_registry.plugin_validation_report.errors
        ]
        assert codes == ["provider_invalid"], codes
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_plugins_help_does_not_load_code(monkeypatch):
    entry_point = _EntryPoint(_provider)
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "--help"])

    assert result.exit_code == 0
    assert entry_point.load_calls == 0


def test_plugins_doctor_loads_only_selected_code(monkeypatch):
    selected = _EntryPoint(_provider)
    disabled = _EntryPoint(_provider)
    disabled.name = "disabled.zhihu"
    disabled.dist = SimpleNamespace(metadata={"Name": "disabled-ai-ops"}, version="1.0.0")
    _install_fake_entry_points(monkeypatch, disabled, selected)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["code_loaded"] is True
    assert payload["summary"] == {"enabled": 1, "invalid": 0, "valid": 1}
    assert selected.load_calls == 1
    assert disabled.load_calls == 0


def test_plugins_doctor_error_is_stable_and_redacted(monkeypatch):
    secret = "cookie=must-not-leak"
    entry_point = _EntryPoint(RuntimeError(secret))
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["errors"] == [
        {
            "code": "entry_point_load_failed",
            "exception_type": "RuntimeError",
            "selector": "fixture-ai-ops:fixture.zhihu",
        }
    ]
    assert secret not in result.output


def test_plugins_unexpected_failure_sanitizes_dynamic_exception_type(monkeypatch):
    secret = "token=must-not-leak"
    injected_error = type(f"RuntimeError\n{secret}\x1b[2J", (Exception,), {})

    def broken_inventory():
        raise injected_error(secret)

    monkeypatch.setattr(
        "ai_ops.publishers.plugin_sdk.metadata.entry_points",
        broken_inventory,
    )
    monkeypatch.setattr(settings, "publisher_plugin_allowlist", ())

    result = CliRunner().invoke(cli.app, ["plugins", "list", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["error"]["exception_type"] == "Exception"
    assert secret not in result.output


def test_plugins_doctor_json_discards_third_party_stdout_and_stderr(monkeypatch):
    secret = "token=must-not-leak-from-plugin-output"

    def noisy_provider():
        print(secret)
        print(secret, file=sys.stderr)
        return _provider()

    entry_point = _EntryPoint(noisy_provider)
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["code_loaded"] is True
    assert secret not in result.output


def test_plugins_doctor_missing_selector_does_not_claim_code_was_loaded(monkeypatch):
    _install_fake_entry_points(monkeypatch)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("missing-ai-ops:missing.zhihu",),
    )

    result = CliRunner().invoke(cli.app, ["plugins", "doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["code_loaded"] is False


def test_top_level_doctor_only_inspects_plugin_metadata(monkeypatch, tmp_path):
    entry_point = _EntryPoint(_provider)
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'missing.db'}")

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    plugin_check = next(item for item in payload["checks"] if item["id"] == "plugins.selection")

    assert entry_point.load_calls == 0
    assert plugin_check["details"]["code_loaded"] is False
    assert plugin_check["outcome"] == "warn"


def test_list_and_top_level_doctor_fail_closed_on_enabled_invalid_version(
    monkeypatch,
    tmp_path,
):
    entry_point = _EntryPoint(_provider)
    entry_point.dist.version = None
    _install_fake_entry_points(monkeypatch, entry_point)
    monkeypatch.setattr(
        settings,
        "publisher_plugin_allowlist",
        ("fixture-ai-ops:fixture.zhihu",),
    )
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'missing.db'}")

    list_result = CliRunner().invoke(cli.app, ["plugins", "list", "--json"])
    list_payload = json.loads(list_result.stdout)
    doctor_result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    doctor_payload = json.loads(doctor_result.stdout)
    plugin_check = next(
        item for item in doctor_payload["checks"] if item["id"] == "plugins.selection"
    )

    assert list_result.exit_code == 1
    assert list_payload["invalid_enabled"] == ["fixture-ai-ops:fixture.zhihu"]
    assert list_payload["plugins"][0]["status"] == "invalid_metadata"
    assert plugin_check["outcome"] == "fail"
    assert plugin_check["details"]["invalid_enabled"] == ["fixture-ai-ops:fixture.zhihu"]
    assert entry_point.load_calls == 0
