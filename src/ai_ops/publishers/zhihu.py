"""知乎 Publisher — 自建实现（开源缺口确认后的合理例外）。

gh search 多关键词穷尽（zhihu publish / api / oauth / mcp / playwright），全部为空。
没有可集成的成熟开源工具，所以基于 Playwright + patchright（drop-in 反检测）自建。

发布路径选择：
  方案 A：知乎 Web API（z_c0 cookie + x-zse-93/96 签名）
          ❌ 签名算法 2024 后变重，维护成本高
  方案 B：Playwright 浏览器自动化（goto write 页面 → 模拟操作）
          ✅ 不破解签名，借浏览器规避风控；反检测能力来自 patchright

本实现走方案 B。

凭证格式（写到 credential 字段，由 CredentialStore 加密落库）：
  {
    "cookies": [
      {"name": "z_c0", "value": "...", "domain": ".zhihu.com", "path": "/"},
      {"name": "d_c0", "value": "...", "domain": ".zhihu.com", "path": "/"},
      ...
    ]
  }
"""
from __future__ import annotations

import asyncio

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


WRITE_URL = "https://zhuanlan.zhihu.com/write"
HOME_URL = "https://www.zhihu.com/"

# 知乎专栏编辑器的核心选择器（2026 版页面，DOM 可能随版本调整）
SEL_TITLE_INPUT = "textarea[placeholder*='标题']"
SEL_BODY_EDITOR = "div.public-DraftEditor-content[contenteditable='true']"
SEL_PUBLISH_BTN = "button:has-text('发布')"
SEL_CONFIRM_BTN = "button:has-text('发布文章')"
SEL_LOGGED_IN_AVATAR = "div.AppHeader-profile, button.AppHeader-profileAvatar"


class ZhihuPublisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举值

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开 zhihu.com，由用户手动扫码登录。

        登录后从 context 拿 cookies，由调用方负责加密落库（manager.update_credential）。
        """
        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = False  # 登录必须有窗口

        async with async_playwright() as p:
            browser = await p.chromium.launch(**kwargs)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(HOME_URL, timeout=60000)
            # 等用户最长 3 分钟扫码 / 输密码
            try:
                await page.wait_for_selector(SEL_LOGGED_IN_AVATAR, timeout=180000)
            except Exception:
                await browser.close()
                return False

            cookies = await ctx.cookies(["https://www.zhihu.com", "https://zhuanlan.zhihu.com"])
            await browser.close()

        # 把 cookies 写回 credential（调用方通常会重新加密落库）
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
                error="知乎专栏暂不支持视频笔记，请改用图文/长文",
            )
        cookies = credential.get("cookies", [])
        if not cookies:
            return PublishResult(success=False, error="知乎凭证缺 cookies")

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
            return PublishResult(success=False, error=f"知乎发布异常: {e}")

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
        """打开 write 页 → 填标题/正文 → 发布 → 抓 URL。"""
        await page.goto(WRITE_URL, timeout=60000)

        # 知乎专栏编辑器是 Draft.js 实现，要等 contenteditable 出现
        await page.wait_for_selector(SEL_BODY_EDITOR, timeout=30000)

        # 标题
        await page.click(SEL_TITLE_INPUT)
        await page.keyboard.type(content.title, delay=30)
        await asyncio.sleep(0.5)

        # 正文 — Draft.js 不接受 fill()，必须点进去 + keyboard.type
        await page.click(SEL_BODY_EDITOR)
        body = content.body or ""
        # 长文按行打字 + 段落间回车，模拟真人节奏
        for paragraph in body.split("\n"):
            await page.keyboard.type(paragraph, delay=20)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)

        # 图片插入（可选）
        for img in content.images:
            await self._insert_image(page, img)

        await asyncio.sleep(2)

        # 点击发布按钮（第一次出选择栏，第二次确认）
        await page.click(SEL_PUBLISH_BTN)
        try:
            await page.click(SEL_CONFIRM_BTN, timeout=10000)
        except Exception:
            # 第二步按钮可能不存在（一步发布版），忽略
            pass

        # 等待 URL 变化（跳转到文章详情）
        try:
            await page.wait_for_url("**/p/*", timeout=30000)
        except Exception:
            return PublishResult(
                success=False,
                error="提交后未跳转，可能未发布成功（被风控或表单问题）",
            )

        article_url = page.url
        # 提取 article_id（URL 模式：zhuanlan.zhihu.com/p/<id>）
        article_id = article_url.rstrip("/").rsplit("/", 1)[-1]

        return PublishResult(
            success=True,
            platform_post_id=article_id,
            platform_url=article_url,
            raw_response={"final_url": article_url},
        )

    async def collect_metrics(self, post_id: str, post_url, credential: dict) -> dict:
        """知乎文章数据采集：直接走 Web API（不需要签名，只要 cookie）。

        endpoint: https://www.zhihu.com/api/v4/articles/{post_id}
        返回字段：voteup_count / comment_count / 阅读估算（zhihu 不暴露 view，只能查关注/收藏）
        """
        import httpx

        cookies = credential.get("cookies", [])
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        headers = {
            "Cookie": cookie_str,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        url = f"https://www.zhihu.com/api/v4/articles/{post_id}"
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                        "raw": {"http_status": r.status_code}}
            data = r.json()
            return {
                "likes": int(data.get("voteup_count", 0)),
                "comments": int(data.get("comment_count", 0)),
                "shares": 0,  # 知乎不暴露分享数
                "views": int(data.get("read_count", 0)),  # 部分文章有
                "raw": {k: data.get(k) for k in ("voteup_count", "comment_count", "read_count", "favorite_count")},
            }
        except Exception as e:
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"error": str(e)}}

    async def _insert_image(self, page, image_path: str) -> None:
        """通过工具栏的上传按钮插入图片。

        知乎工具栏的图片按钮 selector 可能随 UI 改动，留 TODO 适配。
        """
        # TODO: 不同知乎 UI 版本选择器不同，先留接口；可用 page.set_input_files 触发隐藏 input
        try:
            file_input = page.locator("input[type='file']").first
            await file_input.set_input_files(image_path)
            await asyncio.sleep(2)  # 等上传完成
        except Exception:
            pass
