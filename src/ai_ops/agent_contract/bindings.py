"""Stable, credential-free bindings for publication targets."""

from __future__ import annotations

import hashlib
from typing import Any

from .digest import canonical_sha256


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _credential_ciphertext_sha256(value: Any) -> str:
    """Hash stored ciphertext without ever returning or canonicalizing it."""

    if value is None:
        raw = b""
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, bytearray):
        raw = bytes(value)
    elif isinstance(value, memoryview):
        raw = value.tobytes()
    else:
        # ORM/database type drift is a configuration error.  Do not use str()
        # because it could copy credential material into an exception or digest.
        raise ValueError("account credential binding has an invalid storage type")
    return hashlib.sha256(raw).hexdigest()


def account_binding_payload(account: Any) -> dict[str, Any]:
    """Project the logical destination and credential generation into a digest.

    Health and rate-limit state are deliberately excluded: they are dynamic
    execution gates, not the identity of the destination approved by a human.
    """

    return {
        "account_id": int(account.id),
        "platform": _enum_value(account.platform),
        "nickname": account.nickname,
        "topic_id": account.topic_id,
        "profile": account.profile or {},
        "credential_ciphertext_sha256": _credential_ciphertext_sha256(account.encrypted_credential),
    }


def account_binding_digest(account: Any) -> str:
    """Return a safe SHA-256 binding for one concrete account destination."""

    return canonical_sha256(account_binding_payload(account))


__all__ = ["account_binding_digest", "account_binding_payload"]
