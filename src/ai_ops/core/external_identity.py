"""Canonical public identities used to bind external account destinations."""

from __future__ import annotations

import re


_ZHIHU_RAW_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ZHIHU_EXTERNAL_ACCOUNT_ID_RE = re.compile(r"^zhihu:id:[A-Za-z0-9_-]{1,128}$")


def normalize_zhihu_external_account_id(value: object) -> str:
    """Validate the canonical stable identity stored in ``Account.profile``."""

    if not isinstance(value, str) or _ZHIHU_EXTERNAL_ACCOUNT_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid Zhihu external account identity")
    return value


def zhihu_external_account_id_from_whoami(profile: object) -> str | None:
    """Project immutable ``whoami.id`` without using the mutable vanity token."""

    if not isinstance(profile, dict):
        return None
    raw_account_id = profile.get("id")
    if (
        not isinstance(raw_account_id, str)
        or _ZHIHU_RAW_ACCOUNT_ID_RE.fullmatch(raw_account_id) is None
    ):
        return None
    return f"zhihu:id:{raw_account_id}"


__all__ = [
    "normalize_zhihu_external_account_id",
    "zhihu_external_account_id_from_whoami",
]
