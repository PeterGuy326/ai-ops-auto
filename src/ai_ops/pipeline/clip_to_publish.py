"""长视频 → FunClip 切片 → 多平台发布计划的编排层。

边界（刻意为之）：
  本流水线只编排到「内容就绪」——产出一组 (Platform, PublishContent) 发布计划。
  它**不触发真发布**。真发布走 PublishJob + scheduler.worker，那条路上有 rate
  limit、风控间隔、pre_publish_check、metrics 闭环——pipeline 直接调
  publisher.publish 会把这些全绕过，所以这里止步于 dry-run 计划。

  下游消费方式：拿 ClipPublishPlan.items，按既有流程为每个 item 建 Article +
  PublishJob 入库，worker 自然会发。
"""
from __future__ import annotations

from ..core.enums import ContentType, Platform
from ..core.schemas import (
    ClipArtifact,
    ClipPublishPlan,
    ClipPublishRequest,
    PublishContent,
    PublishPlanItem,
)
from ..video.clipper_base import VideoClipperBase


class ClipToPublishPipeline:
    """把 VideoClipper 的切片产物编排成多平台发布计划。"""

    def __init__(self, clipper: VideoClipperBase | None = None) -> None:
        # 默认用 FunClip；注入自定义 clipper 便于测试 / 换实现
        if clipper is None:
            from ..video.clipper import FunClipClipper

            clipper = FunClipClipper()
        self.clipper = clipper

    async def plan(self, request: ClipPublishRequest) -> ClipPublishPlan:
        """切片 → 每切片 × 每平台 生成一条发布计划。

        故意不发布——产出 ClipPublishPlan 供调用方预览 / 落 Job。
        """
        if not request.platforms:
            raise ValueError("ClipPublishRequest.platforms must not be empty")

        clip_result = await self.clipper.clip(request.clip_request)

        items: list[PublishPlanItem] = []
        for clip in clip_result.clips:
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
            clip_count=len(clip_result.clips),
            meta={
                "platforms": [p.value for p in request.platforms],
                "transcribed": clip_result.transcript is not None,
                "clip_meta": clip_result.meta,
            },
        )

    def _to_publish_content(
        self, clip: ClipArtifact, request: ClipPublishRequest
    ) -> PublishContent:
        """ClipArtifact → 平台无关的 PublishContent。

        body 缺省退回切片对应的转写文字（dest_text），让短视频自带文案兜底。
        """
        return PublishContent(
            title=request.title,
            body=request.body or (clip.dest_text or ""),
            content_type=ContentType.VIDEO,
            videos=[clip.video_path],
            tags=list(request.tags),
            extra={
                "source": "funclip",
                "dest_text": clip.dest_text,
                "clip_start_ms": clip.start_ms,
                "clip_end_ms": clip.end_ms,
            },
        )
