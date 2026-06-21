"""HappyHorseEngine 单测 —— mock httpx，验证 idealab 视频任务异步契约（实测确认）。

真实契约：
  建任务  POST {jobs_url}          {model, extendParams:{input:{prompt}, parameters:{...}}} → {id, status:running}
  轮询    POST {jobs_url}/{id}     {model} → 进行中 {status:running}；完成 {generations:[{id,url}]}

验证：
  1. 建任务 endpoint/Bearer/payload（model + extendParams.input.prompt + parameters）
  2. 竖屏 ratio=9:16；duration 钳到 3-15
  3. 轮询 POST /jobs/{id}，从 generations[0].url 取成片
  4. 失败 status → RuntimeError
  5. 接进 ScriptToDramaPipeline → 抖音发布计划
"""
from __future__ import annotations

import asyncio

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType, Platform
from ai_ops.core.schemas import VideoBrief
from ai_ops.video.happyhorse import HappyHorseEngine


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    calls: list = []
    post_seq: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.calls.append(("POST", url, headers, json))
        return FakeClient.post_seq.pop(0)

    async def get(self, url, headers=None):
        FakeClient.calls.append(("GET", url, headers, None))
        return FakeClient.post_seq.pop(0)


@pytest.fixture
def hh_env(monkeypatch):
    monkeypatch.setattr(settings, "wukong_api_key", "WK")
    monkeypatch.setattr(settings, "wukong_video_model", "happyhorse-1.0-t2v")
    monkeypatch.setattr(settings, "wukong_video_jobs_url", "https://gw/api/openai/v1/video/generations/jobs")
    monkeypatch.setattr(settings, "wukong_download", False)
    monkeypatch.setattr(settings, "wukong_poll_interval_seconds", 0)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    FakeClient.calls = []
    FakeClient.post_seq = []
    yield


@pytest.mark.asyncio
async def test_render_jobs_contract(hh_env):
    FakeClient.post_seq = [
        _Resp({"id": "job-1", "status": "running", "object": "video.generation.job"}),
        _Resp({"id": "job-1", "status": "running"}),  # 轮询1：进行中
        _Resp({"generations": [{"id": "g1", "url": "https://cdn/hh.mp4"}]}),  # 轮询2：完成
    ]
    art = await HappyHorseEngine().render(
        VideoBrief(theme="逆袭短剧", script="开场打脸", duration_seconds=20, resolution="1080x1920")
    )
    assert art.video_path == "https://cdn/hh.mp4"
    assert art.meta["engine"] == "happyhorse"
    assert art.meta["job_id"] == "job-1"

    # 建任务请求
    m, url, headers, body = FakeClient.calls[0]
    assert m == "POST"
    assert url == settings.wukong_video_jobs_url
    assert headers["Authorization"] == "Bearer WK"
    assert body["model"] == "happyhorse-1.0-t2v"
    assert body["extendParams"]["input"]["prompt"] == "开场打脸"
    assert body["extendParams"]["parameters"]["ratio"] == "9:16"   # 竖屏
    assert body["extendParams"]["parameters"]["duration"] == 15    # 20→15 钳制
    # 轮询命中 POST /jobs/{id}
    assert FakeClient.calls[-1][:2] == ("POST", "https://gw/api/openai/v1/video/generations/jobs/job-1")


@pytest.mark.asyncio
async def test_render_failed(hh_env):
    FakeClient.post_seq = [
        _Resp({"id": "job-x", "status": "running"}),
        _Resp({"status": "failed", "message": "censored"}),
    ]
    with pytest.raises(RuntimeError, match="失败"):
        await HappyHorseEngine().render(VideoBrief(theme="x", duration_seconds=3))


@pytest.mark.asyncio
async def test_happyhorse_drives_drama_pipeline(hh_env):
    from ai_ops.pipeline import ScriptToDramaPipeline
    from ai_ops.pipeline.script_to_drama import DramaRequest

    FakeClient.post_seq = [
        _Resp({"id": "job-9", "status": "running"}),
        _Resp({"generations": [{"id": "g", "url": "https://cdn/drama.mp4"}]}),
    ]
    pipe = ScriptToDramaPipeline(engine=HappyHorseEngine())
    plan = await pipe.plan(
        DramaRequest(
            brief=VideoBrief(theme="逆袭短剧·第一集", script="...", duration_seconds=10, resolution="1080x1920"),
            platforms=[Platform.DOUYIN],
            title="逆袭短剧·第一集",
            tags=["短剧"],
        )
    )
    assert plan.clip_count == 1
    assert plan.items[0].content.videos == ["https://cdn/drama.mp4"]
    assert plan.items[0].content.content_type == ContentType.VIDEO


@pytest.mark.asyncio
async def test_health_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "wukong_api_key", "")
    assert await HappyHorseEngine().health() is False
    monkeypatch.setattr(settings, "wukong_api_key", "k")
    assert await HappyHorseEngine().health() is True
