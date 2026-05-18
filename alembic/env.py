"""alembic env.py — ai-ops-auto schema 演进入口。

与默认模板的关键差异：
  1. target_metadata = Base.metadata（import 自 ai_ops.core.models），
     autogenerate 才能识别 model 变更。
  2. database_url 优先级：DATABASE_URL env > ai_ops.config.settings.database_url
     > alembic.ini 的 fallback。
     - DATABASE_URL env：测试 / CI / 临时验证用（DONE step 4/5 直接走此路径）
     - settings.database_url：生产路径（运维 alembic upgrade head 时复用 app 配置）
     - ini fallback：纯本地、未配 settings 时兜底
  3. render_as_batch=True 永远生效。SQLite ALTER TABLE 不能 add/drop FK column，
     必须走 batch（CREATE temp table + COPY + DROP + RENAME）。Postgres 上
     render_as_batch=True 也兼容，autogenerate 出来的 batch_alter_table 在
     非-SQLite 后端会退化成普通 alter，无副作用。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 确保 src/ 在 path 上——alembic CLI 不走 pip install，import ai_ops 会失败
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_ops.core.models import Base  # noqa: E402

config = context.config

# 日志：alembic.ini 的 [loggers] 配置直接生效（默认 WARN，不刷屏）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """按优先级解析 sqlalchemy.url。

    1. DATABASE_URL env（CI / 临时验证 / DONE step 4-5 用此路径）
    2. ai_ops.config.settings.database_url（生产 / 复用 app 配置）
    3. alembic.ini 的 fallback（纯本地兜底）
    """
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    try:
        from ai_ops.config import settings
        cfg_url = (settings.database_url or "").strip()
        if cfg_url:
            return cfg_url
    except Exception:
        # settings 加载失败（如 pydantic-settings 校验报错），fallback 到 ini
        pass
    return config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    """offline 模式：生成 SQL 脚本，不连 DB。"""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite ALTER TABLE 限制必备
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online 模式：连真 DB 跑迁移。"""
    # 覆盖 ini 的 sqlalchemy.url（env / settings 优先）
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite ALTER TABLE 限制必备
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
