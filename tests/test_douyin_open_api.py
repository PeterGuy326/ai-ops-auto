"""DouyinOpenApiPublisher 单测 — httpx.MockTransport 全 mock，不依赖真账号/真网络。

覆盖失败语义契约（与 wechat_mp_api / zhihu_cli 同一纪律）：
  1. 预检（内容/凭证/文件）失败 → 不触网，无副作用
  2. 非 Open Platform 凭证（浏览器会话）→ 让开，可 fallback 到 SAU
  3. 上传阶段任何失败 → 确定未发布（outcome_uncertain=False），按类别决定可否重试
  4. create_video 明确拒绝 → 确定未发布，不重试
  5. create_video 结果未知（超时/坏 JSON/瞬时错误/缺 item_id）→ outcome_uncertain=True
  6. happy path：item_id 作为 post identity，不伪造 platform_url
  7. access_token / client_secret / open_id 永不出现在 URL、error、raw_response
  8. 请求形状符合官方文档（header 名、query 参数、body 字段、分片编号）
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_ops.config import settings
from ai_ops.core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers import douyin_open_api as m
from ai_ops.publishers.douyin_open_api import DouyinOpenApiPublisher

MB = 1024 * 1024

CREDENTIAL = {
    "client_key": "client-key-value",
    "client_secret": "client-secret-value",
    "open_id": "open-id-value",
    "access_token": "access-token-value",
    "refresh_token": "refresh-token-value",
}
# Shapes copied from the official response examples (encrypted ids carry @ + / =).
VIDEO_ID = "@9VxVx/WTUc8oLGK3atFqTs7912buPvyEOpZwrGXgL1MTbqer1nD"
ITEM_ID = "@8hxdhauTCMppanGnM4ltGM780mDqPP+KPpR0qQOmLVAXb/T060zdRmYqig357zEBq6CZRp4NVe6qLIJW/V/x1w=="


def _ok(**data) -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"error_code": 0, **data}, "extra": {"error_code": 0}}
    )


def _err(code: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {"error_code": code, "description": "platform description"},
            "extra": {"error_code": code},
        },
    )


def _upload_ok() -> httpx.Response:
    return _ok(video={"video_id": VIDEO_ID, "width": 720, "height": 1280})


def _content(video: str, **overrides) -> PublishContent:
    base = dict(
        title="测试标题",
        body="正文",
        content_type=ContentType.VIDEO,
        videos=[video],
        tags=["话题一"],
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
    monkeypatch.setattr(settings, "douyin_open_api_asset_root", root)
    monkeypatch.setattr(settings, "douyin_open_api_timeout_seconds", 5)
    return root


def _video(root, name="a.mp4", size=4096) -> str:
    path = root / name
    path.write_bytes(b"\x00" * size)
    return str(path)


def _publisher(router: _Router) -> DouyinOpenApiPublisher:
    return DouyinOpenApiPublisher(transport=httpx.MockTransport(router))


# ---------------------------------------------------------------------------
# 请求形状 / happy path
# ---------------------------------------------------------------------------


def test_direct_upload_then_create_video_uses_the_documented_wire_shape(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _upload_ok()).on(
        m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID, video_id="7215132967188971234")
    )
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )

    assert result.success is True
    # The worker rewrites success without effect_applied into a failure.
    assert result.effect_applied is True
    assert result.outcome_uncertain is False
    assert result.platform_post_id == ITEM_ID
    # No official page documents a share-URL format, so none is invented.
    assert result.platform_url is None
    assert router.paths() == [m._UPLOAD_VIDEO_PATH, m._CREATE_VIDEO_PATH]

    upload, create = router.calls
    assert upload.headers["access-token"] == CREDENTIAL["access_token"]
    assert create.headers["access-token"] == CREDENTIAL["access_token"]
    assert upload.url.params["open_id"] == CREDENTIAL["open_id"]
    assert create.url.params["open_id"] == CREDENTIAL["open_id"]
    assert create.headers["content-type"] == "application/json"
    assert upload.headers["content-type"].startswith("multipart/form-data")
    body = json.loads(create.content)
    # video_id is the only required field; publish_way's enum values are not
    # documented, so it is omitted rather than guessed.
    assert body == {"video_id": VIDEO_ID, "text": "测试标题\n正文\n#话题一"}


def test_optional_extra_fields_map_to_their_official_body_names(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _upload_ok()).on(
        m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID)
    )
    content = _content(
        _video(assets),
        extra={"douyin_cover_tsp": 2.5, "douyin_private_status": 1, "douyin_download_type": 1},
    )
    asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), content))

    body = json.loads(router.calls[1].content)
    assert body == {
        "video_id": VIDEO_ID,
        "text": "测试标题\n正文\n#话题一",
        "cover_tsp": 2.5,
        "private_status": 1,
        "download_type": 1,
    }


def test_secrets_never_reach_the_url_error_or_raw_response(assets):
    secrets = ("access-token-value", "client-secret-value", "refresh-token-value")
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _upload_ok()).on(
        m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID)
    )
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )
    for request in router.calls:
        # open_id is a documented query parameter; tokens are header/body only.
        assert set(request.url.params) == {"open_id"}
        assert all(secret not in str(request.url) for secret in secrets)
    dumped = json.dumps(result.raw_response, ensure_ascii=False) + (result.error or "")
    assert all(secret not in dumped for secret in secrets)


def test_exception_text_is_not_surfaced(assets):
    # httpx exceptions can embed the request URL and headers.
    router = _Router().on(m._UPLOAD_VIDEO_PATH, httpx.ReadTimeout("boom access-token-value"))
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )
    assert result.success is False
    assert "access-token-value" not in (result.error or "")
    assert "boom" not in (result.error or "")


# ---------------------------------------------------------------------------
# 上传阶段：确定未发布
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, retryable",
    [
        (2190005, False),  # 文件太大了
        (2114021, False),  # 分片不能小于5MB
        (28003017, False),  # quota已用完
        (9999999, False),  # unlisted code: definitive, meaning unknown
        (28003018, True),  # 请求频率过高
        (2100004, True),  # 系统繁忙
    ],
)
def test_upload_platform_failures_never_reach_create_video(assets, code, retryable):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _err(code))
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is retryable
    assert router.paths() == [m._UPLOAD_VIDEO_PATH]
    assert result.raw_response["write_started"] is False
    assert result.raw_response["error_code"] == code


def test_upload_credential_error_asks_for_a_login(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _err(2190008))
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )

    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert "登录" in (result.error or "")


@pytest.mark.parametrize(
    "failure, retryable",
    [
        (httpx.ReadTimeout("slow"), True),
        (httpx.ConnectError("refused"), True),
        ("non-json", True),
    ],
)
def test_lost_upload_responses_are_safe_to_retry(assets, failure, retryable):
    handler = httpx.Response(502, text="<html>bad gateway</html>") if failure == "non-json" else failure
    router = _Router().on(m._UPLOAD_VIDEO_PATH, handler)
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )

    # An upload only creates an isolated file id, never a public post.
    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is retryable
    assert router.paths() == [m._UPLOAD_VIDEO_PATH]
    assert "bad gateway" not in (result.error or "")


# ---------------------------------------------------------------------------
# create_video：公开副作用的写边界
# ---------------------------------------------------------------------------


def _create_router(final) -> _Router:
    return _Router().on(m._UPLOAD_VIDEO_PATH, _upload_ok()).on(m._CREATE_VIDEO_PATH, final)


def _publish_to_create(assets, final):
    router = _create_router(final)
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
    )
    return result, router


@pytest.mark.parametrize("code", [2114006, 2114007, 2100005, 210005, 28001007, 9999999])
def test_create_video_definitive_rejection_is_a_safe_failure(assets, code):
    result, _router = _publish_to_create(assets, _err(code))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert result.raw_response["outcome"] == "rejected"


def test_create_video_credential_rejection_is_a_safe_failure(assets):
    result, _router = _publish_to_create(assets, _err(2190008))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is False


def test_create_video_rate_limit_is_a_safe_retryable_failure(assets):
    result, _router = _publish_to_create(assets, _err(28003018))

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is True


@pytest.mark.parametrize(
    "final",
    [
        _err(2100004),  # 系统繁忙 does not prove the item was not created
        _err(28001005),  # 系统内部错误
        httpx.ReadTimeout("slow"),
        httpx.Response(502, text="<html>bad gateway</html>"),
        _ok(),  # error_code 0 without item_id
        _ok(item_id="not a valid item id"),
        httpx.Response(200, json={"data": {}}),  # no error_code at all
    ],
    ids=["transient", "internal", "timeout", "non-json", "no-item-id", "bad-item-id", "no-code"],
)
def test_unreadable_create_video_outcomes_are_uncertain(assets, final):
    result, _router = _publish_to_create(assets, final)

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["needs_reconciliation"] is True
    assert "核验" in (result.error or "")


def test_create_video_that_never_left_the_machine_is_a_safe_failure(assets):
    result, _router = _publish_to_create(assets, httpx.ConnectError("refused"))

    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert result.raw_response["write_started"] is False


# ---------------------------------------------------------------------------
# 预检与让开
# ---------------------------------------------------------------------------


def _never_called(request):
    raise AssertionError("no request may be sent")


def test_browser_credentials_step_aside_for_the_next_adapter(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    result = asyncio.run(
        _publisher(router).publish(1, {"cookies": {"sessionid": "x"}}, _content(_video(assets)))
    )

    # An SAU account must keep its fallback and its retry budget.
    assert result.success is False
    assert result.effect_applied is False
    assert result.outcome_uncertain is False
    assert result.retryable is True
    assert router.calls == []


@pytest.mark.parametrize(
    "override",
    [
        {"client_secret": ""},
        {"open_id": ""},
        {"access_token": ""},
        {"access_token": None},
    ],
    ids=["no-secret", "no-open-id", "no-token", "token-none"],
)
def test_incomplete_grant_fails_before_the_network(assets, override):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    result = asyncio.run(
        _publisher(router).publish(1, {**CREDENTIAL, **override}, _content(_video(assets)))
    )

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert router.calls == []


def test_exact_approval_fails_closed(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    content = _content(_video(assets)).model_copy(update={"exact_approval": True})
    result = asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), content))

    assert result.success is False
    assert result.retryable is False
    assert "Agent contract rendering" in (result.error or "")
    assert router.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"images": ["/tmp/cover.jpg"]},
        {"videos": []},
        {"content_type": ContentType.IMAGE_TEXT},
        {"extra": {"unexpected": 1}},
        {"extra": {"douyin_download_type": 2}},
        {"extra": {"douyin_private_status": 3}},
        {"extra": {"douyin_private_status": True}},
        {"extra": {"douyin_cover_tsp": -1}},
        {"tags": ["two words"]},
        {"tags": ["a#b"]},
        {"title": "字" * 1001},
    ],
    ids=[
        "empty-title",
        "images",
        "no-video",
        "wrong-type",
        "unknown-extra",
        "download-type",
        "private-status",
        "bool-enum",
        "negative-cover",
        "tag-space",
        "tag-hash",
        "too-long",
    ],
)
def test_invalid_content_is_refused_before_the_network(assets, overrides):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets), **overrides))
    )

    assert result.success is False
    assert result.retryable is False
    assert router.calls == []


def test_valid_optional_enums_are_accepted(assets):
    for extra in (
        {},
        {"douyin_private_status": 0},
        {"douyin_private_status": 2, "douyin_download_type": 1, "douyin_cover_tsp": 0},
    ):
        assert m.validate_content(_content(_video(assets), extra=extra)) is None


def test_video_must_be_a_regular_file_inside_the_asset_root(assets, tmp_path, monkeypatch):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 64)
    symlink = assets / "link.mp4"
    real = assets / "real.mp4"
    real.write_bytes(b"\x00" * 64)
    symlink.symlink_to(real)
    (assets / "notes.txt").write_bytes(b"\x00" * 64)
    (assets / "empty.mp4").write_bytes(b"")

    monkeypatch.setattr(settings, "douyin_open_api_max_video_bytes", 32)
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    for path in (outside, symlink, assets / "notes.txt", assets / "empty.mp4", real):
        result = asyncio.run(
            _publisher(router).publish(1, dict(CREDENTIAL), _content(str(path)))
        )
        assert result.success is False, path
        assert result.retryable is False
    assert router.calls == []


@pytest.mark.parametrize("delta, chunked", [(0, False), (1, True)])
def test_direct_upload_limit_decides_the_path(assets, monkeypatch, delta, chunked):
    limit = 4096
    monkeypatch.setattr(settings, "douyin_open_api_direct_upload_bytes", limit)
    # Keep the chunk path valid for a tiny file by shrinking the part window.
    monkeypatch.setattr(m, "_PART_MIN_BYTES", 1)
    monkeypatch.setattr(settings, "douyin_open_api_part_bytes", 1500)
    captured: list[bytes] = []
    router = (
        _Router()
        .on(m._UPLOAD_VIDEO_PATH, _upload_ok())
        .on(m._INIT_PART_PATH, _ok(upload_id="@upload-id"))
        .on(
            m._UPLOAD_PART_PATH,
            lambda request: (captured.append(request.content), _ok())[1],
        )
        .on(m._COMPLETE_PART_PATH, _upload_ok())
        .on(m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID))
    )
    payload = bytes(index % 251 for index in range(limit + delta))
    video = assets / "limit.mp4"
    video.write_bytes(payload)

    result = asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), _content(str(video))))

    assert result.success is True, result.error
    assert (m._UPLOAD_VIDEO_PATH in router.paths()) is not chunked
    assert (m._INIT_PART_PATH in router.paths()) is chunked


def test_chunked_upload_parts_are_numbered_from_one_and_reassemble(assets, monkeypatch):
    monkeypatch.setattr(settings, "douyin_open_api_direct_upload_bytes", 1024)
    monkeypatch.setattr(m, "_PART_MIN_BYTES", 1)
    monkeypatch.setattr(settings, "douyin_open_api_part_bytes", 3 * 1024)
    payload = bytes(index % 251 for index in range(7 * 1024))
    video = assets / "chunked.mp4"
    video.write_bytes(payload)
    parts: dict[int, bytes] = {}

    def part_handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["part_number"])
        raw = request.content
        head = raw.index(b"\r\n\r\n") + 4
        tail = raw.rindex(b"\r\n--")
        parts[number] = raw[head:tail]
        return _ok()

    router = (
        _Router()
        .on(m._UPLOAD_VIDEO_PATH, _never_called)
        .on(m._INIT_PART_PATH, _ok(upload_id="@upload-id"))
        .on(m._UPLOAD_PART_PATH, part_handler)
        .on(m._COMPLETE_PART_PATH, _upload_ok())
        .on(m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID))
    )
    result = asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), _content(str(video))))

    assert result.success is True, result.error
    assert router.paths() == (
        [m._INIT_PART_PATH] + [m._UPLOAD_PART_PATH] * 3 + [m._COMPLETE_PART_PATH, m._CREATE_VIDEO_PATH]
    )
    assert sorted(parts) == [1, 2, 3]
    assert b"".join(parts[number] for number in sorted(parts)) == payload
    assert max(map(len, parts.values())) - min(map(len, parts.values())) <= 1
    init, complete = router.calls[0], router.calls[-2]
    # init hands out the id; complete and every part send it back in the query.
    assert dict(init.url.params) == {"open_id": CREDENTIAL["open_id"]}
    assert complete.url.params["upload_id"] == "@upload-id"
    for request in router.calls:
        assert request.url.params["open_id"] == CREDENTIAL["open_id"]


def test_failed_part_stops_before_complete_and_create(assets, monkeypatch):
    monkeypatch.setattr(settings, "douyin_open_api_direct_upload_bytes", 1024)
    monkeypatch.setattr(m, "_PART_MIN_BYTES", 1)
    monkeypatch.setattr(settings, "douyin_open_api_part_bytes", 3 * 1024)
    video = assets / "chunked.mp4"
    video.write_bytes(b"\x00" * (7 * 1024))
    seen: list[int] = []
    router = (
        _Router()
        .on(m._INIT_PART_PATH, _ok(upload_id="@upload-id"))
        .on(m._UPLOAD_PART_PATH, lambda request: (seen.append(1), _err(2114020))[1])
        .on(m._COMPLETE_PART_PATH, _never_called)
        .on(m._CREATE_VIDEO_PATH, _never_called)
    )

    result = asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), _content(str(video))))

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert router.paths() == [m._INIT_PART_PATH, m._UPLOAD_PART_PATH]
    assert result.raw_response["write_started"] is False


# ---------------------------------------------------------------------------
# login / health_check：/oauth/refresh_token/
# ---------------------------------------------------------------------------

REFRESH_OK = {
    "data": {
        "access_token": "new-access-token-value",
        "error_code": 0,
        "expires_in": "86400",
        "open_id": "open-id-value",
        "refresh_token": "refresh-token-value",
    },
    "message": "success",
}


def _oauth_router(response) -> _Router:
    return _Router().on(m._REFRESH_TOKEN_PATH, response)


def test_login_refreshes_the_access_token_in_place(assets):
    router = _oauth_router(httpx.Response(200, json=REFRESH_OK))
    credential = dict(CREDENTIAL, access_token=None, access_token_expires_at=None)
    del credential["open_id"]  # a grant stored without it is completed by the refresh

    ok = asyncio.run(_publisher(router).login(1, credential))

    assert ok is True
    request = router.calls[0]
    assert request.method == "POST"
    # Official contract: form-encoded body, the documented field names.
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(httpx.QueryParams(request.content)) == {
        "client_key": CREDENTIAL["client_key"],
        "client_secret": CREDENTIAL["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": CREDENTIAL["refresh_token"],
    }
    # Secrets travel in the body only, never in the URL.
    assert str(request.url) == m._API_BASE + m._REFRESH_TOKEN_PATH
    assert credential["access_token"] == "new-access-token-value"
    assert credential["open_id"] == "open-id-value"
    # The endpoint does not rotate the refresh token.
    assert credential["refresh_token"] == CREDENTIAL["refresh_token"]
    assert credential["access_token_expires_at"]


@pytest.mark.parametrize(
    "response, code_hint",
    [
        ({"data": {"error_code": 10010}}, "10010"),
        ({"data": {"error_code": 10013}}, "10013"),
        ({"data": {"error_code": 424242}}, "424242"),  # unlisted: still a credential verdict
    ],
    ids=["expired", "bad-secret", "unlisted"],
)
def test_login_reports_credential_failures_without_touching_the_token(
    assets, response, code_hint
):
    router = _oauth_router(httpx.Response(200, json=response))
    credential = dict(CREDENTIAL)
    before = dict(credential)

    ok = asyncio.run(_publisher(router).login(1, credential))

    assert ok is False
    assert credential == before
    assert code_hint in (_publisher(router).last_login_error or "") or code_hint in (
        DouyinOpenApiPublisher._login_error("credential", int(code_hint))
    )


def test_login_expired_refresh_token_explains_the_next_step(assets):
    router = _oauth_router(httpx.Response(200, json={"data": {"error_code": 10010}}))
    publisher = _publisher(router)

    assert asyncio.run(publisher.login(1, dict(CREDENTIAL))) is False
    assert "重新完成用户授权" in (publisher.last_login_error or "")


@pytest.mark.parametrize(
    "response",
    [
        {"data": {"error_code": 10001}},  # 系统错误
        {"data": {"error_code": 0}},  # no token
        {"data": {}},  # no code
        "non-json",
        httpx.ReadTimeout("slow"),
    ],
    ids=["transient", "no-token", "no-code", "non-json", "timeout"],
)
def test_login_transient_or_unreadable_refresh_leaves_the_credential_alone(
    assets, response
):
    handler = httpx.Response(502, text="<html/>") if response == "non-json" else response
    router = _oauth_router(handler)
    credential = dict(CREDENTIAL)
    before = dict(credential)

    assert asyncio.run(_publisher(router).login(1, credential)) is False
    assert credential == before


def test_login_with_an_incomplete_grant_does_not_call_the_network(assets):
    router = _oauth_router(_never_called)
    publisher = _publisher(router)

    assert asyncio.run(publisher.login(1, {"client_key": "k"})) is False
    assert "client_secret" in (publisher.last_login_error or "")
    assert router.calls == []


def test_health_steps_aside_for_browser_and_incomplete_credentials(assets):
    router = _oauth_router(_never_called)
    publisher = _publisher(router)

    browser = asyncio.run(publisher.health_check(1, {"cookies": {}}))
    incomplete = asyncio.run(
        publisher.health_check(1, {"client_key": "k", "refresh_token": "r"})
    )
    # UNKNOWN lets the scheduler's probe continue to the next adapter.
    assert browser is AccountHealth.UNKNOWN
    assert incomplete is AccountHealth.UNKNOWN
    assert router.calls == []


@pytest.mark.parametrize(
    "response, expected",
    [
        (REFRESH_OK, AccountHealth.HEALTHY),
        ({"data": {"error_code": 10010}}, AccountHealth.EXPIRED),
        ({"data": {"error_code": 10001}}, AccountHealth.UNKNOWN),
        ("non-json", AccountHealth.UNKNOWN),
    ],
    ids=["healthy", "expired", "transient", "unreadable"],
)
def test_health_maps_the_refresh_verdict(assets, response, expected):
    handler = httpx.Response(502, text="x") if response == "non-json" else httpx.Response(200, json=response)
    publisher = _publisher(_oauth_router(handler))

    assert asyncio.run(publisher.health_check(1, dict(CREDENTIAL))) is expected


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_classify_response_orders_credential_before_transient():
    assert m.classify_response({"data": {"error_code": 0}, "extra": {"error_code": 0}}) == ("ok", 0)
    assert m.classify_response({"data": {"error_code": 2190008}}) == ("credential", 2190008)
    assert m.classify_response({"data": {"error_code": 28003018}}) == ("rate_limited", 28003018)
    assert m.classify_response({"data": {"error_code": 2100004}}) == ("transient", 2100004)
    assert m.classify_response({"data": {"error_code": 2114021}}) == ("rejected", 2114021)
    assert m.classify_response({"data": {"error_code": 7777777}}) == ("rejected", 7777777)
    assert m.classify_response({"data": {}}) == ("invalid", None)
    assert m.classify_response(None) == ("invalid", None)
    # A bool is never an error code.
    assert m.classify_response({"data": {"error_code": True}}) == ("invalid", None)
    # ``data`` wins over ``extra`` when they disagree.
    assert m.classify_response({"data": {"error_code": 0}, "extra": {"error_code": 2190008}}) == ("ok", 0)


def test_classify_refresh_uses_the_oauth_code_table():
    assert m.classify_refresh(REFRESH_OK) == ("ok", 0)
    assert m.classify_refresh({"data": {"error_code": 10001}}) == ("transient", 10001)
    assert m.classify_refresh({"data": {"error_code": 10010}}) == ("credential", 10010)
    assert m.classify_refresh({"data": {"access_token": "t"}}) == ("invalid", None)
    assert m.classify_refresh({"data": {"error_code": 0}}) == ("invalid", None)


@pytest.mark.parametrize(
    "tags, expected",
    [
        (["a", "b"], "#a #b"),
        (["#a", "##b"], "#a #b"),
        ([], ""),
    ],
)
def test_hashtags_are_inlined_into_the_text(tags, expected):
    content = _content("unused.mp4", tags=tags)
    tail = f"\n{expected}" if expected else ""
    assert m.build_publish_text(content) == f"测试标题\n正文{tail}"


def test_part_boundaries_keep_every_part_inside_the_platform_window():
    for total in range(300 * MB + 1, 320 * MB, 7919 * 101):
        for part in (5 * MB, 10 * MB, 20 * MB, 100 * MB):
            parts = m.part_boundaries(total, part)
            assert parts, (total, part)
            assert sum(length for _, length in parts) == total
            offset = 0
            for start, length in parts:
                assert start == offset
                offset += length
                assert 5 * MB <= length <= 100 * MB, (total, part, length)
    # Whole-file and empty inputs.
    assert m.part_boundaries(20 * MB, 20 * MB) == [(0, 20 * MB)]
    assert m.part_boundaries(0, 20 * MB) == []


def test_target_is_honoured_when_it_clears_the_platform_minimum():
    for total in (301 * MB, 400 * MB + 3, 1000 * MB, 4 * 1024 * MB):
        for part in (10 * MB, 20 * MB, 100 * MB):
            parts = m.part_boundaries(total, part)
            assert len(parts) == -(-total // part)
            assert max(length for _, length in parts) <= part


def test_target_bending_on_the_minimum_yields_to_the_minimum():
    """The 5 MB floor is a platform rule (2114021); the target is a preference.

    301 MB at a 5 MB target splits into 61 parts of 4.94 MB - every part, not
    just a tail, is under the floor.  The part count is reduced until each
    part clears it, which makes the parts slightly larger than the target.
    """

    total, part = 301 * MB, 5 * MB
    parts = m.part_boundaries(total, part)

    assert len(parts) == 60
    assert sum(length for _, length in parts) == total
    assert min(length for _, length in parts) >= 5 * MB
    # The overshoot is a fraction of the target, nowhere near the 100 MB ceiling.
    assert max(length for _, length in parts) < part * 1.1


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_publisher_identity():
    publisher = DouyinOpenApiPublisher(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    assert publisher.platform is Platform.DOUYIN
    assert publisher.kind is PublisherKind.DOUYIN_OPEN_API
    # No audited exact renderer: Agent planning must fail closed for DOUYIN.
    assert publisher.agent_contract_renderer_descriptor is None


def test_registry_gate_and_priority_over_sau(monkeypatch):
    from ai_ops.core.enums import PublisherKind as Kind
    from ai_ops.publishers.registry import build_default_registry
    from ai_ops.publishers.social_auto_upload import SAU_PLATFORM_MAP

    assert Platform.DOUYIN in SAU_PLATFORM_MAP
    monkeypatch.setattr(settings, "douyin_open_api_enabled", False)
    kinds = [p.kind for p in build_default_registry().resolve(Platform.DOUYIN)]
    assert Kind.DOUYIN_OPEN_API not in kinds

    monkeypatch.setattr(settings, "douyin_open_api_enabled", True)
    kinds = [p.kind for p in build_default_registry().resolve(Platform.DOUYIN)]
    # The API adapter is tried first; SAU remains as the fallback.
    assert kinds[0] is Kind.DOUYIN_OPEN_API
    assert kinds[-1] is Kind.SOCIAL_AUTO_UPLOAD


# ---------------------------------------------------------------------------
# publish() 不得抛异常
#
# worker 把任何逃出 publish() 的异常都当成「结果未知、永久不可重试」。
# 所以一个纯本地问题（文件不可读、凭证里有个错字符）如果不在这里被
# 转成普通的失败结果，就会让一个根本没发出请求的任务永久卡死。
# ---------------------------------------------------------------------------


def test_unreadable_video_is_a_plain_failure(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    root.mkdir()
    video = root / "locked.mp4"
    video.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(settings, "douyin_open_api_asset_root", root)
    monkeypatch.setattr(settings, "douyin_open_api_timeout_seconds", 5)
    video.chmod(0)
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)

    result = asyncio.run(_publisher(router).publish(1, dict(CREDENTIAL), _content(str(video))))

    video.chmod(0o644)  # let tmp_path clean up
    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert router.calls == []


def test_video_removed_after_the_upload_does_not_affect_the_write(assets):
    # The write path must not touch the file again once the bytes are sent.
    victim = assets / "gone.mp4"
    victim.write_bytes(b"\x00" * 64)

    def upload_then_vanish(request: httpx.Request) -> httpx.Response:
        victim.unlink()
        return _upload_ok()

    router = (
        _Router()
        .on(m._UPLOAD_VIDEO_PATH, upload_then_vanish)
        .on(m._CREATE_VIDEO_PATH, _ok(item_id=ITEM_ID))
    )

    result = asyncio.run(
        _publisher(router).publish(1, dict(CREDENTIAL), _content(str(victim)))
    )

    assert result.success is True
    assert result.platform_post_id == ITEM_ID
    assert result.raw_response["video_bytes"] == 64


@pytest.mark.parametrize(
    "override",
    [
        {"access_token": "tok中"},  # non-ASCII header value
        {"open_id": "has space"},
        {"client_secret": {"a": 1}},  # not a string at all
        {"client_key": 12345},
        {"refresh_token": "line\nbreak"},
        {"access_token": "tab\there"},
    ],
    ids=["non-ascii", "space", "dict", "int", "newline", "tab"],
)
def test_wire_unsafe_credentials_are_refused_before_the_network(assets, override):
    # Left to httpx these fail inside the request as an encoding error, which
    # looks like a network fault - or, on the write path, an unknown outcome.
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    result = asyncio.run(
        _publisher(router).publish(1, {**CREDENTIAL, **override}, _content(_video(assets)))
    )

    assert result.success is False
    assert result.outcome_uncertain is False
    assert result.retryable is False
    assert "ASCII" in (result.error or "")
    assert router.calls == []


@pytest.mark.parametrize("field", ["client_key", "client_secret", "refresh_token"])
def test_login_and_health_refuse_wire_unsafe_values(assets, field):
    router = _oauth_router(_never_called)
    publisher = _publisher(router)
    credential = {**CREDENTIAL, field: {"not": "a string"}}

    assert asyncio.run(publisher.login(1, credential)) is False
    assert "ASCII" in (publisher.last_login_error or "")
    assert asyncio.run(publisher.health_check(1, credential)) is AccountHealth.UNKNOWN
    assert router.calls == []


def test_non_httperror_transport_exceptions_never_escape(assets):
    # httpx.InvalidURL / httpx.StreamError are plain Exceptions, not HTTPError.
    for exception in (httpx.StreamError("stream"), httpx.InvalidURL("bad")):
        router = _Router().on(m._UPLOAD_VIDEO_PATH, exception)
        result = asyncio.run(
            _publisher(router).publish(1, dict(CREDENTIAL), _content(_video(assets)))
        )
        assert result.success is False
        assert result.outcome_uncertain is False
        assert result.retryable is True


def test_float_enums_and_non_finite_cover_are_refused(assets):
    router = _Router().on(m._UPLOAD_VIDEO_PATH, _never_called)
    for extra in (
        {"douyin_private_status": 1.0},
        {"douyin_download_type": 0.0},
        {"douyin_cover_tsp": float("nan")},
        {"douyin_cover_tsp": float("inf")},
    ):
        result = asyncio.run(
            _publisher(router).publish(
                1, dict(CREDENTIAL), _content(_video(assets), extra=extra)
            )
        )
        assert result.success is False, extra
        assert result.retryable is False
    assert router.calls == []
