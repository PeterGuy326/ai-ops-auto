"""生成→入库一体编排单测（fake 引擎/provider/driver，不烧额度）。

  1. 短剧：generate_drama_to_library → 视频素材入库(DRAFT)
  2. 播客：generate_podcast_to_library → 音频素材入库(DRAFT)
  3. 博客：generate_blog_to_library → 长文素材入库(DRAFT)
  4. 入库后可直接走中台：approve → distribute
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.content import orchestrator
from ai_ops.core.enums import ArticleStatus, ContentType, Platform
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
async def test_generate_drama_to_library(session, topic):
    from ai_ops.core.enums import VideoEngineKind
    from ai_ops.core.schemas import VideoArtifact, VideoBrief
    from ai_ops.pipeline.script_to_drama import DramaRequest
    from ai_ops.video.base import VideoEngineBase

    class FakeEngine(VideoEngineBase):
        kind = VideoEngineKind.KLING

        async def render(self, brief):
            return VideoArtifact(video_path="/tmp/final.mp4", duration_seconds=10, meta={})

        async def health(self):
            return True

    req = DramaRequest(
        brief=VideoBrief(theme="逆袭短剧", duration_seconds=10, resolution="1080x1920"),
        platforms=[Platform.DOUYIN], title="逆袭短剧·第一集", tags=["短剧"],
    )
    arts = await orchestrator.generate_drama_to_library(session, topic.id, req, engine=FakeEngine())
    assert len(arts) == 1
    assert arts[0].content_type == ContentType.VIDEO and arts[0].status == ArticleStatus.DRAFT
    assert session.query(Asset).filter_by(article_id=arts[0].id).count() == 1


@pytest.mark.asyncio
async def test_generate_podcast_to_library(session, topic):
    from ai_ops.core.enums import PodcastProviderKind
    from ai_ops.core.schemas import PodcastArtifact, PodcastBrief, PodcastSpeaker
    from ai_ops.podcast.base import PodcastProviderBase

    class FakeProvider(PodcastProviderBase):
        kind = PodcastProviderKind.LISTENHUB

        async def generate(self, brief):
            return PodcastArtifact(episode_id="ep1", title="对谈", audio_url="https://cdn/a.mp3",
                                   scripts=[{"speakerId": "v1", "content": "hi"}], credits=3)

        async def health(self):
            return True

    art = await orchestrator.generate_podcast_to_library(
        session, topic.id, PodcastBrief(query="AI 运营", speakers=[PodcastSpeaker(speaker_id="v1")]),
        provider=FakeProvider(), title="我的播客", target_platforms=[Platform.BILIBILI],
    )
    assert art.content_type == ContentType.AUDIO and art.status == ArticleStatus.DRAFT
    assert session.query(Asset).filter_by(article_id=art.id).count() == 1


@pytest.mark.asyncio
async def test_generate_blog_to_library_then_distribute(session, topic):
    from ai_ops.content.generator import LLMDriver

    class FakeLLM(LLMDriver):
        async def complete(self, system, user, **kwargs):
            return "# AI 运营自动化\n\n正文内容。"

    art = await orchestrator.generate_blog_to_library(
        session, topic.id, topic_name="AI 运营自动化实践",
        keywords=["AI运营"], target_platforms=[Platform.GITHUB_PAGES], driver=FakeLLM(),
    )
    assert art.content_type == ContentType.LONG_ARTICLE and art.status == ArticleStatus.DRAFT
    assert art.body and art.title == "AI 运营自动化实践"

    # 入库后直接走中台
    from ai_ops.content import distributor
    gh = Account(platform=Platform.GITHUB_PAGES, nickname="博客", profile={})
    session.add(gh)
    session.flush()
    distributor.approve(session, art.id)
    jobs = distributor.distribute(session, art.id, account_ids=[gh.id])
    assert len(jobs) == 1
