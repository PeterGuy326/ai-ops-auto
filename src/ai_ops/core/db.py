from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base
# 把分散在子包的 ORM 模型显式注册到 Base.metadata——
# 否则 init_db() 的 create_all 扫不到 jobhunt 四张表（仅 import 副作用，故 noqa）。
from ..jobhunt import models as _jobhunt_models  # noqa: E402,F401

def enable_sqlite_foreign_keys(engine) -> None:
    """Make declared SQLite foreign keys enforceable on every pooled connection."""

    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


_engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
enable_sqlite_foreign_keys(_engine)
# expire_on_commit=False 是 production-safe 的关键约定：
# 默认 True 时 commit 后所有 ORM attribute 会被 expire，下次 access 触发 auto-refresh；
# 若此时 session 已关闭（如 worker 跳出 session_scope 后读 job.account_id 拼日志/
# notify 快照），就抛 DetachedInstanceError —— 真发布会直接炸。
# 业界共识（FastAPI / SQLAlchemy 官方文档）web 服务统一用 False，refresh 按需手动。
SessionLocal = sessionmaker(
    bind=_engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)


class DatabaseSchemaError(RuntimeError):
    """The configured database cannot safely be used by this code version."""


def init_db() -> dict[str, Any]:
    """Safely initialize or upgrade the configured database to Alembic head.

    This is the public initialization path used by the CLI.  It deliberately
    never calls ``Base.metadata.create_all``: an empty database is migrated
    from Alembic base, while an existing unversioned database is adopted only
    when its reflected schema exactly matches the current ORM metadata.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return require_database_at_head(allow_upgrade=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# ============================================================================
# Round 5 · schema 漂移自检 + 自动 alembic 升级
# ----------------------------------------------------------------------------
# 底层逻辑：dev/prod schema parity。生产走 Dockerfile entrypoint 的安全初始化
# 命令；dev 本地可选择在进程启动期自动升级。应用运行入口不再调用 create_all，
# 防止无 alembic_version 的数据库进入“看似可用、实际无法升级”的状态。本节提供：
#   - check_schema_drift()     : 启动期自检（lifespan SCHEMA-CHECK 调）
#   - try_auto_upgrade()       : dev 默认开/prod 默认关的应用进程内 upgrade
#   - require_database_at_head(): 应用启动闸门，只接受 head（可显式允许安全升级）
#   - get_db_alembic_head()    : 查 DB 当前 alembic head（无表/无 DB 返 None）
#   - get_code_alembic_head()  : 扫 alembic/versions/ 拿代码侧 head
# 查询/升级 helper 结构化返回；启动闸门失败时抛 DatabaseSchemaError。
# ============================================================================


def _alembic_config():
    """构造一个绑定到 settings.database_url 的 alembic Config。

    优先级与 alembic/env.py 一致：DATABASE_URL env > settings.database_url > ini fallback。
    这里我们让 env.py 的 _resolve_database_url() 接管，所以**不在此覆盖 sqlalchemy.url**——
    保持 alembic CLI / 此处 Python API / lifespan 三条路径同语义。

    返回 alembic.config.Config；如 alembic.ini 不存在返 None（容错）。
    """
    from alembic.config import Config

    # 可编辑/源码安装时读仓库根；普通 wheel 安装时读
    # pyproject data-files 放到 sys.prefix/share 下的副本。
    source_root = Path(__file__).resolve().parents[3]
    candidates = (
        source_root / "alembic.ini",
        Path(sys.prefix) / "share" / "ai-ops-auto" / "alembic.ini",
    )
    ini_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if ini_path is None:
        return None
    cfg = Config(str(ini_path))
    # alembic 的 script_location 是相对 alembic.ini 所在目录的"alembic"——
    # Config(str(ini)) 已自动以 ini 的 dirname 为 here，无需再设。
    return cfg


def get_db_alembic_head(engine=None):
    """查 DB 当前 alembic_version 表里的 head revision。

    容错：
      - DB 文件不存在 / 连不上 → None
      - alembic_version 表不存在（早期 create_all 建的 dev DB）→ None
      - 表存在但为空 → None
      - 任何其它异常 → None（不抛，调用方按 None 当"未知"处理）
    """
    from sqlalchemy import create_engine, inspect, text

    try:
        eng = engine or create_engine(
            settings.database_url,
            future=True,
            connect_args={"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {},
        )
        if engine is None:
            enable_sqlite_foreign_keys(eng)
        with eng.connect() as conn:
            insp = inspect(conn)
            if "alembic_version" not in insp.get_table_names():
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            if row is None:
                return None
            return row[0]
    except Exception:
        return None


def get_code_alembic_head():
    """扫 alembic/versions/ 拿代码侧 head revision。

    用 alembic.script.ScriptDirectory.get_current_head() —— 这是 alembic 内部
    判定 "upgrade head 应该升到哪" 的官方 API，比自己扫文件 parse down_revision 稳。

    多 head（branch）场景：返回 None 而非随机选一个（业务暂未用 branch；如未来
    引入需要在此处明确策略）。

    容错：alembic.ini 不存在 / ScriptDirectory 加载失败 → None。
    """
    from alembic.script import ScriptDirectory

    try:
        cfg = _alembic_config()
        if cfg is None:
            return None
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        if not heads or len(heads) > 1:
            # 无 head / 多 head（未合并的 branch）→ 不猜
            return heads[0] if len(heads) == 1 else None
        return heads[0]
    except Exception:
        return None


def check_schema_drift():
    """聚合 db_head / code_head / 是否对齐 / 缺哪些 migration。

    返回 dict（结构稳定，方便 lifespan / 单测断言）：
      {
        "db_head": str | None,        # DB 当前 head；None = 无 alembic_version 表
        "code_head": str | None,      # 代码 head；None = 无 alembic.ini
        "in_sync": bool,              # db_head == code_head 且非 None
        "missing_migrations": [str],  # 从 db_head（不含）走到 code_head 需要跑的 rev id 列表
      }

    in_sync 判定：
      - 两者都非 None 且相等 → True
      - 其它一切（含 db_head=None 即"create_all 建的旧 dev DB"）→ False

    missing_migrations 计算：
      - db_head=None → 返回所有 rev（按 upgrade 顺序，base → head）
      - 否则返回 db_head 之后到 code_head 之间的 rev 列表（不含 db_head 自身）
      - 计算失败 → 返回空列表（in_sync 仍按上面规则定）
    """
    db_head = get_db_alembic_head()
    code_head = get_code_alembic_head()
    in_sync = db_head is not None and code_head is not None and db_head == code_head

    missing = []
    try:
        from alembic.script import ScriptDirectory

        cfg = _alembic_config()
        if cfg is not None and code_head is not None:
            script = ScriptDirectory.from_config(cfg)
            # iterate_revisions(upper, lower) 返回从 upper 走到 lower 的 rev（顺序反着），
            # lower=None 表示从 base 开始
            revs = list(script.iterate_revisions(code_head, db_head))
            # 反转成 upgrade 顺序（先跑的在前）
            missing = [r.revision for r in reversed(revs)]
    except Exception:
        missing = []

    return {
        "db_head": db_head,
        "code_head": code_head,
        "in_sync": in_sync,
        "missing_migrations": missing,
    }

def _summarize_schema_diffs(diffs: list[Any]) -> str:
    """Return stable, credential-free labels for Alembic schema differences."""
    labels: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, tuple) and value and isinstance(value[0], str):
            labels.add(value[0])
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(diffs)
    return ", ".join(sorted(labels)) or "schema differences"


def _inspect_unversioned_schema() -> tuple[str, str | None]:
    """Classify a database whose Alembic revision is absent.

    Returns ``("empty", None)`` for a truly empty default schema,
    ``("current", None)`` only when the reflected schema exactly matches
    ``Base.metadata``, and ``("unsafe", reason)`` for everything else.

    The exact comparison is intentionally stricter than checking a handful of
    well-known tables.  Alembic autogenerate compares columns, types,
    nullability, defaults, indexes, unique constraints and foreign keys.  The
    explicit table/view-set check additionally rejects an empty
    ``alembic_version`` table left by a failed migration and unrelated tables.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    eng = create_engine(
        settings.database_url,
        future=True,
        connect_args={"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {},
    )
    enable_sqlite_foreign_keys(eng)
    try:
        with eng.connect() as conn:
            inspector = inspect(conn)
            actual_tables = set(inspector.get_table_names())
            actual_views = set(inspector.get_view_names())
            if not actual_tables and not actual_views:
                return "empty", None

            expected_tables = {table.name for table in Base.metadata.sorted_tables}
            missing = sorted(expected_tables - actual_tables)
            unexpected = sorted(actual_tables - expected_tables)
            if missing or unexpected or actual_views:
                parts = []
                if missing:
                    parts.append(f"missing tables={missing}")
                if unexpected:
                    parts.append(f"unexpected tables={unexpected}")
                if actual_views:
                    parts.append(f"unexpected views={sorted(actual_views)}")
                return "unsafe", "; ".join(parts)

            context = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            diffs = compare_metadata(context, Base.metadata)
            if diffs:
                return "unsafe", f"metadata mismatch: {_summarize_schema_diffs(diffs)}"
            return "current", None
    finally:
        eng.dispose()

def try_auto_upgrade(dry_run: bool = False, force: bool = False) -> dict:
    """应用进程内尝试 alembic upgrade head。

    参数：
      dry_run: True → 只 check_schema_drift 返回会做什么，不真跑
      force:   True → 忽略 settings.auto_upgrade_db 开关（scripts/init_db.py --upgrade 用）

    返回 dict（稳定结构）：
      {
        "attempted": bool,   # 是否真发起了 upgrade（False = 已 in_sync 或开关关）
        "from_rev": str|None,
        "to_rev": str|None,
        "ok": bool,
        "error": str|None,
        "reason": str,       # attempted=False 时的原因（human-readable）
      }

    设计原则：
      - 异常不抛：任何 alembic 失败 → ok=False + error=str(e)，调用方决定是否 raise
      - 默认不动 schema：settings.auto_upgrade_db=False 时直接返回 attempted=False
        （生产应用进程不该改 schema —— prod 走 Dockerfile entrypoint subprocess）
      - 已 in_sync 不重复跑：节省启动时间 + 避免不必要的 alembic 锁
      - 无版本数据库只允许两种状态：真空库从 base 升级；与当前 ORM metadata
        精确一致的历史 create_all 库直接 stamp head。部分/未知 schema 一律拒绝。
    """
    drift = check_schema_drift()
    from_rev = drift["db_head"]
    to_rev = drift["code_head"]

    if drift["in_sync"]:
        return {
            "attempted": False,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": True,
            "error": None,
            "reason": "already in sync",
        }

    if not force and not settings.auto_upgrade_db:
        return {
            "attempted": False,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": False,
            "error": None,
            "reason": "auto_upgrade_db disabled (prod default; set AUTO_UPGRADE_DB=true for dev)",
        }

    try:
        from alembic import command

        cfg = _alembic_config()
        if cfg is None or to_rev is None:
            return {
                "attempted": False,
                "from_rev": from_rev,
                "to_rev": to_rev,
                "ok": False,
                "error": "Alembic config or a unique code head is unavailable",
                "reason": "migration graph unavailable",
            }

        unversioned_state = None
        unversioned_error = None
        if from_rev is None:
            unversioned_state, unversioned_error = _inspect_unversioned_schema()
            if unversioned_state == "unsafe":
                return {
                    "attempted": False,
                    "from_rev": None,
                    "to_rev": to_rev,
                    "ok": False,
                    "error": unversioned_error,
                    "reason": "unsafe unversioned schema refused",
                }

        if dry_run:
            action = (
                "stamp current schema at head"
                if unversioned_state == "current"
                else f"upgrade {from_rev} -> {to_rev}"
            )
            return {
                "attempted": False,
                "from_rev": from_rev,
                "to_rev": to_rev,
                "ok": True,
                "error": None,
                "reason": f"dry_run: would {action}",
            }

        if unversioned_state == "current":
            # The database already has the exact current schema.  Running any
            # migration would collide with existing tables/columns, so adopt
            # it without touching business data.
            command.stamp(cfg, "head")
            reason = "exact current unversioned schema stamped at head"
        else:
            # Covers a truly empty unversioned DB and a normal versioned DB
            # behind head.  Both must traverse the real migration graph.
            command.upgrade(cfg, "head")
            reason = "upgrade executed"

        # 再查一次确认（不依赖 alembic 内部返回值，事实证明最稳）
        new_head = get_db_alembic_head()
        ok = new_head == to_rev
        return {
            "attempted": True,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": ok,
            "error": None if ok else f"after upgrade db head={new_head}, expected {to_rev}",
            "reason": reason,
        }
    except Exception as e:
        return {
            "attempted": True,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "reason": "upgrade raised",
        }


def require_database_at_head(*, allow_upgrade: bool | None = None) -> dict[str, Any]:
    """Gate application startup on a verified Alembic head revision.

    ``allow_upgrade`` defaults to ``AUTO_UPGRADE_DB``.  Production API/worker
    processes therefore validate only, while development can opt into the
    same safe upgrade/adoption path used by ``ai-ops init-db``.
    """
    if allow_upgrade is None:
        allow_upgrade = settings.auto_upgrade_db

    drift = check_schema_drift()
    if drift["in_sync"]:
        return {
            "attempted": False,
            "from_rev": drift["db_head"],
            "to_rev": drift["code_head"],
            "ok": True,
            "error": None,
            "reason": "already in sync",
        }

    if not allow_upgrade:
        raise DatabaseSchemaError(
            "database schema is not at Alembic head "
            f"(db={drift['db_head']}, code={drift['code_head']}); "
            "run `ai-ops init-db` before starting the service"
        )

    result = try_auto_upgrade(force=True)
    if not result["ok"]:
        raise DatabaseSchemaError(
            "database initialization refused or failed "
            f"(reason={result['reason']})"
        )

    verified = check_schema_drift()
    if not verified["in_sync"]:
        raise DatabaseSchemaError(
            "database initialization did not reach the unique Alembic head "
            f"(db={verified['db_head']}, code={verified['code_head']})"
        )
    return result
