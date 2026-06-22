"""生成→入库一体编排 —— 一键「生成 AI 内容并落进素材库(DRAFT 待审)」。

底层逻辑：
  pipeline 只管「生成」，distributor.stage_* 只管「入库」。本模块把两者串成一步，
  让调用方一个函数就完成「生成 → 自动进待审池」，不用手动两段。
  仍是**先审后发**：产物落 DRAFT，后续走 approve → distribute → 自动排期真发。

可注入：engine / provider / driver 均可替换，便于本地用 fake 验证而不烧额度。
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.orm import Session

from ..core.enums import Platform
from . import distributor


async def generate_drama_to_library(
    session: Session,
    topic_id: int,
    drama_request,
    *,
    engine=None,
    clipper=None,
    tags: Sequence[str] = (),
):
    """短剧：ScriptToDramaPipeline 生成成片/切片 → 入库为视频素材(DRAFT)。"""
    from ..pipeline import ScriptToDramaPipeline

    plan = await ScriptToDramaPipeline(engine=engine, clipper=clipper).plan(drama_request)
    return distributor.stage_clip_plan(
        session, topic_id, plan,
        title=drama_request.title, tags=tags or drama_request.tags,
    )


async def generate_podcast_to_library(
    session: Session,
    topic_id: int,
    brief,
    *,
    provider=None,
    title: str = "",
    target_platforms: Sequence[Platform] = (),
):
    """播客：TopicToPodcastPipeline 生成 → 入库为音频素材(DRAFT)。"""
    from ..pipeline import TopicToPodcastPipeline

    res = await TopicToPodcastPipeline(provider=provider).run(brief, title=title)
    return distributor.stage_podcast_result(session, topic_id, res, target_platforms=target_platforms)


async def generate_blog_to_library(
    session: Session,
    topic_id: int,
    *,
    topic_name: str,
    keywords: Sequence[str] = (),
    persona: Optional[dict] = None,
    topic_slug: str = "",
    target_platforms: Sequence[Platform] = (),
    driver=None,
):
    """博客/长文：LLM 生成正文 → 入库为长文素材(DRAFT)。

    注意：走生成-only 路径（不调 GitHubPages 直发），统一落待审，先审后发。
    """
    from .generator import ContentGenerator

    gen = await ContentGenerator(driver=driver).generate(
        topic_name=topic_name, keywords=list(keywords),
        persona=persona or {}, platform_hint="博客", topic_slug=topic_slug,
    )
    return distributor.stage_blog_content(
        session, topic_id,
        title=gen.get("title") or topic_name,
        body=gen.get("body", ""),
        target_platforms=target_platforms,
        extra={"tags": gen.get("tags", []), "cover_hint": gen.get("cover_hint", "")},
    )
