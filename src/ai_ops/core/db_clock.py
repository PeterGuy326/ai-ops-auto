"""Database-side UTC clock expressions for lease fencing."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Interval, func, literal


def database_utc_now(session, *, after_seconds: int = 0):
    """Return a database-evaluated naive UTC timestamp expression.

    Lease predicates must be evaluated after any row-lock wait. A Python
    timestamp captured before sending the statement can otherwise let a stale
    owner finalize after its lease has expired.
    """
    seconds = max(0, int(after_seconds))
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        arguments: list[object] = ["%Y-%m-%d %H:%M:%f", "now"]
        if seconds:
            arguments.append(f"+{seconds} seconds")
        return func.strftime(*arguments)

    if dialect_name == "postgresql":
        current = func.timezone("UTC", func.clock_timestamp())
    else:
        current = func.current_timestamp()
    if seconds:
        return current + literal(timedelta(seconds=seconds), type_=Interval())
    return current
