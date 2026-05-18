"""初始化数据库。

两条路径（不互斥，按场景选）：

1. dev / 测试：本脚本走 `Base.metadata.create_all()`，一次性把所有表建出来。
   优点：零配置，pytest 套件全部走此路径（in-memory engine + create_all）。
   缺点：不能加字段（schema 变更 = drop 表 + 重建）；不可用于生产。

2. 生产：用 alembic 跑迁移：
        alembic upgrade head        # 升到最新 schema
        alembic downgrade -1        # 回退一步
        alembic current             # 当前版本
        alembic history             # 完整链路

   生产部署必走 alembic（schema 变更可平滑、可回滚）。本脚本仍可作为
   一次性"绿地"初始化（首次部署、CI 准备测试 DB）的快捷入口，但生产环境
   严禁用本脚本绕过 alembic 加字段。
"""
from ai_ops.core.db import init_db

if __name__ == "__main__":
    init_db()
    print("OK: database initialized (Base.metadata.create_all 路径)")
    print()
    print("提示：生产环境请改用 `alembic upgrade head` 走迁移管理；")
    print("     本脚本仅供 dev / 测试 快速初始化，不能用于已有数据的 schema 变更。")
