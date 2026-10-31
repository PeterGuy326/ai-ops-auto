"""WechatMpApiPublisher 单测 — httpx.MockTransport 全 mock，不依赖真账号/真网络。

覆盖失败语义契约（与 zhihu_cli 同一纪律）：
  1. token 阶段失败 → 无写副作用
  2. 封面/草稿确定性 errcode → 确定失败，可安全终止
  3. draft 成功 + freepublish 明确拒绝 → effect_applied=True（草稿已存在），不 fallback
  4. 请求已发出但响应未知（超时）→ outcome_uncertain=True，禁止自动重试
  5. happy path：publish_id + 有界回查 article_url
  6. appid/secret/access_token 永不出现在 raw_response
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers.wechat_mp_api import WechatMpApiPublisher


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """发布是异步的；单测把有界轮询压到最小，避免真实 sleep。"""
    monkeypatch.setattr(settings, "wechat_mp_api_publish_poll_attempts", 1)
    monkeypatch.setattr(settings, "wechat_mp_api_publish_poll_interval_seconds", 0.5)


CREDENTIAL = {"appid": "wx-app-id", "secret": "super-secret-value"}
TOKEN = "test-access-token-do-not-leak"


def _content(**overrides) -> PublishContent:
    base = dict(
        title="测试文章标题",
        body="正文 **加粗**",
        content_type=ContentType.IMAGE_TEXT,
        images=[],
    )
    base.update(overrides)
    return PublishContent(**base)


def _jpg(tmp_path) -> str:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return str(cover)


class _Router:
    """按路径分发 mock 响应；记录请求体供断言（注意断言时脱敏）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, httpx.Request]] = []
        self.handlers: dict[str, object] = {}

    def on(self, path: str, handler) -> "_Router":
        self.handlers[path] = handler
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((path, request))
        handler = self.handlers.get(path)
        if handler is None:
            return httpx.Response(404, json={"errcode": -1, "errmsg": "unrouted"})
        if isinstance(handler, Exception):
            raise handler
        return handler(request)


def _token_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 7200})


def _material_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"media_id": "COVER_MEDIA_ID", "url": "https://mmbiz/x"})


def _draft_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"media_id": "DRAFT_MEDIA_ID"})


def _submit_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"publish_id": "PUBLISH_ID_1", "errcode": 0})


def _status_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "publish_status": [
                {
                    "article_id": "ARTICLE_ID_1",
                    "article_url": "https://mp.weixin.qq.com/s/abc123",
                    "article_detail": {},
                }
            ]
        },
    )


def _make_publisher(router: _Router) -> WechatMpApiPublisher:
    return WechatMpApiPublisher(transport=httpx.MockTransport(router))


def test_metadata():
    pub = _make_publisher(_Router())
    assert pub.platform == Platform.WECHAT_MP
    assert pub.kind == PublisherKind.WECHAT_MP_API


# ----- 入口校验（无任何网络调用）-----


def test_exact_approval_fails_closed():
    pub = _make_publisher(_Router())
    content = _content(exact_approval=True)
    result = asyncio.run(pub.publish(1, CREDENTIAL, content))
    assert result.success is False
    assert "exact renderer" in (result.error or "")


def test_missing_cover_fails_closed():
    pub = _make_publisher(_Router())
    result = asyncio.run(pub.publish(1, CREDENTIAL, _content(images=[])))
    assert result.success is False
    assert "封面" in (result.error or "")


def test_multiple_images_rejected():
    pub = _make_publisher(_Router())
    result = asyncio.run(pub.publish(1, CREDENTIAL, _content(images=["a.jpg", "b.jpg"])))
    assert result.success is False
    assert "仅支持封面图 1 张" in (result.error or "")


def test_title_too_long_rejected():
    pub = _make_publisher(_Router())
    result = asyncio.run(pub.publish(1, CREDENTIAL, _content(title="长" * 65)))
    assert result.success is False
    assert "64" in (result.error or "")


def test_unknown_extra_key_rejected():
    pub = _make_publisher(_Router())
    result = asyncio.run(
        pub.publish(1, CREDENTIAL, _content(extra={"evil_key": "x"}))
    )
    assert result.success is False
    assert "evil_key" in (result.error or "")


# ----- token 阶段 -----


def test_token_error_no_write_side_effect(tmp_path):
    router = _Router().on("/cgi-bin/stable_token", lambda r: httpx.Response(200, json={"errcode": 40001}))
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.effect_applied is False
    assert result.raw_response["stage"] == "token"
    # token 失败时不得发起任何写路径请求
    assert [p for p, _ in router.calls] == ["/cgi-bin/stable_token"]


def test_missing_credential_fails_closed(tmp_path):
    pub = _make_publisher(_Router())
    result = asyncio.run(pub.publish(1, {}, _content(images=[_jpg(tmp_path)])))
    assert result.success is False
    assert "appid" in (result.error or "")


# ----- 封面 / 草稿阶段 -----


def test_cover_upload_error_is_deterministic_failure(tmp_path):
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", lambda r: httpx.Response(200, json={"errcode": 40007}))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert "errcode 40007" in (result.error or "")


def test_draft_error_is_deterministic_failure(tmp_path):
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", lambda r: httpx.Response(200, json={"errcode": 40058}))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.effect_applied is False
    assert result.retryable is False
    assert "errcode 40058" in (result.error or "")


def test_draft_timeout_is_unknown_and_no_retry(tmp_path):
    """draft/add 已发出但超时：服务端可能已落草稿 → outcome_uncertain。"""
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", httpx.ConnectTimeout("boom"))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["write_started"] is True


# ----- 发布阶段 -----


def test_publish_rejected_after_draft_keeps_draft_evidence(tmp_path):
    """草稿已建成、发布被明确拒绝：effect_applied=True，保留 draft id 供清理。"""
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", _draft_ok)
        .on("/cgi-bin/freepublish/submit", lambda r: httpx.Response(200, json={"errcode": 200013}))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.effect_applied is True
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert result.raw_response["draft_media_id"] == "DRAFT_MEDIA_ID"
    assert result.raw_response["publish_errcode"] == 200013


def test_publish_submit_timeout_is_uncertain(tmp_path):
    """发布提交已发出但结果未知：草稿确定存在，禁止 fallback/自动重试。"""
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", _draft_ok)
        .on("/cgi-bin/freepublish/submit", httpx.ReadTimeout("boom"))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is False
    assert result.effect_applied is True
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert "核验" in (result.error or "")


def test_happy_path_publish_with_article_url(tmp_path):
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", _draft_ok)
        .on("/cgi-bin/freepublish/submit", _submit_ok)
        .on("/cgi-bin/freepublish/get", _status_ok)
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is True
    assert result.platform_post_id == "ARTICLE_ID_1"
    assert result.platform_url == "https://mp.weixin.qq.com/s/abc123"
    assert result.raw_response["outcome"] == "published"
    # 草稿请求体不包含本地路径
    draft_calls = [r for p, r in router.calls if p == "/cgi-bin/draft/add"]
    assert len(draft_calls) == 1
    body = json.loads(draft_calls[0].content)
    assert body["articles"][0]["title"] == "测试文章标题"
    assert str(tmp_path) not in json.dumps(body)


def test_publish_pending_still_success_with_publish_id(tmp_path):
    """发布受理但 URL 未生成：SUCCESS + publish_id，error 说明 pending。"""
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", _draft_ok)
        .on("/cgi-bin/freepublish/submit", _submit_ok)
        .on("/cgi-bin/freepublish/get", lambda r: httpx.Response(200, json={"publish_status": []}))
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    assert result.success is True
    assert result.platform_post_id == "PUBLISH_ID_1"
    assert result.platform_url is None
    assert result.raw_response["outcome"] == "publish_pending"


# ----- 凭证不泄露 -----


@pytest.mark.parametrize(
    "submit_handler",
    [_submit_ok, lambda r: httpx.Response(200, json={"errcode": 200013}), httpx.ReadTimeout("boom")],
    ids=["ok", "rejected", "timeout"],
)
def test_secrets_never_reach_raw_response(tmp_path, submit_handler):
    router = (
        _Router()
        .on("/cgi-bin/stable_token", _token_ok)
        .on("/cgi-bin/material/add_material", _material_ok)
        .on("/cgi-bin/draft/add", _draft_ok)
        .on("/cgi-bin/freepublish/submit", submit_handler)
    )
    result = asyncio.run(
        _make_publisher(router).publish(1, CREDENTIAL, _content(images=[_jpg(tmp_path)]))
    )
    dumped = json.dumps(result.raw_response, ensure_ascii=False)
    assert "super-secret-value" not in dumped
    assert "wx-app-id" not in dumped
    assert TOKEN not in dumped


# ----- login / health -----


def test_login_success_and_failure():
    pub = _make_publisher(_Router().on("/cgi-bin/stable_token", _token_ok))
    assert asyncio.run(pub.login(1, CREDENTIAL)) is True
    assert pub.last_login_error is None

    failing = _make_publisher(
        _Router().on("/cgi-bin/stable_token", lambda r: httpx.Response(200, json={"errcode": 40125}))
    )
    assert asyncio.run(failing.login(1, CREDENTIAL)) is False
    assert failing.last_login_error


def test_login_ip_allowlist_hint():
    pub = _make_publisher(
        _Router().on("/cgi-bin/stable_token", lambda r: httpx.Response(200, json={"errcode": 40164}))
    )
    assert asyncio.run(pub.login(1, CREDENTIAL)) is False
    assert "IP 白名单" in (pub.last_login_error or "")


def test_health_check_mapping():
    healthy = _make_publisher(_Router().on("/cgi-bin/stable_token", _token_ok))
    assert asyncio.run(healthy.health_check(1, CREDENTIAL)) == AccountHealth.HEALTHY

    expired = _make_publisher(
        _Router().on("/cgi-bin/stable_token", lambda r: httpx.Response(200, json={"errcode": 40001}))
    )
    assert asyncio.run(expired.health_check(1, CREDENTIAL)) == AccountHealth.EXPIRED

    unreachable = _make_publisher(
        _Router().on("/cgi-bin/stable_token", httpx.ConnectError("network down"))
    )
    assert asyncio.run(unreachable.health_check(1, CREDENTIAL)) == AccountHealth.UNKNOWN

    assert asyncio.run(healthy.health_check(1, {})) == AccountHealth.EXPIRED


# ----- registry 装配 -----


def test_registry_gate(monkeypatch):
    from ai_ops.core.enums import Platform as P
    from ai_ops.publishers.registry import build_default_registry

    monkeypatch.setattr(settings, "wechat_mp_api_publisher_enabled", False)
    registry_off = build_default_registry()
    kinds_off = [
        registry_off.kind_value(p) for p in registry_off.resolve(P.WECHAT_MP)
    ]
    assert "wechat_mp_api" not in kinds_off

    monkeypatch.setattr(settings, "wechat_mp_api_publisher_enabled", True)
    registry_on = build_default_registry()
    resolved = registry_on.resolve(P.WECHAT_MP)
    assert registry_on.kind_value(resolved[0]) == "wechat_mp_api"  # 优先于浏览器草稿版
    assert any(registry_on.kind_value(p) == "social_auto_upload" for p in resolved)
