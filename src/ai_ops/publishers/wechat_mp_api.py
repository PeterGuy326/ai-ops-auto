"""微信公众号官方 API Publisher — 草稿箱（draft）+ 发布能力（freepublish）。

与浏览器版 `wechat_mp.py`（persistent-context，仅保存草稿）不同，本适配器走
微信公众平台**文档化官方接口**（`api.weixin.qq.com`），不依赖浏览器、cookie 或
非公开端点：

  1. ``POST /cgi-bin/stable_token``          appid/secret 换 access_token
  2. ``POST /cgi-bin/material/add_material`` 首图上传为永久素材 → thumb_media_id
  3. ``POST /cgi-bin/draft/add``             创建草稿 → draft media_id
  4. ``POST /cgi-bin/freepublish/submit``    发布 → publish_id（异步）
  5. ``POST /cgi-bin/freepublish/get``       有界轮询回查 article_id/article_url

发布语义说明：
  官方「发布」（freepublish）把文章发表到公众号主页并生成可回查 URL，
  **不等于群发**（masssend，推送给粉丝、订阅号每天 1 次）。本适配器绝不调用
  群发接口。发布内容可通过 freepublish/delete 撤回，但已推达读者不可撤——
  因此整个链路默认关闭（``WECHAT_MP_API_PUBLISHER_ENABLED=false``），
  完成专用账号 canary 前不得进入真实写主链路。

失败语义（与 zhihu_cli 同一纪律）：
  - 服务端明确 errcode → 确定失败，无发布副作用；draft 已存在时记
    ``effect_applied=True`` 并保留 draft media_id 供人工清理，绝不 fallback。
  - 请求已发出但响应未知（超时/连接中断）→ ``outcome_uncertain=True``、
    ``retryable=False``，停止一切自动重试，要求人工到 mp 后台核验。
  - 凭证（appid/secret/access_token）永不进入 raw_response / 日志。

Agent contract：本适配器暂不提供 exact renderer descriptor；Agent 精确计划
阶段对该平台继续 fail closed（见 README「项目方向」），仅 legacy 管理面可用。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path

import httpx

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from .base import PublisherBase


_API_BASE = "https://api.weixin.qq.com"
_STABLE_TOKEN_PATH = "/cgi-bin/stable_token"
_ADD_MATERIAL_PATH = "/cgi-bin/material/add_material"
_DRAFT_ADD_PATH = "/cgi-bin/draft/add"
_FREEPUBLISH_SUBMIT_PATH = "/cgi-bin/freepublish/submit"
_FREEPUBLISH_GET_PATH = "/cgi-bin/freepublish/get"

# 凭证类 errcode：secret 失效/账号无权限，health 判 EXPIRED；
# 40164 是 IP 白名单问题，属部署配置错误，保持 UNKNOWN 并提示。
_CREDENTIAL_EXPIRED_ERRCODES = {40001, 40125, 41002, 41004, 42001}
_IP_ALLOWLIST_ERRCODE = 40164

_MAX_TITLE_CHARS = 64
_ALLOWED_CONTENT_TYPES = {ContentType.IMAGE_TEXT, ContentType.LONG_ARTICLE}
_ALLOWED_EXTRA_KEYS = ("author", "digest", "content_source_url")

# markdown 渲染与浏览器版 wechat_mp.py、知乎 exact renderer 共用同一 pinned 版本
# （pyproject.toml dependencies 里 markdown==3.10.3），保证同一内容在两条链路上
# 产出一致 HTML。


def _render_body_html(body: str) -> str:
    import markdown

    return markdown.markdown(body, extensions=["fenced_code", "tables", "nl2br"])


class WechatMpApiPublisher(PublisherBase):
    """官方 API 公众号发布器，默认关闭，启用后仍属 Stub 级 canary。"""

    platform = Platform.WECHAT_MP
    kind = PublisherKind.WECHAT_MP_API

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
            headers={"User-Agent": "ai-ops-auto-wechat-mp-api/0.1"},
        )

    @staticmethod
    def _parse_api_json(payload: bytes | str) -> dict | None:
        """公众号 API 永远返回 JSON 对象；解析失败按未知处理。"""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _fetch_access_token(
        self, credential: dict, timeout: float
    ) -> tuple[str | None, int | None, str | None]:
        """stable_token 换取 access_token。返回 (token, errcode, error)。

        token 只在内存传递，绝不写入日志或 raw_response。
        """
        appid = credential.get("appid")
        secret = credential.get("secret")
        if not appid or not secret:
            return None, None, "公众号凭证缺少 appid/secret，请先在账号里配置"
        body = {"grant_type": "client_credential", "appid": appid, "secret": secret}
        try:
            async with self._client(timeout) as client:
                resp = await client.post(_STABLE_TOKEN_PATH, json=body)
        except httpx.HTTPError as exc:
            return None, None, f"stable_token 请求失败: {type(exc).__name__}"
        data = self._parse_api_json(resp.content)
        if data is None:
            return None, None, "stable_token 返回不是合法 JSON"
        errcode = data.get("errcode")
        if errcode:
            return None, int(errcode), f"stable_token 失败（errcode {errcode}）"
        token = data.get("access_token")
        if not token or not isinstance(token, str):
            return None, None, "stable_token 返回缺少 access_token"
        return token, None, None

    async def _post_json(
        self,
        path: str,
        token: str,
        payload: dict,
        timeout: float,
    ) -> tuple[dict | None, bool, str | None]:
        """POST JSON 并解析；返回 (data, request_reached_server, error)。

        ``request_reached_server=False`` 只在连接根本没建立时出现，此时可以
        确定无副作用；其余（超时/5xx/坏 JSON）都按"服务端可能已受理"处理。
        """
        try:
            async with self._client(timeout) as client:
                resp = await client.post(
                    path, params={"access_token": token}, json=payload
                )
        except httpx.TimeoutException:
            return None, True, f"{path} 请求超时，服务端结果未知"
        except httpx.HTTPError as exc:
            return None, True, f"{path} 请求失败且结果未知: {type(exc).__name__}"
        data = self._parse_api_json(resp.content)
        if data is None:
            return None, True, f"{path} 返回不是合法 JSON（HTTP {resp.status_code}）"
        return data, True, None

    # ---------------- 校验 ----------------

    def _validate_content(self, content: PublishContent) -> str | None:
        if content.exact_approval:
            return "微信公众号 API 暂无 exact renderer；Agent 精确计划已 fail closed"
        if content.content_type not in _ALLOWED_CONTENT_TYPES:
            return "微信公众号 API 仅支持图文/长文内容"
        if content.videos:
            return "微信公众号 API 不支持视频资产"
        if not content.title.strip():
            return "公众号标题为空"
        if len(content.title) > _MAX_TITLE_CHARS:
            return f"公众号标题超过 {_MAX_TITLE_CHARS} 字上限"
        if not content.body.strip():
            return "公众号正文为空"
        extra = content.extra or {}
        unknown = set(extra).difference(_ALLOWED_EXTRA_KEYS)
        if unknown:
            return f"公众号 API 不支持的 extra 字段: {sorted(unknown)}"
        digest = extra.get("digest")
        if digest is not None and (not isinstance(digest, str) or len(digest) > 120):
            return "公众号摘要（digest）必须是不超过 120 字的字符串"
        for key in ("author", "content_source_url"):
            value = extra.get(key)
            if value is not None and not isinstance(value, str):
                return f"公众号 extra.{key} 必须是字符串"
        if not content.images:
            return "公众号图文草稿必须提供封面图（images[0]）"
        if len(content.images) > 1:
            return (
                "公众号 API canary 仅支持封面图 1 张；正文内嵌图片请使用可公网访问的 URL"
            )
        return None

    def _validate_cover(self, image: str) -> tuple[Path | None, str | None]:
        candidate = Path(image).expanduser()
        if candidate.is_symlink():
            return None, "公众号封面图不能是符号链接"
        try:
            path = candidate.resolve(strict=True)
        except OSError:
            return None, "公众号封面图不存在或不可读"
        if not path.is_file():
            return None, "公众号封面图不是常规文件"
        mime, _ = mimetypes.guess_type(path.name)
        if path.suffix.lower() not in {".jpg", ".jpeg"} or mime != "image/jpeg":
            return None, "公众号 API canary 仅接受 JPEG 封面图"
        return path, None

    # ---------------- PublisherBase ----------------

    async def login(self, account_id: int, credential: dict) -> bool:
        """官方 API 无扫码概念；login 等价于验证 appid/secret 可换 token。"""
        self.last_login_error = None
        token, errcode, error = await self._fetch_access_token(
            credential, float(settings.wechat_mp_api_timeout_seconds)
        )
        if token is not None:
            return True
        if errcode == _IP_ALLOWLIST_ERRCODE:
            self.last_login_error = (
                "服务器 IP 未加入公众号后台 IP 白名单（errcode 40164）"
            )
        else:
            self.last_login_error = error or "公众号凭证验证失败"
        return False

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        if not credential.get("appid") or not credential.get("secret"):
            return AccountHealth.EXPIRED
        token, errcode, _ = await self._fetch_access_token(
            credential, float(settings.wechat_mp_api_timeout_seconds)
        )
        if token is not None:
            return AccountHealth.HEALTHY
        if errcode in _CREDENTIAL_EXPIRED_ERRCODES:
            return AccountHealth.EXPIRED
        return AccountHealth.UNKNOWN

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """草稿 → 发布两段式；发布接口是外部副作用写边界。"""
        validation_error = self._validate_content(content)
        if validation_error:
            return PublishResult(success=False, retryable=False, error=validation_error)

        cover_path, cover_error = self._validate_cover(content.images[0])
        if cover_path is None:
            return PublishResult(success=False, retryable=False, error=cover_error)

        body_html = _render_body_html(content.body)
        if len(body_html.encode("utf-8")) > settings.wechat_mp_api_max_content_bytes:
            return PublishResult(
                success=False,
                retryable=False,
                error="公众号渲染后正文超过配置的内容字节上限",
            )

        timeout = float(settings.wechat_mp_api_timeout_seconds)
        token, errcode, error = await self._fetch_access_token(credential, timeout)
        if token is None:
            return PublishResult(
                success=False,
                retryable=False,
                error=error or "获取 access_token 失败",
                raw_response={"stage": "token", "errcode": errcode},
            )

        raw: dict[str, object] = {"adapter": "wechat-mp-api", "write_started": False}

        # ---- 1. 封面 → 永久素材 ----
        media_id, material_error, request_started = await self._upload_cover(
            token, cover_path, timeout
        )
        if media_id is None:
            # 素材上传是隔离的库内副作用，不是发布；确定性失败可安全终止。
            raw.update({"stage": "cover_upload", "request_started": request_started})
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=request_started and material_error is None,
                retryable=False,
                error=material_error or "封面素材上传失败",
                raw_response=raw,
            )
        raw["cover_material_created"] = True

        # ---- 2. draft/add ----
        draft_media_id, draft_err, draft_unknown, draft_raw = await self._create_draft(
            token, content, media_id, body_html, timeout
        )
        raw.update(draft_raw)
        if draft_media_id is None:
            if draft_unknown:
                # 草稿写入结果未知：可能已在服务端落库，禁止 fallback/重试。
                raw["write_started"] = True
                return PublishResult(
                    success=False,
                    effect_applied=None,
                    outcome_uncertain=True,
                    retryable=False,
                    error="公众号草稿创建结果未知；请到 mp 后台核验后再决定是否重试",
                    raw_response=raw,
                )
            raw["write_started"] = False
            return PublishResult(
                success=False,
                effect_applied=False,
                outcome_uncertain=False,
                retryable=False,
                error=draft_err or "公众号草稿创建失败",
                raw_response=raw,
            )
        raw["draft_media_id"] = draft_media_id
        raw["write_started"] = True

        # ---- 3. freepublish/submit（公开发布写边界）----
        return await self._submit_publish(token, draft_media_id, raw, content, timeout)

    # ---------------- 内部步骤 ----------------

    async def _upload_cover(
        self, token: str, cover: Path, timeout: float
    ) -> tuple[str | None, str | None, bool]:
        """material/add_material 上传 JPEG 封面 → 永久 media_id。"""
        mime = mimetypes.guess_type(cover.name)[0] or "image/jpeg"
        try:
            async with self._client(timeout) as client:
                resp = await client.post(
                    _ADD_MATERIAL_PATH,
                    params={"access_token": token, "type": "image"},
                    files={"media": (cover.name, cover.read_bytes(), mime)},
                )
        except httpx.TimeoutException:
            return None, "封面上传超时，素材结果未知", True
        except httpx.HTTPError as exc:
            return None, f"封面上传失败且结果未知: {type(exc).__name__}", True
        data = self._parse_api_json(resp.content)
        if data is None:
            return None, f"封面上传返回不是合法 JSON（HTTP {resp.status_code}）", True
        errcode = data.get("errcode")
        if errcode:
            return None, f"封面上传失败（errcode {errcode}）", True
        media_id = data.get("media_id")
        if not media_id or not isinstance(media_id, str):
            return None, "封面上传返回缺少 media_id", True
        return media_id, None, True

    async def _create_draft(
        self,
        token: str,
        content: PublishContent,
        thumb_media_id: str,
        body_html: str,
        timeout: float,
    ) -> tuple[str | None, str | None, bool, dict]:
        """draft/add 创建单图文草稿。返回 (media_id, error, unknown, raw)。"""
        extra = content.extra or {}
        article: dict[str, object] = {
            "title": content.title.strip(),
            "content": body_html,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        if extra.get("author"):
            article["author"] = extra["author"]
        if extra.get("digest"):
            article["digest"] = extra["digest"]
        if extra.get("content_source_url"):
            article["content_source_url"] = extra["content_source_url"]

        data, reached, error = await self._post_json(
            _DRAFT_ADD_PATH, token, {"articles": [article]}, timeout
        )
        raw: dict[str, object] = {"stage": "draft_add", "request_reached": reached}
        if data is None:
            return None, error, True, raw
        errcode = data.get("errcode")
        if errcode:
            raw["errcode"] = int(errcode)
            return (
                None,
                f"草稿创建失败（errcode {errcode}）",
                False,
                raw,
            )
        media_id = data.get("media_id")
        if not media_id or not isinstance(media_id, str):
            return None, "草稿创建返回缺少 media_id", True, raw
        return media_id, None, False, raw

    async def _submit_publish(
        self,
        token: str,
        draft_media_id: str,
        raw: dict[str, object],
        content: PublishContent,
        timeout: float,
    ) -> PublishResult:
        """freepublish/submit + freepublish/get 有界回查。"""
        data, reached, error = await self._post_json(
            _FREEPUBLISH_SUBMIT_PATH, token, {"media_id": draft_media_id}, timeout
        )
        raw["publish_submitted"] = reached
        publish_id: str | None = None
        if data is not None:
            errcode = data.get("errcode")
            if not errcode:
                candidate = data.get("publish_id")
                if isinstance(candidate, str) and candidate:
                    publish_id = candidate
                    raw["publish_id"] = publish_id
            else:
                raw["publish_errcode"] = int(errcode)

        if publish_id is None:
            if data is not None:
                # 服务端明确拒绝发布：草稿副作用已发生，绝不 fallback 重发。
                return PublishResult(
                    success=False,
                    effect_applied=True,
                    outcome_uncertain=False,
                    retryable=False,
                    error=error or f"发布被拒绝（errcode {raw.get('publish_errcode')}）",
                    raw_response=raw,
                )
            # 响应未知：发布可能已受理，转人工核验。
            return PublishResult(
                success=False,
                effect_applied=True,
                outcome_uncertain=True,
                retryable=False,
                error=(
                    "freepublish 提交结果未知；草稿已存在，请到 mp 后台「发表记录」核验"
                ),
                raw_response=raw,
            )

        # ---- 4. 有界回查 article_url（发布是异步的，允许暂时 pending）----
        article_id, article_url = await self._poll_publish_status(
            token, publish_id, timeout
        )
        if article_url:
            raw["outcome"] = "published"
            raw["article_id"] = article_id
        else:
            raw["outcome"] = "publish_pending"
        return PublishResult(
            success=True,
            platform_post_id=article_id or publish_id,
            platform_url=article_url,
            error=None if article_url else "发布已受理，文章 URL 尚未生成（publish_pending）",
            raw_response=raw,
        )

    async def _poll_publish_status(
        self, token: str, publish_id: str, timeout: float
    ) -> tuple[str | None, str | None]:
        attempts = settings.wechat_mp_api_publish_poll_attempts
        interval = settings.wechat_mp_api_publish_poll_interval_seconds
        for _ in range(attempts):
            await asyncio.sleep(interval)
            data, _reached, _error = await self._post_json(
                _FREEPUBLISH_GET_PATH, token, {"publish_id": publish_id}, timeout
            )
            if data is None or data.get("errcode"):
                continue
            statuses = data.get("publish_status")
            if not isinstance(statuses, list):
                continue
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                url = status.get("article_url")
                article_id = status.get("article_id")
                if isinstance(url, str) and url.startswith("http"):
                    return (
                        article_id if isinstance(article_id, str) else None,
                        url,
                    )
        return None, None

    # ---------------- 运维辅助 ----------------

    async def get_publish_status(
        self, credential: dict, publish_id: str
    ) -> dict | None:
        """人工对账入口：返回 freepublish/get 的原始状态（脱敏后）。

        供运营在 outcome_uncertain 或 publish_pending 后手动核对，不进自动重试。
        """
        timeout = float(settings.wechat_mp_api_timeout_seconds)
        token, _errcode, _error = await self._fetch_access_token(credential, timeout)
        if token is None:
            return None
        data, _reached, _error = await self._post_json(
            _FREEPUBLISH_GET_PATH,
            token,
            {"publish_id": publish_id},
            timeout,
        )
        if data is None or data.get("errcode"):
            return None
        return data
