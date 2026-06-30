"""Playwright/Patchright 引擎工厂。

in-process 场景（直接 import Playwright 写 Publisher，如 ZhihuPublisher）下，
sitecustomize.py 的 PYTHONPATH 注入不会生效——必须显式按 settings.browser_engine
动态 import 对应的库。
"""
from __future__ import annotations

from ..config import settings


def get_async_playwright():
    """按当前 settings.browser_engine 返回对应的 async_playwright 上下文管理器。

    用法：
        from ..runtime.playwright_factory import get_async_playwright
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.browser_headless)
            ...
    """
    engine = settings.browser_engine
    if engine == "patchright":
        from patchright.async_api import async_playwright
        return async_playwright
    if engine == "camoufox":
        # Camoufox 不是 Playwright API，需要业务代码用 AsyncCamoufox 上下文
        # 这里回退到原生 playwright + Firefox 提示
        raise RuntimeError(
            "camoufox 不兼容 Playwright API，请直接用 "
            "`from camoufox.async_api import AsyncCamoufox`"
        )
    # 默认 / playwright_chromium / playwright_chrome_channel
    from playwright.async_api import async_playwright
    return async_playwright


def get_launch_kwargs() -> dict:
    """根据 settings 生成 chromium.launch() 的关键字参数。

    channel 仅在 playwright_chrome_channel 时设置（使用本地真 Chrome）。
    proxy 在配置了 browser_proxy 时设置。
    """
    kwargs: dict = {"headless": settings.browser_headless}
    if settings.browser_engine == "playwright_chrome_channel":
        kwargs["channel"] = "chrome"
    if settings.browser_proxy:
        kwargs["proxy"] = {"server": settings.browser_proxy}
    return kwargs


async def resolve_cdp_ws_url(http_url: str) -> str:
    """把 http 调试端点解析成可直连的 ws 端点（绕 Chrome 111+ 的 CDP 400）。

    实测坑（见记忆 tiktok-seller-cdp-bridge）：
      - connect_over_cdp("http://127.0.0.1:9333") 会 400（origin 校验）。
        解法：先 GET /json/version 拿 webSocketDebuggerUrl，再直连那个 ws。
      - ws 里若是 //localhost，会被解析成 IPv6 ::1 导致 ECONNREFUSED，
        统一替换成 127.0.0.1。
    """
    import httpx

    base = http_url.rstrip("/")
    # trust_env=False：本地调试端点必须直连，绝不能走系统 HTTP 代理
    # （httpx 不认 NO_PROXY 里的 127.* glob，会把 127.0.0.1 也塞进代理 → 502）
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(f"{base}/json/version")
        resp.raise_for_status()
        ws = resp.json().get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError(
            f"{base}/json/version 没返回 webSocketDebuggerUrl（Chrome 没开 --remote-debugging-port？）"
        )
    return ws.replace("localhost", "127.0.0.1").replace("//[::1]", "//127.0.0.1")
