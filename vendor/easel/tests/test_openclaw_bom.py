from __future__ import annotations

import json
import pytest

import web.app as web
from easel import gateway_endpoint, local_agents, openclaw_workspace


@pytest.fixture(params=["utf-8", "utf-8-sig"])
def config_file(request, tmp_path, monkeypatch):
    path = tmp_path / "openclaw.json"
    data = {
        "gateway": {"port": 19004},
        "agents": {"defaults": {
            "workspace": str(tmp_path / "创作空间"),
            "model": {"primary": "custom/model-one"},
        }},
        "models": {"providers": {"custom": {
            "baseUrl": "https://example.invalid/v1",
            "apiKey": "test-placeholder",
            "models": [{"id": "model-one", "reasoning": False}],
        }}},
        "preserved": {"name": "原配置", "enabled": True},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding=request.param)
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_GATEWAY_PORT", raising=False)
    monkeypatch.delenv("EASEL_GATEWAY_PORT", raising=False)
    monkeypatch.setattr(web, "_oc_config_path", lambda: path)
    monkeypatch.setattr(web, "_read_env", lambda: {})
    return path


def test_configured_gateway_port_and_endpoint(config_file):
    assert gateway_endpoint.configured_port() == 19004
    assert gateway_endpoint.resolve_port() == (19004, "openclaw.json")
    assert gateway_endpoint.healthz_url().endswith(":19004/healthz")


def test_explicit_workspace(config_file):
    assert openclaw_workspace._from_config() == config_file.parent / "创作空间"


def test_configured_provider_detection(config_file):
    assert local_agents._configured_providers() == {"custom"}


def test_reasoning_disabled_model(config_file, monkeypatch):
    monkeypatch.setattr(web, "THINKING_LEVEL", "medium")
    assert web._agent_thinking_level() == "off"


def test_model_channels_and_primary(config_file):
    channels = web._model_channels(runtime="openclaw")
    assert channels["primary"] == "custom/model-one"
    row = next(row for row in channels["channels"]["chat"]["rows"]
               if row["name"] == "custom")
    assert row["model"] == "model-one"
    assert row["role"] == "主"


def test_provider_credentials(config_file):
    assert web._openclaw_provider_creds() == {
        "custom": ("https://example.invalid/v1", "test-placeholder"),
    }


@pytest.mark.parametrize("operation", [
    "sync_chat", "sync_anthropic", "set_primary", "declare_anthropic",
])
def test_config_updates_preserve_existing_data_and_backup(config_file, operation):
    original = config_file.read_bytes()
    if operation == "sync_chat":
        note = web._sync_openclaw_chat(
            {"custom": {"model": "model-two"}}, {"custom"}, "custom/model-two",
        )
    elif operation == "sync_anthropic":
        note = web._sync_anthropic_provider("https://example.invalid", "")
    elif operation == "set_primary":
        note = web._set_openclaw_primary("custom/model-two")
    else:
        note = web._declare_anthropic_provider("https://example.invalid")
    assert note and "失败" not in note
    updated = json.loads(config_file.read_text(encoding="utf-8"))
    assert updated["preserved"] == {"name": "原配置", "enabled": True}
    assert updated["gateway"] == {"port": 19004}
    assert updated["models"]["providers"]["custom"]["apiKey"] == "test-placeholder"
    assert config_file.with_name("openclaw.json.bak-web").read_bytes() == original
    if operation in ("sync_chat", "set_primary"):
        assert updated["agents"]["defaults"]["model"]["primary"] == "custom/model-two"
    if operation == "sync_chat":
        assert updated["models"]["providers"]["custom"]["models"][0]["id"] == "model-two"
    if operation in ("sync_anthropic", "declare_anthropic"):
        assert updated["models"]["providers"]["anthropic"]["baseUrl"] == "https://example.invalid"


def test_invalid_bom_json_retains_fallback(config_file):
    config_file.write_bytes(b"\xef\xbb\xbf{invalid-json")
    assert gateway_endpoint.configured_port() is None
    assert openclaw_workspace._from_config() is None
    assert local_agents._configured_providers() == set()
