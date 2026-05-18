"""tests/test_superseded_by.py — PublishJob.superseded_by_job_id + helper 字段读写契约。

核心契约：
  1. 字段默认值 = None（"未被覆盖"是常态，老/新 job 一开始都该是 None）
  2. _mark_job_superseded helper 调用后字段真写入 + flush 可读
  3. self-FK 真工作：旧 job.superseded_by_job_id 能存新 job.id，FK 不报错

走的 session 套路：复用现有测试模板（SessionLocal.configure(bind=engine) +
Base.metadata.create_all），不引入 alembic 路径——alembic 路径在
test_alembic_migration.py 已覆盖；本文件只验字段语义，与迁移工具解耦。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic
from ai_ops.scheduler.worker import _mark_job_superseded


# ---------------------------------------------------------------------------
# Fixture：复用现有套路（in-memory engine + SessionLocal.configure(bind=engine)）
# ---------------------------------------------------------------------------


@pytest.fixture
def session_in_memory(monkeypatch):
    """提供 in-memory SQLite 上的 production SessionLocal。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)

    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _mk_topic_article_account(s) -> tuple[Topic, Article, Account]:
    """造一组最小 fixture 数据（topic + article + account），用于 PublishJob 创建。"""
    topic = Topic(name="t-superseded", keywords=[], persona={}, target_platforms=[])
    s.add(topic)
    s.flush()
    art = Article(
        topic_id=topic.id,
        title="title",
        body="body",
        content_type=ContentType.IMAGE_TEXT,
        status=ArticleStatus.READY,
    )
    s.add(art)
    acc = Account(
        platform=Platform.XIAOHONGSHU,
        nickname="nick",
        profile={},
        health=AccountHealth.HEALTHY,
    )
    s.add(acc)
    s.flush()
    return topic, art, acc


def _mk_job(s, art_id: int, acc_id: int) -> PublishJob:
    """造一个干净 PublishJob，所有字段走默认值。"""
    job = PublishJob(
        article_id=art_id,
        account_id=acc_id,
        platform=Platform.XIAOHONGSHU,
        status=JobStatus.PENDING,
    )
    s.add(job)
    s.flush()
    return job


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_superseded_by_field_default_is_none(session_in_memory) -> None:
    """新建 PublishJob 的 superseded_by_job_id 默认 = None（未被覆盖态）。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        job = _mk_job(s, art.id, acc.id)
        s.commit()

        # 重新查回来，验证默认值
        fresh = s.get(PublishJob, job.id)
        assert fresh is not None
        assert fresh.superseded_by_job_id is None, (
            f"新 job 的 superseded_by_job_id 默认应为 None，实际={fresh.superseded_by_job_id}"
        )


def test_mark_job_superseded_helper_sets_fk(session_in_memory) -> None:
    """_mark_job_superseded(s, old, new) 调用后旧 job.superseded_by_job_id = new.id。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        old_job = _mk_job(s, art.id, acc.id)
        new_job = _mk_job(s, art.id, acc.id)
        s.commit()

        ok = _mark_job_superseded(s, old_job.id, new_job.id)
        assert ok is True, "存在的 job 调用 helper 应返回 True"
        s.commit()

        fresh_old = s.get(PublishJob, old_job.id)
        assert fresh_old.superseded_by_job_id == new_job.id, (
            f"helper 应把 old.superseded_by_job_id 设为 {new_job.id}，"
            f"实际={fresh_old.superseded_by_job_id}"
        )
        # 新 job 自身不应被动到
        fresh_new = s.get(PublishJob, new_job.id)
        assert fresh_new.superseded_by_job_id is None, (
            "helper 不应动新 job 自身的字段"
        )


def test_mark_job_superseded_refuses_self_reference(session_in_memory) -> None:
    """helper 对自指（old==new）返回 False 不写入，避免数据污染。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        job = _mk_job(s, art.id, acc.id)
        s.commit()

        ok = _mark_job_superseded(s, job.id, job.id)
        assert ok is False, "自指调用应被拒绝"
        s.commit()

        fresh = s.get(PublishJob, job.id)
        assert fresh.superseded_by_job_id is None, "自指被拒后字段不应被改"


def test_mark_job_superseded_missing_old_returns_false(session_in_memory) -> None:
    """旧 job 不存在 → helper 返回 False（不抛异常，让上游降级处理）。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        new_job = _mk_job(s, art.id, acc.id)
        s.commit()

        # 旧 job id=99999 不存在
        ok = _mark_job_superseded(s, 99999, new_job.id)
        assert ok is False


def test_self_referential_fk_persists_round_trip(session_in_memory) -> None:
    """直接 ORM 写入 self-FK，commit 后另一 session 读出值正确。

    这条比 helper 多一层验证：纯 ORM 路径下 self-FK 也工作（FK 约束未被
    SQLAlchemy 静默吞掉，超越 helper 之上保证字段本身的语义完整）。
    """
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        old_job = _mk_job(s, art.id, acc.id)
        new_job = _mk_job(s, art.id, acc.id)
        old_job.superseded_by_job_id = new_job.id
        s.commit()
        old_id = old_job.id
        new_id = new_job.id

    # 全新 session 读出来验证
    with SessionLocal() as s2:
        fresh = s2.get(PublishJob, old_id)
        assert fresh.superseded_by_job_id == new_id, (
            f"另一 session 读出的 superseded_by_job_id 应={new_id}，"
            f"实际={fresh.superseded_by_job_id}"
        )
