"""剧本/主题 → AI 短剧成片 → 切片 → 多平台发布计划的场景编排层。

底层逻辑：
  - 短剧链路 = 「生成一个视频」+「（可选）切成多个短片段」+「扇出多平台」。
  - 视频引擎是**可插拔边界**（吃 VideoEngineBase）：
      · 轻短剧（口播/资讯剧情）→ MoneyPrinterTurbo（已集成）
      · 真剧情（分镜/角色）   → 将来插文生图/图生视频引擎，编排层不动
  - 切片是**可选**：长成片要拆多条投流时才切；短成片直接整条发。

边界（与 clip_to_publish 对齐，刻意为之）：
  本流水线只编排到「内容就绪」——产出 ClipPublishPlan（一组 PublishPlanItem）。
  **不触发真发布**。真发布走 PublishJob + worker，那条路上有 rate-limit / 风控
  间隔 / metrics 闭环；pipeline 直发会绕过这些。

可注入：engine / clipper 均可替换，便于本地用 fake 引擎 + fake clipper 验证编排。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.enums import ContentType, Platform
from ..core.schemas import (
    ClipArtifact,
    ClipPublishPlan,
    ClipRequest,
    ClipSegment,
    PublishContent,
    PublishPlanItem,
    VideoArtifact,
    VideoBrief,
)
from ..video.base import VideoEngineBase
from ..video.clipper_base import VideoClipperBase


class DramaRequest(BaseModel):
    """AI 短剧编排入参。"""
    brief: VideoBrief                      # 喂给视频引擎：theme/script/keywords/voice/...
    platforms: list[Platform]              # 目标平台（抖音/小红书/...）
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    # 切片配置：给了 segments 就切，否则整条成片直接发
    clip_segments: list[ClipSegment] = Field(default_factory=list)
    clip_output_dir: str = "./data/clips"
    lang: str = "zh"


class ScriptToDramaPipeline:
    """脚本/主题 → 短剧成片 →（可选切片）→ 多平台发布计划。"""

    def __init__(
        self,
        engine: VideoEngineBase | None = None,
        clipper: VideoClipperBase | None = None,
    ) -> None:
        # 引擎默认按配置选（配了可灵 key → Kling 云引擎，否则本地 MPT 轻短剧）；
        # clipper 默认 FunClip，仅在需要切片时实例化
        if engine is None:
            from ..video import build_default_video_engine

            engine = build_default_video_engine()
        self.engine = engine
        self._clipper = clipper  # 懒加载：不切片就不碰 FunClip

    def _get_clipper(self) -> VideoClipperBase:
        if self._clipper is None:
            from ..video.clipper import FunClipClipper

            self._clipper = FunClipClipper()
        return self._clipper

    async def plan(self, request: DramaRequest) -> ClipPublishPlan:
        """生成成片 →（可选切片）→ 每片 × 每平台 一条发布计划。

        故意不发布——产出 ClipPublishPlan 供调用方预览 / 落 Job。
        """
        if not request.platforms:
            raise ValueError("DramaRequest.platforms must not be empty")

        # 1. 视频引擎生成短剧成片
        artifact: VideoArtifact = await self.engine.render(request.brief)

        # 2. 切片（可选）：给了 segments 才切；否则整条成片作为唯一「切片」
        clips: list[ClipArtifact]
        transcribed = False
        if request.clip_segments:
            clip_req = ClipRequest(
                input_video=artifact.video_path,
                segments=request.clip_segments,
                output_dir=request.clip_output_dir,
                lang=request.lang,
            )
            clip_result = await self._get_clipper().clip(clip_req)
            clips = clip_result.clips
            transcribed = clip_result.transcript is not None
        else:
            clips = [
                ClipArtifact(
                    video_path=artifact.video_path,
                    dest_text=None,
                    start_ms=0,
                    end_ms=int(artifact.duration_seconds * 1000),
                    meta={"whole_take": True, **artifact.meta},
                )
            ]

        # 3. 每片 × 每平台 扇出发布计划
        items: list[PublishPlanItem] = []
        for clip in clips:
            for platform in request.platforms:
                items.append(
                    PublishPlanItem(
                        platform=platform,
                        content=self._to_publish_content(clip, request),
                        source_clip_path=clip.video_path,
                    )
                )

        return ClipPublishPlan(
            items=items,
            clip_count=len(clips),
            meta={
                "platforms": [p.value for p in request.platforms],
                "engine": getattr(self.engine, "kind", None)
                and self.engine.kind.value,
                "sliced": bool(request.clip_segments),
                "transcribed": transcribed,
                "video_artifact": artifact.meta,
                "duration_seconds": artifact.duration_seconds,
            },
        )

    def _to_publish_content(
        self, clip: ClipArtifact, request: DramaRequest
    ) -> PublishContent:
        """ClipArtifact → 平台无关 PublishContent（VIDEO）。

        body 缺省退回切片转写文字（dest_text），让短视频自带文案兜底。
        """
        return PublishContent(
            title=request.title,
            body=request.body or (clip.dest_text or ""),
            content_type=ContentType.VIDEO,
            videos=[clip.video_path],
            tags=list(request.tags),
            extra={
                "source": "ai_drama",
                "dest_text": clip.dest_text,
                "clip_start_ms": clip.start_ms,
                "clip_end_ms": clip.end_ms,
            },
        )
