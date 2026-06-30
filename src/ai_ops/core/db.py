from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base
# 把分散在子包的 ORM 模型显式注册到 Base.metadata——
# 否则 init_db() 的 create_all 扫不到 jobhunt 四张表（仅 import 副作用，故 noqa）。
from ..jobhunt import models as _jobhunt_models  # noqa: E402,F401

_engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
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


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(_engine)


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
# 底层逻辑：dev/prod schema parity。生产走 Dockerfile entrypoint 跑 subprocess
# alembic upgrade（已稳定）；dev 本地 uvicorn 启动期长期裸奔——开发者 git pull
# 拿到新 model 后 init_db() 只对全新空 DB 有效，已存在的旧 DB 不会被 create_all
# 自动 ALTER 加字段，启动可能炸。本节提供：
#   - check_schema_drift()     : 启动期自检（lifespan SCHEMA-CHECK 调）
#   - try_auto_upgrade()       : dev 默认开/prod 默认关的应用进程内 upgrade
#   - get_db_alembic_head()    : 查 DB 当前 alembic head（无表/无 DB 返 None）
#   - get_code_alembic_head()  : 扫 alembic/versions/ 拿代码侧 head
# 全部容错：异常 → 返 None / {ok: False}，不抛（让 lifespan 自己决定是否 raise）。
# ============================================================================
from pathlib import Path as _Path


def _alembic_config():
    """构造一个绑定到 settings.database_url 的 alembic Config。

    优先级与 alembic/env.py 一致：DATABASE_URL env > settings.database_url > ini fallback。
    这里我们让 env.py 的 _resolve_database_url() 接管，所以**不在此覆盖 sqlalchemy.url**——
    保持 alembic CLI / 此处 Python API / lifespan 三条路径同语义。

    返回 alembic.config.Config；如 alembic.ini 不存在返 None（容错）。
    """
    from alembic.config import Config

    # alembic.ini 在仓库根（src 的上上级）。lifespan / scripts 入口 cwd 不固定，
    # 用 db.py 文件路径上溯解析最可靠。
    repo_root = _Path(__file__).resolve().parents[3]
    ini_path = repo_root / "alembic.ini"
    if not ini_path.exists():
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




def _detect_legacy_create_all_db() -> tuple[bool, str | None]:
    """检测"业务表已存在但无 alembic_version 表"的早期 dev DB 场景。

    这是 Round 5 P9 事故的另一面：DB 是 create_all 建的，alembic 跑 baseline migration
    时会撞 "table already exists"，正确做法是先 stamp 到 baseline 再 upgrade 跑增量。

    返回 (是否是 legacy create_all DB, 推断的 baseline rev)。baseline rev 永远是 alembic
    versions 链路的最早 rev（无 down_revision 的那个）。
    """
    from sqlalchemy import create_engine, inspect

    try:
        eng = create_engine(
            settings.database_url,
            future=True,
            connect_args={"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {},
        )
        with eng.connect() as conn:
            insp = inspect(conn)
            tables = set(insp.get_table_names())
            # 业务表存在 + DB head 为空（无 alembic_version 表或表里没 row）→ legacy
            # 注意：alembic 失败迁移可能留下空 alembic_version 表，单看 "表是否存在" 不够，
            # 要看 get_db_alembic_head() 是否真有 rev —— 这才是 alembic 认知的 "schema 在哪"。
            has_business = bool(tables & {"topics", "accounts", "publish_jobs"})
            if not has_business:
                return False, None
            if get_db_alembic_head() is not None:
                # 已有 alembic rev → 不是 legacy（正规迁移路径）
                return False, None
    except Exception:
        return False, None

    # 找 baseline（无 down_revision 的 rev）
    try:
        from alembic.script import ScriptDirectory

        cfg = _alembic_config()
        if cfg is None:
            return False, None
        script = ScriptDirectory.from_config(cfg)
        for rev in script.walk_revisions():
            if rev.down_revision is None:
                return True, rev.revision
    except Exception:
        return False, None
    return False, None

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

    if dry_run:
        return {
            "attempted": False,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": True,
            "error": None,
            "reason": f"dry_run: would upgrade {from_rev} -> {to_rev}",
        }

    try:
        from alembic import command

        cfg = _alembic_config()
        if cfg is None:
            return {
                "attempted": False,
                "from_rev": from_rev,
                "to_rev": to_rev,
                "ok": False,
                "error": "alembic.ini not found",
                "reason": "config missing",
            }
        # Round 5 P9 事故关键路径：legacy create_all DB（业务表已存在但无 alembic_version）
        # 直接 upgrade 会撞 "table already exists"。正确做法：先 stamp baseline，再 upgrade
        # 跑增量。stamp 只写 alembic_version 表，不动业务表，安全。
        if from_rev is None:
            is_legacy, baseline_rev = _detect_legacy_create_all_db()
            if is_legacy and baseline_rev:
                command.stamp(cfg, baseline_rev)
                from_rev = baseline_rev  # 升级日志体现真实起点
        command.upgrade(cfg, "head")
        # 再查一次确认（不依赖 alembic 内部返回值，事实证明最稳）
        new_head = get_db_alembic_head()
        ok = new_head == to_rev
        return {
            "attempted": True,
            "from_rev": from_rev,
            "to_rev": to_rev,
            "ok": ok,
            "error": None if ok else f"after upgrade db head={new_head}, expected {to_rev}",
            "reason": "upgrade executed",
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
