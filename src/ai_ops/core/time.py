"""Project datetime convention helpers.

Database ``DateTime`` columns are timezone-naive for legacy SQLite/PostgreSQL
compatibility. Every naive value therefore represents UTC, never host-local time.
"""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc_naive(value: datetime | None) -> datetime | None:
    """Normalize an aware datetime to UTC and remove tzinfo for persistence."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
