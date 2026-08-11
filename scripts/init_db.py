"""Safely initialize or upgrade the configured database to Alembic head.

This compatibility script uses the same guarded path as ``ai-ops init-db``:

* an empty database traverses the real migration chain;
* a versioned older database upgrades normally;
* an unversioned database is stamped at head only when its reflected schema
  exactly matches the current SQLAlchemy metadata;
* every other unversioned business database is refused without mutation.

``--upgrade`` remains accepted for backwards compatibility but is now a no-op:
the safe Alembic path is the default and only runtime initialization path.
Unit tests may still use ``Base.metadata.create_all`` on isolated test engines.
"""
from __future__ import annotations

import argparse
import sys


def _safe_initialize() -> int:
    from ai_ops.core.db import DatabaseSchemaError, init_db

    try:
        result = init_db()
    except DatabaseSchemaError as exc:
        print(f"FAIL: database initialization refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            "FAIL: database initialization failed "
            f"({type(exc).__name__}); inspect application logs.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: database is at Alembic head (rev={result['to_rev']}, "
        f"reason={result['reason']})."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="安全初始化 / 升级 ai-ops-auto 数据库")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="兼容旧命令；安全 Alembic 升级现已是默认且唯一的初始化路径",
    )
    parser.parse_args()
    return _safe_initialize()


if __name__ == "__main__":
    raise SystemExit(main())
