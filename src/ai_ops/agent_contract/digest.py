"""Canonical JSON and digest helpers for the Agent contract.

The helpers in this module deliberately operate on plain values instead of ORM
objects.  Services must select the fields that form a contract before hashing;
credentials, adapter responses, and other incidental state must never become
part of an approval digest by accident.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel


class CanonicalizationError(ValueError):
    """A value cannot be represented by the stable JSON contract."""


def normalize_utc_datetime(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    The current database stores UTC as naive datetimes, so a naive input is
    interpreted as UTC.  Aware values are converted to the same instant in UTC.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_datetime(value: datetime) -> str:
    normalized = normalize_utc_datetime(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonicalize(value: Any) -> Any:
    """Convert supported Python values into a deterministic JSON value.

    Unsupported objects fail closed.  In particular, this function never uses
    ``str(object)`` as a fallback because repr/string output may contain memory
    addresses, credentials, or other unstable process state.
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", by_alias=True, exclude_none=False)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite floats are not canonical JSON")
        return value
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical JSON object keys must be strings")
            normalized[key] = canonicalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [canonicalize(item) for item in value]
        return sorted(items, key=canonical_json)
    raise CanonicalizationError(f"unsupported canonical JSON value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize one value using the stable Agent-contract JSON profile."""

    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 bytes for :func:`canonical_json`."""

    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return a lowercase, unprefixed SHA-256 digest of canonical JSON."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


_MISSING = object()


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=False)
    if isinstance(value, Mapping):
        if name in value:
            return value[name]
    elif hasattr(value, name):
        return getattr(value, name)
    if default is not _MISSING:
        return default
    raise CanonicalizationError(f"digest input is missing required field {name!r}")


def canonical_asset(value: Any) -> dict[str, Any]:
    """Project an asset-like value onto fields that affect published content."""

    return {
        "asset_type": canonicalize(_field(value, "asset_type")),
        "source": canonicalize(_field(value, "source")),
        "local_path": canonicalize(_field(value, "local_path")),
        "meta": canonicalize(_field(value, "meta", {})),
    }


def canonical_publication_target(value: Any) -> dict[str, Any]:
    """Project a target-like value onto the immutable publication destination."""

    return {
        "account_id": canonicalize(_field(value, "account_id")),
        "platform": canonicalize(_field(value, "platform")),
        "account_binding_digest": canonicalize(_field(value, "account_binding_digest")),
        "approved_external_account_id": canonicalize(
            _field(value, "approved_external_account_id", None)
        ),
        "execution": canonicalize(_field(value, "execution")),
    }


def content_digest_payload(
    *,
    title: str,
    body: str,
    content_type: Any,
    extra: Mapping[str, Any] | None,
    assets: Iterable[Any],
) -> dict[str, Any]:
    """Build the canonical mutable-content payload used for approval binding.

    Asset order is intentionally preserved.  It can affect a carousel, video
    sequence, or cover choice and therefore represents a content mutation.
    """

    return {
        "title": canonicalize(title),
        "body": canonicalize(body),
        "content_type": canonicalize(content_type),
        "extra": canonicalize(extra or {}),
        "assets": [canonical_asset(asset) for asset in assets],
    }


def content_digest(
    *,
    title: str,
    body: str,
    content_type: Any,
    extra: Mapping[str, Any] | None,
    assets: Iterable[Any],
) -> str:
    """Digest the mutable article content and its ordered asset projection."""

    return canonical_sha256(
        content_digest_payload(
            title=title,
            body=body,
            content_type=content_type,
            extra=extra,
            assets=assets,
        )
    )


def plan_digest_payload(
    *,
    content_digest: str,
    targets: Iterable[Any],
    planned_for: datetime | None,
) -> dict[str, Any]:
    """Build a canonical plan payload independent of caller target ordering."""

    normalized_targets = [canonical_publication_target(target) for target in targets]
    normalized_targets.sort(key=canonical_json)
    return {
        "content_digest": canonicalize(content_digest),
        "targets": normalized_targets,
        "planned_for": canonicalize(planned_for),
    }


def plan_digest(
    *,
    content_digest: str,
    targets: Iterable[Any],
    planned_for: datetime | None,
) -> str:
    """Digest a content revision, canonical target set, and UTC plan time."""

    return canonical_sha256(
        plan_digest_payload(
            content_digest=content_digest,
            targets=targets,
            planned_for=planned_for,
        )
    )


__all__ = [
    "CanonicalizationError",
    "canonical_asset",
    "canonical_json",
    "canonical_json_bytes",
    "canonical_publication_target",
    "canonical_sha256",
    "canonicalize",
    "content_digest",
    "content_digest_payload",
    "normalize_utc_datetime",
    "plan_digest",
    "plan_digest_payload",
]
