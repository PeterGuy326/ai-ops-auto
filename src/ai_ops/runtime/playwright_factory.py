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
