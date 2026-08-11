"""素材分发中台单测 —— 入库待审 → 审核 → 按账号分发 → 留痕。

覆盖：
  1. stage_to_library：生成产物落库为 DRAFT(待审) + 挂 video/audio Asset
  2. 审核闸：DRAFT 直接 distribute 被拒（防误直发）
  3. approve：DRAFT → READY
  4. distribute（显式账号）：每账号一条 PublishJob(PENDING)，素材转 SCHEDULED
  5. distribute（按 target_platforms 自动选号）
  6. list_account_jobs：按个人账号查留痕记录
  7. 文章 / 视频 / 博客 三类素材都能入库
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.content import distributor as dist
from ai_ops.content import manager as content_manager
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Asset, Base, PublishJob, Topic
from ai_ops.core.schemas import ArticleIn


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def topic(session):
    t = Topic(name="逆袭短剧", category="drama")
    session.add(t)
    session.flush()
    return t


def _acc(session, platform, nick):
    a = Account(platform=platform, nickname=nick, profile={})
    session.add(a)
    session.flush()
    return a


def test_stage_video_to_library_is_draft(session, topic):
    art = dist.stage_to_library(
        session,
        topic_id=topic.id,
        title="逆袭短剧·第一集",
        content_type=ContentType.VIDEO,
        body="高能打脸",
        video_paths=["/data/outputs/happyhorse/ep1.mp4"],
        target_platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
    )
    assert art.status == ArticleStatus.DRAFT          # 入库即待审，不直发
    assets = session.query(Asset).filter_by(article_id=art.id).all()
    assert len(assets) == 1 and assets[0].asset_type == AssetType.VIDEO
    assert art.target_platforms == ["douyin", "xiaohongshu"]


def test_draft_cannot_distribute(session, topic):
    art = dist.stage_to_library(
        session, topic_id=topic.id, title="x", content_type=ContentType.VIDEO,
        video_paths=["/v.mp4"], target_platforms=[Platform.DOUYIN],
    )
    _acc(session, Platform.DOUYIN, "抖音号A")
    with pytest.raises(ValueError, match="审核"):
        dist.distribute(session, art.id)            # DRAFT 不能分发


def test_approve_then_distribute_explicit_accounts(session, topic):
    art = dist.stage_to_library(
        session, topic_id=topic.id, title="第一集", content_type=ContentType.VIDEO,
        video_paths=["/v.mp4"], target_platforms=[Platform.DOUYIN],
    )
    a1 = _acc(session, Platform.DOUYIN, "抖音号A")
    a2 = _acc(session, Platform.DOUYIN, "抖音号B")

    dist.approve(session, art.id)
    assert session.get(Article, art.id).status == ArticleStatus.READY

    jobs = dist.distribute(session, art.id, account_ids=[a1.id, a2.id])
    assert len(jobs) == 2                            # 每账号一条分发记录
    assert {j.account_id for j in jobs} == {a1.id, a2.id}
    assert all(j.status == JobStatus.PENDING and j.platform == Platform.DOUYIN for j in jobs)
    assert session.get(Article, art.id).status == ArticleStatus.SCHEDULED  # 分发后转已排期


def test_distribute_auto_pick_by_platform(session, topic):
    art = dist.stage_to_library(
        session, topic_id=topic.id, title="多平台", content_type=ContentType.VIDEO,
        video_paths=["/v.mp4"], target_platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
    )
    dy = _acc(session, Platform.DOUYIN, "抖音号")
    xhs = _acc(session, Platform.XIAOHONGSHU, "小红书号")
    _acc(session, Platform.ZHIHU, "知乎号")  # 不在 target_platforms，不应被选

    dist.approve(session, art.id)
    jobs = dist.distribute(session, art.id)          # 不传账号 → 按 target_platforms 自动选
    assert {j.account_id for j in jobs} == {dy.id, xhs.id}
    assert {j.platform for j in jobs} == {Platform.DOUYIN, Platform.XIAOHONGSHU}


def test_distribute_does_not_select_unhealthy_accounts(session, topic):
    art = dist.stage_to_library(
        session,
        topic_id=topic.id,
        title="健康门禁",
        content_type=ContentType.VIDEO,
        target_platforms=[Platform.DOUYIN],
    )
    healthy = _acc(session, Platform.DOUYIN, "健康账号")
    banned = _acc(session, Platform.DOUYIN, "封禁账号")
    banned.health = AccountHealth.BANNED
    dist.approve(session, art.id)

    jobs = dist.distribute(session, art.id)

    assert [job.account_id for job in jobs] == [healthy.id]


def test_explicit_unhealthy_account_is_rejected(session, topic):
    art = dist.stage_to_library(
        session,
        topic_id=topic.id,
        title="显式健康门禁",
        content_type=ContentType.VIDEO,
        target_platforms=[Platform.DOUYIN],
    )
    expired = _acc(session, Platform.DOUYIN, "过期账号")
    expired.health = AccountHealth.EXPIRED
    dist.approve(session, art.id)

    with pytest.raises(ValueError, match=r"健康状态不可分发.*expired"):
        dist.distribute(session, art.id, account_ids=[expired.id])


def test_distributed_jobs_inherit_future_article_schedule(session, topic):
    planned = datetime.utcnow() + timedelta(days=1)
    art = dist.stage_to_library(
        session,
        topic_id=topic.id,
        title="明天再发",
        content_type=ContentType.VIDEO,
        video_paths=["/v.mp4"],
        target_platforms=[Platform.DOUYIN],
    )
    art.scheduled_at = planned
    account = _acc(session, Platform.DOUYIN, "排期账号")
    dist.approve(session, art.id)

    jobs = dist.distribute(session, art.id, account_ids=[account.id])

    assert len(jobs) == 1
    assert jobs[0].scheduled_at == planned


def test_aware_article_schedule_round_trips_as_utc_naive_job(session, topic):
    local_plan = datetime(
        2026,
        8,
        10,
        10,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    article_out = content_manager.create_article(
        session,
        ArticleIn(
            topic_id=topic.id,
            title="带时区排期",
            content_type=ContentType.VIDEO,
            target_platforms=[Platform.DOUYIN],
            scheduled_at=local_plan,
        ),
    )
    account = _acc(session, Platform.DOUYIN, "时区账号")
    article_id = article_out.id
    account_id = account.id
    # Emulate separate create and distribute API requests: force a DB round trip
    # before the second operation so SQLite cannot hide timezone loss in memory.
    session.commit()
    session.expunge_all()
    assert session.get(Article, article_id).scheduled_at == datetime(2026, 8, 10, 2, 0)

    dist.approve(session, article_id)
    jobs = dist.distribute(session, article_id, account_ids=[account_id])
    job_id = jobs[0].id
    session.commit()
    session.expunge_all()

    reloaded = session.get(PublishJob, job_id)
    assert reloaded.scheduled_at == datetime(2026, 8, 10, 2, 0)


def test_list_account_jobs_records(session, topic):
    art = dist.stage_to_library(
        session, topic_id=topic.id, title="留痕", content_type=ContentType.VIDEO,
        video_paths=["/v.mp4"], target_platforms=[Platform.DOUYIN],
    )
    a1 = _acc(session, Platform.DOUYIN, "抖音号A")
    dist.approve(session, art.id)
    dist.distribute(session, art.id, account_ids=[a1.id])

    recs = dist.list_account_jobs(session, a1.id)
    assert len(recs) == 1
    assert recs[0].article_id == art.id and recs[0].platform == Platform.DOUYIN


def test_article_and_blog_also_stage(session, topic):
    # 图文文章
    a = dist.stage_to_library(
        session, topic_id=topic.id, title="文章", content_type=ContentType.IMAGE_TEXT,
        body="正文", image_paths=["/img/1.png"], target_platforms=[Platform.XIAOHONGSHU],
    )
    # 博客长文
    b = dist.stage_to_library(
        session, topic_id=topic.id, title="博客", content_type=ContentType.LONG_ARTICLE,
        body="# 标题\n正文", target_platforms=[Platform.GITHUB_PAGES],
    )
    assert a.content_type == ContentType.IMAGE_TEXT and a.status == ArticleStatus.DRAFT
    assert b.content_type == ContentType.LONG_ARTICLE and b.status == ArticleStatus.DRAFT
    assert session.query(Asset).filter_by(article_id=a.id).count() == 1   # 图片
    assert session.query(Asset).filter_by(article_id=b.id).count() == 0   # 博客正文无文件
