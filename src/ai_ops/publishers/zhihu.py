"""知乎 Publisher — 自建实现（开源缺口确认后的合理例外）。

gh search 多关键词穷尽（zhihu publish / api / oauth / mcp / playwright），全部为空。
没有可集成的成熟开源工具，所以基于 Playwright + patchright（drop-in 反检测）自建。

发布路径选择：
  方案 A：知乎 Web API（z_c0 cookie + x-zse-93/96 签名）
          ❌ 签名算法 2024 后变重，维护成本高
  方案 B：Playwright 浏览器自动化（goto write 页面 → 模拟操作）
          ✅ 不破解签名，借浏览器规避风控；反检测能力来自 patchright

本实现走方案 B。

凭证格式（写到 credential 字段，由 CredentialStore 用 Fernet 加密落库）：
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
import random
import re

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


LOGIN_URL = "https://www.zhihu.com/signin?next=%2F"
WRITE_URL = "https://zhuanlan.zhihu.com/write"
HOME_URL = "https://www.zhihu.com/"

# 知乎专栏页面选择器（实测验证，2026 Q2）
SEL_TITLE_INPUT = 'textarea[placeholder*="标题"]'
SEL_BODY_EDITOR = ".public-DraftEditor-content"
SEL_COVER_FILE_INPUT = ".UploadPicture-wrapper input[type=file]"
SEL_PUBLISH_BTN = 'button:text-is("发布"):not([disabled])'
SEL_LOGGED_IN_AVATAR = ".AppHeader-userInfo, .Avatar--md, [class*=AppHeader-profile]"


# 知乎专栏公开 URL 严格模式：zhuanlan.zhihu.com/p/<纯数字 id>（容错末尾斜杠）
# /edit 后缀 = 草稿状态；其他形态（answer/question/follow 等）保守判失败
_ZHIHU_PUBLIC_URL_RE = re.compile(r"^https?://zhuanlan\.zhihu\.com/p/\d+/?$")


def _check_published_url(url: str) -> tuple[bool, str]:
    """判断知乎文章 URL 是否处于公开发布状态。

    现状（修复前）：_do_publish 抓到 final_url 后直接返回 success=True，
    但 /p/{id}/edit 也满足 wait_for_url("**/p/*") 通配。当发布按钮未真发、
    页面停在草稿编辑态时，会把草稿当成功返回——这就是「虚假闭环」，
    比 fail 更危险，因为系统给了 SUCCESS 信号但内容根本没公开。

    判定规则：
      /p/{id}/edit  → (False, url)：草稿
      /p/{id}       → (True, url)：公开
      /p/{id}/      → (True, url.rstrip("/"))：公开，末尾斜杠归一化
      其他形态      → (False, url)：未知，保守判失败（不冒虚假闭环风险）
    """
    if not url:
        return False, url
    # /edit 后缀（含末尾斜杠）= 草稿，无条件判失败
    if url.rstrip("/").endswith("/edit"):
        return False, url
    # 必须严格匹配 zhuanlan /p/<digits>(/)?$ 才算真公开
    if _ZHIHU_PUBLIC_URL_RE.match(url):
        return True, url.rstrip("/")
    return False, url


async def _random_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """模拟真人节奏的随机延迟，分散在关键操作之间降低风控触发概率。"""
    await asyncio.sleep(random.uniform(lo, hi))


async def _paste_html_to_draftjs(page, html: str) -> None:
    """通过合成 ClipboardEvent 把 HTML 注入到知乎 Draft.js 编辑器。

    Draft.js 不接受 fill() 或 keyboard.type 的批量注入，必须走 paste 事件
    才能保留段落 / 代码块 / 列表等结构，且比逐字 type 快几十倍、被风控概率更低。
    """
    await page.click(SEL_BODY_EDITOR, force=True)
    await asyncio.sleep(0.5)
    await page.evaluate(
        """(html) => {
            const editor = document.querySelector('.public-DraftEditor-content');
            if (!editor) throw new Error('no .public-DraftEditor-content');
            editor.focus();
            const dt = new DataTransfer();
            dt.setData('text/html', html);
            dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
            const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
            editor.dispatchEvent(ev);
        }""",
        html,
    )


class ZhihuPublisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举值

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开知乎登录页，由用户手动扫码 / 验证码登录。

        登录后从 context 拿 cookies，写回 credential（调用方负责重新加密落库，
        通常通过 manager.update_credential 走 Fernet 加密管线）。

        采用 URL + 头像双重轮询，比单纯 wait_for_selector 更稳：
          - 知乎登录跳转链可能停在 captcha / signin 中间页
          - 部分账号扫码后直接到 zhuanlan，AppHeader-profile 选择器名也变种较多
        """
        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = False  # 登录必须有窗口

        async with async_playwright() as p:
            browser = await p.chromium.launch(**kwargs)
            try:
                ctx = await browser.new_context()
                page = await ctx.new_page()
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

                max_wait, waited = 300, 0
                while waited < max_wait:
                    await asyncio.sleep(2)
                    waited += 2
                    url = page.url or ""
                    if not url or url.startswith("about:blank"):
                        continue
                    # 登录成功后跳转目标 URL 中 signin / login / captcha 字样消失
                    if "signin" in url or "login" in url or "captcha" in url:
                        continue
                    # 二次确认：DOM 里出现头像 / 用户信息
                    has_user = await page.evaluate(
                        "() => !!(document.querySelector('.AppHeader-userInfo, .Avatar--md, [class*=AppHeader-profile]'))"
                    )
                    if has_user:
                        cookies = await ctx.cookies(
                            ["https://www.zhihu.com", "https://zhuanlan.zhihu.com"]
                        )
                        credential["cookies"] = cookies
                        return bool(cookies)
                return False
            finally:
                await browser.close()

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
        """打开 write 页 → 填标题 → HTML 粘贴正文 → 可选封面 → 发布 → 抓 URL。

        关键升级（实测验证 2026 Q2）：
          1. 正文走 markdown → html → ClipboardEvent 注入，比 keyboard.type 快几十倍
          2. 封面通过 .UploadPicture-wrapper 下隐藏 file input 直接 set_input_files
          3. 标题填完 / 正文粘完 / 点发布前都插入随机延迟，模拟真人
        """
        await page.goto(WRITE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # 标题
        await page.wait_for_selector(SEL_TITLE_INPUT, timeout=15000)
        await page.click(SEL_TITLE_INPUT, force=True)
        await asyncio.sleep(0.5)
        await page.fill(SEL_TITLE_INPUT, content.title)
        await _random_delay(2, 3)

        # 正文 — markdown 渲染为 HTML 再粘贴到 Draft.js
        # lazy import：避免 markdown 包缺失时整个 publishers 包初始化失败
        import markdown
        body = content.body or ""
        html = markdown.markdown(body, extensions=["fenced_code", "tables", "nl2br"])
        await _paste_html_to_draftjs(page, html)
        await _random_delay(3, 5)

        # 封面（取 images[0] 当封面，失败不阻断发布）
        if content.images:
            try:
                await page.set_input_files(
                    SEL_COVER_FILE_INPUT,
                    content.images[0],
                    timeout=10000,
                )
                await _random_delay(4, 6)
            except Exception:
                # 封面失败不阻断主发布流程
                pass

        # 点击发布
        await _random_delay(2, 3)
        try:
            publish_btn = await page.wait_for_selector(SEL_PUBLISH_BTN, timeout=15000)
        except Exception:
            return PublishResult(
                success=False,
                error="「发布」按钮未 enable（标题/正文可能没填好）",
            )
        if not publish_btn:
            return PublishResult(
                success=False,
                error="「发布」按钮未 enable（标题/正文可能没填好）",
            )
        await publish_btn.scroll_into_view_if_needed()
        await _random_delay(1, 2)
        url_before = page.url
        await publish_btn.click()

        # 等待 URL 变化（跳转到文章详情）
        try:
            await page.wait_for_url("**/p/*", timeout=30000)
        except Exception:
            # 没拿到详情页跳转，再宽松判断 URL 是否变化
            await asyncio.sleep(5)
            if page.url == url_before:
                return PublishResult(
                    success=False,
                    error="提交后未跳转，可能未发布成功（被风控或表单问题）",
                )

        article_url = page.url

        # 闭环关键：判 /edit 后缀，草稿状态不算成功，防止虚假闭环
        is_published, normalized_url = _check_published_url(article_url)
        if not is_published:
            return PublishResult(
                success=False,
                platform_url=article_url,
                error=f"知乎仍处于草稿状态或 URL 异常: {article_url}",
                raw_response={
                    "final_url": article_url,
                    "is_published": False,
                },
            )

        # 提取 article_id（URL 已通过严格正则，rstrip 已在 _check_published_url 内做）
        article_id = normalized_url.rsplit("/", 1)[-1]

        return PublishResult(
            success=True,
            platform_post_id=article_id,
            platform_url=normalized_url,
            raw_response={
                "final_url": article_url,
                "is_published": True,
            },
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
