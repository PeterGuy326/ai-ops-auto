"""ScriptToDramaPipeline 单测 —— AI 短剧编排本地验证。

注入 fake 视频引擎 + fake clipper，不依赖真实 MoneyPrinterTurbo / FunClip：
  1. 不切片：整条成片 → 每平台一条计划（item 数 = 1 × platforms）
  2. 切片：每片 × 每平台扇出（item 数 = clips × platforms）
  3. PublishContent：content_type=VIDEO、videos 指向成片/切片路径
  4. 抖音平台正确进计划，且 SAU 发布器能据此构造 douyin upload_video
  5. 空 platforms → ValueError
  6. meta 里 engine/sliced/duration 等可观测字段齐备
"""
from __future__ import annotations

import pytest

from ai_ops.core.enums import ContentType, Platform, VideoClipperKind, VideoEngineKind
from ai_ops.core.schemas import (
    ClipArtifact,
    ClipRequest,
    ClipResult,
    ClipSegment,
    VideoArtifact,
    VideoBrief,
)
from ai_ops.pipeline.script_to_drama import DramaRequest, ScriptToDramaPipeline
from ai_ops.publishers.social_auto_upload import SAU_HTTP_TYPE_MAP, SocialAutoUploadPublisher
from ai_ops.video.base import VideoEngineBase
from ai_ops.video.clipper_base import VideoClipperBase


class FakeEngine(VideoEngineBase):
    """受控视频引擎：render() 返回构造好的成片。"""

    kind = VideoEngineKind.MONEY_PRINTER_TURBO

    def __init__(self, video_path="/tmp/drama/final.mp4", duration=58.0):
        self._path = video_path
        self._dur = duration

    async def render(self, brief: VideoBrief) -> VideoArtifact:
        return VideoArtifact(
            video_path=self._path,
            duration_seconds=self._dur,
            meta={"engine": "fake", "theme": brief.theme},
        )

    async def health(self) -> bool:
        return True


class FakeClipper(VideoClipperBase):
    kind = VideoClipperKind.FUNCLIP

    def __init__(self, n=3):
        self._n = n

    async def transcribe(self, input_video, output_dir, lang="zh"):
        raise NotImplementedError

    async def health(self) -> bool:
        return True

    async def clip(self, request: ClipRequest) -> ClipResult:
        clips = [
            ClipArtifact(
                video_path=f"/tmp/drama/clip_{i:03d}.mp4",
                dest_text=f"高能片段 {i}",
                start_ms=i * 1000,
                end_ms=i * 1000 + 8000,
                meta={"seg_index": i},
            )
            for i in range(1, self._n + 1)
        ]
        return ClipResult(clips=clips, transcript=None, meta={"faked": True})


def _brief():
    return VideoBrief(theme="逆袭短剧·第一集", keywords=["短剧", "爽文"], duration_seconds=58)


@pytest.mark.asyncio
async def test_drama_whole_take_no_slice():
    """不给 segments：整条成片 → 每平台一条计划。"""
    pipe = ScriptToDramaPipeline(engine=FakeEngine())
    plan = await pipe.plan(
        DramaRequest(
            brief=_brief(),
            platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
            title="逆袭短剧·第一集",
            tags=["短剧", "爽文"],
        )
    )
    assert plan.clip_count == 1
    assert len(plan.items) == 2  # 1 整条 × 2 平台
    item = plan.items[0]
    assert item.content.content_type == ContentType.VIDEO
    assert item.content.videos == ["/tmp/drama/final.mp4"]
    assert plan.meta["sliced"] is False
    assert plan.meta["duration_seconds"] == 58.0
    assert plan.meta["engine"] == VideoEngineKind.MONEY_PRINTER_TURBO.value


@pytest.mark.asyncio
async def test_drama_sliced_fanout():
    """给 segments：每片 × 每平台扇出。"""
    pipe = ScriptToDramaPipeline(engine=FakeEngine(), clipper=FakeClipper(n=3))
    plan = await pipe.plan(
        DramaRequest(
            brief=_brief(),
            platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
            title="逆袭短剧·切片",
            clip_segments=[
                ClipSegment(dest_text="高能片段 1"),
                ClipSegment(dest_text="高能片段 2"),
                ClipSegment(dest_text="高能片段 3"),
            ],
        )
    )
    assert plan.clip_count == 3
    assert len(plan.items) == 6  # 3 切片 × 2 平台
    assert plan.meta["sliced"] is True
    # body 缺省退回切片 dest_text
    douyin_items = [i for i in plan.items if i.platform == Platform.DOUYIN]
    assert len(douyin_items) == 3
    assert douyin_items[0].content.body == "高能片段 1"


@pytest.mark.asyncio
async def test_drama_douyin_publisher_command_buildable():
    """抖音进计划后，SAU 发布器能据 content 构造 douyin upload_video。"""
    pipe = ScriptToDramaPipeline(engine=FakeEngine())
    plan = await pipe.plan(
        DramaRequest(brief=_brief(), platforms=[Platform.DOUYIN], title="抖音短剧")
    )
    sau = SocialAutoUploadPublisher(Platform.DOUYIN)
    assert sau.sau_platform == "douyin"
    assert SAU_HTTP_TYPE_MAP[Platform.DOUYIN] == 3
    item = plan.items[0]
    is_video = bool(item.content.videos) or item.content.content_type == ContentType.VIDEO
    assert is_video is True  # → action 会是 upload_video


@pytest.mark.asyncio
async def test_drama_empty_platforms_raises():
    pipe = ScriptToDramaPipeline(engine=FakeEngine())
    with pytest.raises(ValueError):
        await pipe.plan(DramaRequest(brief=_brief(), platforms=[], title="x"))
