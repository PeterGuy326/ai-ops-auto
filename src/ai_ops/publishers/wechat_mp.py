"""微信公众号 mp Publisher — 自建实现（持久化 user-data-dir 模式）。

为什么用 persistent_context 而不是 storage_state：
  公众号 mp 后台对 storage_state 模式不友好——cookie 即使齐全，跨进程加载后
  仍会判定为未登录。只有 launch_persistent_context（整体持久化浏览器内部状态
  含指纹 / IndexedDB / Service Worker cache）能稳定保住登录态。

凭证格式（写到 credential 字段，由 CredentialStore 加密落库）：
  {
    "profile_dir": "/abs/path/to/wechat_mp_<account_id>",
    "last_login_at": "2026-xx-xx"
  }
  路径本身不算敏感数据，但走统一的 Fernet 加密通道保持与其他 publisher 架构一致。

阶段 1：仅支持「保存为草稿」——**绝不实现 send-draft**。
  公众号群发不可撤回 + 每天次数限制（订阅号 1 次/天、服务号 4 次/月），自动化
  误触代价过高，按 publishing-sop §三-C 决策 publisher 层只保草稿，
  send-draft 必须人工在 mp 后台二次确认。
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


# ---------------- URL ----------------
LOGIN_URL = "https://mp.weixin.qq.com/"
HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"
# 图文草稿编辑器入口：t=media/appmsg_edit_v2&action=edit&type=77 = 图文类型
APPMSG_NEW_URL = (
    "https://mp.weixin.qq.com/cgi-bin/appmsg"
    "?t=media/appmsg_edit_v2&action=edit&type=77&createType=0&token=&lang=zh_CN"
)


# ---------------- Selectors（首次真发时 inspect 后回填）----------------
# 公众号后台是 iframe 嵌套布局——编辑器整体跑在子 iframe 内（按 §三-C 已知坑 #3）。
# 主要 iframe selector：实测多数 mp 编辑器路径走 `iframe[id^="ueditor"]` 或主 frame `iframe`
# TODO[mp-real]: inspect after first real publish to confirm selector

# 编辑器 iframe（注释中标主路径 + fallback；_locate_editor_root 会按序尝试）
EDITOR_FRAME_SELECTOR = "iframe"
# 标题输入框（mp 文章编辑器常见命名）
TITLE_INPUT_SELECTOR = "#title, input[placeholder*='标题'], textarea[placeholder*='标题']"
# 正文编辑器主体（mp 老版本基于百度 ueditor，class 名常见 .edui-editor-iframeholder + 内嵌 iframe）
CONTENT_EDITOR_SELECTOR = (
    ".ProseMirror, "
    ".rich_media_content, "
    "#ueditor_0, "
    ".edui-editor-iframeholder iframe, "
    "[contenteditable='true']"
)
# 封面上传：hidden file input
COVER_UPLOAD_INPUT_SELECTOR = (
    "input[type=file][accept*='image'], "
    ".js_cover_upload input[type=file], "
    ".cover-upload input[type=file]"
)
# 保存为草稿按钮——**必须 :text-is 精确匹配**，避免命中"群发""保存并发送"等含字按钮
# （沿用 §三-B 知乎坑 #1 教训：has-text 是 substring）
SAVE_DRAFT_BUTTON_SELECTOR = 'button:text-is("保存为草稿"), a:text-is("保存为草稿"), button:text-is("保存草稿")'


# ---------------- 反爬约束 ----------------
# 不实现「群发」「发送给全部用户」「mass-send」/send-draft 系列按钮的任何 selector 与 click 调用。
# 公众号群发不可撤回 + 每天次数限制，本 sprint 只走 save-draft，人工在 mp 后台二次确认才点群发。
# 如未来扩展，必须加用户二次确认开关 + dry-run 通道，不在 publisher 内静默触发。


def _default_profile_dir(account_id: int) -> Path:
    base = settings.data_dir / "browser_profiles"
    base.mkdir(parents=True, exist_ok=True)
    return (base / f"wechat_mp_{account_id}").resolve()


async def _random_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """随机停顿，模拟人工节奏，规避 mp 节奏检测。"""
    await asyncio.sleep(random.uniform(lo, hi))


class WechatMpPublisher(PublisherBase):
    platform = Platform.WECHAT_MP
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD  # 复用枚举，无需新增 PublisherKind 项

    async def login(self, account_id: int, credential: dict) -> bool:
        """打开 mp 后台扫码登录，浏览器状态整体持久化到 user-data-dir。

        登录完成后 credential 里写入 profile_dir 路径（绝对路径），
        调用方负责加密落库。
        """
        profile_dir = Path(credential.get("profile_dir") or _default_profile_dir(account_id))
        profile_dir.mkdir(parents=True, exist_ok=True)

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = False  # 登录必须有窗口

        # launch_persistent_context 接收 channel / proxy 是 named 参数，需要从
        # 通用 launch kwargs 里抽出来单独传，避免 **kwargs 重名报错
        channel = kwargs.pop("channel", None)
        proxy = kwargs.pop("proxy", None)

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=channel,
                proxy=proxy,
                viewport={"width": 1440, "height": 900},
                **kwargs,
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(LOGIN_URL, timeout=60000)
            except Exception:
                await ctx.close()
                return False

            # poll 等待跳转到 cgi-bin/home（明确成功标志）
            max_wait, waited = 300, 0
            success = False
            while waited < max_wait:
                await asyncio.sleep(3)
                waited += 3
                url = page.url or ""
                if "cgi-bin/home" not in url:
                    continue
                await asyncio.sleep(3)  # 等页面渲染完
                relogin_hint = await page.evaluate(
                    "() => document.body.innerText.includes('请重新登录') "
                    "|| document.body.innerText.includes('请重新扫码')"
                )
                if relogin_hint:
                    continue
                has_admin = await page.evaluate(
                    "() => !!document.querySelector("
                    "'.weui-desktop-account, .weui-desktop-account__nickname, "
                    ".account_info, [class*=accountInfo], #js_index_msg') "
                    "|| document.body.innerText.length > 1000"
                )
                if has_admin:
                    success = True
                    break

            await ctx.close()

        if success:
            credential["profile_dir"] = str(profile_dir)
            return True
        return False

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """阶段 1：仅支持保存草稿（不触发群发）。

        走 launch_persistent_context（profile_dir 是登录态持久化的关键），
        进图文编辑器 → 填标题 → 正文 paste → 上传封面 → 点「保存为草稿」→
        抓草稿管理后台 URL。
        """
        if content.content_type == ContentType.VIDEO:
            return PublishResult(
                success=False,
                error="公众号视频走视频号路径（Platform.WECHAT_VIDEO），本 publisher 仅做图文",
            )

        profile_dir_str = credential.get("profile_dir")
        if not profile_dir_str or not Path(profile_dir_str).exists():
            return PublishResult(
                success=False,
                error="公众号凭证缺 profile_dir 或目录不存在，请先 POST /accounts/{id}/login",
            )

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        # 草稿保存可 headless（不抢焦点）；若反爬严起来再改回 False
        channel = kwargs.pop("channel", None)
        proxy = kwargs.pop("proxy", None)

        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir_str,
                    channel=channel,
                    proxy=proxy,
                    viewport={"width": 1440, "height": 900},
                    **kwargs,
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    return await self._do_publish(page, content)
                finally:
                    await ctx.close()
        except Exception as e:
            return PublishResult(success=False, error=f"公众号草稿保存异常: {e}")

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        profile_dir_str = credential.get("profile_dir")
        if not profile_dir_str or not Path(profile_dir_str).exists():
            return AccountHealth.EXPIRED

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        kwargs["headless"] = True
        channel = kwargs.pop("channel", None)
        proxy = kwargs.pop("proxy", None)

        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir_str,
                    channel=channel,
                    proxy=proxy,
                    viewport={"width": 1440, "height": 900},
                    **kwargs,
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                try:
                    await page.goto(HOME_URL, timeout=30000)
                    await asyncio.sleep(3)
                    url = page.url or ""
                    if "login" in url:
                        return AccountHealth.EXPIRED
                    has_admin = await page.evaluate(
                        "() => !!document.querySelector("
                        "'.weui-desktop-account__nickname, .weui-desktop-account__name, "
                        ".account_info, [class*=accountInfo]')"
                    )
                    return AccountHealth.HEALTHY if has_admin else AccountHealth.EXPIRED
                finally:
                    await ctx.close()
        except Exception:
            return AccountHealth.UNKNOWN

    # ---------------- 内部 ----------------

    async def _do_publish(self, page, content: PublishContent) -> PublishResult:
        """图文编辑器五段式：goto → title → body → cover → save-draft。

        与 ToutiaoPublisher 同形：先填标题、后填正文；mp 没有"两步发布"，
        只点一次「保存为草稿」即落服务端，不可能在本路径上误触发群发。
        TODO[mp-real]: 首次真发后回填确切 selector + 草稿管理 URL 模式
        """
        # markdown 转换 lazy import：声明在 pyproject.toml dependencies，
        # 但万一部署环境漏装，给出清晰错误而不是 import time 直接挂模块
        try:
            import markdown
        except ImportError:
            return PublishResult(
                success=False,
                error="缺少 markdown 包（pip install markdown），无法将正文转 HTML 注入 mp 编辑器",
            )

        if not (content.title or "").strip():
            return PublishResult(success=False, error="公众号草稿标题为空，无法保存")

        try:
            await page.goto(APPMSG_NEW_URL, timeout=60000)
        except Exception as e:
            return PublishResult(success=False, error=f"打开 mp 图文编辑器失败: {e}")
        await _random_delay(3, 5)

        # ---- 解析编辑器根（优先 iframe，再 fallback 主 frame）----
        editor_root = await self._resolve_editor_root(page)

        # ---- 标题 ----
        try:
            await editor_root.wait_for_selector(TITLE_INPUT_SELECTOR, timeout=15000)
        except Exception as e:
            return PublishResult(success=False, error=f"未找到 mp 标题输入框: {e}")
        try:
            await editor_root.locator(TITLE_INPUT_SELECTOR).first.fill(content.title)
        except Exception as e:
            return PublishResult(success=False, error=f"填写标题失败: {e}")
        await _random_delay(2, 3)

        # ---- 正文：markdown → HTML → ClipboardEvent paste ----
        html = markdown.markdown(
            content.body or "",
            extensions=["fenced_code", "tables", "nl2br"],
        )
        try:
            await self._paste_html_to_editor(page, editor_root, html)
        except Exception as e:
            return PublishResult(success=False, error=f"正文粘贴失败: {e}")
        await _random_delay(3, 5)

        # ---- 封面（mp 图文封面必填，但失败时也尝试保草稿——平台行为）----
        cover_uploaded = False
        if content.images:
            try:
                await editor_root.locator(COVER_UPLOAD_INPUT_SELECTOR).first.set_input_files(
                    content.images[0], timeout=10000
                )
                await _random_delay(5, 7)  # 等服务端处理
                cover_uploaded = True
            except Exception as e:
                # 封面失败 → 整体失败（mp 草稿对封面有强约束，无封面也存不出有效草稿）
                return PublishResult(success=False, error=f"上传封面失败: {e}")

        # ---- 点「保存为草稿」按钮 ----
        try:
            await editor_root.wait_for_selector(SAVE_DRAFT_BUTTON_SELECTOR, timeout=15000)
            await editor_root.locator(SAVE_DRAFT_BUTTON_SELECTOR).first.click()
        except Exception as e:
            return PublishResult(success=False, error=f"点「保存为草稿」失败: {e}")

        await _random_delay(4, 6)

        # ---- 抓草稿 ID / URL ----
        final_url = page.url or ""
        draft_id = self._extract_draft_id(final_url)
        return PublishResult(
            success=True,
            platform_post_id=draft_id,
            platform_url=final_url,
            raw_response={
                "final_url": final_url,
                "cover_uploaded": cover_uploaded,
                "stage": "draft_only",
            },
        )

    async def _resolve_editor_root(self, page):
        """返回编辑器操作根：优先 frame_locator(iframe)，否则 page 本身。

        mp 后台多数情况下编辑器跑在 iframe 内（§三-C 坑 #3），但版本迭代中
        也出现过取消嵌套的形态——`page.frame_locator(...)` 命中不到时
        fallback 到 `page` 自己，让 selector 也能在主文档上跑。
        """
        try:
            fl = page.frame_locator(EDITOR_FRAME_SELECTOR)
            # 用 count 触发一次解析，不抛即认为找到 frame
            await fl.locator("body").count()
            return fl
        except Exception:
            return page

    async def _paste_html_to_editor(self, page, editor_root, html: str) -> None:
        """通过合成 ClipboardEvent 把 HTML 注入到 mp 编辑器（ProseMirror/ueditor 通用）。

        逐字 keyboard.type 在 mp 会被风控 + 格式全丢，paste 路径让编辑器
        自己跑 paste handler 解析结构。selector 同 CONTENT_EDITOR_SELECTOR。
        """
        try:
            await editor_root.locator(CONTENT_EDITOR_SELECTOR).first.click(force=True)
        except Exception:
            # 编辑器还没渲染好，等一波再 paste
            await asyncio.sleep(1)
        await asyncio.sleep(0.5)
        # 用 page.evaluate（不是 frame.evaluate）注入 paste 事件——
        # 现代 mp 编辑器通过 contenteditable 监听 paste，事件冒泡能到 document
        await page.evaluate(
            """({selector, html}) => {
                const editor = document.querySelector(selector.split(',')[0].trim())
                    || document.querySelector('[contenteditable=true]');
                if (!editor) throw new Error('no editor element');
                editor.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
                const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                editor.dispatchEvent(ev);
            }""",
            {"selector": CONTENT_EDITOR_SELECTOR, "html": html},
        )

    @staticmethod
    def _extract_draft_id(url: str) -> str | None:
        """从 mp 编辑器/草稿管理 URL 抽 token=xxx&appmsgid=xxx；没抽到返回 None。

        mp 草稿落库后 URL 通常带 `appmsgid=` 或跳到草稿管理 `cgi-bin/appmsgpublish?...&appmsgid=xxx`。
        TODO[mp-real]: 首次真发后回填确切的 ID 字段名（可能是 mid / appmsgid / draft_id）。
        """
        import re
        if not url:
            return None
        for key in ("appmsgid", "mid", "draft_id"):
            m = re.search(rf"[?&]{key}=(\d+)", url)
            if m:
                return m.group(1)
        return None
