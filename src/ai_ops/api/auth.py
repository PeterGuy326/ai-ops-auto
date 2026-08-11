"""Task F · API Key 鉴权依赖。

底层逻辑：API 是攻击面顶点。`api/main.py` 暴露的 POST /accounts/{id}/login、
POST /jobs/{id}/run、DELETE /accounts/{id} 等接口若无任何鉴权，任何能访问
端口的人都能触发扫码 / 改 cookie / 删账号——部署即裸奔。

设计原则：
1. JSON API 使用路由级 Depends，避免影响 /health、/docs；/ui/* 则由独立的
   签名 session middleware 统一保护，防止模板路由遗漏鉴权。
2. ``hmac.compare_digest`` 常量时间比较，防时序攻击。直接 == 比较会按字符长度
   提前返回，攻击者可通过时延猜出 key 前缀。
3. ``settings.api_key == ""`` 视为 dev 模式自动放行——本地调试无需配置；
   首次命中时 logger.warning 一次（避免每请求刷屏），生产部署必须设非空。
4. ``APIKeyHeader(auto_error=False)``——dev 模式下不存在 header 也不该 422，
   由本依赖自行决定 401 vs 放行。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import settings

logger = logging.getLogger(__name__)

_API_KEY_HEADER_NAME = "X-API-Key"

# UI session 只保存随机 session id + 签发时间，不保存 API key（包括可逆编码）。
# 8 小时后服务端强制失效；cookie 的 Max-Age 只是浏览器侧的第二道限制。
UI_SESSION_COOKIE = "ai_ops_ui_session"
UI_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
_UI_SESSION_VERSION = "v1"
_UI_CLOCK_SKEW_SECONDS = 30

# auto_error=False：缺 header 时返回 None，让本依赖自己决定（dev 放行 / prod 401）
_api_key_scheme = APIKeyHeader(name=_API_KEY_HEADER_NAME, auto_error=False)

# 模块级"已 warn 标志"，防止 dev 模式每个请求都刷 warning（吵且没意义）
_dev_mode_warned = False


def api_key_dev_mode() -> bool:
    """是否处于 dev 模式（api_key 未配置）。

    暴露给外部代码（如 health 探针 / 测试）判断当前部署是否开启了鉴权。
    """
    return settings.api_key == ""


def _warn_dev_mode_once() -> None:
    """仅打印一次 dev 模式 warning，避免每请求刷屏。"""
    global _dev_mode_warned
    if not _dev_mode_warned:
        logger.warning(
            "dev 模式: API key 未配置（settings.api_key 为空），所有受保护路由对外开放。"
            "生产部署必须通过 env API_KEY=... 注入非空值。"
        )
        _dev_mode_warned = True


def api_keys_match(provided: str | None) -> bool:
    """用常量时间比较验证 API key，不把 key 写入日志或错误响应。"""
    expected = settings.api_key
    if expected == "" or not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


def require_api_key(provided: str | None = Depends(_api_key_scheme)) -> str:
    """FastAPI 依赖：校验 X-API-Key header。

    Returns:
        通过校验后返回 provided key（dev 模式返回 ""，但调用方一般忽略）。

    Raises:
        HTTPException 401: key 缺失或不匹配（非 dev 模式下）。
    """
    expected = settings.api_key

    # dev 模式：不论是否带 header 一律放行 + warn 一次
    if expected == "":
        _warn_dev_mode_once()
        return provided or ""

    # 生产模式：必须带 header
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": _API_KEY_HEADER_NAME},
        )

    # 常量时间比较：防时序攻击（直接 == 会按字符长度提前返回）
    if not api_keys_match(provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": _API_KEY_HEADER_NAME},
        )

    return provided


@dataclass(frozen=True)
class UISession:
    """通过签名校验后的 UI 会话（不包含任何凭证）。"""

    session_id: str
    issued_at: int


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        (value + padding).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


def _ui_signing_key() -> bytes:
    """从 API key 单向派生 UI 专用签名 key，避免跨用途复用。"""
    return hmac.new(
        settings.api_key.encode("utf-8"),
        b"ai-ops-auto/ui-session/v1",
        hashlib.sha256,
    ).digest()


def _sign_ui_value(purpose: bytes, value: str) -> str:
    signature = hmac.new(
        _ui_signing_key(),
        purpose + b"\x00" + value.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _urlsafe_b64encode(signature)


def create_ui_session_cookie(*, now: int | None = None) -> str:
    """创建短期、有签名且不含 API key 的 UI session cookie 值。"""
    issued_at = int(time.time() if now is None else now)
    session_id = secrets.token_urlsafe(24)
    payload = f"{_UI_SESSION_VERSION}.{issued_at}.{session_id}"
    return f"{payload}.{_sign_ui_value(b'session', payload)}"


def validate_ui_session_cookie(
    token: str | None,
    *,
    now: int | None = None,
) -> UISession | None:
    """验证 UI session 的签名、格式和服务端有效期。"""
    if settings.api_key == "" or not token:
        return None
    try:
        version, issued_text, session_id, signature = token.split(".", 3)
        issued_at = int(issued_text)
    except (TypeError, ValueError):
        return None

    if version != _UI_SESSION_VERSION or not session_id or not signature:
        return None

    payload = f"{version}.{issued_at}.{session_id}"
    expected_signature = _sign_ui_value(b"session", payload)
    try:
        signature_ok = hmac.compare_digest(
            _urlsafe_b64decode(signature),
            _urlsafe_b64decode(expected_signature),
        )
    except (ValueError, UnicodeError):
        return None
    if not signature_ok:
        return None

    current_time = int(time.time() if now is None else now)
    age = current_time - issued_at
    if age < -_UI_CLOCK_SKEW_SECONDS or age > UI_SESSION_MAX_AGE_SECONDS:
        return None
    return UISession(session_id=session_id, issued_at=issued_at)


def ui_csrf_token(session: UISession) -> str:
    """生成与指定会话绑定的 CSRF token。"""
    value = f"{session.issued_at}.{session.session_id}"
    return _sign_ui_value(b"csrf", value)


def verify_ui_csrf(request: Request, provided: str | None) -> None:
    """拒绝未携带当前 UI session 对应 CSRF token 的写请求。"""
    if api_key_dev_mode():
        # 空 API_KEY 是显式 dev 模式，保持原有的本地无登录/无 token 易用性。
        _warn_dev_mode_once()
        return

    session = getattr(request.state, "ui_session", None)
    if not isinstance(session, UISession):
        raise HTTPException(status_code=401, detail="UI login required")
    expected = ui_csrf_token(session)
    if not provided:
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    try:
        valid = hmac.compare_digest(
            provided.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, UnicodeError):
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def set_ui_session_cookie(response, token: str) -> None:
    """写入仅限 /ui 的安全 cookie。API_KEY 模式要求 HTTPS。"""
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value=token,
        max_age=UI_SESSION_MAX_AGE_SECONDS,
        path="/ui",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_ui_session_cookie(response) -> None:
    response.set_cookie(
        key=UI_SESSION_COOKIE,
        value="",
        max_age=0,
        expires=0,
        path="/ui",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _canonical_ui_path(path: str) -> str | None:
    """识别 /ui 及会被 StripApiPrefixMiddleware 改写的 /api/ui。"""
    if path.startswith("/api/"):
        path = path[4:]
    if path == "/ui" or path.startswith("/ui/"):
        return path
    return None


class UIAuthMiddleware(BaseHTTPMiddleware):
    """API_KEY 非空时，为所有 /ui 页面统一实施 session 鉴权。"""

    async def dispatch(self, request: Request, call_next):
        ui_path = _canonical_ui_path(request.url.path)
        if ui_path is None:
            return await call_next(request)

        enabled = not api_key_dev_mode()
        request.state.ui_auth_enabled = enabled
        request.state.ui_session = None
        request.state.ui_csrf_token = ""

        if not enabled:
            _warn_dev_mode_once()
            return await call_next(request)

        # 登录页是唯一无需已有 session 的 UI 页面；登录 POST 自身校验 API key。
        if ui_path == "/ui/login":
            return await call_next(request)

        session = validate_ui_session_cookie(request.cookies.get(UI_SESSION_COOKIE))
        if session is None:
            if request.method in {"GET", "HEAD"}:
                return RedirectResponse("/ui/login", status_code=303)
            return HTMLResponse("UI login required", status_code=401)

        request.state.ui_session = session
        request.state.ui_csrf_token = ui_csrf_token(session)
        return await call_next(request)


def _reset_dev_warn_for_test() -> None:
    """测试辅助：重置 dev warn 标志位，避免测试间互相影响。"""
    global _dev_mode_warned
    _dev_mode_warned = False
