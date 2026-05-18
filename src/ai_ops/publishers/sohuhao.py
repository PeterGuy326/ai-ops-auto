"""搜狐号 Publisher — 自建实现（门户系媒体管道，与头条号互补）。

为什么自建：
  - 搜狐号官方 OAuth/开放接口在 2020 前后停服，目前只剩 mp.sohu.com Web 端可用；
  - gh search 多关键词穷尽（sohuhao publish / sohu mp / sohu media auto），
    仅 1-2 个低星且已 stale，没有可集成的成熟开源；
  - 走和 ToutiaoPublisher 完全一致的 Playwright + patchright 自建路径，
    复用 runtime/playwright_factory 反检测能力，不重复造轮子。

发布主路径（推断 + 待回填，2026 Q2 首发账号前未实测）：
  - 登录 URL：https://mp.sohu.com/login
  - 编辑器入口：https://mp.sohu.com/mpfe/v3/main/editor
    （搜狐号有「自媒体平台 mp.sohu.com」和「老搜狐号 sohu.com/mp」两套，
     2024+ 默认走 mp.sohu.com/mpfe/v3 这套，selector 全部带
     `# TODO[shh-real]: inspect after first real publish` 标记，
     首次真发账号 inspect DOM 后回填确认）
  - 编辑器栈：栈未确认，3 层 fallback selector
    （ProseMirror / DraftJS / ueditor 兜底，搜狐老 mp 是 ueditor，
     新 mp 可能升级到 React + DraftJS）
  - 发布按钮：精确 `:text-is("发布")`，**不要用 substring**——
    `has-text` substring 会误命中「发布设置 / 发布历史 / 取消发布」
    （知乎 publishing-sop §三-B 的反面教训：substring 匹配第一个含字的元素
     会点错按钮、导致整次发布被埋）。

凭证格式（与 ZhihuPublisher / ToutiaoPublisher 对齐）：
  {"cookies": [...]}  cookies 即 Playwright cookies list。

数据回流闭环：
  publish 完成后 navigate 到「作品管理后台」抓真实公开链 /a/<id>_<author>，
  同张卡片同时显示 view/comment/like —— 顺手抓出来塞进
  raw_response["initial_metadata"]；collect_metrics 复用同一路径，
  不调搜狐第三方数据接口（省签名 / 省风控 / 省第三方依赖）。

公开 URL 形态判定：
  - 公开：https://www.sohu.com/a/<digit>_<author>/?
  - 草稿：含 /edit / /draft 后缀
  - 其他：保守判失败（防虚假闭环，参考 ZhihuPublisher 的 _check_published_url 套路）
"""
from __future__ import annotations

import asyncio
import random
import re

from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.parsers import parse_count as _parse_count  # noqa: F401  保留模块级别名供测试导入
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


# ---------------- URL ----------------
# TODO[shh-real]: inspect after first real publish 确认/回填
LOGIN_URL = "https://mp.sohu.com/login"
HOME_URL = "https://mp.sohu.com/mpfe/v3/main/home"
EDITOR_URL = "https://mp.sohu.com/mpfe/v3/main/editor"

# 作品管理后台（按时间倒序，第一张即最新发布的那篇）
# TODO[shh-real]: inspect after first real publish 确认/回填
PROFILE_ARTICLES_URL = "https://mp.sohu.com/mpfe/v3/main/article/list"


# ---------------- Selectors ----------------
# TODO[shh-real]: inspect after first real publish 确认/回填
# 搜狐 mp 标题输入框可能是 input 也可能是 textarea，先按通用 placeholder 命中
TITLE_INPUT_SELECTOR = 'input[placeholder*="标题"], textarea[placeholder*="标题"]'

# 编辑器栈未确认（ueditor / DraftJS / ProseMirror）→ 3 层 fallback，由
# _paste_html_to_editor 内的 JS 选最先命中的容器注入
# TODO[shh-real]: inspect after first real publish 确认/回填
CONTENT_EDITOR_SELECTOR = (
    ".ProseMirror, .public-DraftEditor-content, .edui-editor-iframeholder iframe"
)

# 封面上传：搜狐号通常是 file input（隐藏在 .upload-cover 里），
# 老 mp 用 .upload-image-wrap，新 mp 倾向 .cover-upload
# TODO[shh-real]: inspect after first real publish 确认/回填
COVER_UPLOAD_INPUT_SELECTOR = (
    '.upload-cover input[type=file], .cover-upload input[type=file], '
    '.upload-image-wrap input[type=file]'
)

# 发布按钮 — 严格 :text-is，禁用 has-text substring（防误命中「发布设置/发布历史」）
# TODO[shh-real]: inspect after first real publish 确认/回填
PUBLISH_BUTTON_SELECTOR = 'button:text-is("发布"):not([disabled])'

# 作品管理后台卡片
# TODO[shh-real]: inspect after first real publish 确认/回填
SEL_ARTICLE_CARD = ".article-list-item, .article-card"
SEL_ARTICLE_CARD_LINK = 'a[href*="/a/"]'

# 互动指标 selector — 业界常见命名 + [class*=] 模糊兜底
# TODO[shh-real]: inspect after first real publish 确认/回填
ARTICLE_CARD_VIEW_SELECTOR = ".article-data-view, [class*=read]"
ARTICLE_CARD_COMMENT_SELECTOR = ".article-data-comment, [class*=comment]"
ARTICLE_CARD_LIKE_SELECTOR = ".article-data-like, [class*=like]"


# ---------------- 搜狐号公开 URL 严格正则 ----------------
# 实测搜狐号公开链形态：
#   https://www.sohu.com/a/<10+ 位数字>_<6+ 位 authorid>
#   末尾允许斜杠 / 查询参数兼容；scheme 兼容 http/https
# 草稿：URL 含 /edit 或 /draft 后缀（草稿态保留原 URL 供运营人工排查）
_SOHU_PUBLIC_URL_RE = re.compile(
    r"^https?://(?:www\.)?sohu\.com/a/\d+_\d+/?(?:\?.*)?$"
)


def _check_published_url(url: str) -> tuple[bool, str]:
    """判断搜狐号文章 URL 是否处于公开发布状态。

    参考 ZhihuPublisher 同名函数的"防虚假闭环"套路：

    判定规则：
      含 /edit / /draft 后缀  → (False, url)：草稿
      https://www.sohu.com/a/<id>_<author>(?) → (True, url 归一化去尾斜杠)
      其他形态                → (False, url)：未知，保守判失败
      空 URL                   → (False, "")

    这是 publishing-sop §三-B 的核心教训：URL 通配匹配会把 /edit 草稿
    当成成功返回（"虚假闭环"），比 fail 更危险——系统给了 SUCCESS 信号
    但内容根本没公开。必须严格正则 + 草稿后缀显式排除。
    """
    if not url:
        return False, url
    # /edit 或 /draft 后缀（含末尾斜杠）= 草稿，无条件判失败
    stripped = url.rstrip("/")
    if stripped.endswith("/edit") or stripped.endswith("/draft"):
        return False, url
    if _SOHU_PUBLIC_URL_RE.match(url):
        # 归一化：去查询参数 + 去末尾斜杠（便于下游提取 article_id）
        canonical = url.split("?", 1)[0].rstrip("/")
        return True, canonical
    return False, url


async def _random_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """随机停顿，模拟人工节奏，规避搜狐 mp 节奏检测。"""
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------- DOM 抓取 JS（publish + collect 两条路径共用）----------------
# 抓单张卡片的全字段（url + 三个计数）。
# - match_post_id 为 None: 取最新一张（publish 路径，作品管理后台时间倒序第一张）
#   非 None: 从所有卡片里找 href 包含 match_post_id 的那张（collect 路径）
# - 字段抓不到返回原始文本字符串，由 Python 侧 _parse_count 统一兜底为 0
# - 多 selector 兜底：精确类名 → [class*=] 模糊 → text=阅读/评论/点赞 邻接节点
_EXTRACT_CARD_JS = """
(args) => {
    const matchId = args && args.matchPostId ? String(args.matchPostId) : null;
    const cards = Array.from(document.querySelectorAll('.article-list-item, .article-card'));
    if (cards.length === 0) return null;

    let card = null;
    if (matchId) {
        for (const c of cards) {
            const a = c.querySelector('a[href*="/a/"]');
            if (a && a.href && a.href.indexOf(matchId) >= 0) {
                card = c;
                break;
            }
        }
        if (!card) return null;
    } else {
        card = cards[0];  // publish 路径：作品管理后台时间倒序，第一张即最新
    }

    const a = card.querySelector('a[href*="/a/"]');
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
                    return t;  // 让 Python 侧 _parse_count 抽数字
                }
            }
        }
        return '';
    };

    return {
        url: url,
        view_count: pickText(['.article-data-view', '[class*="read"]', '[class*="view"]'], '阅读'),
        comment_count: pickText(['.article-data-comment', '[class*="comment"]'], '评论'),
        like_count: pickText(['.article-data-like', '[class*="like"]', '[class*="digg"]'], '点赞'),
        share_count: pickText(['.article-data-share', '[class*="share"]'], '转发'),
        publish_time: pickText(['.article-publish-time', '[class*="time"]', '[class*="date"]'], ''),
    };
}
"""


class SohuhaoPublisher(PublisherBase):
    """搜狐号 publisher — 结构与 ToutiaoPublisher 对称。

    selector 全部带 `# TODO[shh-real]: inspect after first real publish` 标记，
    首次真发账号 inspect DOM 后逐项回填确认；本类 happy-path/edge case 在测试侧
    已用 mock 验证逻辑契约，selector 微调时只动顶部常量、不动业务流。
    """

    platform = Platform.SOHUHAO
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举

    async def login(self, account_id: int, credential: dict) -> bool:
        """有窗口模式打开搜狐号登录页，等用户扫码完成后从 context 拿 cookies。

        判定登录成功：URL 跳离 login，且不再含 captcha 等中间页字样。
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
                    if "login" in url or "captcha" in url:
                        continue
                    cookies = await ctx.cookies([
                        "https://mp.sohu.com",
                        "https://www.sohu.com",
                    ])
                    if cookies:
                        credential["cookies"] = cookies
                        return True
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
                error="搜狐号视频走 mp.sohu.com/mpfe/v3/main/video 路径，本 publisher 仅做图文/长文",
            )
        cookies = credential.get("cookies", [])
        if not cookies:
            return PublishResult(success=False, error="搜狐号凭证缺 cookies")

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
            return PublishResult(success=False, error=f"搜狐号发布异常: {e}")

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """轻量探活：打开 HOME_URL，URL 不含 login/auth 即视为登录态有效。"""
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
        """采集搜狐号文章互动数据 —— 复用作品管理后台 navigate 路径，不调第三方 API。

        失败策略（与 ToutiaoPublisher 对齐）：
          - cookies 缺失 → zeros + raw.error
          - navigate 失败 → zeros + raw.error
          - 卡片未找到（文章已下架/被删/翻页超出）→ zeros + raw.not_found=True
        任何分支都返回标准 Metrics 字段，不抛异常。
        """
        cookies = credential.get("cookies", [])
        if not cookies:
            return {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                    "raw": {"error": "凭证缺 cookies"}}

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
        # markdown 转换 lazy import：声明在 pyproject.toml dependencies，
        # 但万一部署环境漏装，给出清晰错误而不是 import time 直接挂模块
        try:
            import markdown
        except ImportError:
            return PublishResult(
                success=False,
                error="缺少 markdown 包（pip install markdown），无法将正文转 HTML 注入搜狐编辑器",
            )

        await page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=30000)
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
        await _random_delay(2, 3)

        # ---- 正文 ----
        html = markdown.markdown(
            content.body or "",
            extensions=["fenced_code", "tables", "nl2br"],
        )
        try:
            await self._paste_html_to_editor(page, html)
        except Exception as e:
            return PublishResult(success=False, error=f"正文粘贴失败: {e}")
        await _random_delay(3, 5)

        # ---- 封面（图文必备，失败不阻断主发布）----
        if content.images:
            try:
                await page.set_input_files(
                    COVER_UPLOAD_INPUT_SELECTOR,
                    content.images[0],
                    timeout=10000,
                )
                await _random_delay(3, 5)
            except Exception:
                pass

        # ---- 发布按钮 ----
        try:
            publish_btn = await page.wait_for_selector(
                PUBLISH_BUTTON_SELECTOR, timeout=15000
            )
        except Exception:
            return PublishResult(
                success=False,
                error="未找到「发布」按钮（标题/正文可能没填好或被风控）",
            )
        if not publish_btn:
            return PublishResult(
                success=False,
                error="未找到「发布」按钮（标题/正文可能没填好或被风控）",
            )
        await publish_btn.scroll_into_view_if_needed()
        await _random_delay(1, 2)

        url_before = page.url
        await publish_btn.click()
        await _random_delay(5, 8)
        url_after = page.url

        # 闭环关键：跳到作品管理后台抓真实 /a/<id>_<author> 链接 + 互动指标快照
        # 抓不到任何字段都不破坏发布——publish 已成功，metadata 是 bonus。
        metadata = await self._fetch_post_metadata(page)
        real_url = (metadata.get("url") if metadata else None) or url_after

        # 防虚假闭环：用 _check_published_url 严格判 url 形态
        is_published, normalized_url = _check_published_url(real_url)
        if not is_published:
            return PublishResult(
                success=False,
                platform_url=real_url,
                error=f"搜狐号 URL 异常或仍处草稿状态: {real_url}",
                raw_response={
                    "final_url": url_after,
                    "real_url": real_url,
                    "is_published": False,
                    "url_changed": url_after != url_before,
                    "initial_metadata": metadata or {},
                },
            )

        # 提取 article_id：搜狐公开链 /a/<id>_<authorid>，取斜杠后整体作为 post_id
        article_id = normalized_url.rsplit("/", 1)[-1]

        return PublishResult(
            success=True,
            platform_post_id=article_id,
            platform_url=normalized_url,
            raw_response={
                "final_url": url_after,
                "real_url": normalized_url,
                "url_resolved_from_backend": bool(metadata and metadata.get("url")),
                "url_changed": url_after != url_before,
                "is_published": True,
                "initial_metadata": metadata or {},
            },
        )

    async def _fetch_post_metadata(self, page, match_post_id: str | None = None) -> dict | None:
        """跳到作品管理后台 + 抓卡片上的全字段（真链 + 三个互动数）。

        参数（与 ToutiaoPublisher 对齐）：
          match_post_id: None → 取最新一张卡片（publish 后调用）
                        非空 → 按 href 匹配（collect_metrics 调用）

        返回 dict 字段（抓不到时为空串，由 _parse_count 兜底为 0）：
          {url, view_count, comment_count, like_count, share_count, publish_time}
        返回 None：卡片整张找不到 / navigate 失败 / 任意异常。

        失败策略：抓不到/找不到/异常 → 返回 None，**不抛**。
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
        """把 markdown 转出的 HTML 通过 ClipboardEvent 注入到搜狐编辑器。

        3 层 fallback selector（先 ProseMirror → 再 DraftJS → 再 ueditor iframe）。
        搜狐 mp 实际编辑器栈未确认，3 层覆盖避免 selector 单点故障。
        逐字 keyboard.type 会被风控 + 慢 + 丢格式，paste HTML 才能保留段落/列表/代码块。
        """
        await page.click(CONTENT_EDITOR_SELECTOR, force=True)
        await asyncio.sleep(0.5)
        await page.evaluate(
            """(html) => {
                // 3 层 fallback：先 ProseMirror、再 DraftJS、再 ueditor iframe
                const candidates = [
                    '.ProseMirror',
                    '.public-DraftEditor-content',
                    '.edui-editor-iframeholder iframe',
                ];
                let editor = null;
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el) { editor = el; break; }
                }
                if (!editor) throw new Error('no editor (ProseMirror/DraftJS/ueditor)');
                // iframe 场景需要切 contentDocument
                let target = editor;
                if (editor.tagName === 'IFRAME' && editor.contentDocument) {
                    target = editor.contentDocument.body;
                }
                target.focus && target.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
                const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                target.dispatchEvent(ev);
            }""",
            html,
        )
