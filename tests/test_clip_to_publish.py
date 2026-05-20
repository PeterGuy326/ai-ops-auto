"""ClipToPublishPipeline 单测 —— 注入 fake clipper，不依赖真实 FunClip。

测试目标：
  1. plan() 产出 item 数 = clips × platforms
  2. PublishContent：content_type=VIDEO、videos 指向切片路径、title/tags 透传
  3. body 缺省时退回切片 dest_text
  4. source_clip_path 溯源正确
  5. 空 platforms → ValueError
  6. 空 clips → 空计划（不报错）
"""
from __future__ import annotations

import pytest

from ai_ops.core.enums import ContentType, Platform, VideoClipperKind
from ai_ops.core.schemas import (
    ClipArtifact,
    ClipPublishRequest,
    ClipRequest,
    ClipResult,
    ClipSegment,
    TranscriptResult,
)
from ai_ops.pipeline import ClipToPublishPipeline
from ai_ops.video.clipper_base import VideoClipperBase


class FakeClipper(VideoClipperBase):
    """受控 clipper：clip() 直接返回构造好的 ClipResult。"""

    kind = VideoClipperKind.FUNCLIP

    def __init__(self, clips: list[ClipArtifact], with_transcript: bool = False) -> None:
        self._clips = clips
        self._with_transcript = with_transcript

    async def transcribe(self, input_video, output_dir, lang="zh"):  # noqa: D102
        return TranscriptResult(srt_path="/fake.srt")

    async def clip(self, request: ClipRequest) -> ClipResult:  # noqa: D102
        return ClipResult(
            clips=self._clips,
            transcript=TranscriptResult(srt_path="/fake.srt") if self._with_transcript else None,
            meta={"run_dir": "/fake/run"},
        )

    async def health(self) -> bool:  # noqa: D102
        return True


def _clip(path: str, text: str) -> ClipArtifact:
    return ClipArtifact(video_path=path, dest_text=text, start_ms=0, end_ms=1000)


def _request(platforms, title="片段标题", body="", tags=None) -> ClipPublishRequest:
    return ClipPublishRequest(
        clip_request=ClipRequest(
            input_video="/in.mp4",
            segments=[ClipSegment(dest_text="x")],
        ),
        platforms=platforms,
        title=title,
        body=body,
        tags=tags or [],
    )


@pytest.mark.asyncio
async def test_plan_item_count_is_clips_times_platforms():
    clipper = FakeClipper([_clip("/c1.mp4", "一"), _clip("/c2.mp4", "二")])
    pipe = ClipToPublishPipeline(clipper=clipper)
    plan = await pipe.plan(_request([Platform.DOUYIN, Platform.XIAOHONGSHU, Platform.BILIBILI]))
    assert plan.clip_count == 2
    assert len(plan.items) == 2 * 3


@pytest.mark.asyncio
async def test_publish_content_is_video_with_clip_path():
    clipper = FakeClipper([_clip("/clips/c1.mp4", "开场白")])
    pipe = ClipToPublishPipeline(clipper=clipper)
    plan = await pipe.plan(_request([Platform.DOUYIN], title="标题A", tags=["热点", "AI"]))
    item = plan.items[0]
    assert item.platform == Platform.DOUYIN
    assert item.content.content_type == ContentType.VIDEO
    assert item.content.videos == ["/clips/c1.mp4"]
    assert item.content.title == "标题A"
    assert item.content.tags == ["热点", "AI"]
    assert item.source_clip_path == "/clips/c1.mp4"
    assert item.content.extra["source"] == "funclip"
    assert item.content.extra["dest_text"] == "开场白"


@pytest.mark.asyncio
async def test_body_falls_back_to_dest_text():
    clipper = FakeClipper([_clip("/c1.mp4", "这段是切片转写文字")])
    pipe = ClipToPublishPipeline(clipper=clipper)
    # body 留空 → 退回 dest_text
    plan = await pipe.plan(_request([Platform.DOUYIN], body=""))
    assert plan.items[0].content.body == "这段是切片转写文字"
    # body 显式给值 → 用显式值
    plan2 = await pipe.plan(_request([Platform.DOUYIN], body="手写文案"))
    assert plan2.items[0].content.body == "手写文案"


@pytest.mark.asyncio
async def test_empty_platforms_raises():
    pipe = ClipToPublishPipeline(clipper=FakeClipper([_clip("/c1.mp4", "一")]))
    with pytest.raises(ValueError, match="platforms must not be empty"):
        await pipe.plan(_request([]))


@pytest.mark.asyncio
async def test_no_clips_yields_empty_plan():
    pipe = ClipToPublishPipeline(clipper=FakeClipper([]))
    plan = await pipe.plan(_request([Platform.DOUYIN]))
    assert plan.clip_count == 0
    assert plan.items == []


@pytest.mark.asyncio
async def test_plan_meta_records_platforms_and_transcript_flag():
    clipper = FakeClipper([_clip("/c1.mp4", "一")], with_transcript=True)
    pipe = ClipToPublishPipeline(clipper=clipper)
    plan = await pipe.plan(_request([Platform.DOUYIN, Platform.BILIBILI]))
    assert plan.meta["platforms"] == ["douyin", "bilibili"]
    assert plan.meta["transcribed"] is True
