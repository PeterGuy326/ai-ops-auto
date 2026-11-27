"""Feature-gated adapter for the Douyin Open Platform video publish API.

This is the documented official API on ``https://open.douyin.com`` (scope
``video.create.bind``), not a consumer endpoint.  The flow is:

  1. ``POST /api/douyin/v1/video/upload_video/``   video bytes -> ``video_id``
     (or the three chunked calls ``init_video_part_upload`` /
     ``upload_video_part`` / ``complete_video_part_upload``)
  2. ``POST /api/douyin/v1/video/create_video/``   ``video_id`` -> ``item_id``

**Eligibility is the real gate, not the code.**  The capability page for
"代替用户发布内容到抖音" (capability 7224121299067469881) lists as 准入条件
"开发者主体需为党政机关或事业单位", website apps only, no sandbox.  An
individual developer or an ordinary company cannot obtain it.  The adapter is
therefore default-off and exists for operators who do hold the grant.

Audited wire facts (from the ``openApiMeta`` embedded in each official page):

* ``access-token`` travels in an HTTP header; ``open_id`` is a query
  parameter; the client key/secret only ever appear in the token form body.
* Every endpoint, including errors, answers HTTP ``200``.  Success is
  ``data.error_code == 0``; the status line carries no signal.
* ``create_video`` requires only ``video_id``.  ``text`` is capped at 1000
  characters and carries hashtags inline; there is no tags field.  The
  optional ``publish_way`` (视频投稿类型) is an enum whose values the page
  does not list, so the adapter omits it and takes the platform default.
* Publishing is followed by platform review, during which only the author can
  see the video.  ``item_id`` is the post identity.
* Upload limits: 4 GB total, 15 minutes; over 300 MB must use chunked upload;
  chunks are 5-100 MB (20 MB recommended).  (The page header also says
  "视频文件大小不超过100MB" while its usage limits say 4 GB; the adapter
  follows the usage limits and lets the platform's own error codes arbitrate.)
* ``part_number`` is documented only as "分片上传时的分片序号" with example
  ``1``.  The page never states whether numbering is 0- or 1-based.  This
  adapter sends 1-based numbers (the example) - UNVERIFIED on the live
  platform; the first real canary must confirm it.

Token handling is deliberately split.  ``login()`` exchanges the stored
``refresh_token`` for a fresh 15-day ``access_token`` and writes it back into
the credential dict, which the API layer then re-encrypts.  That endpoint does
*not* rotate the refresh token (it keeps its 30-day lifetime); renewal is a
separate, 5-times-maximum call this adapter never makes.  ``publish()`` only
reads the stored access token: its credential mutations are not persisted, so
a token refreshed there would be lost on the next run, and inserting a token
exchange into the write path would blur the result semantics.  An expired
token during publish is therefore reported as "needs login", never refreshed.

Result semantics (the worker stops fallback and retry on ``outcome_uncertain``):

======================  =================  =================  ==========
stage / failure         effect_applied    outcome_uncertain  retryable
======================  =================  =================  ==========
validation              False             False              False
not this adapter's      False             False              True
credential
upload, definitive      False             False              False
upload, rate limited    False             False              True
upload, unknown/lost    False             False              True
create_video, refused  False             False              False
create_video, unknown  True              True               False
create_video, ok       True              False              -
======================  =================  =================  ==========

Upload steps create only an isolated file id, never a public post, which is
why their failures are never ``outcome_uncertain``.

**Stepping aside.**  Enabling the canary must not hurt an account that still
publishes through social-auto-upload with a browser credential.  When the
stored credential is not an Open Platform grant, ``health_check`` returns
``UNKNOWN`` (so the scheduler's probe continues to the next adapter) and
``publish`` returns a plain, fallback-able failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import json
import math
import mimetypes
import os
from pathlib import Path
import re
from typing import Any

import httpx

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.receipts import write_publish_receipt
from .base import AgentContractRendererUnavailable, PublisherBase

_API_BASE = "https://open.douyin.com"
_UPLOAD_VIDEO_PATH = "/api/douyin/v1/video/upload_video/"
_INIT_PART_PATH = "/api/douyin/v1/video/init_video_part_upload/"
_UPLOAD_PART_PATH = "/api/douyin/v1/video/upload_video_part/"
_COMPLETE_PART_PATH = "/api/douyin/v1/video/complete_video_part_upload/"
_CREATE_VIDEO_PATH = "/api/douyin/v1/video/create_video/"
_REFRESH_TOKEN_PATH = "/oauth/refresh_token/"

_MAX_TEXT_CHARS = 1000
_ALLOWED_EXTRA_KEYS = ("douyin_cover_tsp", "douyin_private_status", "douyin_download_type")
_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi"}
# What /oauth/refresh_token/ itself needs; open_id comes back from it.
_REFRESH_FIELDS = ("client_key", "client_secret", "refresh_token")
# Everything this adapter sends on the wire, as a header / query / form value.
_WIRE_FIELDS = _REFRESH_FIELDS + ("open_id", "access_token")

# Official error-code tables (per-endpoint ErrCodeList + /openapi/status-code).
# Token/capability problems: the request was refused before any write.
_CREDENTIAL_ERROR_CODES = frozenset(
    {
        28001003,  # access_token无效
        28001008,  # access_token过期,请刷新或重新授权
        28001012,  # 用户未授权该OpenAPI
        28001014,  # 应用未授权任何能力
        28001015,  # access_token 和 openId 不匹配
        28001016,  # 当前应用已被封禁或下线
        28001018,  # 应用未获得该能力
        28001019,  # 应用该能力已被封禁
        2190002,  # access_token 无效
        2190003,  # 用户未授权该api
        2190004,  # 应用未获得该能力
        2190008,  # access_token过期
        2190015,  # access_token 和 openId不匹配
    }
)
# 请求频率过高: a refusal caused by our own pacing. Deterministically not
# published, so it stays a safe failure that may be retried later.
_RATE_LIMITED_ERROR_CODES = frozenset({28003018})
# "系统繁忙 / 内部错误 / 请重试": the service reports a failure.  An upload
# step is safely repeatable (no public post exists yet); create_video is
# treated as UNKNOWN, because "busy" does not prove the item was not created.
_TRANSIENT_ERROR_CODES = frozenset({2100004, 28001005, 28001006})
# Definitive rejections: the platform refused and nothing was published.
# Retrying the same request yields the same code, so never retried
# automatically.  2114020-2114026 are listed only on the upload_video_part
# page (uploadId无效 / 分片不能小于5MB / 分片不能大于100MB / 总大小超过限制).
_REJECTED_ERROR_CODES = frozenset(
    {
        2100005,  # upload: 参数错误; create_video: 标题文字不得多于1000个
        210005,  # create_video: 参数不合法
        2114005,  # 视频投稿功能已封禁
        2114006,  # 视频时长不能超过15分钟
        2114007,  # 视频发布数量超过每日上限
        2190005,  # 文件太大了 (direct upload over 300MB)
        2190007,  # 无效的视频文件id
        28001007,  # 参数不合法
        28003017,  # quota已用完
        2114020,  # uploadId无效
        2114021,  # 分片不能小于5MB
        2114022,  # 分片不能大于100MB
        2114026,  # 分片视频总大小超过限制
    }
)
# /oauth/refresh_token/ reports under data.error_code with its own small table.
_OAUTH_ERROR_CODES = frozenset(
    {
        10002,  # 参数错误
        10004,  # 应用权限不足，请申请对应的 scope 权限
        10005,  # 参数缺失
        10010,  # refresh_token 无效或过期
        10013,  # client_key 或者 client_secret 错误
        10020,  # 更新 refresh_token 次数超出限制
    }
)
_OAUTH_TRANSIENT_CODE = 10001  # 系统错误，请稍后重试
_OAUTH_REFRESH_EXPIRED_CODE = 10010

# ``part_number`` example value on the official page.  See the module docstring.
_PART_NUMBER_STARTS_AT = 1
_PART_MIN_BYTES = 5 * 1024 * 1024
_PART_MAX_BYTES = 100 * 1024 * 1024

# Encrypted ids are base64-like with ``@``; both carry the same character set.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9@+/=_.\-]{8,512}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9@+/=_.\-]{8,512}$")

# Operator-facing step names for error messages.  API paths are kept out of
# user-visible text because they carry no meaning to the account operator.
_UPLOAD_STEP_LABELS = {
    _UPLOAD_VIDEO_PATH: "整体上传",
    _INIT_PART_PATH: "分片初始化",
    _UPLOAD_PART_PATH: "分片上传",
    _COMPLETE_PART_PATH: "分片合并",
    _CREATE_VIDEO_PATH: "创建作品",
}


def _int_or_none(value: Any) -> int | None:
    """Accept an int, or a numeric string (the docs show ``"expires_in": "86400"``)."""

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
    """Douyin answers a JSON object; anything else is an unknown outcome."""

    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _error_code(data: dict | None) -> int | None:
    if not isinstance(data, dict):
        return None
    # Official success bodies repeat the code in ``data`` and ``extra``; error
    # examples carry it in both.  ``data`` wins when they disagree.
    for section in ("data", "extra"):
        body = data.get(section)
        if isinstance(body, dict):
            code = _int_or_none(body.get("error_code"))
            if code is not None:
                return code
    return None


def classify_response(data: dict | None) -> tuple[str, int | None]:
    """Return ``(kind, error_code)`` for a parsed platform response.

    ``kind`` is one of ``ok`` / ``credential`` / ``rate_limited`` /
    ``transient`` / ``rejected`` / ``invalid``.  The platform's description
    text is deliberately not returned: it can echo request data.

    An unlisted non-zero code is treated as ``rejected``.  It is a definitive
    answer from the platform, so the request was refused; but since its
    meaning is unknown the caller decides retryability per stage.
    """

    if data is None:
        return "invalid", None
    code = _error_code(data)
    if code == 0:
        return "ok", 0
    if code is None:
        return "invalid", None
    if code in _CREDENTIAL_ERROR_CODES:
        return "credential", code
    if code in _RATE_LIMITED_ERROR_CODES:
        return "rate_limited", code
    if code in _TRANSIENT_ERROR_CODES:
        return "transient", code
    return "rejected", code


def classify_refresh(data: dict | None) -> tuple[str, int | None]:
    """Classify an ``/oauth/refresh_token/`` response as ``(kind, error_code)``.

    ``kind``: ``ok`` | ``credential`` | ``transient`` | ``invalid``.  This
    endpoint reports under ``data.error_code`` with its own small code table.
    """

    if data is None:
        return "invalid", None
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    code = _int_or_none(body.get("error_code"))
    token = body.get("access_token")
    if code == 0 and isinstance(token, str) and token:
        return "ok", 0
    if code is None or code == 0:
        # No usable verdict: a missing code, or a success code without a
        # token (a truncated or gateway-mangled reply).  Neither says the
        # grant is bad, so it must not be reported as an expired account.
        return "invalid", None
    if code == _OAUTH_TRANSIENT_CODE:
        # 系统错误，请稍后重试: the stored grant is untouched.
        return "transient", code
    # A listed credential code, or any unlisted non-zero answer.  A refresh is
    # a read of the stored grant rather than a write, so nothing was published
    # either way; the account needs a fresh user authorisation.
    return "credential", code


def _hashtag_tags(content: PublishContent) -> list[str]:
    """Normalise tags to Douyin hashtag tokens.

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
            raise ValueError("抖音话题标签不能为空")
        if any(ch.isspace() for ch in tag) or "#" in tag or "@" in tag:
            raise ValueError("抖音话题标签不能包含空格、# 或 @")
        tags.append(tag)
    return tags


def build_publish_text(content: PublishContent) -> str:
    """Title, optional body, and inline ``#hashtags`` within the 1000 cap.

    Douyin has no tags field: tags are ``#tag`` tokens appended to ``text``.
    """

    parts = [content.title.strip()]
    body = content.body.strip()
    if body:
        parts.append(body)
    tags = _hashtag_tags(content)
    if tags:
        parts.append(" ".join(f"#{tag}" for tag in tags))
    return "\n".join(part for part in parts if part)
def _is_strict_int_in(value: Any, allowed: tuple[int, ...]) -> bool:
    """``True`` when ``value`` is unset (``None``) or a plain int in ``allowed``.

    ``bool`` is rejected even though it subclasses ``int``, and so is a float
    such as ``1.0``: ``1.0 in (0, 1, 2)`` is ``True`` in Python, which would
    let ``1.0`` be sent where the platform documents an integer.
    """

    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value in allowed



def validate_content(content: PublishContent) -> str | None:
    """Return an error string, or ``None`` when the content is publishable."""

    if content.content_type != ContentType.VIDEO or len(content.videos) != 1:
        return "抖音 API 当前要求恰好一个本地视频文件"
    if content.images:
        return "抖音视频发布不支持图片资产"
    if not content.title.strip():
        return "抖音标题不能为空"
    if "\x00" in content.title or "\x00" in content.body:
        return "抖音标题或正文含 NUL 字符"
    extra = content.extra or {}
    unknown = set(extra).difference(_ALLOWED_EXTRA_KEYS)
    if unknown:
        return f"抖音 API 不支持的 extra 字段: {sorted(unknown)}"
    # Official enums: private_status 0/1/2 (全部人可见/自见/好友可见),
    # download_type 0/1 (可以下载/作者不允许下载).  Strict integers only: a
    # float such as 1.0 compares equal to 1 but is not what the API documents.
    private_status = extra.get("douyin_private_status")
    if not _is_strict_int_in(private_status, (0, 1, 2)):
        return "抖音 extra.douyin_private_status 必须是 0/1/2 的整数"
    download_type = extra.get("douyin_download_type")
    if not _is_strict_int_in(download_type, (0, 1)):
        return "抖音 extra.douyin_download_type 必须是 0/1 的整数"
    # A finite number only: NaN passes every ``< 0`` style comparison yet is
    # rejected by the JSON encoder - after the upload has already happened.
    cover_tsp = extra.get("douyin_cover_tsp")
    if cover_tsp is not None and (
        isinstance(cover_tsp, bool)
        or not isinstance(cover_tsp, (int, float))
        or not math.isfinite(cover_tsp)
        or cover_tsp < 0
    ):
        return "抖音 extra.douyin_cover_tsp 必须是非负有限秒数"
    try:
        text = build_publish_text(content)
    except ValueError as exc:
        return str(exc)
    if len(text) > _MAX_TEXT_CHARS:
        return f"抖音标题加话题总字数不能超过 {_MAX_TEXT_CHARS} 字"
    return None


def extract_video_id(data: dict | None) -> str | None:
    """Pull the encrypted ``video_id`` out of an upload response."""

    body = data.get("data") if isinstance(data, dict) else None
    video = body.get("video") if isinstance(body, dict) else None
    video_id = video.get("video_id") if isinstance(video, dict) else None
    if isinstance(video_id, str) and _VIDEO_ID_RE.fullmatch(video_id):
        return video_id
    return None


def extract_item(data: dict | None) -> tuple[str | None, str | None]:
    """Return ``(item_id, video_id)`` from a ``create_video`` response."""

    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return None, None
    item_id = body.get("item_id")
    plain_video_id = body.get("video_id")
    return (
        item_id if isinstance(item_id, str) and _ITEM_ID_RE.fullmatch(item_id) else None,
        plain_video_id if isinstance(plain_video_id, str) and plain_video_id else None,
    )


# Every value this adapter puts in a header, a query string or a form body
# must be wire-safe.  Platform-issued ids and tokens are printable ASCII; a
# value outside that set is a configuration mistake.  Catching it here keeps
# it a plain "fix the credential" failure: left to httpx it would surface as
# an encoding error that looks like a network fault, or on the write path as
# an unknown outcome that can never be retried.
_WIRE_SAFE_RE = re.compile(r"^[\x21-\x7e]+$")


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
    """True when the stored credential is meant to be a Douyin Open Platform grant.

    Decided by the *presence* of the grant's keys, not by whether their values
    are well-formed.  A browser/cookie credential for the social-auto-upload
    path has neither key, and this adapter then steps aside (``UNKNOWN``
    health, fallback-able publish failure) instead of declaring the account
    expired.  But ``{"client_key": 12345, ...}`` is a misconfigured app
    grant, not a browser session: stepping aside there would route the job to
    social-auto-upload, which cannot use it, and would hide the real mistake.
    """

    cred = credential or {}
    return "client_key" in cred or "refresh_token" in cred


def safe_error_detail(kind: str, error_code: int | None) -> dict[str, Any]:
    """Credential-free detail block for ``raw_response`` / receipts."""

    return {"error_kind": kind, "error_code": error_code}


def part_boundaries(total_bytes: int, part_bytes: int) -> list[tuple[int, int]]:
    """``(offset, length)`` for every part, each within the 5-100 MB window.

    ``part_bytes`` is the operator's target size.  The file is split into
    ``ceil(total / part_bytes)`` near-equal parts, so no part exceeds the
    target.  Equal splitting leaves no short tail, but a small target can
    still make every part too small (301 MB at a 5 MB target gives one part
    of 4.9 MB).  The platform documents no exemption for a small last part
    (2114021 分片不能小于5MB), so when that happens the part count is reduced
    until the smallest part clears the minimum; each reduction only makes the
    parts larger, and they stay far below the 100 MB ceiling for any file
    that reaches this path (over 300 MB).

    Returns ``[]`` for an empty input and a single whole-file part when the
    file already fits in one target part.
    """

    if total_bytes <= 0 or part_bytes <= 0:
        return []
    if total_bytes <= part_bytes:
        return [(0, total_bytes)]
    count = -(-total_bytes // part_bytes)
    while count > 1 and total_bytes // count < _PART_MIN_BYTES:
        count -= 1
    base, remainder = divmod(total_bytes, count)
    boundaries: list[tuple[int, int]] = []
    offset = 0
    for index in range(count):
        length = base + (1 if index < remainder else 0)
        boundaries.append((offset, length))
        offset += length
    return boundaries


class DouyinOpenApiPublisher(PublisherBase):
    """抖音开放平台官方视频发布 canary（默认关闭）。

    凭证字段（由账号管理面加密存储）::

        client_key      网站应用 ClientKey
        client_secret   网站应用 ClientSecret
        open_id         授权用户在该应用下的唯一标识（query 参数）
        access_token    由 ``login()`` 从平台换取并写回
        refresh_token   以 ``rft.`` 开头；30 天有效，需用户授权后才有

    ``login()`` 用 refresh_token 换取新的 access_token（15 天）并原地写回；
    refresh_token 本身不变。refresh_token 过期后必须重新完成用户授权，
    本适配器不提供授权流程。
    """

    platform = Platform.DOUYIN
    kind = PublisherKind.DOUYIN_OPEN_API
    # No exact renderer projection is audited for this canary: the Agent
    # planner must fail closed for DOUYIN rather than guess the payload.
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
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=self._transport,
            headers={"User-Agent": "ai-ops-auto-douyin-open-api/0.1"},
        )

    async def _post_for_json(
        self,
        path: str,
        *,
        token: str | None = None,
        timeout: float,
        params: dict | None = None,
        data: dict | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
    ) -> tuple[dict | None, bool]:
        """POST and parse the JSON body.

        Returns ``(data, server_may_have_received)``.  The flag is ``False``
        only when no connection could be established, which is the one case
        that proves the server never saw the request.  Exception text is never
        surfaced: httpx messages can embed the URL and its query string.

        Nothing may escape this method.  The worker treats any exception
        leaving ``publish()`` as an unknown outcome that can never be
        retried, so every failure - including body-read and protocol errors
        that are not ``httpx.HTTPError`` subclasses - is turned into a value.
        """

        headers = {"access-token": token} if token else None
        try:
            async with self._client(timeout) as client:
                resp = await client.post(
                    path,
                    params=params,
                    data=data,
                    json=json_body,
                    files=files,
                    headers=headers,
                )
                content = resp.content
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return None, False
        except Exception:
            # ReadError / RemoteProtocolError / DecodingError (HTTPError), and
            # InvalidURL / StreamError (plain Exception): the request may have
            # been received, so the caller must treat it as unreadable.
            return None, True
        return parse_api_json(content), True

    # ---------------- 凭证 ----------------

    @staticmethod
    def _missing_refresh_fields(credential: dict) -> list[str]:
        """Fields the refresh call itself needs.

        ``open_id`` is deliberately absent: ``/oauth/refresh_token/`` returns
        it, so a grant stored without one is completed by a refresh rather
        than rejected.
        """

        cred = credential or {}
        return [f for f in _REFRESH_FIELDS if not cred.get(f)]

    @classmethod
    def _credential_error(cls, credential: dict) -> str | None:
        """Why publish cannot proceed, or ``None``.  Pure: no request is sent."""

        missing = cls._missing_refresh_fields(credential)
        if missing:
            return f"抖音开放平台凭证不完整，缺少: {','.join(missing)}"
        unsafe = wire_unsafe_credential_fields(credential)
        if unsafe:
            return (
                "抖音开放平台凭证字段必须是可打印 ASCII 字符串（不含空白）: "
                + ",".join(unsafe)
            )
        if not credential.get("open_id"):
            return "抖音开放平台凭证缺少 open_id；请先执行一次登录以补全"
        if not credential.get("access_token"):
            return "抖音 access_token 缺失；请先执行登录刷新令牌"
        return None

    async def _refresh(self, credential: dict, timeout: float) -> tuple[str, int | None]:
        """``POST /oauth/refresh_token/``.  Returns ``(kind, error_code)``.

        On ``ok`` the new ``access_token`` (and ``open_id``, when returned)
        replaces the stored value in place.  Nothing is written otherwise.
        """

        body = {
            "client_key": credential["client_key"],
            "client_secret": credential["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": credential["refresh_token"],
        }
        try:
            async with self._client(timeout) as client:
                resp = await client.post(_REFRESH_TOKEN_PATH, data=body)
        except httpx.HTTPError:
            return "transient", None
        data = parse_api_json(resp.content)
        kind, code = classify_refresh(data)
        if kind != "ok":
            return kind, code
        payload = data["data"]
        credential["access_token"] = payload["access_token"]
        returned_open_id = payload.get("open_id")
        if isinstance(returned_open_id, str) and returned_open_id:
            # The refresh response is authoritative for the app-scoped id.
            credential["open_id"] = returned_open_id
        expires_in = _int_or_none(payload.get("expires_in"))
        if expires_in is not None and expires_in > 0:
            credential["access_token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
        return "ok", 0

    async def login(self, account_id: int, credential: dict) -> bool:
        """用 refresh_token 刷新 access_token，原地写回 credential。"""
        self.last_login_error = None
        missing = self._missing_refresh_fields(credential)
        if missing:
            self.last_login_error = f"抖音开放平台凭证不完整，缺少: {','.join(missing)}"
            return False
        unsafe = wire_unsafe_credential_fields(credential)
        if unsafe:
            self.last_login_error = (
                "抖音开放平台凭证字段必须是可打印 ASCII 字符串（不含空白）: "
                + ",".join(unsafe)
            )
            return False
        kind, code = await self._refresh(
            credential, float(settings.douyin_open_api_timeout_seconds)
        )
        if kind == "ok":
            return True
        self.last_login_error = self._login_error(kind, code)
        return False

    @staticmethod
    def _login_error(kind: str, code: int | None) -> str:
        if code == _OAUTH_REFRESH_EXPIRED_CODE:
            return "抖音 refresh_token 已失效（error_code 10010）；请重新完成用户授权"
        if kind == "credential":
            return f"抖音凭证校验失败（error_code {code}）；请检查 client_key/secret 与应用能力"
        if kind == "transient":
            return "抖音刷新令牌接口暂不可用，请稍后重试"
        return "抖音刷新令牌接口返回异常，请稍后重试"

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """Probe the grant by refreshing it.

        Three states only.  ``HEALTHY``: the platform issued a token.
        ``EXPIRED``: the platform definitively refused the stored grant (its
        refresh token is invalid/expired, or the app credentials are wrong), so
        the account needs a fresh user authorisation.  ``UNKNOWN``: no verdict -
        the credential is not an Open Platform grant, it is locally incomplete
        (a configuration problem a re-login cannot fix), or the refresh timed
        out / answered unreadably.  ``UNKNOWN`` lets the scheduler's probe
        continue to the next adapter instead of overwriting it.

        The refresh is a real request and it updates ``access_token`` on the
        dict in place; the health scheduler does not persist that, so no
        stored state changes here.
        """

        if not is_api_credential(credential):
            return AccountHealth.UNKNOWN
        if self._missing_refresh_fields(credential):
            return AccountHealth.UNKNOWN
        if wire_unsafe_credential_fields(credential):
            # A malformed stored value: a configuration problem, not a verdict.
            return AccountHealth.UNKNOWN
        kind, _code = await self._refresh(
            credential, float(settings.douyin_open_api_timeout_seconds)
        )
        if kind == "ok":
            return AccountHealth.HEALTHY
        if kind == "credential":
            return AccountHealth.EXPIRED
        return AccountHealth.UNKNOWN

    # ---------------- 校验 ----------------

    def _validate_video(
        self, content: PublishContent
    ) -> tuple[Path | None, int | None, str | None]:
        """One readable, non-empty regular video file inside the asset root.

        Returns ``(path, size, error)``.  Readability is proven here by
        opening the file and reading from it, and the size comes from the same
        open descriptor: every later filesystem failure that can be caught
        locally is caught now, before any request is sent.  Nothing escapes.
        """

        candidate = Path(content.videos[0]).expanduser()
        if candidate.is_symlink():
            return None, None, "抖音视频文件不能是符号链接"
        try:
            video = candidate.resolve(strict=True)
            root = (
                settings.agent_asset_vault_root
                if content.exact_approval
                else settings.douyin_open_api_asset_root
            )
            root = root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None, None, "抖音视频不存在或不可读"
        mime, _ = mimetypes.guess_type(video.name)
        if (
            not video.is_relative_to(root)
            or video.suffix.lower() not in _ALLOWED_VIDEO_SUFFIXES
            or not (mime or "").startswith("video/")
        ):
            return None, None, f"抖音视频必须是 {root} 内的受支持本地视频"
        try:
            with video.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                readable = handle.read(1)
        except (OSError, ValueError):
            return None, None, "抖音视频不存在或不可读"
        if not size or not readable:
            return None, None, "抖音视频为空"
        if size > settings.douyin_open_api_max_video_bytes:
            return None, None, "抖音视频超过配置大小上限"
        return video, size, None

    # ---------------- 上传 ----------------

    @staticmethod
    def _upload_outcome(data: dict | None, reached: bool) -> tuple[str, bool]:
        """Classify a failed upload-step reply as ``(state, retryable)``.

        Upload steps create only an isolated file id, never a public post, so
        nothing here is ``outcome_uncertain``; the registry may fall back to
        the next adapter and a retry cannot duplicate a post.

        * ``credential``  the platform refused the token or the capability.
          Not retryable: a login is needed, and this adapter never refreshes
          inside the write path.
        * ``rejected``     a definitive non-zero answer (bad part size, file
          too large, quota used up, an unlisted code).  Not retryable, except
          for a rate limit, which is only pacing.
        * ``unknown``      a transient "系统繁忙" code, a timeout after the
          request was sent, or an unreadable body.  Retryable.
        * ``unreachable``  no connection was made.  Retryable.
        """

        if data is None:
            return ("unknown", True) if reached else ("unreachable", True)
        kind, _code = classify_response(data)
        if kind == "credential":
            return "credential", False
        if kind == "rate_limited":
            return "rejected", True
        if kind == "rejected":
            return "rejected", False
        return "unknown", True

    @classmethod
    def _upload_failure(
        cls, path: str, state: str, retryable: bool, data: dict | None, reached: bool
    ) -> PublishResult:
        """Build the safe-failure result for an upload step."""

        label = _UPLOAD_STEP_LABELS.get(path, path)
        raw: dict[str, object] = {
            "adapter": "douyin-open-api",
            "action": "video-upload",
            "stage": "upload",
            # No public write has started at this point.
            "write_started": False,
            "outcome": state,
        }
        if data is None:
            if reached:
                error = f"抖音视频上传未确认（{label}）：请求超时或返回无法解析；未产生作品，可重试"
            else:
                error = f"抖音视频上传未发出（{label}）：无法连接开放平台"
        else:
            kind, code = classify_response(data)
            raw.update(safe_error_detail(kind, code))
            if state == "credential":
                error = f"抖音 access_token 无效或已过期（{label}，error_code {code}）；请先刷新登录"
            else:
                error = f"抖音视频上传被拒绝（{label}，error_code {code}）"
        return PublishResult(
            success=False,
            effect_applied=False,
            outcome_uncertain=False,
            retryable=retryable,
            error=error,
            raw_response=raw,
        )

    async def _upload_direct(
        self, token: str, open_id: str, video: Path, timeout: float
    ) -> tuple[str | None, PublishResult | None]:
        """``upload_video`` in one request: ``(video_id, None)`` or ``(None, failure)``."""

        with video.open("rb") as handle:
            files = {"video": (video.name, handle, "application/octet-stream")}
            data, reached = await self._post_for_json(
                _UPLOAD_VIDEO_PATH,
                token=token,
                timeout=timeout,
                params={"open_id": open_id},
                files=files,
            )
        return self._read_video_id_step(_UPLOAD_VIDEO_PATH, data, reached)

    @classmethod
    def _read_video_id_step(
        cls, path: str, data: dict | None, reached: bool
    ) -> tuple[str | None, PublishResult | None]:
        """Finish an upload step that must yield a ``video_id``."""

        if data is not None and classify_response(data)[0] == "ok":
            video_id = extract_video_id(data)
            if video_id is not None:
                return video_id, None
            # error_code 0 without a usable video_id: the file id is unknown,
            # but still no public post.  Reported as an unknown, retryable
            # upload rather than an ok.
            return None, cls._upload_failure(path, "unknown", True, None, True)
        state, retryable = cls._upload_outcome(data, reached)
        return None, cls._upload_failure(path, state, retryable, data, reached)

    async def _upload_chunked(
        self, token: str, open_id: str, video: Path, timeout: float
    ) -> tuple[str | None, PublishResult | None]:
        """``init_video_part_upload`` -> ``upload_video_part`` xN -> ``complete``.

        Each part is read into memory on its own, so a multi-GB file never
        loads whole.
        """

        data, reached = await self._post_for_json(
            _INIT_PART_PATH,
            token=token,
            timeout=timeout,
            params={"open_id": open_id},
            json_body={},
        )
        if data is None or classify_response(data)[0] != "ok":
            state, retryable = self._upload_outcome(data, reached)
            return None, self._upload_failure(_INIT_PART_PATH, state, retryable, data, reached)
        body = data.get("data")
        upload_id = body.get("upload_id") if isinstance(body, dict) else None
        if not isinstance(upload_id, str) or not upload_id:
            return None, self._upload_failure(_INIT_PART_PATH, "unknown", True, None, True)

        total_bytes = video.stat().st_size
        boundaries = part_boundaries(total_bytes, settings.douyin_open_api_part_bytes)
        if not boundaries:
            return None, self._upload_failure(_UPLOAD_PART_PATH, "rejected", False, None, False)
        with video.open("rb") as handle:
            for index, (offset, length) in enumerate(boundaries):
                handle.seek(offset)
                chunk = handle.read(length)
                files = {"video": (video.name, chunk, "application/octet-stream")}
                data, reached = await self._post_for_json(
                    _UPLOAD_PART_PATH,
                    token=token,
                    timeout=timeout,
                    params={
                        "open_id": open_id,
                        "upload_id": upload_id,
                        "part_number": _PART_NUMBER_STARTS_AT + index,
                    },
                    files=files,
                )
                if data is None or classify_response(data)[0] != "ok":
                    state, retryable = self._upload_outcome(data, reached)
                    return None, self._upload_failure(
                        _UPLOAD_PART_PATH, state, retryable, data, reached
                    )

        data, reached = await self._post_for_json(
            _COMPLETE_PART_PATH,
            token=token,
            timeout=timeout,
            params={"open_id": open_id, "upload_id": upload_id},
            json_body={},
        )
        return self._read_video_id_step(_COMPLETE_PART_PATH, data, reached)

    # ---------------- PublisherBase ----------------

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """上传 → ``create_video``；后者是公开副作用的写边界。"""
        if content.exact_approval:
            try:
                self.render_agent_contract_payload(content)
            except AgentContractRendererUnavailable as exc:
                return PublishResult(success=False, retryable=False, error=str(exc))

        validation_error = validate_content(content)
        if validation_error:
            return PublishResult(success=False, retryable=False, error=validation_error)

        if not is_api_credential(credential):
            # Not this adapter's credential (e.g. a browser session for the
            # social-auto-upload path): step aside so the registry falls back.
            return PublishResult(
                success=False,
                error="账号未配置抖音开放平台凭证（client_key/refresh_token）",
                raw_response={
                    "adapter": "douyin-open-api",
                    "write_started": False,
                    "outcome": "not_applicable",
                },
            )
        credential_error = self._credential_error(credential)
        if credential_error:
            return PublishResult(
                success=False,
                retryable=False,
                error=credential_error,
                raw_response={"adapter": "douyin-open-api", "write_started": False},
            )
        open_id = credential["open_id"]

        video, video_bytes, video_error = self._validate_video(content)
        if video is None:
            assert video_error is not None
            return PublishResult(success=False, retryable=False, error=video_error)

        timeout = float(settings.douyin_open_api_timeout_seconds)
        token = credential["access_token"]
        if video_bytes > settings.douyin_open_api_direct_upload_bytes:
            video_id, failure = await self._upload_chunked(token, open_id, video, timeout)
        else:
            video_id, failure = await self._upload_direct(token, open_id, video, timeout)
        if video_id is None:
            assert failure is not None
            return failure

        return await self._create_video(
            token, open_id, video_id, content, video_bytes, timeout
        )

    async def _create_video(
        self,
        token: str,
        open_id: str,
        video_id: str,
        content: PublishContent,
        video_bytes: int,
        timeout: float,
    ) -> PublishResult:
        """``create_video`` is the public write; every unreadable outcome is uncertain."""
        extra = content.extra or {}
        body: dict[str, object] = {"video_id": video_id, "text": build_publish_text(content)}
        if extra.get("douyin_cover_tsp") is not None:
            body["cover_tsp"] = extra["douyin_cover_tsp"]
        if extra.get("douyin_private_status") is not None:
            body["private_status"] = extra["douyin_private_status"]
        if extra.get("douyin_download_type") is not None:
            body["download_type"] = extra["douyin_download_type"]

        raw: dict[str, object] = {
            "adapter": "douyin-open-api",
            "action": "video-create",
            "stage": "create_video",
            "video_bytes": video_bytes,
        }
        data, reached = await self._post_for_json(
            _CREATE_VIDEO_PATH,
            token=token,
            timeout=timeout,
            params={"open_id": open_id},
            json_body=body,
        )
        kind, code = classify_response(data)
        raw.update(safe_error_detail(kind, code))
        if data is not None and kind == "ok":
            raw["write_started"] = True
            item_id, _plain_video_id = extract_item(data)
            if item_id is None:
                # Accepted (error_code 0) but no usable identity: the item may
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
                        error="抖音已受理发布但未返回 item_id；请到抖音创作者后台核验后再决定是否重发",
                        raw_response=raw,
                    ),
                )
            raw["outcome"] = "confirmed"
            # No platform_url: the official pages give no documented share-URL
            # format for item_id/video_id, and a guessed link would be written
            # to the job, notifications and reports as if it were verified.
            # Operators take the link from the Douyin creator console.
            return self._journal(
                content,
                PublishResult(
                    success=True,
                    effect_applied=True,
                    platform_post_id=item_id,
                    error="发布已受理（后续有平台审核）；作品链接请到抖音创作者后台查看",
                    raw_response=raw,
                ),
            )

        # Not ok.
        if not reached:
            # No connection: the create request never left this machine.
            raw.update({"write_started": False, "outcome": "not_started"})
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=True,
                error="抖音创建作品请求未发出（无法连接开放平台）；可重试",
                raw_response=raw,
            )
        raw["write_started"] = True
        if data is not None and kind in ("credential", "rejected"):
            # A definitive non-zero answer: the platform refused, nothing was
            # published.  Not retryable - the same request yields the same code.
            raw["outcome"] = "rejected"
            label = "登录态失效，请先刷新令牌" if kind == "credential" else "平台拒绝"
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=False,
                error=f"抖音创建作品被拒绝（{label}，error_code {code}）",
                raw_response=raw,
            )
        if data is not None and kind == "rate_limited":
            raw["outcome"] = "rejected"
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=True,
                error=f"抖音创建作品被限流（error_code {code}）；未发布，可稍后重试",
                raw_response=raw,
            )
        # Sent, but no proof either way: transient "系统繁忙", an unreadable
        # body, or a timeout.  The item may exist - stop everything.
        raw["outcome"] = "unknown"
        raw["needs_reconciliation"] = True
        return self._journal(
            content,
            PublishResult(
                success=False,
                effect_applied=True,
                outcome_uncertain=True,
                retryable=False,
                error="抖音创建作品结果未知；请到抖音创作者后台核验后再决定是否手动重发",
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

