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

数据回流闭环（Task C, 2026 Q2）：
  publish 完成已经要 navigate 到作品管理后台抓真链 /item/{id}/，那张
  .article-card 卡片同时显示 view/comment/like —— 顺手抓出来塞进
  raw_response["initial_metadata"]，并实现 collect_metrics 复用同一路径，
  下游 collect_metrics 飞轮（1h/24h/7d）就不用调头条创作中心数据接口
  （省签名 / 省风控 / 省第三方依赖）。
"""
from __future__ import annotations

import asyncio
import random
import re

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

# 发布完成后去作品管理后台抓 /item/{id}/ 真实链接 + 互动指标（Task C 闭环）
# 直接拿 page.url 只能拿到发布页 URL，不可分享、不可定位真文章，下游分析/分享全失效。
# 同时该页面 .article-card 上已显示 view/comment/like，一次 navigate 顺手都抓出来。
PROFILE_ARTICLES_URL = "https://mp.toutiao.com/profile_v4/graphic/articles"
SEL_ARTICLE_CARD = ".article-card"
# 作品管理后台按时间倒序排列，第一个 .article-card 即最新发布的那篇
SEL_ARTICLE_CARD_ITEM_LINK = '.article-card a[href*="/item/"]'

# 互动指标 selector —— 业界常见命名 + 兜底 [class*=] 模糊匹配
# TODO[tt-real]: 首次真发后 inspect DOM 回填真实 class 名（byte-design 系命名约定可能带 hash 后缀）
ARTICLE_CARD_VIEW_SELECTOR = ".article-card-data-view"
ARTICLE_CARD_COMMENT_SELECTOR = ".article-card-data-comment"
ARTICLE_CARD_LIKE_SELECTOR = ".article-card-data-like"


# ---------------- 数字解析（统一沉到 core/parsers，保留别名兼容旧调用方）----------------
# TD-Z3-debt 闭环（2026 Q2）：通用 UI 数字解析逻辑搬到 core/parsers.py 作为基础设施层，
# 让 scheduler 也能正向 import，解除 worker → toutiao 反向依赖。
# 这里保留 `_parse_count` 别名 → 模块内 4 处调用 + tests/test_toutiao_publisher.py
# 的 `from ai_ops.publishers.toutiao import _parse_count` 全部零改动继续工作。
from ..core.parsers import parse_count as _parse_count  # noqa: E402,F401


async def _random_delay(lo: float, hi: float) -> None:
    """随机停顿，模拟人工节奏，规避头条节奏检测。"""
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------- DOM 抓取 JS（publish + collect 两条路径共用）----------------
# 抓单张卡片的全字段（url + 三个计数）。
# - 入参 match_post_id: None 表示取最新一张（publish 路径）；
#   非空表示从所有卡片里找 href 包含 match_post_id 的那张（collect 路径）
# - 抓不到字段返回原始文本字符串，由 Python 侧 _parse_count 统一兜底为 0
# - 多 selector 兜底：先精确类名，再 [class*=] 模糊，最后 text=阅读/评论/点赞 邻接节点
_EXTRACT_CARD_JS = """
(args) => {
    const matchId = args && args.matchPostId ? String(args.matchPostId) : null;
    const cards = Array.from(document.querySelectorAll('.article-card'));
    if (cards.length === 0) return null;

    let card = null;
    if (matchId) {
        for (const c of cards) {
            const a = c.querySelector('a[href*="/item/"]');
            if (a && a.href && a.href.indexOf(matchId) >= 0) {
                card = c;
                break;
            }
        }
        if (!card) return null;
    } else {
        card = cards[0];  // publish 路径：作品管理后台时间倒序，第一张即最新
    }

    const a = card.querySelector('a[href*="/item/"]');
    const url = a ? a.href : null;

    // 字段抓取：精确 selector -> 模糊 [class*=] -> 文本邻接 三层兜底
    const pickText = (selectors, keyword) => {
        for (const sel of selectors) {
            const el = card.querySelector(sel);
            if (el) {
                const t = (el.textContent || '').trim();
                if (t) return t;
            }
        }
        // text 邻接兜底：找含 keyword 的节点，取其父/兄弟里的数字
        if (keyword) {
            const all = card.querySelectorAll('*');
            for (const el of all) {
                const t = (el.textContent || '').trim();
                if (t && t.indexOf(keyword) >= 0 && t.length < 30) {
                    return t;  // 让 Python 侧 _parse_count 抽数字
                }
            }
        }
        return '';
    };

    return {
        url: url,
        view_count: pickText(['.article-card-data-view', '[class*="view"]', '[class*="read"]'], '阅读'),
        comment_count: pickText(['.article-card-data-comment', '[class*="comment"]'], '评论'),
        like_count: pickText(['.article-card-data-like', '[class*="like"]', '[class*="digg"]'], '点赞'),
        share_count: pickText(['.article-card-data-share', '[class*="share"]'], '转发'),
        publish_time: pickText(['.article-card-publish-time', '[class*="time"]', '[class*="date"]'], ''),
    };
}
"""


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

    async def collect_metrics(
        self,
        post_id: str,
        post_url,
        credential: dict,
    ) -> dict:
        """采集头条文章互动数据 —— 复用作品管理后台 navigate 路径（不调第三方 API）。

        闭环：作品管理后台 .article-card 上原本就显示 view/comment/like，
        publish 时已经会去这个页面抓真链（_fetch_post_metadata），
        这里只是用同一套抓取逻辑，按 post_id 匹配卡片采集最新数字。

        失败策略：
          - cookies 缺失 → zeros + raw.error
          - navigate 失败 → zeros + raw.error
          - 卡片未找到（文章已下架/被删/或还没刷出来）→ zeros + raw.not_found=true
        任何分支都返回标准 Metrics 字段，不抛异常——飞轮调度方期望 dict 不期望异常。
        """
        cookies = credential.get("cookies", [])
        if not cookies:
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"error": "凭证缺 cookies"}}

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = True  # 采集走 headless 不需要窗口

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
            # 没找到匹配卡片
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

        # 闭环关键：跳到作品管理后台抓真实 /item/{id}/ 链接 + 互动指标快照
        # 抓不到任何字段都不破坏发布——publish 已成功，metadata 是 bonus。
        metadata = await self._fetch_post_metadata(page)
        real_url = (metadata.get("url") if metadata else None) or url_after
        url_resolved = bool(metadata and metadata.get("url"))

        return PublishResult(
            success=True,
            platform_url=real_url,
            raw_response={
                "final_url": url_after,
                "real_url": real_url,
                "url_resolved_from_backend": url_resolved,
                "url_changed": url_after != url_before,
                # 第一份 Metrics 快照：worker 可以直接落 Metrics 表，
                # 不用等 collect 飞轮 1h 后第一次跑（follow-up: worker 层接入）
                "initial_metadata": metadata or {},
            },
        )

    async def _fetch_post_metadata(self, page, match_post_id: str | None = None) -> dict | None:
        """跳到作品管理后台 + 抓 .article-card 上的全字段（真链 + 三个互动数）。

        参数：
          match_post_id: None → 取最新一张卡片（publish 后调用，时间倒序第一张即最新）
                        非空 → 从所有卡片里按 /item/{id}/ 匹配那张（collect_metrics 调用）

        返回 dict 字段（任何字段抓不到时为原始字符串或空串，由 _parse_count 兜底）：
          {url, view_count, comment_count, like_count, share_count, publish_time}
        返回 None：卡片整张找不到 / navigate 失败 / 任意异常。

        失败策略：抓不到/找不到/异常 → 返回 None，**不抛**——
        publish 路径调用方负责降级（fallback 到原 URL），
        collect 路径调用方负责降级（返回 zeros + raw.not_found）。
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
