"""生成→素材库拉通单测：pipeline 产物自动入库为 DRAFT 待审。

  1. ScriptToDramaPipeline(fake引擎) → stage_clip_plan → 视频素材入库(按切片归并多平台)
  2. TopicToPodcastPipeline(fake provider) → stage_podcast_result → 音频素材入库
  3. 入库后走中台：approve → distribute 按账号留痕
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.content import distributor as dist
from ai_ops.core.enums import ArticleStatus, AssetType, ContentType, Platform
from ai_ops.core.models import Account, Asset, Base, Topic


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
def topic(session):
    t = Topic(name="逆袭短剧", category="drama")
    session.add(t)
    session.flush()
    return t


@pytest.mark.asyncio
async def test_drama_plan_auto_stage(session, topic):
    from ai_ops.core.schemas import VideoArtifact, VideoBrief
    from ai_ops.pipeline.script_to_drama import DramaRequest, ScriptToDramaPipeline
    from ai_ops.video.base import VideoEngineBase
    from ai_ops.core.enums import VideoEngineKind

    class FakeEngine(VideoEngineBase):
        kind = VideoEngineKind.KLING

        async def render(self, brief):
            return VideoArtifact(video_path="/tmp/drama/final.mp4", duration_seconds=10, meta={})

        async def health(self):
            return True

    pipe = ScriptToDramaPipeline(engine=FakeEngine())
    plan = await pipe.plan(
        DramaRequest(
            brief=VideoBrief(theme="逆袭短剧", duration_seconds=10, resolution="1080x1920"),
            platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
            title="逆袭短剧·第一集",
            tags=["短剧"],
        )
    )
    # 不切片 → 1 整条成片 × 2 平台 = 2 items，归并成 1 份视频素材(双平台)
    arts = dist.stage_clip_plan(session, topic.id, plan, title="逆袭短剧·第一集", tags=["短剧"])
    assert len(arts) == 1
    art = arts[0]
    assert art.content_type == ContentType.VIDEO
    assert art.status == ArticleStatus.DRAFT
    assert set(art.target_platforms) == {"douyin", "xiaohongshu"}
    assert session.query(Asset).filter_by(article_id=art.id, asset_type=AssetType.VIDEO).count() == 1

    # 入库后走中台：审核 + 按账号分发
    dy = Account(platform=Platform.DOUYIN, nickname="抖音号", profile={})
    session.add(dy)
    session.flush()
    dist.approve(session, art.id)
    jobs = dist.distribute(session, art.id, account_ids=[dy.id])
    assert len(jobs) == 1 and jobs[0].platform == Platform.DOUYIN


@pytest.mark.asyncio
async def test_podcast_result_auto_stage(session, topic):
    from ai_ops.core.schemas import PodcastArtifact, PodcastBrief, PodcastSpeaker
    from ai_ops.pipeline.topic_to_podcast import TopicToPodcastPipeline
    from ai_ops.podcast.base import PodcastProviderBase
    from ai_ops.core.enums import PodcastProviderKind

    class FakeProvider(PodcastProviderBase):
        kind = PodcastProviderKind.LISTENHUB

        async def generate(self, brief):
            return PodcastArtifact(
                episode_id="ep-1", title="AI 对谈", audio_url="https://cdn/ep1.mp3",
                scripts=[{"speakerId": "v1", "content": "大家好"}], credits=5,
            )

        async def health(self):
            return True

    pipe = TopicToPodcastPipeline(provider=FakeProvider())
    res = await pipe.run(
        PodcastBrief(query="AI 内容运营", speakers=[PodcastSpeaker(speaker_id="v1")]),
        title="我的播客",
    )
    art = dist.stage_podcast_result(session, topic.id, res, target_platforms=[Platform.BILIBILI])
    assert art.content_type == ContentType.AUDIO
    assert art.status == ArticleStatus.DRAFT
    assert art.title == "我的播客"
    assert session.query(Asset).filter_by(article_id=art.id, asset_type=AssetType.AUDIO).count() == 1


def test_blog_content_stage(session, topic):
    art = dist.stage_blog_content(
        session, topic.id, title="AI 运营自动化实践", body="# 标题\n正文",
        target_platforms=[Platform.GITHUB_PAGES],
    )
    assert art.content_type == ContentType.LONG_ARTICLE
    assert art.status == ArticleStatus.DRAFT
    assert art.target_platforms == ["github_pages"]
