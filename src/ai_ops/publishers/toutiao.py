"""今日头条 / 头条号 Publisher — 自建实现（开源缺口确认后的合理例外）。

gh search 多关键词穷尽（toutiao publish/api/skill 等），唯二命中：
  - axdlee/toutiao-publish (4⭐, shell skill)
  - OceanBBBBbb/auto_write_toutiaohao (6⭐, 老代码)
都不够成熟，所以自建。

走和 ZhihuPublisher 一致的"Playwright + patchright factory"路径，
复用 runtime/playwright_factory 反检测能力，不重复造轮子。

实测验证（2026 Q2）：
  - 登录 URL：https://mp.toutiao.com/auth/page/login
  - 发布入口：https://mp.toutiao.com/profile_v4/graphic/publish
  - 编辑器：ProseMirror（字节系通用），逐字 keyboard.type 会被风控 + 慢，
    实测改走 HTML markdown → ClipboardEvent paste 注入；
  - 封面：抽屉式（先点 + 卡片 → 抽屉 file input → 抽屉「确定」→ 主页面渲染）；
  - 发布：两步（「预览并发布」进预览页 → 预览页「确认发布」才真发服务端）。

凭证格式：与 ZhihuPublisher 相同 ({"cookies": [...]})，cookies 即 Playwright cookies list。
"""
from __future__ import annotations

import asyncio
import random

from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


# ---------------- URL ----------------
LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
HOME_URL = "https://mp.toutiao.com/profile_v4/index"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"

# ---------------- Selectors（实测验证，2026 Q2）----------------
# 头条发布页只有一个 textarea，就是标题
SEL_TITLE_INPUT = "textarea"
# 字节系通用富文本编辑器
SEL_BODY_EDITOR = ".ProseMirror"

# 封面上传是抽屉式：先点 + 卡片，弹出 byte-drawer
SEL_COVER_ADD_PLUS = ".article-cover-add"
SEL_COVER_DRAWER_FILE_INPUT = ".upload-image-panel input[type=file]"
SEL_COVER_DRAWER_CONFIRM = (
    '.byte-drawer-wrapper button.byte-btn-primary.byte-btn-size-large:has-text("确定")'
)
SEL_COVER_PREVIEW_IMG = ".article-cover-images img"

# 发布是两步：第一步进预览页，第二步在预览页才真发
SEL_PUBLISH_BTN = 'button:has-text("预览并发布")'
SEL_FINAL_CONFIRM_BTN = 'button:has-text("确认发布")'


async def _random_delay(lo: float, hi: float) -> None:
    """随机停顿，模拟人工节奏，规避头条节奏检测。"""
    await asyncio.sleep(random.uniform(lo, hi))


class ToutiaoPublisher(PublisherBase):
    platform = Platform.TOUTIAO
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开头条号登录页，等用户扫码完成后从 context 拿 cookies。

        实测：登录成功后 URL 会从 auth/login 跳走（通常到 profile_v4 或 index），
        以此判定比等待 avatar selector 更稳——头条 UI 经常微调，URL 判定语义最稳。
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

                # poll：等 URL 跳离 login/auth 即视为登录成功
                max_wait, waited = 300, 0
                while waited < max_wait:
                    await asyncio.sleep(2)
                    waited += 2
                    url = page.url or ""
                    if (
                        url
                        and "login" not in url
                        and "auth" not in url
                        and not url.startswith("about:blank")
                    ):
                        cookies = await ctx.cookies([
                            "https://mp.toutiao.com",
                            "https://www.toutiao.com",
                        ])
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
        """轻量探活：打开 profile_v4/index，URL 不含 login/auth 即视为登录态有效。"""
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
                    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    url = page.url or ""
                    healthy = bool(
                        url and "login" not in url and "auth" not in url
                    )
                    return AccountHealth.HEALTHY if healthy else AccountHealth.EXPIRED
                finally:
                    await browser.close()
        except Exception:
            return AccountHealth.UNKNOWN

    # ---------------- 内部 ----------------

    async def _do_publish(self, page, content: PublishContent) -> PublishResult:
        # markdown 转换 lazy import：声明在 pyproject.toml dependencies，
        # 但万一部署环境漏装，给出清晰错误而不是 import time 直接挂模块
        try:
            import markdown
        except ImportError:
            return PublishResult(
                success=False,
                error="缺少 markdown 包（pip install markdown），无法将正文转 HTML 注入 ProseMirror",
            )

        await page.goto(PUBLISH_URL, wait_until="commit", timeout=30000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        await _random_delay(2, 3)

        # ---- 标题 ----
        try:
            await page.wait_for_selector(SEL_TITLE_INPUT, timeout=15000)
        except Exception as e:
            return PublishResult(success=False, error=f"未找到标题输入框: {e}")
        try:
            await page.click(SEL_TITLE_INPUT, force=True)
            await _random_delay(1, 2)
        except Exception:
            pass
        await page.fill(SEL_TITLE_INPUT, content.title)
        await _random_delay(3, 5)

        # ---- 正文：markdown → HTML → ClipboardEvent paste 进 ProseMirror ----
        html = markdown.markdown(
            content.body or "",
            extensions=["fenced_code", "tables", "nl2br"],
        )
        try:
            await self._paste_html_to_prosemirror(page, html)
        except Exception as e:
            return PublishResult(success=False, error=f"正文粘贴失败: {e}")
        await _random_delay(4, 6)

        # ---- 封面（图文必备）----
        if content.images:
            ok = await self._upload_cover_via_drawer(page, content.images[0])
            if not ok:
                # 封面失败不直接 abort——头条允许无封面发布，记录到 raw_response 让上层观察
                pass
            await _random_delay(2, 3)

        # ---- 发布第一步：进预览页 ----
        try:
            publish_btn = await page.wait_for_selector(SEL_PUBLISH_BTN, timeout=10000)
        except Exception:
            return PublishResult(success=False, error="未找到「预览并发布」按钮")
        if not publish_btn:
            return PublishResult(success=False, error="未找到「预览并发布」按钮")
        await publish_btn.scroll_into_view_if_needed()
        await _random_delay(1, 2)
        await publish_btn.click()
        await _random_delay(3, 5)

        # ---- 发布第二步：预览页「确认发布」才真发到服务端 ----
        try:
            final_btn = await page.wait_for_selector(SEL_FINAL_CONFIRM_BTN, timeout=15000)
        except Exception:
            return PublishResult(
                success=False,
                error="未找到「确认发布」按钮（预览页加载失败或被风控）",
            )
        if not final_btn:
            return PublishResult(
                success=False,
                error="未找到「确认发布」按钮（预览页加载失败或被风控）",
            )

        url_before = page.url
        await final_btn.click()
        await _random_delay(5, 8)
        url_after = page.url

        return PublishResult(
            success=True,
            platform_url=url_after,
            raw_response={
                "final_url": url_after,
                "url_changed": url_after != url_before,
            },
        )

    async def _paste_html_to_prosemirror(self, page, html: str) -> None:
        """把 markdown 转出的 HTML 通过 ClipboardEvent 注入到 ProseMirror。

        逐字 keyboard.type 在头条 ProseMirror 会触发：
          1) 速度被风控；
          2) 格式（粗体/链接/列表）全丢失；
        实测改 paste HTML，ProseMirror 内置的 paste handler 会自动解析格式。
        """
        await page.click(SEL_BODY_EDITOR, force=True)
        await asyncio.sleep(0.5)
        await page.evaluate(
            """(html) => {
                const editor = document.querySelector('.ProseMirror');
                if (!editor) throw new Error('no .ProseMirror');
                editor.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
                const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                editor.dispatchEvent(ev);
            }""",
            html,
        )

    async def _upload_cover_via_drawer(self, page, cover_path: str) -> bool:
        """头条封面上传是抽屉式（实测验证，2026 Q2）：

        流程：点 + 卡片 → 等抽屉 file input → set_input_files →
              等图片上传（~5s）→ 点抽屉「确定」→ 等主页面 .article-cover-images img 真渲染
        """
        try:
            await page.click(SEL_COVER_ADD_PLUS, force=True)
        except Exception:
            return False

        try:
            await page.wait_for_selector(SEL_COVER_DRAWER_FILE_INPUT, timeout=15000)
            await page.set_input_files(
                SEL_COVER_DRAWER_FILE_INPUT, cover_path, timeout=10000
            )
        except Exception:
            return False

        await asyncio.sleep(5)  # 等抽屉里图片上传完成

        try:
            confirm = await page.wait_for_selector(
                SEL_COVER_DRAWER_CONFIRM, timeout=10000
            )
            if not confirm:
                return False
            await confirm.click()
        except Exception:
            return False

        # 等主页面封面真渲染（naturalWidth>0 才算真加载到了，不是占位）
        try:
            await page.wait_for_function(
                "() => { const img = document.querySelector('.article-cover-images img'); "
                "return !!(img && img.complete && img.naturalWidth > 0); }",
                timeout=20000,
            )
            return True
        except Exception:
            return False
