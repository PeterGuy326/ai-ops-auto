from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base

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
