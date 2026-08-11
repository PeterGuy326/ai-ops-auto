"""Fail-closed configuration boundaries for Agent contract identities/assets."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_ops.config import AgentPrincipalConfig, SCOPE_JOB_READ, Settings


TOKEN_HASH = "a" * 64


@pytest.mark.parametrize(
    "principal_id",
    ["a", "Agent_01.prod:writer", "a" + "b" * 127],
)
def test_principal_id_accepts_only_bounded_log_safe_identifiers(principal_id):
    principal = AgentPrincipalConfig(
        principal_id=principal_id,
        type="agent",
        token_sha256=TOKEN_HASH,
        scopes=(SCOPE_JOB_READ,),
    )

    assert principal.principal_id == principal_id


@pytest.mark.parametrize(
    "principal_id",
    [
        "",
        "-leading-separator",
        ".leading-separator",
        ":leading-separator",
        " surrounding",
        "surrounding ",
        "embedded space",
        "line\nbreak",
        "path/segment",
        "unicode-身份",
        "a" * 129,
    ],
)
def test_principal_id_rejects_ambiguous_or_unsafe_characters(principal_id):
    with pytest.raises(ValidationError, match="principal_id"):
        AgentPrincipalConfig(
            principal_id=principal_id,
            type="agent",
            token_sha256=TOKEN_HASH,
            scopes=(SCOPE_JOB_READ,),
        )


@pytest.mark.parametrize(
    ("import_relative", "vault_relative"),
    [
        (Path("shared"), Path("shared")),
        (Path("imports"), Path("imports/vault")),
        (Path("vault/imports"), Path("vault")),
        (Path("one/../shared"), Path("shared")),
    ],
)
def test_agent_asset_roots_must_not_be_equal_or_nested(
    tmp_path,
    import_relative,
    vault_relative,
):
    with pytest.raises(ValidationError, match="separate, non-overlapping"):
        Settings(
            _env_file=None,
            agent_asset_import_root=tmp_path / import_relative,
            agent_asset_vault_root=tmp_path / vault_relative,
        )


def test_agent_asset_roots_accept_distinct_sibling_directories(tmp_path):
    configured = Settings(
        _env_file=None,
        agent_asset_import_root=tmp_path / "imports",
        agent_asset_vault_root=tmp_path / "vault",
    )

    assert configured.agent_asset_import_root == tmp_path / "imports"
    assert configured.agent_asset_vault_root == tmp_path / "vault"


def test_agent_asset_total_limit_must_cover_the_per_file_limit(tmp_path):
    with pytest.raises(ValidationError, match="MAX_TOTAL_BYTES"):
        Settings(
            _env_file=None,
            agent_asset_import_root=tmp_path / "imports",
            agent_asset_vault_root=tmp_path / "vault",
            agent_asset_max_bytes=1024,
            agent_asset_max_total_bytes=512,
        )


def test_agent_metrics_timeout_must_fit_inside_recovery_lease():
    with pytest.raises(ValidationError, match="EXTERNAL_OPERATION_LEASE_SECONDS"):
        Settings(
            _env_file=None,
            agent_metrics_collection_timeout_seconds=300,
            agent_external_operation_lease_seconds=300,
        )


def test_agent_metrics_recovery_has_bounded_defaults():
    configured = Settings(_env_file=None)

    assert configured.agent_metrics_collection_timeout_seconds == 120
    assert configured.agent_external_operation_lease_seconds == 300
