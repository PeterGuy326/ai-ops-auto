"""今日头条 / 头条号 Publisher — 自建实现（开源缺口确认后的合理例外）。

gh search 多关键词穷尽（toutiao publish/api/skill 等），唯二命中：
  - axdlee/toutiao-publish (4⭐, shell skill)
  - OceanBBBBbb/auto_write_toutiaohao (6⭐, 老代码)
都不够成熟，所以自建。

走和 ZhihuPublisher 一致的"Playwright + patchright factory"路径，
复用 runtime/playwright_factory 反检测能力，不重复造轮子。

发布入口：https://mp.toutiao.com/profile_v4/graphic/publish
凭证格式：与 ZhihuPublisher 相同 ({"cookies": [...]})
"""
from __future__ import annotations

import asyncio

from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


HOME_URL = "https://mp.toutiao.com/"
WRITE_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"

# 头条号编辑器关键选择器（基于 2026 推测版，上线前需用真账号校准）
SEL_TITLE_INPUT = "input[placeholder*='标题'], textarea[placeholder*='标题']"
# 头条用 ProseMirror 富文本（Doyin/字节系通用）
SEL_BODY_EDITOR = "div.ProseMirror[contenteditable='true']"
SEL_PUBLISH_BTN = "button:has-text('发布')"
SEL_CONFIRM_BTN = "button:has-text('确认发布')"
SEL_LOGGED_IN_AVATAR = ".user-avatar, .header-avatar, .avatar-img"


class ToutiaoPublisher(PublisherBase):
    platform = Platform.TOUTIAO
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开头条号，等用户扫码登录后从 context 拿 cookies。"""
        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = False

        async with async_playwright() as p:
            browser = await p.chromium.launch(**kwargs)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(HOME_URL, timeout=60000)
            try:
                await page.wait_for_selector(SEL_LOGGED_IN_AVATAR, timeout=180000)
            except Exception:
                await browser.close()
                return False
            cookies = await ctx.cookies([
                "https://mp.toutiao.com",
                "https://www.toutiao.com",
            ])
            await browser.close()

        credential["cookies"] = cookies
        return bool(cookies)

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        if content.content_type == ContentType.VIDEO:
            return PublishResult(
                success=False,
                error="头条视频走 mp.toutiao.com/profile_v4/xigua/publish_video 路径，本 publisher 仅做图文/长文",
            )
        cookies = credential.get("cookies", [])
        if not cookies:
            return PublishResult(success=False, error="头条凭证缺 cookies")

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**kwargs)
                try:
                    ctx = await browser.new_context()
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    return await self._do_publish(page, content)
                finally:
                    await browser.close()
        except Exception as e:
            return PublishResult(success=False, error=f"头条发布异常: {e}")

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        cookies = credential.get("cookies", [])
        if not cookies:
            return AccountHealth.EXPIRED

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = True

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**kwargs)
                try:
                    ctx = await browser.new_context()
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    await page.goto(HOME_URL, timeout=30000)
                    found = await page.locator(SEL_LOGGED_IN_AVATAR).count()
                    return AccountHealth.HEALTHY if found else AccountHealth.EXPIRED
                finally:
                    await browser.close()
        except Exception:
            return AccountHealth.UNKNOWN

    # ---------------- 内部 ----------------

    async def _do_publish(self, page, content: PublishContent) -> PublishResult:
        await page.goto(WRITE_URL, timeout=60000)
        await page.wait_for_selector(SEL_BODY_EDITOR, timeout=30000)

        # 标题
        await page.click(SEL_TITLE_INPUT)
        await page.keyboard.type(content.title, delay=30)
        await asyncio.sleep(0.5)

        # 正文（ProseMirror 编辑器，同样要点进去 + keyboard.type）
        await page.click(SEL_BODY_EDITOR)
        body = content.body or ""
        for paragraph in body.split("\n"):
            await page.keyboard.type(paragraph, delay=20)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)

        # 图片插入
        for img in content.images:
            try:
                file_input = page.locator("input[type='file']").first
                await file_input.set_input_files(img)
                await asyncio.sleep(2)
            except Exception:
                pass

        await asyncio.sleep(2)

        # 发布
        await page.click(SEL_PUBLISH_BTN)
        try:
            await page.click(SEL_CONFIRM_BTN, timeout=10000)
        except Exception:
            pass

        # 等待跳转到文章详情或成功提示
        try:
            await page.wait_for_url("**/profile_v4/**", timeout=30000)
        except Exception:
            return PublishResult(
                success=False,
                error="提交后未跳转，可能未发布成功（被风控/审核或表单问题）",
            )

        return PublishResult(
            success=True,
            platform_url=page.url,
            raw_response={"final_url": page.url},
        )
