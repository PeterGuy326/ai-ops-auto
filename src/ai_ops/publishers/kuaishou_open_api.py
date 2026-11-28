"""Feature-gated adapter for the Kuaishou Open Platform video publish API.

This is the documented official API on ``https://open.kuaishou.com`` (scope
``user_video_publish``), not a consumer endpoint.  The flow is (官方文档
「创建视频」docId 12):

  1. ``POST /openapi/photo/start_upload``      -> ``upload_token`` + ``endpoint``
  2. upload the bytes to ``http://{endpoint}/api/upload`` (files under 10 MB)
     or ``/api/upload/fragment`` xN + ``/api/upload/complete`` (larger files)
  3. ``POST /openapi/photo/publish``           multipart ``cover`` + ``caption``
     -> ``video_info.photo_id``

**Eligibility is the real gate, not the code.**  Onboarding needs developer
qualification review (企业/法人/管理员信息, platform-audited), an audited web
app, and an approved interface-permission request for the publish scope
(docId 2, 3, 30).  The adapter is default-off and exists for operators who
hold that grant.

Audited wire facts (from the official document centre, fetched 2026-11-28):

* ``access_token`` and ``app_id`` travel in the URL query string of every
  ``openapi`` call.  ``app_secret`` only ever appears in the two OAuth calls.
* Success is a top-level ``result == 1``; the HTTP status line carries no
  signal.  Error codes are a top-level non-1 ``result`` plus ``error_msg``.
* ``cover`` (a jpg file, under 10 MB) and ``caption`` are both *required* on
  publish.  There is no tags field: hashtags are inline ``#tag`` tokens in
  the caption.  No caption length limit is documented; the platform's own
  ``100100400`` (异常参数) arbitrates.
* The upload gateway is plain ``http://{endpoint}/...`` by official design
  (docId 12: "上传接口 均为 http://{host}/...").  The video bytes therefore
  cross the network unencrypted, with ``upload_token`` in the URL.  The
  adapter follows the documented scheme rather than guessing at https: the
  gateway's TLS support is undocumented, and an invented scheme would fail
  every upload.  Operators who need encrypted transit must not enable it.
* ``fragment_id`` is 0-based (docId 12, and docId 30 FAQ) and
  ``fragment_count`` is the total; fragments should not exceed 10 MB.
* ``pending`` (still processing, not watchable) and ``play_url`` (absent
  until transcoding finishes) are response fields.  The adapter does not
  poll ``/openapi/photo/info``: nothing documents it as required, it needs
  the extra ``user_video_info`` scope, and the post already exists by then.

Token handling differs from the Douyin adapter in one decisive way.
Kuaishou's ``access_token`` lasts 48 hours and its ``refresh_token`` is
**single-use**: ``/oauth2/refresh_token`` returns a new refresh token and
retires the old one at once (docId 8, "老的refresh_token随即失效").  In this
code base only ``login()`` has its credential writes persisted (the API
layer re-encrypts the dict); ``publish()`` and ``health_check()`` mutations
are dropped.  So:

* ``login()`` is the only place a refresh happens, and it writes back the
  new ``access_token`` *and* the new ``refresh_token`` together.
* ``publish()`` only reads the stored access token.  An expired token is
  reported as "needs login", never refreshed - a refresh there would retire
  the stored refresh token and lock the account out of its next login.
* ``health_check()`` is a read-only probe (``/openapi/photo/count``).  The
  daily health scheduler does not persist credential changes, so refreshing
  in it would retire the stored refresh token every night.

Consequence for operators: nothing in this repository refreshes a Kuaishou
token automatically.  ``POST /accounts/{id}/login`` must be called within
every 48-hour window, or publishes fail with "needs login" (and fall back to
the social-auto-upload browser path when that account has one).

Result semantics (the worker stops fallback and retry on ``outcome_uncertain``):

======================  =================  =================  ==========
stage / failure         effect_applied    outcome_uncertain  retryable
======================  =================  =================  ==========
validation              False             False              False
not this adapter's      False             False              True
credential
upload, definitive      False             False              False
upload, unknown/lost    False             False              True
publish, refused        False             False              False
publish, limited       False             False              False
publish, unknown       True               True               False
publish, ok             True               False              -
======================  =================  =================  ==========

Upload steps create only an isolated upload token, never a public post,
which is why their failures are never ``outcome_uncertain``.

**Stepping aside.**  When the stored credential is not an Open Platform grant,
``health_check`` returns ``UNKNOWN`` (the scheduler's probe continues to the
next adapter) and ``publish`` returns a plain, fallback-able failure.

**Transport safety.**  httpx exception text can embed a full URL, and here
every URL carries ``access_token`` / ``upload_token`` in its query string.
No exception message is ever surfaced; every transport failure becomes a
value.  The upload ``endpoint`` comes from a server response and is used to
build a URL, so it must be a bare hostname before anything is sent to it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Any

import httpx

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..observability.structured_logging import cap_httpx_logger
from ..runtime.receipts import write_publish_receipt
from .base import AgentContractRendererUnavailable, PublisherBase

_API_BASE = "https://open.kuaishou.com"
_START_UPLOAD_PATH = "/openapi/photo/start_upload"
_PUBLISH_PATH = "/openapi/photo/publish"
_PHOTO_COUNT_PATH = "/openapi/photo/count"
_REFRESH_TOKEN_PATH = "/oauth2/refresh_token"

_UPLOAD_DIRECT_PATH = "/api/upload"
_UPLOAD_FRAGMENT_PATH = "/api/upload/fragment"
_UPLOAD_COMPLETE_PATH = "/api/upload/complete"

# 官方：直接上传适合小于 10M 的文件；分片建议不大于 10M。
_DIRECT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

# ``cover``: 封面图(<10MB) (docId 12); 服务端 SDK: 请使用jpg文件 (docId 42).
_COVER_MAX_BYTES = 10 * 1024 * 1024
_COVER_SUFFIXES = {".jpg", ".jpeg"}
_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi"}
_ALLOWED_EXTRA_KEYS = ("kuaishou_cover",)
_COVER_EXTRA_KEY = "kuaishou_cover"

# OAuth refresh needs these; the openapi calls need ``app_id`` + ``access_token``.
_REFRESH_FIELDS = ("app_id", "app_secret", "refresh_token")
# Everything this adapter sends on the wire, as a query / form value.
_WIRE_FIELDS = _REFRESH_FIELDS + ("access_token",)

# Official error-code tables (docId 19 「错误码」, docId 12 「创建视频」).
# Token/capability problems: refused before any write.
_CREDENTIAL_ERROR_CODES = frozenset(
    {
        100200102,  # 请求被拒绝，可能是无效的 token 等
        100200108,  # access_token过期
        100200109,  # 用户取消该 app 授权
        100200110,  # 用户授权过期
        100200111,  # 用户未授权过
        100200113,  # refresh_token过期
    }
)
# 服务内部错误: reported failure.  Repeatable for an upload step; UNKNOWN for
# the publish step, because "internal error" does not prove nothing was created.
_TRANSIENT_ERROR_CODES = frozenset({100200500})
# 100100402 (docId 12): 1. 用户被封禁; 2. 用户一天请求该接口超过1k次;
# 3. app一天请求该接口超过10w次.  One code covers a ban and rate limits, and
# the response does not say which.  A ban is permanent for the account, so the
# conservative reading is "definitive refusal, do not retry automatically".
_LIMITED_OR_BANNED_CODE = 100100402
# Definitive rejections: the platform refused and nothing was published.
# 100120003 (视频发布失败) is included although it sounds transient: the
# platform states the publish failed, and an immediate retry of the identical
# request is what the daily-request limits punish.
_REJECTED_ERROR_CODES = frozenset(
    {
        100120001,  # 视频不存在
        100120002,  # 视频未上传
        100120003,  # 视频发布失败
        100100400,  # 异常参数
        100200100,  # 请求缺少参数或参数类型错误
        100200101,  # 无效的client / app / developer
        100200104,  # 请求的 grantType 不支持
        100200106,  # 请求的 scope 错误
        100200107,  # 无效的 openid
    }
)

# 官方「状态含义」 of /openapi/photo/info (docId 14): the same three content
# codes, without the leading ``1001``.  Only used by the read-only health probe.
_HEALTH_REJECTED_ERROR_CODES = frozenset({120001, 120002, 120003, 100400})

# ``fragment_id`` is 0-based (docId 12; docId 30 FAQ).
_FRAGMENT_ID_STARTS_AT = 0

# Platform-issued ids are short alphanumeric tokens (e.g. ``3xwn3kkerxj6g9n``).
# Tight on purpose: the id is persisted as ``platform_post_id`` and rendered
# into notifications and reports.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Every value this adapter puts in a query string must be wire-safe.  Platform
# tokens are printable ASCII; anything else is a configuration mistake, and
# left to httpx it would surface as an encoding error that looks like a
# network fault.
_WIRE_SAFE_RE = re.compile(r"^[\x21-\x7e]+$")
# The upload ``endpoint`` comes back in the start_upload response and is used
# to build a URL.  It must be a bare hostname with at least one dot: no
# scheme, userinfo, port, path or IP literal, so a tampered reply cannot
# redirect the video bytes (and the upload token) to an arbitrary address.
# The real gateway is a platform domain such as ``uploader.test.gifshow.com``.
_UPLOAD_ENDPOINT_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
# A hostname label that a resolver may read as a number: decimal (``127``),
# hex (``0x7f``) or a dotted form made only of such labels.  getaddrinfo
# turns ``0x7f.1``, ``127.1`` and ``0x7f000001`` into 127.0.0.1, none of
# which ``ipaddress`` recognises, so the shape check alone lets them through.
_NUMERIC_LABEL_RE = re.compile(r"^(?:\d+|0[xX][0-9a-fA-F]+)$")
# ``play_url`` is returned by the platform and persisted as ``platform_url``.
_URL_RE = re.compile(r"^https?://[^\s\x00-\x1f\x7f\"'<>\\]+$")

# Operator-facing step names for error messages.  API paths are kept out of
# user-visible text because they carry no meaning to the account operator.
_UPLOAD_STEP_LABELS = {
    _START_UPLOAD_PATH: "发起上传",
    _UPLOAD_DIRECT_PATH: "整体上传",
    _UPLOAD_FRAGMENT_PATH: "分片上传",
    _UPLOAD_COMPLETE_PATH: "分片合并",
    _PUBLISH_PATH: "发布作品",
}

def _int_or_none(value: Any) -> int | None:
    """Accept an int, or a numeric string (``"1"``).  ``True`` is not ``1`` here."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_api_json(payload: bytes | str) -> dict | None:
    """Kuaishou answers a JSON object; anything else is an unknown outcome."""

    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _result_code(data: dict | None) -> int | None:
    """The top-level ``result`` field, as an int when it is one."""

    if not isinstance(data, dict):
        return None
    return _int_or_none(data.get("result"))


def classify_response(data: dict | None) -> tuple[str, int | None]:
    """Return ``(kind, result_code)`` for a parsed platform response.

    ``kind`` is one of ``ok`` / ``credential`` / ``limited`` / ``transient`` /
    ``rejected`` / ``invalid``.  The platform's ``error_msg`` text is
    deliberately not returned: it can echo request data.

    An unlisted non-1 ``result`` is treated as ``rejected``.  It is a
    definitive answer from the platform, so the request was refused; since
    its meaning is unknown the caller decides retryability per stage.
    """

    if data is None:
        return "invalid", None
    code = _result_code(data)
    if code is None:
        return "invalid", None
    if code == 1:
        return "ok", 1
    if code in _CREDENTIAL_ERROR_CODES:
        return "credential", code
    if code == _LIMITED_OR_BANNED_CODE:
        return "limited", code
    if code in _TRANSIENT_ERROR_CODES:
        return "transient", code
    return "rejected", code


def classify_refresh(data: dict | None) -> tuple[str, int | None]:
    """Classify an ``/oauth2/refresh_token`` response as ``(kind, result_code)``.

    ``kind``: ``ok`` | ``credential`` | ``transient`` | ``invalid``.

    ``ok`` requires ``result == 1`` *and* both a new ``access_token`` and a
    new ``refresh_token``.  Requiring the refresh token matters more here
    than on Douyin: Kuaishou retires the old refresh token the moment this
    call succeeds, so a reply without its replacement would leave the
    account with no usable grant.  Such a reply is reported as ``invalid``
    (nothing is written back) and ``login()`` tells the operator that the
    stored refresh token may have been consumed and a re-authorisation may
    be needed.
    """

    if data is None:
        return "invalid", None
    code = _result_code(data)
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if (
        code == 1
        and isinstance(access, str)
        and access
        and isinstance(refresh, str)
        and refresh
    ):
        return "ok", 1
    if code is None or code == 1:
        return "invalid", None
    if code in _TRANSIENT_ERROR_CODES:
        return "transient", code
    # A listed credential code, or any unlisted non-1 answer.
    return "credential", code


def _hashtag_tags(content: PublishContent) -> list[str]:
    """Normalise tags to hashtag tokens for the caption.

    A leading ``#`` is accepted as the operator's way of writing a hashtag;
    anything else that would break the inline ``#token`` syntax is refused so
    the caller fails before the upload rather than after it.
    """

    tags: list[str] = []
    for raw in content.tags:
        tag = str(raw).strip()
        while tag.startswith("#"):
            tag = tag[1:]
        if not tag:
            raise ValueError("快手话题标签不能为空")
        if any(ch.isspace() for ch in tag) or "#" in tag or "@" in tag:
            raise ValueError("快手话题标签不能包含空格、# 或 @")
        tags.append(tag)
    return tags


def build_caption(content: PublishContent) -> str:
    """Title, optional body, and inline ``#hashtags``.

    Kuaishou has no tags field: tags are ``#tag`` tokens inside ``caption``.
    Raises ``ValueError`` for an unusable tag; ``validate_content`` makes
    that a pre-flight error, so by the time this is called the tags are good.
    """

    parts = [content.title.strip()]
    body = content.body.strip()
    if body:
        parts.append(body)
    tags = _hashtag_tags(content)
    if tags:
        parts.append(" ".join(f"#{tag}" for tag in tags))
    return "\n".join(part for part in parts if part)


def is_upload_endpoint(value: object) -> bool:
    """True only for a bare DNS hostname such as ``uploader.example.com``.

    The ``endpoint`` of a ``start_upload`` reply decides where the video
    bytes - and ``upload_token`` - are sent, over plain http.  A tampered or
    malformed reply must not turn into a request to an arbitrary address, so
    everything but a dotted hostname is refused: a scheme, userinfo, port or
    path (rejected by the shape), and an IPv4 / IPv6 literal (which the shape
    alone lets through, e.g. ``169.254.169.254``).

    Beyond the dotted shape, any label that a resolver could read as a number
    is refused - decimal or hex - because ``getaddrinfo`` turns non-standard
    IPv4 spellings such as ``0x7f.1``, ``127.1`` and ``0x7f000001`` into
    ``127.0.0.1`` while ``ipaddress`` does not recognise them.  The cost is
    that a legitimate hostname with an all-numeric label (``123.example.com``)
    is refused too; the platform's upload gateway is not named that way, and
    a refusal is a retryable upload failure that falls back to the next
    adapter.

    What this does *not* do is validate where the hostname resolves: a
    platform domain that resolves to a private address is not caught here.
    """

    if not isinstance(value, str) or not _UPLOAD_ENDPOINT_RE.fullmatch(value):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass  # not an IP literal: a name, checked label by label below
    else:
        return False
    return not any(_NUMERIC_LABEL_RE.fullmatch(label) for label in value.split("."))


def extract_upload_endpoint(data: dict | None) -> tuple[str | None, str | None]:
    """``(upload_token, endpoint)`` from a ``start_upload`` response."""

    if not isinstance(data, dict):
        return None, None
    token = data.get("upload_token")
    endpoint = data.get("endpoint")
    token_ok = isinstance(token, str) and bool(_WIRE_SAFE_RE.fullmatch(token))
    return (
        token if token_ok else None,
        endpoint if is_upload_endpoint(endpoint) else None,
    )


def extract_photo(data: dict | None) -> tuple[str | None, str | None]:
    """``(photo_id, play_url)`` from a ``publish`` response.

    ``play_url`` is ``None`` unless it is an absolute http(s) URL: it is
    absent while the video is still processing (docId 42), and an empty or
    malformed value must not be persisted as ``platform_url``.
    """

    body = data.get("video_info") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return None, None
    photo_id = body.get("photo_id")
    play_url = body.get("play_url")
    if isinstance(photo_id, str) and _ID_RE.fullmatch(photo_id):
        found_id: str | None = photo_id
    else:
        found_id = None
    if isinstance(play_url, str) and _URL_RE.fullmatch(play_url):
        found_url: str | None = play_url
    else:
        found_url = None
    return found_id, found_url


def wire_unsafe_credential_fields(credential: dict) -> list[str]:
    """Names of the credential fields that are set but not plain printable ASCII."""

    bad: list[str] = []
    for field in _WIRE_FIELDS:
        value = (credential or {}).get(field)
        if value is not None and (
            not isinstance(value, str) or not _WIRE_SAFE_RE.fullmatch(value)
        ):
            bad.append(field)
    return bad


def is_api_credential(credential: dict | None) -> bool:
    """True when the stored credential is meant to be a Kuaishou Open Platform grant.

    Decided by the *presence* of the grant's keys, not by whether their values
    are well-formed.  A browser/cookie credential for the social-auto-upload
    path has neither key, and this adapter then steps aside (``UNKNOWN``
    health, fallback-able publish failure) instead of declaring the account
    expired.  But ``{"app_id": 12345, ...}`` is a misconfigured app grant,
    not a browser session: stepping aside there would route the job to
    social-auto-upload, which cannot use it, and would hide the real mistake.
    """

    cred = credential or {}
    return "app_id" in cred or "refresh_token" in cred


def safe_error_detail(kind: str, error_code: int | None) -> dict[str, Any]:
    """Credential-free detail block for ``raw_response`` / receipts."""

    return {"error_kind": kind, "error_code": error_code}


def fragment_boundaries(total_bytes: int, fragment_bytes: int) -> list[tuple[int, int]]:
    """``(offset, length)`` for every fragment.

    ``fragment_bytes`` is the operator's target size (at most 10 MB by the
    setting's bound).  The file is split into ``ceil(total / fragment_bytes)``
    near-equal fragments, so no fragment exceeds the target and there is no
    short tail.  Returns ``[]`` for an empty input.
    """

    if total_bytes <= 0 or fragment_bytes <= 0:
        return []
    if total_bytes <= fragment_bytes:
        return [(0, total_bytes)]
    count = -(-total_bytes // fragment_bytes)
    base, remainder = divmod(total_bytes, count)
    boundaries: list[tuple[int, int]] = []
    offset = 0
    for index in range(count):
        length = base + (1 if index < remainder else 0)
        boundaries.append((offset, length))
        offset += length
    return boundaries


def validate_content(content: PublishContent) -> str | None:
    """Return an error string, or ``None`` when the content is publishable."""

    if content.content_type != ContentType.VIDEO or len(content.videos) != 1:
        return "快手 API 当前要求恰好一个本地视频文件"
    if content.images:
        return "快手视频发布不支持图片资产"
    title = content.title.strip()
    if not title:
        return "快手标题不能为空"
    if "\x00" in title or "\x00" in content.body:
        return "快手标题或正文含 NUL 字符"
    extra = content.extra or {}
    unknown = set(extra).difference(_ALLOWED_EXTRA_KEYS)
    if unknown:
        return f"快手 API 不支持的 extra 字段: {sorted(unknown)}"
    cover = extra.get(_COVER_EXTRA_KEY)
    if not isinstance(cover, str) or not cover.strip():
        return "快手发布必须提供封面图（extra.kuaishou_cover，本地 jpg 路径）"
    try:
        _hashtag_tags(content)
    except ValueError as exc:
        return str(exc)
    return None

class KuaishouOpenApiPublisher(PublisherBase):
    """快手开放平台官方视频发布 canary（默认关闭）。

    凭证字段（由账号管理面加密存储）::

        app_id          开放平台应用的唯一标识
        app_secret      应用密钥（只在 ``login()`` 的刷新调用里使用）
        access_token    由 ``login()`` 换取并写回；官方有效期 48 小时
        refresh_token   以 ``rft.`` 开头；**一次性**，每次 ``login()`` 轮换

    ``login()`` 刷新令牌并把新的 access_token 与 refresh_token **一起**写回；
    旧的 refresh_token 随即失效。refresh_token（180 天）过期后必须重新完成
    用户授权，本适配器不提供授权流程。仓库内没有任何自动续期：必须在每个
    48 小时窗口内调用一次 ``POST /accounts/{id}/login``。
    """

    platform = Platform.KUAISHOU
    kind = PublisherKind.KUAISHOU_OPEN_API
    # No exact renderer projection is audited for this canary: the Agent
    # planner must fail closed for KUAISHOU rather than guess the payload.
    agent_contract_renderer_descriptor = None

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = _API_BASE,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self.last_login_error: str | None = None

    # ---------------- HTTP 基础 ----------------

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # access_token / app_secret / refresh_token / upload_token 都在查询串里，
        # 而 httpx 的 INFO 请求行会打印完整 URL。这里自己保证上限，不依赖调用方
        # 初始化过日志：logger 级别是进程级的，钉在 WARNING 后不随 root 变。
        cap_httpx_logger()
        return httpx.AsyncClient(
            timeout=timeout,
            transport=self._transport,
            headers={"User-Agent": "ai-ops-auto-kuaishou-open-api/0.1"},
        )

    @staticmethod
    async def _send(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[dict | None, bool]:
        """Send one request and parse the JSON body.

        Returns ``(data, server_may_have_received)``.  The flag is ``False``
        only when no connection could be established, which is the one case
        that proves the server never saw the request.

        Nothing may escape this method.  The worker treats any exception
        leaving ``publish()`` as an unknown outcome that can never be
        retried, so every failure - body-read and protocol errors that are
        not ``httpx.HTTPError`` subclasses included - becomes a value.  And
        no exception text is ever surfaced: httpx messages can embed the URL,
        whose query string here carries ``access_token`` / ``app_secret`` /
        ``upload_token``.
        """

        try:
            resp = await client.request(method, url, **kwargs)
            content = resp.content
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return None, False
        except Exception:
            # ReadError / RemoteProtocolError / DecodingError (HTTPError), and
            # InvalidURL / StreamError (plain Exception): the request may have
            # been received, so the caller must treat it as unreadable.
            return None, True
        return parse_api_json(content), True

    async def _api_call(
        self,
        path: str,
        credential: dict,
        *,
        method: str = "POST",
        timeout: float,
        params: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
    ) -> tuple[dict | None, bool]:
        """Call an ``openapi`` endpoint; ``access_token`` + ``app_id`` go in the query."""

        query = {"access_token": credential["access_token"], "app_id": credential["app_id"]}
        query.update(params or {})
        async with self._client(timeout) as client:
            return await self._send(
                client, method, self._base_url + path, params=query, data=data, files=files
            )

    # ---------------- 凭证 ----------------

    @staticmethod
    def _missing_publish_fields(credential: dict) -> list[str]:
        """Fields every ``openapi`` call needs (both travel in the query string)."""

        cred = credential or {}
        return [f for f in ("app_id", "access_token") if not cred.get(f)]

    @staticmethod
    def _missing_refresh_fields(credential: dict) -> list[str]:
        """Fields the refresh call itself needs."""

        cred = credential or {}
        return [f for f in _REFRESH_FIELDS if not cred.get(f)]

    @classmethod
    def _credential_error(cls, credential: dict) -> str | None:
        """Why publish cannot proceed, or ``None``.  Pure: no request is sent."""

        missing = cls._missing_publish_fields(credential)
        if missing:
            return (
                "快手开放平台凭证不完整，缺少: "
                + ",".join(missing)
                + "；请先执行一次登录刷新令牌"
            )
        unsafe = wire_unsafe_credential_fields(credential)
        if unsafe:
            return (
                "快手开放平台凭证字段必须是可打印 ASCII 字符串（不含空白）: "
                + ",".join(unsafe)
            )
        return None

    async def _refresh(self, credential: dict, timeout: float) -> tuple[str, int | None]:
        """``GET /oauth2/refresh_token``.  Returns ``(kind, result_code)``.

        On ``ok`` the new ``access_token`` and the new ``refresh_token``
        replace the stored values *together* - the old refresh token is
        already retired by the platform at this point.  Nothing is written
        otherwise.
        """

        params = {
            "app_id": credential["app_id"],
            "app_secret": credential["app_secret"],
            "refresh_token": credential["refresh_token"],
            "grant_type": "refresh_token",
        }
        async with self._client(timeout) as client:
            data, _reached = await self._send(
                client, "GET", self._base_url + _REFRESH_TOKEN_PATH, params=params
            )
        kind, code = classify_refresh(data)
        if kind != "ok":
            return kind, code
        credential["access_token"] = data["access_token"]
        credential["refresh_token"] = data["refresh_token"]
        expires_in = _int_or_none(data.get("expires_in"))
        if expires_in is not None and expires_in > 0:
            credential["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
        return "ok", 1

    async def login(self, account_id: int, credential: dict) -> bool:
        """刷新 access_token（48 小时），并把轮换后的 refresh_token 一起写回。"""
        self.last_login_error = None
        missing = self._missing_refresh_fields(credential)
        if missing:
            self.last_login_error = (
                "快手开放平台凭证不完整，缺少: " + ",".join(missing)
            )
            return False
        unsafe = wire_unsafe_credential_fields(credential)
        if unsafe:
            self.last_login_error = (
                "快手开放平台凭证字段必须是可打印 ASCII 字符串（不含空白）: "
                + ",".join(unsafe)
            )
            return False
        kind, code = await self._refresh(
            credential, float(settings.kuaishou_open_api_timeout_seconds)
        )
        if kind == "ok":
            return True
        self.last_login_error = self._login_error(kind, code)
        return False

    @staticmethod
    def _login_error(kind: str, code: int | None) -> str:
        if code == 100200113:
            return "快手 refresh_token 已过期（result 100200113）；请重新完成用户授权"
        if kind == "credential":
            return (
                f"快手凭证校验失败（result {code}）；请检查 app_id/app_secret、"
                "用户授权与应用接口权限"
            )
        if kind == "transient":
            return "快手刷新令牌接口暂不可用，请稍后重试"
        # ``invalid``: no usable verdict.  The stored refresh token may already
        # have been consumed (a reply that was truncated after the platform
        # retired it), so say so instead of implying a harmless retry.
        return (
            "快手刷新令牌接口返回异常；若再次登录仍失败，说明 refresh_token 已被"
            "轮换，需要重新完成用户授权"
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """Read-only probe.  **Never refreshes the token here.**

        The daily health scheduler does not persist credential changes, and
        Kuaishou retires the old refresh token on every refresh: refreshing
        in this probe would retire the stored token every night and lock the
        account out of its next login.  So the probe only calls the read-only
        ``/openapi/photo/count`` with the stored access token.

        ``HEALTHY``: the platform accepted the token.  ``EXPIRED``: the
        platform definitively refused the grant (token expired / user revoked
        authorisation), so the account needs ``login()`` or a fresh user
        authorisation.  ``UNKNOWN``: no verdict - not an Open Platform
        credential, locally incomplete, the probe timed out / answered
        unreadably, or any other code.  ``photo/count`` needs the
        ``user_video_info`` scope, which a publish-only grant may lack; the
        platform's answer for a missing scope is not documented as a
        credential code, so it lands in ``UNKNOWN`` rather than being
        misreported as ``EXPIRED``.  ``UNKNOWN`` lets the scheduler's probe
        continue to the next adapter.
        """

        if not is_api_credential(credential):
            return AccountHealth.UNKNOWN
        if self._missing_publish_fields(credential) or wire_unsafe_credential_fields(
            credential
        ):
            return AccountHealth.UNKNOWN
        # _send never raises, so nothing here can escape into the scheduler.
        data, _reached = await self._api_call(
            _PHOTO_COUNT_PATH,
            credential,
            method="GET",
            timeout=float(settings.kuaishou_open_api_timeout_seconds),
        )
        kind, _code = classify_response(data)
        if kind == "ok":
            return AccountHealth.HEALTHY
        if kind == "credential":
            return AccountHealth.EXPIRED
        return AccountHealth.UNKNOWN

    # ---------------- 校验 ----------------

    def _resolve_asset(
        self, raw_path: str, *, exact_approval: bool, label: str
    ) -> tuple[Path | None, str]:
        """Resolve one local asset inside the configured root.

        Returns ``(path, error)``.  Symlinks are refused, the path must stay
        inside the root, and the file must exist: the platform receives
        bytes that this process can actually read.
        """

        root = (
            settings.agent_asset_vault_root
            if exact_approval
            else settings.kuaishou_open_api_asset_root
        )
        candidate = Path(raw_path).expanduser()
        if candidate.is_symlink():
            return None, f"快手{label}不能是符号链接"
        try:
            resolved = candidate.resolve(strict=True)
            resolved_root = root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None, f"快手{label}不存在或不可读"
        if not resolved.is_relative_to(resolved_root):
            return None, f"快手{label}必须是 {resolved_root} 内的本地文件"
        return resolved, ""

    def _validate_video(
        self, content: PublishContent
    ) -> tuple[Path | None, int | None, str | None]:
        """One readable, non-empty regular video file inside the asset root.

        Returns ``(path, size, error)``.  Readability is proven here by
        opening the file and reading from it, and the size comes from the
        same open descriptor: every later filesystem failure that can be
        caught locally is caught now, before any request is sent.
        """

        video, error = self._resolve_asset(
            content.videos[0], exact_approval=content.exact_approval, label="视频文件"
        )
        if video is None:
            return None, None, error
        mime, _ = mimetypes.guess_type(video.name)
        if video.suffix.lower() not in _ALLOWED_VIDEO_SUFFIXES or not (mime or "").startswith(
            "video/"
        ):
            return None, None, "快手视频必须是受支持的本地视频文件"
        try:
            with video.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                readable = handle.read(1)
        except (OSError, ValueError):
            return None, None, "快手视频不存在或不可读"
        if not size or not readable:
            return None, None, "快手视频为空"
        if size > settings.kuaishou_open_api_max_video_bytes:
            return None, None, "快手视频超过配置大小上限"
        return video, size, None

    def _validate_cover(self, content: PublishContent) -> tuple[Path | None, str | None]:
        """The required ``cover``: a readable jpg file under the platform's 10 MB.

        Returns ``(path, error)``.  The cover is sent in the publish request,
        so it is validated together with the video before the upload starts:
        discovering a missing cover after a multi-hundred-MB upload would
        waste the upload and the account's daily request budget.
        """

        raw = (content.extra or {}).get(_COVER_EXTRA_KEY)
        cover, error = self._resolve_asset(
            str(raw), exact_approval=content.exact_approval, label="封面图"
        )
        if cover is None:
            return None, error
        if cover.suffix.lower() not in _COVER_SUFFIXES:
            return None, "快手封面图必须是 jpg 文件"
        try:
            with cover.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                readable = handle.read(1)
        except (OSError, ValueError):
            return None, "快手封面图不存在或不可读"
        if not size or not readable:
            return None, "快手封面图为空"
        if size >= _COVER_MAX_BYTES:
            return None, "快手封面图不能超过 10MB"
        return cover, None

    # ---------------- 上传 ----------------

    @staticmethod
    def _upload_outcome(data: dict | None, reached: bool) -> tuple[str, bool]:
        """Classify a failed upload-step reply as ``(state, retryable)``.

        Upload steps create only an isolated upload token, never a public
        post, so nothing here is ``outcome_uncertain``; the registry may fall
        back to the next adapter and a retry cannot duplicate a post.

        * ``credential``  the platform refused the token or the capability.
          Not retryable: a login is needed, and this adapter never refreshes
          inside the write path.
        * ``rejected``     a definitive non-1 answer.  Not retryable - the
          same request yields the same code.  This includes ``100100402``
          (用户被封禁 / 超过当日请求上限): one code, two meanings, and the
          reply does not say which; a retry helps neither and spends budget.
        * ``unknown``      a transient "服务内部错误" code, a timeout after
          the request was sent, or an unreadable body.  Retryable.
        * ``unreachable``  no connection was made.  Retryable.
        """

        if data is None:
            return ("unknown", True) if reached else ("unreachable", True)
        kind, _code = classify_response(data)
        if kind == "credential":
            return "credential", False
        if kind in ("limited", "rejected"):
            return "rejected", False
        return "unknown", True

    @classmethod
    def _upload_failure(
        cls, step: str, state: str, retryable: bool, data: dict | None, reached: bool
    ) -> PublishResult:
        """Build the safe-failure result for an upload step."""

        label = _UPLOAD_STEP_LABELS.get(step, step)
        raw: dict[str, object] = {
            "adapter": "kuaishou-open-api",
            "action": "video-upload",
            "stage": "upload",
            # No public write has started at this point.
            "write_started": False,
            "outcome": state,
        }
        if data is None:
            if reached:
                error = f"快手视频上传未确认（{label}）：请求超时或返回无法解析；未产生作品，可重试"
            else:
                error = f"快手视频上传未发出（{label}）：无法连接开放平台"
        else:
            kind, code = classify_response(data)
            raw.update(safe_error_detail(kind, code))
            if state == "credential":
                error = f"快手 access_token 无效或已过期（{label}，result {code}）；请先刷新登录"
            else:
                error = f"快手视频上传被拒绝（{label}，result {code}）"
        return PublishResult(
            success=False,
            effect_applied=False,
            outcome_uncertain=False,
            retryable=retryable,
            error=error,
            raw_response=raw,
        )

    async def _start_upload(
        self, credential: dict, timeout: float
    ) -> tuple[str | None, str | None, PublishResult | None]:
        """``start_upload``: ``(upload_token, endpoint, failure)``."""

        step = _START_UPLOAD_PATH
        data, reached = await self._api_call(step, credential, timeout=timeout)
        if data is not None and classify_response(data)[0] == "ok":
            upload_token, endpoint = extract_upload_endpoint(data)
            if upload_token is not None and endpoint is not None:
                return upload_token, endpoint, None
            # result 1 without a usable upload_token / endpoint.  A reply whose
            # endpoint is not a bare hostname is treated the same way: nothing
            # is ever sent to an address that failed validation.
            return None, None, self._upload_failure(step, "unknown", True, None, True)
        state, retryable = self._upload_outcome(data, reached)
        return None, None, self._upload_failure(step, state, retryable, data, reached)

    async def _upload(
        self, endpoint: str, upload_token: str, video: Path, video_bytes: int, timeout: float
    ) -> PublishResult | None:
        """Upload the bytes to the gateway.  ``None`` on success.

        Files up to 10 MB go in one request; larger ones are split into
        near-equal fragments of at most the configured size (10 MB by
        default and at most), numbered from 0, then merged.
        """

        base = f"http://{endpoint}"
        try:
            size_now = video.stat().st_size
        except OSError:
            # Removed since validation: nothing was published.  Retryable: the
            # next attempt re-validates and reports the real problem there.
            return self._upload_failure(_UPLOAD_DIRECT_PATH, "unknown", True, None, False)
        if size_now != video_bytes:
            # Rewritten since validation.  Uploading a different length than
            # was checked would send unchecked bytes, so stop; a retry starts
            # from validation again.
            return self._upload_failure(_UPLOAD_DIRECT_PATH, "unknown", True, None, False)

        if video_bytes <= _DIRECT_UPLOAD_MAX_BYTES:
            step = _UPLOAD_DIRECT_PATH
            with video.open("rb") as handle:
                files = {"file": (video.name, handle, "application/octet-stream")}
                data, reached = await self._gateway_call(
                    base, step, upload_token, timeout=timeout, files=files
                )
            if data is not None and classify_response(data)[0] == "ok":
                return None
            state, retryable = self._upload_outcome(data, reached)
            return self._upload_failure(step, state, retryable, data, reached)

        boundaries = fragment_boundaries(
            video_bytes, settings.kuaishou_open_api_fragment_bytes
        )
        if not boundaries:
            return self._upload_failure(_UPLOAD_FRAGMENT_PATH, "rejected", False, None, False)
        with video.open("rb") as handle:
            for index, (offset, length) in enumerate(boundaries):
                handle.seek(offset)
                chunk = handle.read(length)
                if len(chunk) != length:
                    # Short read: the file changed underneath us.  Stop here,
                    # before the merge, so no partial upload is completed.
                    return self._upload_failure(
                        _UPLOAD_FRAGMENT_PATH, "unknown", True, None, False
                    )
                # 官方 B.1: body is the raw fragment bytes (data-binary).
                data, reached = await self._gateway_call(
                    base,
                    _UPLOAD_FRAGMENT_PATH,
                    upload_token,
                    timeout=timeout,
                    extra_params={
                        "fragment_id": _FRAGMENT_ID_STARTS_AT + index,
                    },
                    content=chunk,
                )
                if data is None or classify_response(data)[0] != "ok":
                    state, retryable = self._upload_outcome(data, reached)
                    return self._upload_failure(
                        _UPLOAD_FRAGMENT_PATH, state, retryable, data, reached
                    )

        data, reached = await self._gateway_call(
            base,
            _UPLOAD_COMPLETE_PATH,
            upload_token,
            timeout=timeout,
            extra_params={"fragment_count": len(boundaries)},
        )
        if data is not None and classify_response(data)[0] == "ok":
            return None
        state, retryable = self._upload_outcome(data, reached)
        return self._upload_failure(_UPLOAD_COMPLETE_PATH, state, retryable, data, reached)

    async def _gateway_call(
        self,
        base: str,
        step: str,
        upload_token: str,
        *,
        timeout: float,
        extra_params: dict | None = None,
        files: dict | None = None,
        content: bytes | None = None,
    ) -> tuple[dict | None, bool]:
        """Call the upload gateway; ``upload_token`` is a query parameter.

        The gateway host comes from the ``start_upload`` reply and the scheme
        is the documented plain ``http``.  ``base`` must already be
        ``"http://" + <validated endpoint>``.

        The body is at most one of: a multipart ``file`` (直接上传 A.2), the
        raw fragment bytes (分片上传 B.1), or none at all (完成分片上传
        B.3, which is query parameters only).
        """

        if files is not None and content is not None:
            # A caller bug.  Reported as a value: this runs on the write path,
            # where an escaping exception would freeze the job as unknown.
            return None, False
        params: dict[str, Any] = {"upload_token": upload_token}
        params.update(extra_params or {})
        headers = {"Content-Type": "application/octet-stream"} if content is not None else None
        async with self._client(timeout) as client:
            return await self._send(
                client,
                "POST",
                base + step,
                params=params,
                files=files,
                content=content,
                headers=headers,
            )

    # ---------------- PublisherBase ----------------

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """上传 → ``photo/publish``；后者是公开副作用的写边界。"""
        if content.exact_approval:
            try:
                self.render_agent_contract_payload(content)
            except AgentContractRendererUnavailable as exc:
                return self._pre_write_failure(str(exc))

        validation_error = validate_content(content)
        if validation_error:
            return self._pre_write_failure(validation_error)

        if not is_api_credential(credential):
            # Not this adapter's credential (e.g. a browser session for the
            # social-auto-upload path): step aside so the registry falls back.
            return PublishResult(
                success=False,
                error="账号未配置快手开放平台凭证（app_id/refresh_token）",
                raw_response={
                    "adapter": "kuaishou-open-api",
                    "write_started": False,
                    "outcome": "not_applicable",
                },
            )
        credential_error = self._credential_error(credential)
        if credential_error:
            return self._pre_write_failure(credential_error)

        # The write path uses no ``assert``: an AssertionError escaping
        # publish() is recorded by the worker as an unknown outcome that
        # blocks retry and fallback.  A broken invariant must still come back
        # as a value, and one that says nothing was written - all of these
        # checks run before the publish request is sent.
        video, video_bytes, video_error = self._validate_video(content)
        if video is None:
            return self._pre_write_failure(video_error or "快手视频文件不可用")
        cover, cover_error = self._validate_cover(content)
        if cover is None:
            return self._pre_write_failure(cover_error or "快手封面图不可用")

        timeout = float(settings.kuaishou_open_api_timeout_seconds)
        upload_token, endpoint, failure = await self._start_upload(credential, timeout)
        if failure is not None:
            return failure
        if not upload_token or not endpoint:
            # _start_upload reports this as a failure; kept as a value so a
            # regression there cannot turn into an exception on the write path.
            return self._upload_failure(_START_UPLOAD_PATH, "unknown", True, None, True)
        failure = await self._upload(
            endpoint, upload_token, video, video_bytes, timeout
        )
        if failure is not None:
            return failure

        return await self._publish_video(
            credential, upload_token, cover, content, video_bytes, timeout
        )

    @staticmethod
    def _pre_write_failure(error: str) -> PublishResult:
        """A local failure found before any request: nothing was written, not retryable."""

        return PublishResult(
            success=False,
            effect_applied=False,
            outcome_uncertain=False,
            retryable=False,
            error=error,
            raw_response={
                "adapter": "kuaishou-open-api",
                "write_started": False,
                "outcome": "not_started",
            },
        )

    async def _publish_video(
        self,
        credential: dict,
        upload_token: str,
        cover: Path,
        content: PublishContent,
        video_bytes: int,
        timeout: float,
    ) -> PublishResult:
        """``photo/publish`` is the public write; every unreadable outcome is uncertain."""
        raw: dict[str, object] = {
            "adapter": "kuaishou-open-api",
            "action": "video-publish",
            "stage": "publish",
            "video_bytes": video_bytes,
        }
        caption = build_caption(content)
        try:
            handle = cover.open("rb")
        except (OSError, ValueError):
            # The cover became unreadable between validation and this call.
            # This is the only place the exception is caught: it proves the
            # publish request was never sent.  The request itself is made
            # outside the ``try`` - _send never raises, and an OSError from a
            # socket here would have to be reported as an *unknown* outcome,
            # not as "not published, safe to retry".
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=True,
                error="快手封面图在发布前变得不可读；未发布，可重试",
                raw_response={**raw, "write_started": False, "outcome": "not_started"},
            )
        with handle:
            files = {
                # 官方: cover 封面图(jpg, <10MB), caption 标题; both required.
                "cover": (cover.name, handle, "image/jpeg"),
                "caption": (None, caption),
            }
            data, reached = await self._api_call(
                _PUBLISH_PATH,
                credential,
                timeout=timeout,
                params={"upload_token": upload_token},
                files=files,
            )

        kind, code = classify_response(data)
        raw.update(safe_error_detail(kind, code))
        if data is not None and kind == "ok":
            raw["write_started"] = True
            photo_id, play_url = extract_photo(data)
            if photo_id is None:
                # Accepted (result 1) but no usable identity: the post may
                # exist.  Never retry or fall back from here.
                raw["outcome"] = "unknown"
                raw["needs_reconciliation"] = True
                return self._journal(
                    content,
                    PublishResult(
                        success=False,
                        effect_applied=True,
                        outcome_uncertain=True,
                        retryable=False,
                        error="快手已受理发布但未返回 photo_id；请到快手创作者后台核验后再决定是否重发",
                        raw_response=raw,
                    ),
                )
            raw["outcome"] = "confirmed"
            return self._journal(
                content,
                PublishResult(
                    success=True,
                    effect_applied=True,
                    platform_post_id=photo_id,
                    # Only a real http(s) URL is kept: play_url is absent
                    # until transcoding finishes (pending), and an empty or
                    # malformed value must not become the job's platform_url.
                    platform_url=play_url,
                    error=None if play_url else "发布成功；作品仍在转码，链接稍后可在快手查看",
                    raw_response=raw,
                ),
            )

        # Not ok.
        if not reached:
            # No connection: the publish request never left this machine.
            raw.update({"write_started": False, "outcome": "not_started"})
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=True,
                error="快手发布请求未发出（无法连接开放平台）；可重试",
                raw_response=raw,
            )
        raw["write_started"] = True
        if data is not None and kind in ("credential", "rejected", "limited"):
            # A definitive non-1 answer: the platform refused, nothing was
            # published.  Not retryable - the same request yields the same
            # code, and for 100100402 the account may be banned or over its
            # daily request budget, which a retry only worsens.
            raw["outcome"] = "rejected"
            if kind == "credential":
                label = "登录态失效，请先刷新登录"
            elif kind == "limited":
                label = "账号被封禁或请求超过当日上限"
            else:
                label = "平台拒绝"
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=False,
                error=f"快手发布被拒绝（{label}，result {code}）",
                raw_response=raw,
            )
        # Sent, but no proof either way: transient "服务内部错误", an unreadable
        # body, or a timeout.  The post may exist - stop everything.
        raw["outcome"] = "unknown"
        raw["needs_reconciliation"] = True
        return self._journal(
            content,
            PublishResult(
                success=False,
                effect_applied=True,
                outcome_uncertain=True,
                retryable=False,
                error="快手发布结果未知；请到快手创作者后台核验后再决定是否手动重发",
                raw_response=raw,
            ),
        )

    def _journal(self, content: PublishContent, result: PublishResult) -> PublishResult:
        """Persist a redacted receipt before returning to the worker (same as the CLI adapters)."""
        write_publish_receipt(
            job_id=content.job_id,
            operation_id=content.operation_id,
            publisher_kind=self.kind.value,
            result=result,
        )
        return result
