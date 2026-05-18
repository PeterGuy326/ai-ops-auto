"""百家号 (Baijiahao) Publisher — 自建实现（百度 SEO 流量管道）。

底层逻辑：
  - SAU 上游 `uploader/` 已覆盖百家号视频但仅 CLI 模式，长图文未覆盖
  - 与头条号同行业（信息流 + SEO 双管道）：头条吃推荐，百家号吃百度搜索
  - 完全套头条 publisher 母本结构：persistent context login + ProseMirror clipboard paste
    + 作品后台抓真链 + collect_metrics 复用同一 navigate 路径
  - 反风控等级 ★★（百度系比字节系稍强），走 `runtime/playwright_factory` 默认引擎

凭证格式（与头条一致，由 CredentialStore Fernet 加密落库）：
  {"cookies": [{"name": "BAIDUID", "value": "...", "domain": ".baidu.com", ...}, ...]}

⚠️ 真账号验证状态：
  所有 selector + URL 均为基于头条结构 + 百家号公开形态的合理推断，
  全部标注 `# TODO[bjh-real]: inspect after first real publish`——
  首次真发后需 inspect DOM 回填真实 class 名 / URL 形态。
"""
from __future__ import annotations

import asyncio
import random
import re

from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.parsers import parse_count as _parse_count  # 复用 core 层统一数字解析
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


# ---------------- URL（实测前推断，待真账号验证）----------------
# TODO[bjh-real]: inspect after first real publish — 百度账号体系登录入口
LOGIN_URL = "https://passport.baidu.com/v2/?login&u=https%3A%2F%2Fbaijiahao.baidu.com%2Fbuilder%2Frc%2Fhome"
HOME_URL = "https://baijiahao.baidu.com/builder/rc/home"
# TODO[bjh-real]: 百家号长图文发布入口，type=news 是常见形态（也可能是 type=article）
EDITOR_URL = "https://baijiahao.baidu.com/builder/rc/edit?type=news"
# 作品管理后台 —— 用于发布后抓真链 + collect_metrics 采集互动数据
PROFILE_ARTICLES_URL = "https://baijiahao.baidu.com/builder/rc/content"

# ---------------- Selectors（实测前推断，待真账号验证）----------------
# TODO[bjh-real]: inspect after first real publish — 百家号编辑器 selector 集中
# 百家号编辑器历史上用过 ueditor / wangEditor / ProseMirror，最新版偏向 ProseMirror 系
TITLE_INPUT_SELECTOR = 'textarea[placeholder*="标题"], input[placeholder*="标题"]'
# 编辑器 3 层 fallback：ProseMirror 主路径 → ueditor 兜底 → 通用 contenteditable 兜底
CONTENT_EDITOR_SELECTOR = ".ProseMirror, .edui-editor-body, [contenteditable=true]"

# 封面上传：百家号通常也是 hidden file input 模式
COVER_UPLOAD_INPUT_SELECTOR = 'input[type=file][accept*="image"]'

# 发布按钮：严格 `:text-is` 精确匹配（吸取知乎 publisher 的 has-text 误命中教训）
# 知乎那个坑：has-text("发布") 会同时命中"再次发布""取消发布"等长文本节点
PUBLISH_BUTTON_SELECTOR = 'button:text-is("发布"):not([disabled])'

# 登录态判断 selector（健康检查用）
LOGGED_IN_SELECTOR = ".user-info, .avatar, [class*=username]"

# 作品卡片 selector（套头条 .article-card 结构）
SEL_ARTICLE_CARD = ".article-list-item, .content-list-item, [class*=article-card]"
SEL_ARTICLE_CARD_ITEM_LINK = 'a[href*="baijiahao.baidu.com/s?"]'

# 互动指标 selector：精确 class → [class*=] 模糊兜底（同头条思路）
# TODO[bjh-real]: inspect after first real publish — 回填百家号 byteclass / vue class hash
ARTICLE_CARD_VIEW_SELECTOR = '.article-data-view, [class*="view"], [class*="read"]'
ARTICLE_CARD_COMMENT_SELECTOR = '.article-data-comment, [class*="comment"]'
ARTICLE_CARD_LIKE_SELECTOR = '.article-data-like, [class*="like"]'


# ---------------- 百家号公开 URL 严格正则 ----------------
# 百家号公开文章形态：https://baijiahao.baidu.com/s?id=<纯数字 id>(&...)?
# /edit 后缀 / builder/rc 路径 = 草稿态；其他形态保守判失败（防虚假闭环）
_BJH_PUBLIC_URL_RE = re.compile(
    r"^https?://baijiahao\.baidu\.com/s\?id=\d+"
)


def _check_published_url(url: str) -> tuple[bool, str]:
    """判断百家号文章 URL 是否处于公开发布状态。

    防虚假闭环：发布按钮未真发时，URL 可能停留在 /builder/rc/edit?id=xxx，
    如果只用 wait_for_url("**baijiahao.baidu.com**") 通配会把草稿当成功——
    比 fail 更危险（系统给 SUCCESS 信号但内容根本没公开）。

    判定规则：
      含 /builder/rc/edit  → (False, url)：草稿编辑态
      含 /edit             → (False, url)：草稿
      严格匹配 /s?id=<digits> → (True, url)：公开
      其他形态             → (False, url)：未知，保守判失败
      空 URL / None        → (False, url)
    """
    if not url:
        return False, url or ""
    # builder/rc/edit 后缀 = 草稿编辑态（百家号特有路径）
    if "/builder/rc/edit" in url:
        return False, url
    # 通用 /edit 后缀兜底
    if url.rstrip("/").endswith("/edit"):
        return False, url
    # 严格匹配公开 URL 形态
    if _BJH_PUBLIC_URL_RE.match(url):
        return True, url
    # 其他形态保守判失败，不冒虚假闭环风险
    return False, url


async def _random_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """随机停顿，模拟真人节奏，规避百家号节奏检测（百度系反爬偏严）。"""
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------- DOM 抓取 JS（publish + collect 两条路径共用）----------------
# 套头条 _EXTRACT_CARD_JS 结构：精确 class → [class*=] 模糊 → text 邻接 三层兜底
# TODO[bjh-real]: 真发后 inspect DOM 收紧 selector
_EXTRACT_CARD_JS = """
(args) => {
    const matchId = args && args.matchPostId ? String(args.matchPostId) : null;
    const cards = Array.from(document.querySelectorAll(
        '.article-list-item, .content-list-item, [class*="article-card"]'
    ));
    if (cards.length === 0) return null;

    let card = null;
    if (matchId) {
        for (const c of cards) {
            const a = c.querySelector('a[href*="baijiahao.baidu.com/s?"]');
            if (a && a.href && a.href.indexOf(matchId) >= 0) {
                card = c;
                break;
            }
        }
        if (!card) return null;
    } else {
        card = cards[0];  // 作品管理后台时间倒序，第一张即最新
    }

    const a = card.querySelector('a[href*="baijiahao.baidu.com/s?"]');
    const url = a ? a.href : null;

    const pickText = (selectors, keyword) => {
        for (const sel of selectors) {
            const el = card.querySelector(sel);
            if (el) {
                const t = (el.textContent || '').trim();
                if (t) return t;
            }
        }
        if (keyword) {
            const all = card.querySelectorAll('*');
            for (const el of all) {
                const t = (el.textContent || '').trim();
                if (t && t.indexOf(keyword) >= 0 && t.length < 30) {
                    return t;
                }
            }
        }
        return '';
    };

    return {
        url: url,
        view_count: pickText(['.article-data-view', '[class*="view"]', '[class*="read"]'], '阅读'),
        comment_count: pickText(['.article-data-comment', '[class*="comment"]'], '评论'),
        like_count: pickText(['.article-data-like', '[class*="like"]', '[class*="digg"]'], '点赞'),
        share_count: pickText(['.article-data-share', '[class*="share"]'], '转发'),
        publish_time: pickText(['.article-publish-time', '[class*="time"]', '[class*="date"]'], ''),
    };
}
"""


class BaijiahaoPublisher(PublisherBase):
    """百家号 Publisher —— 套头条母本结构，百度 SEO 流量管道。"""

    platform = Platform.BAIJIAHAO
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开百度 passport 登录页，用户扫码 / 短信验证完成后从 context 拿 cookies。

        实测：登录成功后 URL 跳转到 baijiahao.baidu.com/builder/rc/home，
        以此判定比等待 avatar selector 更稳——百度 UI 经常微调。
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

                # poll：等 URL 跳到 builder 域名即视为登录成功
                max_wait, waited = 300, 0
                while waited < max_wait:
                    await asyncio.sleep(2)
                    waited += 2
                    url = page.url or ""
                    if (
                        url
                        and "passport.baidu.com" not in url
                        and "login" not in url
                        and not url.startswith("about:blank")
                    ):
                        cookies = await ctx.cookies([
                            "https://baijiahao.baidu.com",
                            "https://passport.baidu.com",
                            "https://www.baidu.com",
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
                error="百家号视频走 /builder/rc/edit?type=video 路径，本 publisher 仅做图文/长文",
            )
        cookies = credential.get("cookies", [])
        if not cookies:
            return PublishResult(success=False, error="百家号凭证缺 cookies")

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
            return PublishResult(success=False, error=f"百家号发布异常: {e}")

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """轻量探活：打开 builder/rc/home，URL 不含 passport/login 即视为登录态有效。"""
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
                        url
                        and "passport.baidu.com" not in url
                        and "login" not in url
                    )
                    return AccountHealth.HEALTHY if healthy else AccountHealth.EXPIRED
                finally:
                    await browser.close()
        except Exception:
            return AccountHealth.UNKNOWN

    async def collect_metrics(
        self,
        post_id: str,
        post_url,
        credential: dict,
    ) -> dict:
        """采集百家号文章互动数据 —— 复用作品管理后台 navigate 路径。

        闭环：作品管理后台 .article-list-item 上原本就显示 view/comment/like，
        publish 时已经会去这个页面抓真链（_fetch_post_metadata），
        这里只是用同一套抓取逻辑，按 post_id 匹配卡片采集最新数字。

        失败策略（与头条 publisher 一致）：
          - cookies 缺失 → zeros + raw.error
          - playwright 启动失败 → zeros + raw.error
          - 卡片未找到（文章已下架/被删/或还没刷出来）→ zeros + raw.not_found=true
        任何分支都返回标准 Metrics 字段，不抛异常——飞轮调度方期望 dict 不期望异常。
        """
        cookies = credential.get("cookies", [])
        if not cookies:
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"error": "凭证缺 cookies"}}

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = True  # 采集走 headless

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**kwargs)
                try:
                    ctx = await browser.new_context()
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    metadata = await self._fetch_post_metadata(page, match_post_id=post_id)
                finally:
                    await browser.close()
        except Exception as e:
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"error": f"collect_metrics exception: {e}"}}

        if not metadata or not metadata.get("url"):
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"not_found": True, "post_id": post_id,
                            "metadata": metadata or {}}}

        return {
            "likes": _parse_count(metadata.get("like_count")),
            "comments": _parse_count(metadata.get("comment_count")),
            "shares": _parse_count(metadata.get("share_count")),
            "views": _parse_count(metadata.get("view_count")),
            "raw": {
                "url": metadata.get("url"),
                "view_count_raw": metadata.get("view_count"),
                "comment_count_raw": metadata.get("comment_count"),
                "like_count_raw": metadata.get("like_count"),
                "share_count_raw": metadata.get("share_count"),
                "publish_time": metadata.get("publish_time"),
            },
        }

    # ---------------- 内部 ----------------

    async def _do_publish(self, page, content: PublishContent) -> PublishResult:
        """打开编辑器 → 填标题 → HTML 粘贴正文 → 可选封面 → 发布 → 抓真链。

        套头条 _do_publish 套路，关键差异：
          - 百家号编辑器多形态兜底（ProseMirror / ueditor / contenteditable 3 选 1）
          - 发布按钮严格 `:text-is("发布")`，不要 has-text（防知乎那种 substring 误命中）
          - 抓真链：作品管理后台 .article-list-item 上拿 baijiahao.baidu.com/s?id= 形态
        """
        # lazy import：避免 markdown 包缺失时整个 publishers 包初始化失败
        try:
            import markdown
        except ImportError:
            return PublishResult(
                success=False,
                error="缺少 markdown 包（pip install markdown），无法将正文转 HTML 注入编辑器",
            )

        await page.goto(EDITOR_URL, wait_until="commit", timeout=30000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        await _random_delay(2, 3)

        # ---- 标题 ----
        try:
            await page.wait_for_selector(TITLE_INPUT_SELECTOR, timeout=15000)
        except Exception as e:
            return PublishResult(success=False, error=f"未找到标题输入框: {e}")
        try:
            await page.click(TITLE_INPUT_SELECTOR, force=True)
            await _random_delay(1, 2)
        except Exception:
            pass
        await page.fill(TITLE_INPUT_SELECTOR, content.title)
        await _random_delay(3, 5)

        # ---- 正文：markdown → HTML → ClipboardEvent paste 进编辑器 ----
        html = markdown.markdown(
            content.body or "",
            extensions=["fenced_code", "tables", "nl2br"],
        )
        try:
            await self._paste_html_to_editor(page, html)
        except Exception as e:
            return PublishResult(success=False, error=f"正文粘贴失败: {e}")
        await _random_delay(4, 6)

        # ---- 封面（图文必备，失败不阻断）----
        if content.images:
            try:
                await page.set_input_files(
                    COVER_UPLOAD_INPUT_SELECTOR,
                    content.images[0],
                    timeout=10000,
                )
                await _random_delay(4, 6)
            except Exception:
                # 封面失败不直接 abort——百家号允许无封面发布
                pass

        # ---- 发布按钮：严格 :text-is，吸取知乎 has-text 误命中教训 ----
        await _random_delay(2, 3)
        try:
            publish_btn = await page.wait_for_selector(PUBLISH_BUTTON_SELECTOR, timeout=15000)
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
        await _random_delay(5, 8)

        # 闭环关键：跳到作品管理后台抓真实 /s?id={id} 链接 + 互动指标快照
        # 直接拿 page.url 只能拿到编辑页 URL，不可分享、不可定位真文章
        metadata = await self._fetch_post_metadata(page)
        real_url = (metadata.get("url") if metadata else None) or page.url
        url_resolved = bool(metadata and metadata.get("url"))

        # 闭环关键：判草稿 vs 公开，防虚假闭环
        is_published, normalized_url = _check_published_url(real_url)
        if not is_published:
            return PublishResult(
                success=False,
                platform_url=real_url,
                error=f"百家号仍处于草稿状态或 URL 异常: {real_url}",
                raw_response={
                    "final_url": real_url,
                    "is_published": False,
                    "url_resolved_from_backend": url_resolved,
                    "initial_metadata": metadata or {},
                },
            )

        # 提取 post_id（百家号 /s?id=<digits>）
        post_id_match = re.search(r"[?&]id=(\d+)", normalized_url)
        post_id = post_id_match.group(1) if post_id_match else None

        return PublishResult(
            success=True,
            platform_post_id=post_id,
            platform_url=normalized_url,
            raw_response={
                "final_url": normalized_url,
                "real_url": real_url,
                "url_resolved_from_backend": url_resolved,
                "url_changed": real_url != url_before,
                "is_published": True,
                # 第一份 Metrics 快照：worker 可以直接落 Metrics 表，
                # 不用等 collect 飞轮 1h 后第一次跑
                "initial_metadata": metadata or {},
            },
        )

    async def _fetch_post_metadata(self, page, match_post_id: str | None = None) -> dict | None:
        """跳到作品管理后台 + 抓 .article-list-item 上的全字段（真链 + 三个互动数）。

        参数：
          match_post_id: None → 取最新一张卡片（publish 后调用）
                        非空 → 从所有卡片里按 ?id={id} 匹配那张（collect_metrics 调用）

        返回 dict 字段（任何字段抓不到时为原始字符串或空串，由 _parse_count 兜底）：
          {url, view_count, comment_count, like_count, share_count, publish_time}
        返回 None：卡片整张找不到 / navigate 失败 / 任意异常。

        失败策略：抓不到/找不到/异常 → 返回 None，**不抛**——
        publish 路径调用方负责降级，collect 路径调用方负责降级。
        """
        try:
            await page.goto(
                PROFILE_ARTICLES_URL,
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await page.wait_for_selector(SEL_ARTICLE_CARD, timeout=15000)
            result = await page.evaluate(
                _EXTRACT_CARD_JS,
                {"matchPostId": match_post_id} if match_post_id else {},
            )
            if not result:
                return None
            return result
        except Exception:
            return None

    async def _paste_html_to_editor(self, page, html: str) -> None:
        """把 markdown 转出的 HTML 通过 ClipboardEvent 注入到编辑器。

        百家号编辑器多形态兜底（ProseMirror / ueditor / contenteditable 3 选 1）。
        逐字 keyboard.type 在百度系编辑器会触发：
          1) 速度被风控；
          2) 格式（粗体/链接/列表）全丢失；
        实测改 paste HTML，编辑器内置 paste handler 会自动解析格式。
        """
        await page.click(CONTENT_EDITOR_SELECTOR, force=True)
        await asyncio.sleep(0.5)
        await page.evaluate(
            """(html) => {
                const editor = document.querySelector(
                    '.ProseMirror, .edui-editor-body, [contenteditable=true]'
                );
                if (!editor) throw new Error('no editor (ProseMirror/ueditor/contenteditable not found)');
                editor.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
                const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                editor.dispatchEvent(ev);
            }""",
            html,
        )
