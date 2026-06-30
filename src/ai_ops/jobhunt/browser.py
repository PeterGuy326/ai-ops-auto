"""jobhunt 浏览器接入层 —— 统一「自启浏览器+注入cookie」与「CDP 复用真 Chrome」两种模式。

为什么要这层：Boss 直聘风控严，自启的干净浏览器 + 注入 cookie 容易被识别、弹验证。
最稳的是直接借用户本人已登录 Boss 的真 Chrome——通过 CDP 远程调试端口接进去。
配了 settings.browser_cdp_url 就走 CDP；否则回退老路子（launch + add_cookies）。

用法：
    async with open_page(credential) as page:
        await page.goto(...)

清理语义按模式区分：
  - CDP 模式：只 close 我们新开的 page + 断开连接，**绝不关用户的真 Chrome**。
  - 自启模式：close 整个 browser（本来就是我们起的）。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from ..config import settings
from ..runtime.playwright_factory import (
    get_async_playwright,
    get_launch_kwargs,
    resolve_cdp_ws_url,
)


def cdp_enabled() -> bool:
    return bool(settings.browser_cdp_url)


def _ensure_localhost_no_proxy() -> None:
    """让 playwright/patchright 的 node driver 连本地 CDP ws 时绕开系统代理。

    系统 NO_PROXY 常写成 `127.*` glob，node 的代理逻辑不认，会把 ws://127.0.0.1
    也塞进 HTTP_PROXY 导致 CDP 命令传输超时。这里追加精确的 127.0.0.1/localhost，
    不动 HTTP_PROXY（Anthropic 等外网仍走代理，LLM 打分不受影响）。
    """
    for key in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(key, "")
        if "127.0.0.1" in cur and "localhost" in cur:
            continue
        os.environ[key] = (cur + ",127.0.0.1,localhost").lstrip(",") if cur else "127.0.0.1,localhost"


@asynccontextmanager
async def open_page(credential: dict | None = None, *, headless: bool | None = None):
    """产出一个可用的 page。CDP 模式复用真 Chrome 登录态，credential 可为空。"""
    if cdp_enabled():
        _ensure_localhost_no_proxy()  # 必须在 driver 子进程 spawn 前设置
        # CDP 连 Boss 必须用 patchright：普通 playwright 的 Runtime.enable 泄漏会被反爬识别，
        # 结果页一加载就重定向回首页。patchright 用隔离上下文绕过（实测两次有效）。
        from patchright.async_api import async_playwright
    else:
        async_playwright = get_async_playwright()
    async with async_playwright() as pw:
        if cdp_enabled():
            ws = await resolve_cdp_ws_url(settings.browser_cdp_url)
            browser = await pw.chromium.connect_over_cdp(ws)
            # 复用已登录的 context（用户真 Chrome 的默认 context）；没有才新建
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            # 关键：复用用户已有的真实标签页。CDP 新开的空白页 goto 会被 Boss 拦在 about:blank
            # （无 opener 的自动化标签被反爬挡导航），用现成标签则导航正常。
            reused = bool(ctx.pages)
            page = ctx.pages[0] if reused else await ctx.new_page()
            try:
                await page.bring_to_front()
            except Exception:
                pass
            try:
                yield page
            finally:
                # 复用的是用户的标签页，别关；只有我们新建的才关。绝不 close 用户的 Chrome。
                if not reused:
                    try:
                        await page.close()
                    except Exception:
                        pass
                await browser.close()  # 只断开 CDP 连接，不关浏览器
        else:
            kwargs = get_launch_kwargs()
            if headless is not None:
                kwargs["headless"] = headless
            browser = await pw.chromium.launch(**kwargs)
            try:
                ctx = await browser.new_context()
                if credential and credential.get("cookies"):
                    await ctx.add_cookies(credential["cookies"])
                page = await ctx.new_page()
                yield page
            finally:
                await browser.close()
