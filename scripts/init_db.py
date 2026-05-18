"""初始化数据库。

两条路径（不互斥，按场景选）：

1. dev / 测试：本脚本默认走 `Base.metadata.create_all()`，一次性把所有表建出来。
   优点：零配置，pytest 套件全部走此路径（in-memory engine + create_all）。
   缺点：不能加字段（schema 变更 = drop 表 + 重建）；不可用于生产。

2. 生产 / dev 自愈：用 alembic 跑迁移：
        alembic upgrade head        # 升到最新 schema
        alembic downgrade -1        # 回退一步
        alembic current             # 当前版本
        alembic history             # 完整链路

   生产部署必走 alembic（schema 变更可平滑、可回滚）。本脚本可作为
   一次性"绿地"初始化（首次部署、CI 准备测试 DB）的快捷入口，但生产环境
   严禁用本脚本绕过 alembic 加字段。

Round 5 新增：`python scripts/init_db.py --upgrade` 改走 alembic upgrade head
（用 try_auto_upgrade(force=True)），适合本地从早期 create_all DB 自愈到最新 schema。
"""
from __future__ import annotations

import argparse
import sys


def _create_all_only() -> int:
    from ai_ops.core.db import init_db

    init_db()
    print("OK: database initialized (Base.metadata.create_all 路径)")
    print()
    print("提示：生产环境请改用 `alembic upgrade head` 走迁移管理；")
    print("     本脚本仅供 dev / 测试 快速初始化，不能用于已有数据的 schema 变更。")
    print("     如需把已存在的 dev DB 升到最新 schema，请加 --upgrade。")
    return 0


def _alembic_upgrade() -> int:
    """显式触发 alembic upgrade head（force=True，绕开 auto_upgrade_db 默认 False）。

    退出码：0 = 成功 / 已在 head；1 = 失败。
    """
    from ai_ops.core.db import check_schema_drift, try_auto_upgrade

    drift = check_schema_drift()
    if drift["in_sync"]:
        print(f"OK: DB already at head (rev={drift['code_head']}), no upgrade needed.")
        return 0

    print(f"[upgrade] db_head={drift['db_head']} code_head={drift['code_head']}")
    print(f"[upgrade] pending migrations: {drift['missing_migrations']}")
    result = try_auto_upgrade(force=True)
    if result["ok"]:
        print(
            f"OK: upgraded {result['from_rev']} -> {result['to_rev']} "
            f"({result['reason']})"
        )
        return 0

    print(
        f"FAIL: upgrade failed (reason={result['reason']}, error={result['error']})",
        file=sys.stderr,
    )
    print(
        "  fallback: 手动跑 `alembic upgrade head`，并把 stderr 贴给运维排查。",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 / 升级 ai-ops-auto 数据库")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="改走 alembic upgrade head（适合把已存在的 dev DB 升到最新 schema）",
    )
    args = parser.parse_args()
    if args.upgrade:
        return _alembic_upgrade()
    return _create_all_only()


if __name__ == "__main__":
    raise SystemExit(main())
