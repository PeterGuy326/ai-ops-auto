"""Task F · API Key 鉴权依赖。

底层逻辑：API 是攻击面顶点。`api/main.py` 暴露的 POST /accounts/{id}/login、
POST /jobs/{id}/run、DELETE /accounts/{id} 等接口若无任何鉴权，任何能访问
端口的人都能触发扫码 / 改 cookie / 删账号——部署即裸奔。

设计原则：
1. 路由级 Depends，而非全局 middleware——避免影响 /health（探活）、/ui/*
   （内置 dashboard）、/admin/*（React 静态资源）、/docs（dev 体验）等公开路径。
2. ``hmac.compare_digest`` 常量时间比较，防时序攻击。直接 == 比较会按字符长度
   提前返回，攻击者可通过时延猜出 key 前缀。
3. ``settings.api_key == ""`` 视为 dev 模式自动放行——本地调试无需配置；
   首次命中时 logger.warning 一次（避免每请求刷屏），生产部署必须设非空。
4. ``APIKeyHeader(auto_error=False)``——dev 模式下不存在 header 也不该 422，
   由本依赖自行决定 401 vs 放行。
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from ..config import settings

logger = logging.getLogger(__name__)

_API_KEY_HEADER_NAME = "X-API-Key"

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
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": _API_KEY_HEADER_NAME},
        )

    return provided


def _reset_dev_warn_for_test() -> None:
    """测试辅助：重置 dev warn 标志位，避免测试间互相影响。"""
    global _dev_mode_warned
    _dev_mode_warned = False
