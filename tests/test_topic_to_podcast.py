"""ListenHub provider + TopicToPodcastPipeline 单测 —— mock httpx，本地零 key/算力。

验证：
  1. 建任务请求：endpoint、Bearer 头、payload（query/speakers/language/mode/sources）
  2. 异步轮询：直到 processStatus=success，解析 audioUrl/scripts/credits
  3. 失败任务 → RuntimeError
  4. pipeline 封装 PublishContent（AUDIO，body=文稿拼接，extra 带 audio_url）
  5. health 依赖 api key
"""
from __future__ import annotations

import asyncio

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType, PodcastProviderKind
from ai_ops.core.schemas import PodcastBrief, PodcastSpeaker
from ai_ops.podcast.listenhub import ListenHubProvider
from ai_ops.pipeline import TopicToPodcastPipeline


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    calls: list = []
    post_resp = None
    get_seq: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeClient.calls.append(("POST", url, json, headers))
        return FakeClient.post_resp

    async def get(self, url, headers=None):
        FakeClient.calls.append(("GET", url, None, headers))
        return FakeClient.get_seq.pop(0)


@pytest.fixture
def lh_env(monkeypatch):
    monkeypatch.setattr(settings, "listenhub_api_key", "LH_KEY")
    monkeypatch.setattr(settings, "listenhub_api_base", "https://api.marswave.ai/openapi")
    monkeypatch.setattr(settings, "listenhub_download", False)
    monkeypatch.setattr(settings, "listenhub_poll_initial_seconds", 0)
    monkeypatch.setattr(settings, "listenhub_poll_interval_seconds", 0)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    FakeClient.calls = []
    FakeClient.post_resp = None
    FakeClient.get_seq = []
    yield


def _brief():
    return PodcastBrief(
        query="AI 如何改变内容运营",
        speakers=[PodcastSpeaker(speaker_id="v1"), PodcastSpeaker(speaker_id="v2")],
        language="zh",
        mode="deep",
        source_urls=["https://example.com/a"],
    )


@pytest.mark.asyncio
async def test_generate_creates_and_polls(lh_env):
    FakeClient.post_resp = _Resp({"episodeId": "ep-1"})
    FakeClient.get_seq = [
        _Resp({"episodeId": "ep-1", "processStatus": "processing"}),
        _Resp(
            {
                "episodeId": "ep-1",
                "processStatus": "success",
                "title": "AI 内容运营对谈",
                "audioUrl": "https://cdn.lh/ep-1.mp3",
                "audioStreamUrl": "https://cdn.lh/ep-1.m3u8",
                "credits": 12,
                "scripts": [
                    {"speakerId": "v1", "speakerName": "主持", "content": "大家好"},
                    {"speakerId": "v2", "speakerName": "嘉宾", "content": "今天聊 AI"},
                ],
            }
        ),
    ]

    art = await ListenHubProvider().generate(_brief())
    assert art.episode_id == "ep-1"
    assert art.title == "AI 内容运营对谈"
    assert art.audio_url == "https://cdn.lh/ep-1.mp3"
    assert art.credits == 12
    assert len(art.scripts) == 2

    method, url, body, headers = FakeClient.calls[0]
    assert method == "POST"
    assert url == "https://api.marswave.ai/openapi/v1/podcast/episodes"
    assert headers["Authorization"] == "Bearer LH_KEY"
    assert body["query"] == "AI 如何改变内容运营"
    assert body["speakers"] == [{"speakerId": "v1"}, {"speakerId": "v2"}]
    assert body["mode"] == "deep"
    assert body["sources"] == [{"type": "url", "content": "https://example.com/a"}]


@pytest.mark.asyncio
async def test_generate_failed_raises(lh_env):
    FakeClient.post_resp = _Resp({"episodeId": "ep-z"})
    FakeClient.get_seq = [_Resp({"episodeId": "ep-z", "processStatus": "failed"})]
    with pytest.raises(RuntimeError, match="失败"):
        await ListenHubProvider().generate(_brief())


@pytest.mark.asyncio
async def test_pipeline_builds_publish_content(lh_env):
    FakeClient.post_resp = _Resp({"episodeId": "ep-2"})
    FakeClient.get_seq = [
        _Resp(
            {
                "episodeId": "ep-2",
                "processStatus": "success",
                "title": "标题",
                "audioUrl": "https://cdn.lh/ep-2.mp3",
                "scripts": [{"speakerId": "v1", "content": "第一段"}, {"speakerId": "v2", "content": "第二段"}],
                "credits": 5,
            }
        )
    ]

    pipe = TopicToPodcastPipeline(provider=ListenHubProvider())
    res = await pipe.run(_brief(), title="我的播客", tags=["AI", "运营"])

    assert res.artifact.episode_id == "ep-2"
    assert res.publish_content is not None
    pc = res.publish_content
    assert pc.content_type == ContentType.AUDIO
    assert pc.title == "我的播客"
    assert pc.tags == ["AI", "运营"]
    assert "第一段" in pc.body and "第二段" in pc.body  # 文稿拼接兜底
    assert pc.extra["audio_url"] == "https://cdn.lh/ep-2.mp3"
    assert res.meta["provider"] == PodcastProviderKind.LISTENHUB.value
    assert res.meta["credits"] == 5


@pytest.mark.asyncio
async def test_health_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "listenhub_api_key", "")
    assert await ListenHubProvider().health() is False
    monkeypatch.setattr(settings, "listenhub_api_key", "k")
    assert await ListenHubProvider().health() is True
