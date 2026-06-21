"""KlingEngine 单测 —— mock httpx，本地零 key/零算力验证云集成层。

验证：
  1. JWT(HS256) header/payload/签名正确
  2. 建任务请求：endpoint、Bearer 头、payload 字段（model/prompt/duration/aspect_ratio/mode）
  3. 异步轮询：直到 task_status=succeed，解析 task_result.videos[0].url
  4. 9:16 竖屏短剧 aspect_ratio
  5. 失败任务 → RuntimeError
  6. 与 ScriptToDramaPipeline 集成：Kling 成片 → 抖音发布计划
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

import pytest

import ai_ops.video.kling as kling_mod
from ai_ops.config import settings
from ai_ops.core.enums import Platform, VideoEngineKind
from ai_ops.core.schemas import VideoBrief
from ai_ops.video.kling import KlingEngine, encode_jwt_hs256


def _b64d(x: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))


def test_jwt_hs256_structure_and_signature():
    tok = encode_jwt_hs256("AK", "SK", now=1_000_000_000)
    h, p, s = tok.split(".")
    assert _b64d(h) == {"alg": "HS256", "typ": "JWT"}
    assert _b64d(p) == {"iss": "AK", "exp": 1_000_001_800, "nbf": 999_999_995}
    expect = (
        base64.urlsafe_b64encode(
            hmac.new(b"SK", f"{h}.{p}".encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    assert s == expect


# ---------- mock httpx ----------
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """脚本化 httpx.AsyncClient：记录请求，按 method+url 返回预置响应。"""

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
def kling_env(monkeypatch):
    monkeypatch.setattr(settings, "kling_access_key", "AK")
    monkeypatch.setattr(settings, "kling_secret_key", "SK")
    monkeypatch.setattr(settings, "kling_download", False)  # 不真下载
    monkeypatch.setattr(settings, "kling_poll_interval_seconds", 0)
    monkeypatch.setattr(settings, "kling_api_base", "https://api.klingai.com")

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    FakeClient.calls = []
    FakeClient.post_resp = None
    FakeClient.get_seq = []
    yield


@pytest.mark.asyncio
async def test_render_creates_task_and_polls_to_succeed(kling_env):
    FakeClient.post_resp = _Resp({"code": 0, "data": {"task_id": "t-123"}})
    FakeClient.get_seq = [
        _Resp({"data": {"task_status": "processing"}}),
        _Resp(
            {
                "data": {
                    "task_status": "succeed",
                    "task_result": {
                        "videos": [{"url": "https://cdn.kling/x.mp4", "duration": "10"}]
                    },
                }
            }
        ),
    ]

    engine = KlingEngine()
    brief = VideoBrief(theme="逆袭短剧", script="开场白", duration_seconds=10, resolution="1080x1920")
    art = await engine.render(brief)

    assert art.video_path == "https://cdn.kling/x.mp4"  # 未下载 → 用远端 URL
    assert art.duration_seconds == 10.0
    assert art.meta["engine"] == "kling"
    assert art.meta["task_id"] == "t-123"

    # 校验建任务请求
    method, url, body, headers = FakeClient.calls[0]
    assert method == "POST"
    assert url == "https://api.klingai.com/v1/videos/text2video"
    assert headers["Authorization"].startswith("Bearer ")
    assert body["model_name"] == settings.kling_model
    assert body["prompt"] == "开场白"
    assert body["duration"] == "10"
    assert body["aspect_ratio"] == "9:16"  # 竖屏短剧
    assert body["mode"] == settings.kling_mode


@pytest.mark.asyncio
async def test_render_failed_task_raises(kling_env):
    FakeClient.post_resp = _Resp({"code": 0, "data": {"task_id": "t-x"}})
    FakeClient.get_seq = [_Resp({"data": {"task_status": "failed", "task_status_msg": "censored"}})]
    engine = KlingEngine()
    with pytest.raises(RuntimeError, match="失败"):
        await engine.render(VideoBrief(theme="x", duration_seconds=5))


@pytest.mark.asyncio
async def test_kling_drives_drama_pipeline(kling_env):
    """Kling 成片 → ScriptToDramaPipeline → 抖音发布计划（端到端 mock）。"""
    from ai_ops.pipeline import ScriptToDramaPipeline
    from ai_ops.pipeline.script_to_drama import DramaRequest

    FakeClient.post_resp = _Resp({"code": 0, "data": {"task_id": "t-9"}})
    FakeClient.get_seq = [
        _Resp(
            {
                "data": {
                    "task_status": "succeed",
                    "task_result": {"videos": [{"url": "https://cdn.kling/drama.mp4", "duration": "10"}]},
                }
            }
        )
    ]

    pipe = ScriptToDramaPipeline(engine=KlingEngine())  # 不切片，整条成片发
    plan = await pipe.plan(
        DramaRequest(
            brief=VideoBrief(theme="逆袭短剧·第一集", duration_seconds=10, resolution="1080x1920"),
            platforms=[Platform.DOUYIN],
            title="逆袭短剧·第一集",
            tags=["短剧"],
        )
    )
    assert plan.clip_count == 1
    assert len(plan.items) == 1
    assert plan.items[0].content.videos == ["https://cdn.kling/drama.mp4"]
    assert plan.meta["engine"] == VideoEngineKind.KLING.value


@pytest.mark.asyncio
async def test_health_requires_keys(monkeypatch):
    monkeypatch.setattr(settings, "kling_access_key", "")
    monkeypatch.setattr(settings, "kling_secret_key", "")
    assert await KlingEngine().health() is False
    monkeypatch.setattr(settings, "kling_access_key", "AK")
    monkeypatch.setattr(settings, "kling_secret_key", "SK")
    assert await KlingEngine().health() is True
