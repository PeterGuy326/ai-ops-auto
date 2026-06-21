"""历史发布回填单测 —— 把之前/手动发的内容纳入按账号管理 + 喂查重。

  1. import_published_post：建 Article(PUBLISHED) + PublishJob(SUCCESS) 挂账号
  2. 幂等：同账号 + 同 platform_post_id 不重复导入
  3. 进 list_account_jobs（按账号留痕含历史）
  4. 自动建「历史导入」专题
  5. 批量导入
  6. 历史内容能被 dedup 查重看到（SUCCESS job + Article.body）
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.content import distributor as dist
from ai_ops.core.enums import ArticleStatus, ContentType, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def account(session):
    a = Account(platform=Platform.DOUYIN, nickname="抖音老号", profile={})
    session.add(a)
    session.flush()
    return a


def test_import_creates_published_records(session, account):
    job = dist.import_published_post(
        session, account.id,
        title="我去年发的爆款", content_type=ContentType.VIDEO,
        body="历史正文", platform_url="https://douyin.com/v/123",
        platform_post_id="dy-123", published_at=datetime(2025, 12, 1),
    )
    assert job.status == JobStatus.SUCCESS
    assert job.account_id == account.id and job.platform == Platform.DOUYIN
    assert job.platform_url == "https://douyin.com/v/123"
    art = session.get(Article, job.article_id)
    assert art.status == ArticleStatus.PUBLISHED
    assert art.extra.get("backfill") is True
    # 自动建「历史导入」专题
    assert session.query(Topic).filter_by(name="历史导入").count() == 1


def test_import_idempotent(session, account):
    kw = dict(title="同一条", platform_post_id="dy-999")
    j1 = dist.import_published_post(session, account.id, **kw)
    j2 = dist.import_published_post(session, account.id, **kw)  # 重复导入
    assert j1.id == j2.id  # 幂等：不重复建
    assert session.query(PublishJob).filter_by(account_id=account.id).count() == 1


def test_import_shows_in_account_records(session, account):
    dist.import_published_post(session, account.id, title="历史A", platform_post_id="a")
    dist.import_published_post(session, account.id, title="历史B", platform_post_id="b")
    recs = dist.list_account_jobs(session, account.id)
    assert len(recs) == 2
    assert all(r.status == JobStatus.SUCCESS for r in recs)


def test_import_bulk(session, account):
    posts = [
        {"title": "历史1", "platform_post_id": "p1"},
        {"title": "历史2", "platform_post_id": "p2"},
        {"title": "历史3", "platform_post_id": "p3"},
    ]
    jobs = dist.import_published_bulk(session, account.id, posts)
    assert len(jobs) == 3
    assert session.query(PublishJob).filter_by(account_id=account.id).count() == 3


def test_imported_history_matches_dedup_query_shape(session, account):
    """历史回填记录满足 dedup（is_too_similar）的查询形状：
    PublishJob(account_id, status=SUCCESS, finished_at) JOIN Article.body。
    """
    dist.import_published_post(
        session, account.id, title="历史爆款",
        body="这是一段很独特的历史文案内容用于查重测试",
        platform_post_id="h1", published_at=datetime.utcnow(),
    )
    # 复刻 is_too_similar 的 join 查询，确认能命中回填的 body
    rows = (
        session.query(Article.body)
        .join(PublishJob, PublishJob.article_id == Article.id)
        .filter(
            PublishJob.account_id == account.id,
            PublishJob.status == JobStatus.SUCCESS,
            PublishJob.finished_at.isnot(None),
        )
        .all()
    )
    assert any("历史文案" in (b or "") for (b,) in rows)
