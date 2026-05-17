"""幂等迁移：为 topics 表加 category 列，为 accounts 表加 topic_id 列。

本项目使用 SQLAlchemy `Base.metadata.create_all()` 模式，没启用 alembic env。
为兼容存量 DB（v0.1 之前已经建过表），用 SQLite/PG 都支持的 `ALTER TABLE ADD COLUMN`
做幂等 patch；新建库直接走 init_db 即可，本脚本无影响。

约束：
- 幂等：列已存在 → 跳过；列不存在 → 加列 + 落 default 给存量行。
- SQLite 的 ALTER TABLE ADD COLUMN 原生支持，无需 batch recreate。
- 对 PG/MySQL 也兼容（同样的 ADD COLUMN 语法）。

用法：
    python scripts/migrate_add_topic_category.py
"""
from __future__ import annotations

import sys

from sqlalchemy import inspect, text

from ai_ops.core.db import _engine


def _has_column(insp, table: str, column: str) -> bool:
    try:
        cols = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False
    return column in cols


def _has_table(insp, table: str) -> bool:
    return table in insp.get_table_names()


def migrate() -> dict:
    """执行迁移，返回 {action: [columns_added]}。"""
    insp = inspect(_engine)
    changes: dict[str, list[str]] = {"added": [], "skipped": [], "missing_tables": []}

    # topics 表
    if not _has_table(insp, "topics"):
        changes["missing_tables"].append("topics")
    else:
        if _has_column(insp, "topics", "category"):
            changes["skipped"].append("topics.category")
        else:
            with _engine.begin() as conn:
                # 落 default 给存量行，nullable=False 由 server_default 保证
                conn.execute(
                    text(
                        "ALTER TABLE topics ADD COLUMN category VARCHAR(32) "
                        "NOT NULL DEFAULT 'general'"
                    )
                )
            changes["added"].append("topics.category")

    # accounts 表
    if not _has_table(insp, "accounts"):
        changes["missing_tables"].append("accounts")
    else:
        if _has_column(insp, "accounts", "topic_id"):
            changes["skipped"].append("accounts.topic_id")
        else:
            with _engine.begin() as conn:
                # nullable=True，存量行直接 NULL；FK 在 SQLite 是 advisory（默认不强制）
                conn.execute(text("ALTER TABLE accounts ADD COLUMN topic_id INTEGER"))
            changes["added"].append("accounts.topic_id")

    return changes


if __name__ == "__main__":
    result = migrate()
    print("OK: migrate_add_topic_category")
    print(f"  added:          {result['added'] or '(none)'}")
    print(f"  skipped:        {result['skipped'] or '(none)'}")
    if result["missing_tables"]:
        print(
            f"  missing_tables: {result['missing_tables']} "
            "(请先跑 python scripts/init_db.py)"
        )
        sys.exit(1)
