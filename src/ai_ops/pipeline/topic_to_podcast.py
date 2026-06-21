"""主题/素材 → AI 播客（ListenHub 这类云服务）→ 内容就绪的场景编排层。

底层逻辑：
  - 「AI 博客 = ListenHub 这种」= AI 播客：给主题/URL → 多音色对话音频 + 文稿。
  - 纯云（ListenHub API），本地零算力；编排层只做：拼 brief → 调 provider →
    （可选）落成 PublishContent 供下游分发（B站/小宇宙/抖音音频投流等）。

边界（与 clip_to_publish / script_to_drama 对齐，刻意为之）：
  止步于「内容就绪」——产出 PodcastArtifact（+ 可选 PublishContent），不触发真发布。
  真发布走各平台 publisher / PublishJob worker（含风控闭环）。

可注入：provider 可替换（ListenHub / 将来 AutoContentAPI / 自组装），编排层不动。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.enums import ContentType
from ..core.schemas import PodcastArtifact, PodcastBrief, PublishContent
from ..podcast.base import PodcastProviderBase


class PodcastResult(BaseModel):
    """AI 播客编排产物：成品 + 可直接喂发布的内容对象。"""
    artifact: PodcastArtifact
    publish_content: PublishContent | None = None
    meta: dict = Field(default_factory=dict)


class TopicToPodcastPipeline:
    """把「一个主题 + 可选素材」编排成「一期已生成的 AI 播客」。"""

    def __init__(self, provider: PodcastProviderBase | None = None) -> None:
        if provider is None:
            from ..podcast import build_default_podcast_provider

            provider = build_default_podcast_provider()
        self.provider = provider

    async def run(
        self,
        brief: PodcastBrief,
        *,
        title: str = "",
        tags: list[str] | None = None,
        build_publish_content: bool = True,
    ) -> PodcastResult:
        """主题 → 生成播客 →（可选）封装成 PublishContent。"""
        artifact = await self.provider.generate(brief)

        content: PublishContent | None = None
        if build_publish_content:
            content = self._to_publish_content(artifact, title=title, tags=tags or [])

        return PodcastResult(
            artifact=artifact,
            publish_content=content,
            meta={
                "provider": getattr(self.provider, "kind", None)
                and self.provider.kind.value,
                "mode": brief.mode,
                "language": brief.language,
                "speakers": len(brief.speakers),
                "credits": artifact.credits,
            },
        )

    def _to_publish_content(
        self, artifact: PodcastArtifact, *, title: str, tags: list[str]
    ) -> PublishContent:
        """PodcastArtifact → 平台无关 PublishContent（AUDIO）。

        body 缺省退回文稿拼接（让音频自带可读文案兜底，便于发图文/简介）。
        音频本地路径（若已下载）放 extra.audio_path；远端 URL 放 extra.audio_url。
        """
        body = "\n\n".join(
            s.get("content", "") for s in artifact.scripts if s.get("content")
        )
        return PublishContent(
            title=title or artifact.title,
            body=body,
            content_type=ContentType.AUDIO,
            tags=tags,
            extra={
                "source": "listenhub",
                "episode_id": artifact.episode_id,
                "audio_path": artifact.audio_path,
                "audio_url": artifact.audio_url,
                "audio_stream_url": artifact.audio_stream_url,
                "credits": artifact.credits,
            },
        )
