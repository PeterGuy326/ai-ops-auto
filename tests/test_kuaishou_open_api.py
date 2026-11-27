"""KuaishouOpenApiPublisher 单测 — httpx.MockTransport 全 mock，不依赖真账号/真网络。

覆盖失败语义契约（与 douyin_open_api / wechat_mp_api / zhihu_cli 同一纪律）：
  1. 预检（内容/凭证/视频/封面）失败 → 不触网，无副作用
  2. 非 Open Platform 凭证（浏览器会话）→ 让开，可 fallback 到 SAU
  3. 上传阶段任何失败 → 确定未发布（outcome_uncertain=False），按类别决定可否重试
  4. photo/publish 明确拒绝 → 确定未发布，不重试
  5. photo/publish 结果未知（超时/坏 JSON/瞬时错误/缺 photo_id）→ outcome_uncertain=True
  6. happy path：photo_id 作为 post identity；play_url 只在是合法 URL 时采用
  7. access_token / app_secret / refresh_token / upload_token 永不出现在 error、raw_response
  8. 请求形状符合官方文档（查询参数、multipart 字段、分片编号从 0 起）
  9. 令牌轮换：只有 login() 刷新；health_check() 与 publish() 绝不刷新
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import kuaishou_open_api as m
from ai_ops.publishers.kuaishou_open_api import KuaishouOpenApiPublisher

MB = 1024 * 1024

# Fake values only.  Every test asserts these never leave the request wire.
CREDENTIAL = {
    "app_id": "app-id-value",
    "app_secret": "app-secret-value",
    "access_token": "access-token-value",
    "refresh_token": "refresh-token-value",
}
NEW_ACCESS = "new-access-token-value"
NEW_REFRESH = "new-refresh-token-value"
UPLOAD_TOKEN = "upload-token-value"
ENDPOINT = "uploader.example.com"
# Shape copied from the official response example.
PHOTO_ID = "3xwn3kkerxj6g9n"
PLAY_URL = "https://www.kuaishou.com/short-video/3xwn3kkerxj6g9n"

ALL_SECRETS = (
    CREDENTIAL["access_token"],
    CREDENTIAL["app_secret"],
    CREDENTIAL["refresh_token"],
    UPLOAD_TOKEN,
    NEW_ACCESS,
    NEW_REFRESH,
)


def _ok(**data) -> httpx.Response:
    return httpx.Response(200, json={"result": 1, **data})


def _err(code: int) -> httpx.Response:
    return httpx.Response(
        200, json={"result": code, "error_msg": "platform description"}
    )


def _start_ok() -> httpx.Response:
    return _ok(upload_token=UPLOAD_TOKEN, endpoint=ENDPOINT)


def _publish_ok(**video_info) -> httpx.Response:
    return _ok(video_info={"photo_id": PHOTO_ID, "pending": False, **video_info})


def _content(video: str, cover: str, **overrides) -> PublishContent:
    base = dict(
        title="测试标题",
        body="正文",
        content_type=ContentType.VIDEO,
        videos=[video],
        tags=["话题一"],
        extra={m._COVER_EXTRA_KEY: cover},
    )
    base.update(overrides)
    return PublishContent(**base)


class _Router:
    """按路径分发 mock 响应；记录请求供断言。"""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.handlers: dict[str, object] = {}

    def on(self, path: str, handler) -> "_Router":
        self.handlers[path] = handler
        return self

    def paths(self) -> list[str]:
        return [request.url.path for request in self.calls]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.handlers.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={})
        if isinstance(handler, Exception):
            raise handler
        if isinstance(handler, httpx.Response):
            return handler
        if isinstance(handler, dict):
            return httpx.Response(200, json=handler)
        return handler(request)


@pytest.fixture
def assets(tmp_path, monkeypatch):
    """An isolated asset root plus a short network timeout."""

    root = tmp_path / "assets"
    root.mkdir()
    monkeypatch.setattr(settings, "kuaishou_open_api_asset_root", root)
    monkeypatch.setattr(settings, "kuaishou_open_api_timeout_seconds", 5)
    return root


def _video(root, name="a.mp4", size=4096) -> str:
    path = root / name
    path.write_bytes(b"\x00" * size)
    return str(path)


def _cover(root, name="c.jpg", size=256) -> str:
    path = root / name
    path.write_bytes(b"\xff\xd8" + b"\x00" * size)
    return str(path)


def _publisher(router: _Router) -> KuaishouOpenApiPublisher:
    return KuaishouOpenApiPublisher(transport=httpx.MockTransport(router))


def _publish(router, content, credential=None) -> object:
    return asyncio.run(
        _publisher(router).publish(1, credential or dict(CREDENTIAL), content)
    )


def _assert_no_secrets(result, *requests: httpx.Request) -> None:
    """No credential may appear in anything the worker persists or shows."""

    dumped = result.model_dump_json()
    leaked = [secret for secret in ALL_SECRETS if secret in dumped]
    assert not leaked, f"credential leaked into PublishResult: {leaked}"


# ---------------------------------------------------------------------------
# 请求形状 / happy path
# ---------------------------------------------------------------------------


def test_direct_upload_then_publish_uses_the_documented_wire_shape(assets):
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_DIRECT_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok(play_url=PLAY_URL))
    )
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is True
    assert result.effect_applied is True
    assert result.outcome_uncertain is False
    assert result.platform_post_id == PHOTO_ID
    assert result.platform_url == PLAY_URL

    start, upload, publish = router.calls
    # 1. start_upload: access_token + app_id in the query string.
    assert start.method == "POST"
    assert dict(start.url.params) == {
        "access_token": CREDENTIAL["access_token"],
        "app_id": CREDENTIAL["app_id"],
    }
    # 2. direct upload: documented plain http, upload_token in the query,
    #    multipart field ``file`` (官方 A.2).
    assert upload.method == "POST"
    assert upload.url.scheme == "http"
    assert upload.url.host == ENDPOINT
    assert dict(upload.url.params) == {"upload_token": UPLOAD_TOKEN}
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in upload.content
    # 3. publish: access_token + app_id + upload_token in the query string,
    #    multipart ``cover`` + ``caption`` (both required).
    assert publish.method == "POST"
    assert dict(publish.url.params) == {
        "access_token": CREDENTIAL["access_token"],
        "app_id": CREDENTIAL["app_id"],
        "upload_token": UPLOAD_TOKEN,
    }
    assert publish.headers["content-type"].startswith("multipart/form-data")
    assert b'name="cover"' in publish.content
    assert b'name="caption"' in publish.content
    # The cover bytes really travel in the publish request.
    assert b"\xff\xd8" in publish.content
    _assert_no_secrets(result)


def test_caption_is_title_body_and_inline_hashtags(assets):
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_DIRECT_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok())
    )
    content = _content(
        _video(assets),
        _cover(assets),
        title="标题",
        body="正文",
        tags=["话题一", "#话题二"],
    )
    assert m.build_caption(content) == "标题\n正文\n#话题一 #话题二"
    assert _publish(router, content).success is True
    # The caption really travels in the publish request's multipart body.
    assert "标题\n正文\n#话题一 #话题二".encode() in router.calls[-1].content


def test_body_is_omitted_when_empty(assets):
    content = _content(_video(assets), _cover(assets), body="", tags=[])
    assert m.build_caption(content) == "测试标题"


# ---------------------------------------------------------------------------
# 分片上传
# ---------------------------------------------------------------------------


def test_large_video_is_fragmented_from_zero_and_merged(assets, monkeypatch):
    received: list[tuple[dict, bytes]] = []
    router = _Router()
    router.on(m._START_UPLOAD_PATH, _start_ok())
    router.on(
        m._UPLOAD_FRAGMENT_PATH,
        lambda request: (received.append((dict(request.url.params), request.content)), _ok())[1],
    )
    router.on(m._UPLOAD_COMPLETE_PATH, _ok())
    router.on(m._PUBLISH_PATH, _publish_ok())

    size = 25 * MB
    payload = bytes(range(256)) * (size // 256)
    video = assets / "big.mp4"
    video.write_bytes(payload)
    result = _publish(router, _content(str(video), _cover(assets)))

    assert result.success is True
    assert [p for p in router.paths() if "fragment" in p or "complete" in p] == [
        m._UPLOAD_FRAGMENT_PATH,
        m._UPLOAD_FRAGMENT_PATH,
        m._UPLOAD_FRAGMENT_PATH,
        m._UPLOAD_COMPLETE_PATH,
    ]
    # 官方: 分片建议不大于 10M; fragment_id 从 0 开始.
    assert [params["fragment_id"] for params, _ in received] == ["0", "1", "2"]
    assert all(params["upload_token"] == UPLOAD_TOKEN for params, _ in received)
    assert all(len(body) <= 10 * MB for _, body in received)
    # The fragments reassemble to exactly the original file.
    assert b"".join(body for _, body in received) == payload
    # Raw binary bodies (官方 B.1 data-binary), not multipart.
    assert all(
        request.headers["content-type"] == "application/octet-stream"
        for request in router.calls
        if request.url.path == m._UPLOAD_FRAGMENT_PATH
    )
    complete = router.calls[-2]
    assert complete.url.path == m._UPLOAD_COMPLETE_PATH
    assert complete.url.scheme == "http" and complete.url.host == ENDPOINT
    assert dict(complete.url.params) == {
        "upload_token": UPLOAD_TOKEN,
        "fragment_count": "3",
    }
    assert complete.content == b""
    _assert_no_secrets(result)


def test_direct_upload_is_used_up_to_exactly_ten_megabytes(assets):
    size = 10 * MB
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_DIRECT_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok())
    )
    assert _publish(router, _content(_video(assets, size=size), _cover(assets))).success
    assert m._UPLOAD_FRAGMENT_PATH not in router.paths()


def test_one_byte_over_ten_megabytes_switches_to_fragments(assets):
    size = 10 * MB + 1
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_FRAGMENT_PATH, _ok())
        .on(m._UPLOAD_COMPLETE_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok())
    )
    assert _publish(router, _content(_video(assets, size=size), _cover(assets))).success
    assert m._UPLOAD_DIRECT_PATH not in router.paths()
    assert m._UPLOAD_FRAGMENT_PATH in router.paths()


def test_fragment_boundaries_are_contiguous_and_within_the_target():
    total = 25 * MB
    boundaries = m.fragment_boundaries(total, 10 * MB)
    assert len(boundaries) == 3
    offset = 0
    for start, length in boundaries:
        assert start == offset
        assert 0 < length <= 10 * MB
        offset += length
    assert offset == total
    # Near-equal: no short tail.
    assert max(length for _, length in boundaries) - min(length for _, length in boundaries) <= 1
    # Degenerate inputs yield nothing.
    assert m.fragment_boundaries(0, 10 * MB) == []
    assert m.fragment_boundaries(5, 10 * MB) == [(0, 5)]
    assert m.fragment_boundaries(10 * MB, 10 * MB) == [(0, 10 * MB)]


def test_fragmented_upload_never_exceeds_the_configured_size(assets, monkeypatch):
    monkeypatch.setattr(settings, "kuaishou_open_api_fragment_bytes", MB)
    sizes: list[int] = []
    router = _Router().on(m._START_UPLOAD_PATH, _start_ok())
    router.on(
        m._UPLOAD_FRAGMENT_PATH,
        lambda request: (sizes.append(len(request.content)), _ok())[1],
    )
    router.on(m._UPLOAD_COMPLETE_PATH, _ok()).on(m._PUBLISH_PATH, _publish_ok())
    size = 12 * MB + 5
    assert _publish(
        router, _content(_video(assets, size=size), _cover(assets))
    ).success
    assert len(sizes) == 13  # 12 whole megabytes + the 5-byte remainder
    assert sum(sizes) == size and max(sizes) <= MB
    assert dict(router.calls[-2].url.params)["fragment_count"] == str(len(sizes))


def test_fragment_failure_stops_before_the_merge_and_publishes_nothing(assets):
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_FRAGMENT_PATH, _err(100200500))
    )
    result = _publish(router, _content(_video(assets, size=25 * MB), _cover(assets)))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    # 服务内部错误: unknown, retryable.
    assert result.retryable is True
    assert router.paths() == [m._START_UPLOAD_PATH, m._UPLOAD_FRAGMENT_PATH]
    assert m._UPLOAD_COMPLETE_PATH not in router.paths()
    assert m._PUBLISH_PATH not in router.paths()
    _assert_no_secrets(result)


def test_failed_merge_does_not_reach_publish(assets):
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_FRAGMENT_PATH, _ok())
        .on(m._UPLOAD_COMPLETE_PATH, _err(100120002))
    )
    result = _publish(router, _content(_video(assets, size=25 * MB), _cover(assets)))

    assert result.success is False and result.effect_applied is False
    assert result.retryable is False
    assert m._PUBLISH_PATH not in router.paths()


# ---------------------------------------------------------------------------
# 预检 / 让开
# ---------------------------------------------------------------------------


def test_start_upload_failure_never_uploads_or_publishes(assets):
    router = _Router().on(m._START_UPLOAD_PATH, _err(100100402))
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    # One code, two meanings (banned / over the daily limit): not retryable.
    assert result.retryable is False
    assert router.paths() == [m._START_UPLOAD_PATH]
    _assert_no_secrets(result)


@pytest.mark.parametrize(
    "endpoint",
    [
        "uploader.example.com/evil",  # a path
        "uploader.example.com?x=1",
        "user@uploader.example.com",  # userinfo
        "uploader.example.com:8080",  # a port
        "127.0.0.1",
        "169.254.169.254",  # metadata address
        "10.0.0.5",
        "[::1]",
        "localhost",
        "https://uploader.example.com",  # a scheme
        "uploader .example.com",
        "uploader.example.com\r\nX-Injected: 1",
        "uploader.example.com\\@evil.com",
        "中文.example.com",
        "",
        # Non-standard IPv4 spellings: getaddrinfo turns each into 127.0.0.1.
        "0x7f.1",
        "0x7f000001",
        "127.1",
        "0177.0.0.1",
        "127.0.0.1.",
        "0xA9.0xFE.0xA9.0xFE",  # 169.254.169.254 in hex
    ],
)
def test_an_endpoint_that_is_not_a_bare_hostname_is_never_contacted(assets, endpoint):
    router = _Router().on(
        m._START_UPLOAD_PATH, _ok(upload_token=UPLOAD_TOKEN, endpoint=endpoint)
    )
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    # Only the start_upload call was made: nothing was sent to the endpoint.
    assert router.paths() == [m._START_UPLOAD_PATH]


@pytest.mark.parametrize(
    "reply",
    [
        _ok(),  # result 1 without upload_token / endpoint
        _ok(upload_token=UPLOAD_TOKEN),  # no endpoint
        _ok(endpoint=ENDPOINT),  # no upload_token
        _ok(upload_token="bad token\n", endpoint=ENDPOINT),  # wire-unsafe token
        _ok(upload_token=UPLOAD_TOKEN, endpoint=""),  # empty
    ],
)
def test_start_upload_ok_without_a_usable_token_or_endpoint_is_unknown_and_retryable(
    assets, reply
):
    router = _Router().on(m._START_UPLOAD_PATH, reply)
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert router.paths() == [m._START_UPLOAD_PATH]


def test_non_api_credential_steps_aside_for_the_next_adapter(assets):
    router = _Router()
    result = _publish(
        router,
        _content(_video(assets), _cover(assets)),
        credential={"cookies": {"session": "browser-session"}},
    )

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert "未配置快手开放平台凭证" in result.error
    assert router.calls == []


@pytest.mark.parametrize(
    ("credential", "needle"),
    [
        ({"refresh_token": "r"}, "app_id,access_token"),
        ({"refresh_token": "r", "access_token": "t"}, "app_id"),
        ({"app_id": "a", "refresh_token": "r"}, "access_token"),
        ({"app_id": "a", "refresh_token": "r", "access_token": "   "}, "access_token"),
    ],
)
def test_incomplete_api_credential_fails_locally_without_touching_the_network(
    assets, credential, needle
):
    router = _Router()
    result = _publish(router, _content(_video(assets), _cover(assets)), credential)

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert needle in result.error
    assert router.calls == []


def test_publish_never_needs_the_app_secret(assets):
    # app_secret is used by login() alone; the write path must not require it.
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_DIRECT_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok())
    )
    credential = {k: v for k, v in CREDENTIAL.items() if k != "app_secret"}
    result = _publish(router, _content(_video(assets), _cover(assets)), credential)

    assert result.success is True
    # Every byte that left this process: URLs (query included) and bodies.
    sent = b"".join(
        str(request.url).encode() + request.content for request in router.calls
    )
    assert b"app_secret" not in sent and CREDENTIAL["app_secret"].encode() not in sent


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", "token with space"),
        ("access_token", "line\nbreak"),
        ("access_token", "非ASCII"),
        ("app_id", 12345),
        ("app_id", None),
    ],
)
def test_wire_unsafe_credential_values_are_refused_before_the_network(
    assets, field, value
):
    # httpx would fail to encode these, and the failure would look like a
    # network fault - on the write path, an unknown outcome.
    router = _Router()
    credential = {**CREDENTIAL, field: value}
    result = _publish(router, _content(_video(assets), _cover(assets)), credential)

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert field in result.error
    assert router.calls == []


def test_misconfigured_app_grant_is_not_mistaken_for_a_browser_session():
    # The keys are present, so it *is* an app grant: stepping aside would route
    # the job to social-auto-upload, which cannot use it.
    assert m.is_api_credential({"app_id": 12345}) is True
    assert m.is_api_credential({"refresh_token": None}) is True
    assert m.is_api_credential({"cookies": {}}) is False
    assert m.is_api_credential({}) is False
    assert m.is_api_credential(None) is False


# ---------------------------------------------------------------------------
# 令牌轮换：只有 login() 刷新
# ---------------------------------------------------------------------------


def _refresh_ok(**overrides) -> httpx.Response:
    body = {
        "result": 1,
        "access_token": NEW_ACCESS,
        "expires_in": 172800,
        "refresh_token": NEW_REFRESH,
        "refresh_token_expires_in": 15552000,
        "scopes": ["user_video_publish"],
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


def test_login_refreshes_and_writes_back_both_tokens_together():
    router = _Router().on(m._REFRESH_TOKEN_PATH, _refresh_ok())
    credential = dict(CREDENTIAL)

    assert asyncio.run(_publisher(router).login(1, credential)) is True

    (request,) = router.calls
    # 官方: GET /oauth2/refresh_token, grant_type 写死 refresh_token.
    assert request.method == "GET"
    assert request.url.path == m._REFRESH_TOKEN_PATH
    assert dict(request.url.params) == {
        "app_id": CREDENTIAL["app_id"],
        "app_secret": CREDENTIAL["app_secret"],
        "refresh_token": CREDENTIAL["refresh_token"],
        "grant_type": "refresh_token",
    }
    assert request.content == b""
    # Kuaishou retires the old refresh token on refresh: the new one must be
    # written back together with the access token, or the account is locked out.
    assert credential["access_token"] == NEW_ACCESS
    assert credential["refresh_token"] == NEW_REFRESH
    assert credential["app_secret"] == CREDENTIAL["app_secret"]
    assert "access_token_expires_at" in credential


@pytest.mark.parametrize(
    "reply",
    [
        # result 1 without a replacement refresh token: the stored one may
        # already be consumed, but nothing may be written back half-way.
        _refresh_ok(refresh_token=""),
        _refresh_ok(refresh_token=None),
        httpx.Response(200, json={"result": 1, "access_token": NEW_ACCESS}),
        # result 1 without an access token.
        httpx.Response(200, json={"result": 1, "refresh_token": NEW_REFRESH}),
        # Tokens that are not strings (a number, a list, an object): truthy,
        # but nothing that can be put in a query string.
        _refresh_ok(refresh_token=172800),
        _refresh_ok(refresh_token=["r"]),
        _refresh_ok(refresh_token={"token": "r"}),
        _refresh_ok(access_token=172800),
        _refresh_ok(access_token=["a"]),
        _refresh_ok(access_token={"token": "a"}),
        # Unreadable / no verdict.
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, text=""),
        httpx.Response(502, text="bad gateway"),
    ],
)
def test_login_writes_nothing_back_unless_both_tokens_come_back(reply):
    router = _Router().on(m._REFRESH_TOKEN_PATH, reply)
    credential = dict(CREDENTIAL)

    assert asyncio.run(_publisher(router).login(1, credential)) is False
    assert credential == CREDENTIAL  # untouched, including the refresh token


def test_login_error_messages_never_echo_the_reply():
    for reply in (
        _refresh_ok(refresh_token=""),
        _err(100200113),
        _err(100200102),
        _err(100200500),
        httpx.Response(200, text="<html>"),
    ):
        router = _Router().on(m._REFRESH_TOKEN_PATH, reply)
        publisher = _publisher(router)
        credential = dict(CREDENTIAL)
        assert asyncio.run(publisher.login(1, credential)) is False
        error = publisher.last_login_error or ""
        assert error  # always actionable
        for secret in ALL_SECRETS + (CREDENTIAL["app_id"],):
            assert secret not in error


def test_expired_refresh_token_is_reported_as_needing_reauthorization():
    router = _Router().on(m._REFRESH_TOKEN_PATH, _err(100200113))
    publisher = _publisher(router)
    assert asyncio.run(publisher.login(1, dict(CREDENTIAL))) is False
    assert "重新完成用户授权" in publisher.last_login_error
    assert "100200113" in publisher.last_login_error


def test_login_fails_locally_for_an_incomplete_grant():
    router = _Router()
    publisher = _publisher(router)
    credential = {k: v for k, v in CREDENTIAL.items() if k != "app_secret"}

    assert asyncio.run(publisher.login(1, credential)) is False
    assert "app_secret" in publisher.last_login_error
    assert router.calls == []


def test_health_check_is_read_only_and_never_refreshes_the_token():
    # The health scheduler does not persist credential changes.  A refresh in
    # here would retire the stored refresh token every night.
    router = _Router().on(m._PHOTO_COUNT_PATH, _ok(public_count=3, all_count=3))
    publisher = _publisher(router)
    credential = dict(CREDENTIAL)

    health = asyncio.run(publisher.health_check(1, credential))

    assert health is AccountHealth.HEALTHY
    assert [request.url.path for request in router.calls] == [m._PHOTO_COUNT_PATH]
    assert router.calls[0].method == "GET"
    assert dict(router.calls[0].url.params) == {
        "access_token": CREDENTIAL["access_token"],
        "app_id": CREDENTIAL["app_id"],
    }
    assert credential == CREDENTIAL  # not a byte changed


@pytest.mark.parametrize(
    "code",
    [
        100200102,  # 请求被拒绝，可能是无效的 token 等
        100200108,  # access_token过期
        100200109,  # 用户取消该 app 授权
        100200110,  # 用户授权过期
        100200111,  # 用户未授权过
    ],
)
def test_health_check_reports_a_refused_token_as_expired(code):
    router = _Router().on(m._PHOTO_COUNT_PATH, _err(code))
    health = asyncio.run(_publisher(router).health_check(1, dict(CREDENTIAL)))
    assert health is AccountHealth.EXPIRED


def test_an_expired_access_token_does_not_refresh_during_publish(assets):
    # publish() cannot persist a refreshed token, and a refresh would retire
    # the stored one.  An expired token is a plain, fallback-able failure.
    router = _Router()
    router.on(m._START_UPLOAD_PATH, _err(100200108))
    router.on(m._REFRESH_TOKEN_PATH, _refresh_ok())  # must never be called
    credential = dict(CREDENTIAL)
    result = _publish(router, _content(_video(assets), _cover(assets)), credential)

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert "刷新登录" in result.error
    assert router.paths() == [m._START_UPLOAD_PATH]
    assert credential == CREDENTIAL  # not refreshed, not touched
    _assert_no_secrets(result)


def test_health_check_steps_aside_for_a_browser_credential():
    router = _Router()
    health = asyncio.run(
        _publisher(router).health_check(1, {"cookies": {"session": "s"}})
    )
    assert health is AccountHealth.UNKNOWN
    assert router.calls == []


def test_health_check_steps_aside_for_a_grant_it_cannot_probe():
    router = _Router()
    for credential in ({"app_id": "a"}, {"app_id": "a", "access_token": "has space"}):
        health = asyncio.run(_publisher(router).health_check(1, credential))
        assert health is AccountHealth.UNKNOWN
    assert router.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        _err(100200500),  # 服务内部错误
        _err(999999),  # unlisted: not interpreted as a credential verdict
        _err(100400),  # 异常参数 (e.g. the user_video_info scope is missing)
        httpx.Response(200, text="<html>"),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_health_check_stays_unknown_when_the_probe_gives_no_verdict(reply):
    router = _Router().on(m._PHOTO_COUNT_PATH, reply)
    health = asyncio.run(_publisher(router).health_check(1, dict(CREDENTIAL)))
    assert health is AccountHealth.UNKNOWN


def test_health_check_never_raises_into_the_scheduler():
    # scheduler/health.py treats an exception as "swallowed, continue", but a
    # probe that never raises is the contract this adapter promises.
    router = _Router().on(
        m._PHOTO_COUNT_PATH, httpx.LocalProtocolError("x" * 10_000)
    )
    health = asyncio.run(_publisher(router).health_check(1, dict(CREDENTIAL)))
    assert health is AccountHealth.UNKNOWN


# ---------------------------------------------------------------------------
# photo/publish 结果语义
# ---------------------------------------------------------------------------


def _up_to_publish(router: _Router, publish_reply) -> _Router:
    return (
        router.on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_DIRECT_PATH, _ok())
        .on(m._PUBLISH_PATH, publish_reply)
    )


def _publishing(assets, publish_reply):
    router = _up_to_publish(_Router(), publish_reply)
    result = _publish(router, _content(_video(assets), _cover(assets)))
    assert router.paths()[-1] == m._PUBLISH_PATH  # the write was attempted
    return result


@pytest.mark.parametrize(
    ("code", "retryable", "needle"),
    [
        (100200108, False, "登录态失效"),  # access_token过期
        (100200111, False, "登录态失效"),  # 用户未授权过
        (100120001, False, "平台拒绝"),  # 视频不存在
        (100120002, False, "平台拒绝"),  # 视频未上传
        (100120003, False, "平台拒绝"),  # 视频发布失败
        (100100400, False, "平台拒绝"),  # 异常参数
        (100100402, False, "封禁或请求超过当日上限"),  # one code, two meanings
        (424242, False, "平台拒绝"),  # unlisted: a definitive answer
    ],
)
def test_publish_refused_means_definitively_not_published(assets, code, retryable, needle):
    result = _publishing(assets, _err(code))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is retryable
    assert needle in result.error and str(code) in result.error
    assert result.platform_post_id is None and result.platform_url is None
    assert result.raw_response["write_started"] is True
    assert result.raw_response["outcome"] == "rejected"
    assert result.raw_response["error_code"] == code
    _assert_no_secrets(result)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.ReadTimeout("timed out"),  # sent, no answer
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("connection reset"),
        httpx.Response(200, text="<html>gateway timeout</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, text=""),
        httpx.Response(200, json={}),  # no result
        httpx.Response(200, json={"result": "not-a-number"}),
        httpx.Response(500, json={"result": 1}),  # HTTP error status, unreadable meaning
        _err(100200500),  # 服务内部错误: reported, but the post may exist
    ],
)
def test_an_unreadable_publish_outcome_is_uncertain_and_blocks_retry(assets, reply):
    # The worker stops fallback and retry on outcome_uncertain: a second
    # attempt here would publish the video twice.
    result = _publishing(assets, reply)

    assert result.success is False
    assert result.effect_applied is True
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.platform_post_id is None
    assert result.raw_response["write_started"] is True
    assert result.raw_response["outcome"] == "unknown"
    assert result.raw_response["needs_reconciliation"] is True
    assert "核验" in result.error
    _assert_no_secrets(result)


def test_publish_ok_without_a_photo_id_is_uncertain(assets):
    for reply in (
        _ok(),  # no video_info
        _ok(video_info={"caption": "t", "pending": False}),  # no photo_id
        _ok(video_info={"photo_id": ""}),
        _ok(video_info={"photo_id": "bad id\nwith newline"}),
        _ok(video_info={"photo_id": "x" * 200}),
        _ok(video_info="not-an-object"),
    ):
        result = _publishing(assets, reply)
        assert result.success is False, reply
        assert result.effect_applied is True
        assert result.outcome_uncertain is True
        assert result.retryable is False
        assert result.raw_response["needs_reconciliation"] is True


def test_publish_ok_is_confirmed_even_while_still_processing(assets):
    # pending=True only means "not watchable yet"; the post exists.
    result = _publishing(assets, _ok(video_info={"photo_id": PHOTO_ID, "pending": True}))

    assert result.success is True and result.effect_applied is True
    assert result.platform_post_id == PHOTO_ID
    # No play_url while transcoding: nothing is invented.
    assert result.platform_url is None
    assert "转码" in result.error
    assert result.raw_response["outcome"] == "confirmed"


@pytest.mark.parametrize(
    "play_url",
    [
        "",
        None,
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://example.com/v",
        "//www.kuaishou.com/v",  # protocol-relative
        "https://exa mple.com/v",
        'https://example.com/v"onload="x',
        "https://example.com/\nv",
        12345,
    ],
)
def test_only_a_well_formed_http_url_becomes_the_platform_url(assets, play_url):
    result = _publishing(assets, _ok(video_info={"photo_id": PHOTO_ID, "play_url": play_url}))

    assert result.success is True and result.platform_post_id == PHOTO_ID
    assert result.platform_url is None


def test_publish_unreachable_is_safe_and_retryable(assets):
    router = _up_to_publish(_Router(), httpx.ConnectError("refused"))
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert result.raw_response["write_started"] is False
    assert result.raw_response["outcome"] == "not_started"


# ---------------------------------------------------------------------------
# 预检：视频 / 封面
# ---------------------------------------------------------------------------


def _pre_flight(assets, content=None, **overrides) -> object:
    """Publish and assert that nothing reached the network.

    ``content`` lets a test hand in the exact content it built; without it a
    default valid content is made and patched with ``overrides``.
    """

    if content is None:
        content = _content(_video(assets), _cover(assets), **overrides)
    else:
        assert not overrides, "pass either a content or overrides, not both"
    router = _Router()
    result = _publish(router, content)
    assert router.calls == [], "a pre-flight failure must not touch the network"
    return result


def _assert_pre_flight_failure(result, needle: str) -> None:
    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert needle in result.error
    assert result.raw_response["write_started"] is False


def test_a_missing_cover_fails_before_the_upload(assets):
    # cover is required by the platform: finding that out after a large
    # upload would waste it and the account's daily request budget.
    router = _Router()
    content = _content(_video(assets, size=25 * MB), _cover(assets))
    content.extra = {}
    result = _publish(router, content)
    assert router.calls == []
    _assert_pre_flight_failure(result, "封面图")


@pytest.mark.parametrize(
    "cover_name",
    ["c.png", "c.webp", "c.mp4", "c", "c.jpg.txt", "c.exe"],
)
def test_only_a_jpg_is_accepted_as_the_cover(assets, cover_name):
    content = _content(_video(assets), _cover(assets, name=cover_name))
    router = _Router()
    result = _publish(router, content)
    assert router.calls == []
    _assert_pre_flight_failure(result, "jpg")


def test_an_oversized_cover_is_refused(assets):
    content = _content(_video(assets), _cover(assets, size=m._COVER_MAX_BYTES))
    result = _pre_flight(assets, content)
    _assert_pre_flight_failure(result, "10MB")


def test_an_empty_cover_is_refused(assets):
    path = assets / "empty.jpg"
    path.write_bytes(b"")
    result = _pre_flight(assets, _content(_video(assets), str(path)))
    _assert_pre_flight_failure(result, "封面图为空")


def test_a_cover_outside_the_asset_root_is_refused(assets, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\x00")
    router = _Router()
    content = _content(_video(assets), str(outside))
    result = _publish(router, content)
    assert router.calls == []
    _assert_pre_flight_failure(result, "内的本地文件")


def test_a_video_outside_the_asset_root_is_refused(assets, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 100)
    router = _Router()
    result = _publish(router, _content(str(outside), _cover(assets)))
    assert router.calls == []
    _assert_pre_flight_failure(result, "内的本地文件")


def test_an_oversized_video_is_refused(assets, monkeypatch):
    monkeypatch.setattr(settings, "kuaishou_open_api_max_video_bytes", 1024)
    result = _pre_flight(assets)
    _assert_pre_flight_failure(result, "大小上限")


def test_an_empty_video_is_refused(assets):
    path = assets / "empty.mp4"
    path.write_bytes(b"")
    result = _pre_flight(assets, _content(str(path), _cover(assets)))
    _assert_pre_flight_failure(result, "视频为空")


@pytest.mark.parametrize(
    "name",
    ["a.png", "a.txt", "a", "a.mp4.exe"],
)
def test_an_unsupported_video_type_is_refused(assets, name):
    content = _content(_video(assets, name=name), _cover(assets))
    result = _pre_flight(assets, content)
    _assert_pre_flight_failure(result, "受支持的本地视频")


def test_an_unreadable_file_fails_before_the_network_without_raising(assets):
    if os.geteuid() == 0:
        pytest.skip("permission bits are not enforced for root")
    video = assets / "locked.mp4"
    video.write_bytes(b"\x00" * 64)
    video.chmod(0o000)
    cover = assets / "locked.jpg"
    cover.write_bytes(b"\xff\xd8\x00")
    cover.chmod(0o000)

    router = _Router()
    result = _publish(router, _content(str(video), _cover(assets)))
    assert router.calls == []
    _assert_pre_flight_failure(result, "不可读")

    router = _Router()
    result = _publish(router, _content(_video(assets), str(cover)))
    assert router.calls == []
    _assert_pre_flight_failure(result, "不可读")


def test_symlinked_assets_are_refused(assets, tmp_path):
    real_video = tmp_path / "real.mp4"
    real_video.write_bytes(b"\x00" * 64)
    real_cover = tmp_path / "real.jpg"
    real_cover.write_bytes(b"\xff\xd8\x00")
    (assets / "link.mp4").symlink_to(real_video)
    (assets / "link.jpg").symlink_to(real_cover)

    router = _Router()
    result = _publish(router, _content(str(assets / "link.mp4"), _cover(assets)))
    assert router.calls == []
    _assert_pre_flight_failure(result, "符号链接")

    router = _Router()
    result = _publish(router, _content(_video(assets), str(assets / "link.jpg")))
    assert router.calls == []
    _assert_pre_flight_failure(result, "符号链接")


def test_a_cover_that_disappears_after_the_upload_is_a_safe_failure(assets):
    # Removed between validation and the publish request: nothing was sent.
    cover = assets / "gone.jpg"
    cover.write_bytes(b"\xff\xd8\x00")
    router = _up_to_publish(_Router(), _publish_ok())
    router.on(
        m._UPLOAD_DIRECT_PATH,
        lambda request: (cover.unlink(), _ok())[1],
    )
    result = _publish(router, _content(_video(assets), str(cover)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert m._PUBLISH_PATH not in router.paths()
    assert result.raw_response["write_started"] is False


# ---------------------------------------------------------------------------
# 内容校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"title": "   "}, "标题不能为空"),
        ({"title": ""}, "标题不能为空"),
        ({"title": "a\x00b"}, "NUL"),
        ({"body": "a\x00b"}, "NUL"),
        ({"content_type": ContentType.IMAGE_TEXT}, "恰好一个本地视频文件"),
        ({"images": ["x.png"]}, "不支持图片资产"),
        ({"extra": {"kuaishou_cover": "c.jpg", "unknown": 1}}, "不支持的 extra 字段"),
        ({"extra": {"kuaishou_cover": "  "}}, "封面图"),
        ({"extra": {"kuaishou_cover": 123}}, "封面图"),
        ({"tags": [""]}, "话题标签不能为空"),
        ({"tags": ["a b"]}, "话题标签不能包含空格"),
        ({"tags": ["a#b"]}, "话题标签不能包含空格"),
        ({"tags": ["a@b"]}, "话题标签不能包含空格"),
    ],
)
def test_invalid_content_fails_before_the_network(assets, overrides, needle):
    result = _pre_flight(assets, **overrides)
    _assert_pre_flight_failure(result, needle)


def test_no_videos_fails_before_the_network(assets):
    router = _Router()
    result = _publish(router, _content(_video(assets), _cover(assets), videos=[]))
    assert router.calls == []
    _assert_pre_flight_failure(result, "恰好一个本地视频文件")


def test_exact_approval_fails_closed_without_a_renderer(assets):
    content = _content(_video(assets), _cover(assets))
    content.exact_approval = True
    router = _Router()
    result = _publish(router, content)

    assert result.success is False and result.retryable is False
    assert router.calls == []


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, ("invalid", None)),
        ({}, ("invalid", None)),
        ({"result": "not-a-number"}, ("invalid", None)),
        ({"result": True}, ("invalid", None)),  # bool is not a code
        ({"result": 1.0}, ("invalid", None)),  # a float is not a code either
        ({"result": 1}, ("ok", 1)),
        ({"result": "1"}, ("ok", 1)),  # numeric strings are accepted
        ({"result": 100200108}, ("credential", 100200108)),
        ({"result": 100100402}, ("limited", 100100402)),
        ({"result": 100200500}, ("transient", 100200500)),
        ({"result": 100120003}, ("rejected", 100120003)),
        ({"result": 424242}, ("rejected", 424242)),  # unlisted
    ],
)
def test_classify_response(data, expected):
    assert m.classify_response(data) == expected


@pytest.mark.parametrize("code", sorted(m._CREDENTIAL_ERROR_CODES))
def test_every_listed_credential_code_classifies_as_credential(code):
    assert m.classify_response({"result": code}) == ("credential", code)


def test_classification_never_returns_platform_text():
    data = {"result": 424242, "error_msg": "echo of the request: access-token-value"}
    assert m.classify_response(data) == ("rejected", 424242)


def test_parse_api_json_accepts_only_an_object():
    assert m.parse_api_json(b'{"result": 1}') == {"result": 1}
    assert m.parse_api_json('{"result": 1}') == {"result": 1}
    for payload in (b"", b"[]", b"null", b"<html>", b"{", None, 12345):
        assert m.parse_api_json(payload) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3xwn3kkerxj6g9n", True),
        ("a-b_C9", True),
        ("", False),
        ("has space", False),
        ("line\nbreak", False),
        ("中文", False),
        ("x" * 129, False),
        (None, False),
        (12345, False),
    ],
)
def test_only_a_well_formed_photo_id_is_taken(value, expected):
    found, _url = m.extract_photo({"result": 1, "video_info": {"photo_id": value}})
    assert (found is not None) is expected
    if expected:
        assert found == value


def test_is_api_credential_and_wire_safety():
    assert m.is_api_credential({"app_id": "a"}) is True
    assert m.is_api_credential({"refresh_token": "r"}) is True
    assert m.is_api_credential({"access_token": "t"}) is False  # a token alone is not a grant
    assert m.is_api_credential({}) is False and m.is_api_credential(None) is False
    assert m.wire_unsafe_credential_fields({"app_id": "ok", "app_secret": "has space"}) == ["app_secret"]
    assert m.wire_unsafe_credential_fields({"access_token": 5}) == ["access_token"]
    assert m.wire_unsafe_credential_fields({"refresh_token": None}) == []  # unset is fine here
    assert m.wire_unsafe_credential_fields({}) == []


def test_registry_and_kind():
    from ai_ops.publishers.kuaishou_open_api import KuaishouOpenApiPublisher

    assert KuaishouOpenApiPublisher.platform is Platform.KUAISHOU
    assert KuaishouOpenApiPublisher.kind is PublisherKind.KUAISHOU_OPEN_API
    # No exact renderer: the Agent planner must fail closed.
    assert KuaishouOpenApiPublisher.agent_contract_renderer_descriptor is None


def test_registry_gate_default_off_and_first_when_enabled(monkeypatch):
    from types import SimpleNamespace

    from ai_ops.publishers.registry import build_default_registry

    def kinds(config):
        registry = build_default_registry(config=config)
        return [
            getattr(publisher.kind, "value", str(publisher.kind))
            for publisher in registry.resolve(Platform.KUAISHOU)
        ]

    base = dict(
        youtube_uploader_enabled=False,
        zhihu_cli_enabled=False,
        baijiahao_publisher_enabled=False,
        sohuhao_publisher_enabled=False,
        easel_wechat_publisher_enabled=False,
        wechat_mp_api_publisher_enabled=False,
        publisher_plugin_allowlist=(),
    )
    off = kinds(SimpleNamespace(**base, browser_engine="chromium"))
    on = kinds(SimpleNamespace(**base, browser_engine="chromium", kuaishou_open_api_enabled=True))

    assert off == ["social_auto_upload"]
    # API/CLI first, SAU kept as the fallback.
    assert on == ["kuaishou_open_api", "social_auto_upload"]
    assert settings.kuaishou_open_api_enabled is False


# ---------------------------------------------------------------------------
# 无异常逃逸：publish() 在任何输入下都只能返回值
# ---------------------------------------------------------------------------


def _must_not_raise(coro_func):
    try:
        return coro_func()
    except BaseException as exc:  # noqa: BLE001 - the test fails on any escape
        pytest.fail(f"publish() raised {type(exc).__name__}: {exc}")


def test_no_exception_ever_escapes_publish(assets):
    """The worker records an escaping exception as an unknown, unretryable outcome."""
    good = _content(_video(assets), _cover(assets))
    video = assets / "a.mp4"
    cover = assets / "c.jpg"

    hostile_replies = [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool timed out"),
        httpx.ReadError("reset"),
        httpx.WriteError("reset"),
        httpx.RemoteProtocolError("disconnected"),
        httpx.LocalProtocolError("x" * 10_000),
        httpx.DecodingError("zlib"),
        httpx.TooManyRedirects("loop"),
        httpx.InvalidURL("bad url"),
        httpx.StreamError("stream"),
        RuntimeError("not even an httpx error"),
        ValueError("not even an httpx error"),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, content=b"\xff\xfe\x00garbage"),
        httpx.Response(200, headers={"content-length": "999999"}, content=b"{}"),
        httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
        httpx.Response(200, json={"result": 1, "upload_token": "u" * 10_000, "endpoint": ENDPOINT}),
        httpx.Response(200, json={"video_info": {"photo_id": "x" * 10_000}}),
        _err(-1),
        _err(0),
    ]
    hostile_credentials = [
        dict(CREDENTIAL),
        {**CREDENTIAL, "access_token": "has space"},
        {**CREDENTIAL, "access_token": "非ASCII"},
        {**CREDENTIAL, "app_id": "\r\n"},
        {**CREDENTIAL, "access_token": None},
        {"app_id": "a", "refresh_token": "r"},
        {"cookies": {"session": "s"}},
        {},
    ]
    hostile_contents = [
        good,
        _content(str(video), str(cover), title="", tags=["bad tag"], extra={}),
        _content("/nonexistent/a.mp4", "/nonexistent/c.jpg"),
        _content(str(video), str(cover), videos=[]),
        # extra=None cannot pass model validation, but model_construct
        # bypasses it: the adapter must still not raise on it.
        PublishContent.model_construct(
            title="t", body="", content_type=ContentType.VIDEO, videos=[str(video)], extra=None
        ),
    ]

    checked = 0
    for path in (m._START_UPLOAD_PATH, m._UPLOAD_DIRECT_PATH, m._PUBLISH_PATH):
        for reply in hostile_replies:
            router = _Router().on(m._START_UPLOAD_PATH, _start_ok())
            router.on(m._UPLOAD_DIRECT_PATH, _ok())
            router.on(m._PUBLISH_PATH, _publish_ok())
            router.on(path, reply)
            for credential in hostile_credentials:
                for content in hostile_contents:
                    result = _must_not_raise(
                        lambda: _publish(router, content, credential)
                    )
                    assert isinstance(result, PublishResult)
                    dumped = result.model_dump_json()
                    for secret in ALL_SECRETS:
                        assert secret not in dumped, (secret, path, type(reply).__name__)
                    # A failure is never both "not applied" and "uncertain".
                    if result.outcome_uncertain:
                        assert result.effect_applied is True
                        assert result.retryable is False
                    checked += 1
    assert checked == 3 * len(hostile_replies) * len(hostile_credentials) * len(hostile_contents)


def test_publish_reports_a_never_sent_request_as_safe(assets):
    router = _Router().on(m._START_UPLOAD_PATH, httpx.ConnectError("refused"))
    result = _publish(router, _content(_video(assets), _cover(assets)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False and result.retryable is True


def test_a_video_removed_after_validation_is_a_safe_failure(assets, monkeypatch):
    video = assets / "gone.mp4"
    video.write_bytes(b"\x00" * 4096)
    cover = _cover(assets)
    publisher = _publisher(_Router())
    # Make the file vanish between validation and the upload.
    original = publisher._validate_video

    def validate_then_remove(content):
        outcome = original(content)
        video.unlink()
        return outcome

    monkeypatch.setattr(publisher, "_validate_video", validate_then_remove)
    result = asyncio.run(publisher.publish(1, dict(CREDENTIAL), _content(str(video), cover)))

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False and result.retryable is True


def test_a_video_rewritten_after_validation_is_a_safe_failure(assets, monkeypatch):
    video = assets / "changed.mp4"
    video.write_bytes(b"\x00" * 4096)
    publisher = _publisher(_Router())
    original = publisher._validate_video

    def validate_then_grow(content):
        outcome = original(content)
        video.write_bytes(b"\x00" * 8192)
        return outcome

    monkeypatch.setattr(publisher, "_validate_video", validate_then_grow)
    result = asyncio.run(
        publisher.publish(1, dict(CREDENTIAL), _content(str(video), _cover(assets)))
    )

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False and result.retryable is True


def test_a_short_read_during_fragmentation_is_a_safe_failure(assets, monkeypatch):
    video = assets / "big.mp4"
    video.write_bytes(b"\x00" * (25 * MB))
    router = (
        _Router()
        .on(m._START_UPLOAD_PATH, _start_ok())
        .on(m._UPLOAD_FRAGMENT_PATH, _ok())
        .on(m._UPLOAD_COMPLETE_PATH, _ok())
        .on(m._PUBLISH_PATH, _publish_ok())
    )
    publisher = _publisher(router)
    original = publisher._validate_video

    def validate_then_shrink(content):
        outcome = original(content)
        video.write_bytes(b"\x00" * 100)  # shorter than validated
        return outcome

    monkeypatch.setattr(publisher, "_validate_video", validate_then_shrink)
    result = asyncio.run(
        publisher.publish(1, dict(CREDENTIAL), _content(str(video), _cover(assets)))
    )

    assert result.success is False and result.effect_applied is False
    assert result.outcome_uncertain is False
    # The size check runs before any fragment is sent.
    assert m._UPLOAD_FRAGMENT_PATH not in router.paths()


def test_gateway_caller_bugs_come_back_as_values(assets):
    publisher = _publisher(_Router())
    result = asyncio.run(
        publisher._gateway_call(
            "http://" + ENDPOINT,
            m._UPLOAD_DIRECT_PATH,
            UPLOAD_TOKEN,
            timeout=1.0,
            files={"file": ("a.mp4", b"x", "video/mp4")},
            content=b"x",  # both a body and files: a caller bug
        )
    )
    assert result == (None, False)


# ---------------------------------------------------------------------------
# refresh_token 是一次性的：login() 对每种回复的分类决定操作员下一步做什么
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        100200100,
        100200101,
        100200102,  # 无效token
        100200108,  # access_token过期
        100200109,  # 用户取消授权
        100200110,  # 授权过期
        100200111,  # 用户未授权过
        424242,  # unlisted non-1: no proof the grant is gone, no proof it is usable
    ],
)
def test_a_refused_refresh_is_a_credential_failure_not_a_transient_one(code):
    # Reported as transient, an operator would wait and retry instead of
    # fixing the grant; the scheduler has no auto-retry for login, so the
    # wording is what tells them what to do.
    router = _Router().on(m._REFRESH_TOKEN_PATH, _err(code))
    publisher = _publisher(router)
    credential = dict(CREDENTIAL)

    assert asyncio.run(publisher.login(1, credential)) is False

    assert "暂不可用" not in publisher.last_login_error
    assert "返回异常" not in publisher.last_login_error
    assert "凭证校验失败" in publisher.last_login_error
    assert str(code) in publisher.last_login_error
    assert credential == CREDENTIAL


def test_a_transient_refresh_failure_is_reported_as_transient():
    router = _Router().on(m._REFRESH_TOKEN_PATH, _err(100200500))  # 服务内部错误
    publisher = _publisher(router)
    credential = dict(CREDENTIAL)

    assert asyncio.run(publisher.login(1, credential)) is False

    assert "暂不可用" in publisher.last_login_error
    assert "凭证校验失败" not in publisher.last_login_error
    assert "返回异常" not in publisher.last_login_error
    assert credential == CREDENTIAL  # the stored refresh token is still good


def test_a_transient_refresh_failure_retries_cleanly_afterwards():
    # The platform's 100200500 retires nothing: the same grant must still work.
    class _Then:
        """Answer with the queued replies in order; the last one repeats."""

        def __init__(self, *replies) -> None:
            self.replies = list(replies)

        def __call__(self, request):
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            return reply() if callable(reply) else reply

    sent = []
    router = _Router().on(
        m._REFRESH_TOKEN_PATH,
        _Then(_err(100200500), lambda: (sent.append(1), _refresh_ok())[1]),
    )
    publisher = _publisher(router)
    credential = dict(CREDENTIAL)

    assert asyncio.run(publisher.login(1, credential)) is False
    assert asyncio.run(publisher.login(1, credential)) is True
    assert sent  # the second call really got the ok reply

    assert credential["refresh_token"] == NEW_REFRESH
    assert credential["access_token"] == NEW_ACCESS


def test_an_invalid_refresh_reply_is_not_called_a_credential_failure():
    # "返回异常" tells the operator the stored token may have been consumed.
    # Calling it a credential failure sends them to check app_id / scopes, and
    # calling it transient tells them to wait - both wrong.
    for reply in (
        _refresh_ok(refresh_token=""),
        _refresh_ok(access_token=""),
        httpx.Response(200, json={"result": 1}),
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(200, json={"result": "later"}),
        httpx.ConnectError("connection refused"),
    ):
        router = _Router().on(m._REFRESH_TOKEN_PATH, reply)
        publisher = _publisher(router)
        credential = dict(CREDENTIAL)

        assert asyncio.run(publisher.login(1, credential)) is False

        assert "返回异常" in publisher.last_login_error, reply
        assert "凭证校验失败" not in publisher.last_login_error
        assert "暂不可用" not in publisher.last_login_error
        assert credential == CREDENTIAL


def test_a_non_one_result_never_counts_as_a_successful_refresh():
    # result 1 *and* both tokens is the only ok.  A reply carrying tokens but
    # another result code must not be written back.
    for reply in (
        httpx.Response(
            200,
            json={
                "result": 100200102,
                "access_token": NEW_ACCESS,
                "refresh_token": NEW_REFRESH,
            },
        ),
        httpx.Response(
            200, json={"result": 0, "access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
        ),
    ):
        router = _Router().on(m._REFRESH_TOKEN_PATH, reply)
        publisher = _publisher(router)
        credential = dict(CREDENTIAL)

        assert asyncio.run(publisher.login(1, credential)) is False

        assert credential == CREDENTIAL


def test_an_unreadable_refresh_reply_does_not_become_ok_when_the_connection_drops():
    # _send returns (None, reached): no body is ever an "invalid", never "ok".
    assert m.classify_refresh(None) == ("invalid", None)


# ---------------------------------------------------------------------------
# 上传步骤的回执：公开写尚未开始
# ---------------------------------------------------------------------------


def _upload_failure_receipt(result) -> object:
    assert result.raw_response["adapter"] == "kuaishou-open-api"
    assert result.raw_response["action"] == "video-upload"
    assert result.raw_response["stage"] == "upload"
    # No post exists at this point; a receipt saying otherwise would send the
    # operator looking for a video that was never published.
    assert result.raw_response["write_started"] is False
    assert "needs_reconciliation" not in result.raw_response
    return result.raw_response["outcome"]


def test_an_upload_failure_receipt_says_no_public_write_started(assets):
    cases = [
        (m._START_UPLOAD_PATH, _err(100200108), "credential", False),
        (m._START_UPLOAD_PATH, _err(100100402), "rejected", False),
        (m._START_UPLOAD_PATH, _err(100200500), "unknown", True),
        (m._START_UPLOAD_PATH, httpx.ConnectError("refused"), "unreachable", True),
        (m._START_UPLOAD_PATH, _ok(), "unknown", True),  # no usable token/endpoint
        (m._UPLOAD_DIRECT_PATH, _err(100120002), "rejected", False),
        (m._UPLOAD_DIRECT_PATH, _err(100200500), "unknown", True),
    ]
    for step, reply, outcome, retryable in cases:
        router = _Router().on(step, reply)
        # Earlier steps succeed when a later one is under test.
        if step != m._START_UPLOAD_PATH:
            router.on(m._START_UPLOAD_PATH, _start_ok())
        result = _publish(router, _content(_video(assets), _cover(assets)))

        assert result.success is False and result.effect_applied is False
        assert result.outcome_uncertain is False
        assert result.retryable is retryable
        assert _upload_failure_receipt(result) == outcome
        assert m._PUBLISH_PATH not in router.paths()


def test_a_fragment_upload_failure_receipt_also_says_no_public_write_started(assets):
    router = _Router().on(m._START_UPLOAD_PATH, _start_ok())
    router.on(m._UPLOAD_FRAGMENT_PATH, _err(100120002))
    result = _publish(router, _content(_video(assets, size=25 * MB), _cover(assets)))

    assert result.effect_applied is False and result.retryable is False
    assert _upload_failure_receipt(result) == "rejected"
    assert m._PUBLISH_PATH not in router.paths()


@pytest.mark.parametrize(
    ("video_size", "step", "label"),
    [
        (4096, m._START_UPLOAD_PATH, "发起上传"),
        (4096, m._UPLOAD_DIRECT_PATH, "整体上传"),
        (25 * MB, m._UPLOAD_FRAGMENT_PATH, "分片上传"),
        (25 * MB, m._UPLOAD_COMPLETE_PATH, "分片合并"),
    ],
)
def test_an_upload_failure_labels_the_step_and_the_code(assets, video_size, step, label):
    router = _Router().on(step, _err(100120002))
    if step != m._START_UPLOAD_PATH:
        router.on(m._START_UPLOAD_PATH, _start_ok())
    if step == m._UPLOAD_COMPLETE_PATH:
        router.on(m._UPLOAD_FRAGMENT_PATH, _ok())  # the fragments all succeed
    result = _publish(router, _content(_video(assets, size=video_size), _cover(assets)))

    assert router.paths()[-1] == step  # the failing step was reached
    assert result.effect_applied is False
    assert label in result.error
    assert "100120002" in result.error
    _assert_no_secrets(result)
