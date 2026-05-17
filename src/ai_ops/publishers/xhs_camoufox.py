"""小红书 Camoufox 直管发布器（反风控主链路）。

为什么单独写一个 Camoufox publisher：
  - Camoufox 不是 Playwright drop-in（Firefox + C++ 层指纹欺骗），
    sitecustomize 注入路线只能给 patchright 用，外部 SAU/XiaohongshuSkills 走 Chromium。
  - 小红书是高风控 ★★★★★，README 自己说 "Camoufox 是 2026 公认反检测最强"，
    所以小红书走自管链路、其它平台仍走 SAU——这是这条规则的唯一例外。
  - 在 in-process 跑，能拿到 per-account 指纹种子 + 持久化 user_data_dir + per-account proxy，
    这三者 subprocess 模式都很难干净地传递。

反风控关键设计：
  1. user_data_dir 按 account_id 隔离 —— Firefox profile 全套持久化，登录态/缓存/Cookie 都不动
  2. 指纹按 account_id 派生（accounts.manager.get_account_fingerprint）—— 同账号每次 launch 同指纹
  3. proxy 优先取账号 profile.proxy，settings.browser_proxy 仅兜底
  4. humanize=True —— Camoufox 内置的真人化鼠标轨迹
  5. 发布前/后各刷一次推荐流 ——模拟真人发布场景
  6. 文案在生成层经 content.humanize 处理，规避 AI 检测

不做：
  - 不自己抠 x-s 签名 —— Camoufox 起真浏览器自然渲染，签名由前端 JS 自己算
  - 不试图绕过滑块/扫码 —— 第一次登录走人工扫码，之后 profile 长期复用
  - 不强行无头 —— settings.browser_headless 默认 False；小红书强烈建议有窗口
"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

from ..accounts.manager import get_account_fingerprint, get_account_proxy
from ..config import settings
from ..core.db import session_scope
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.models import Account
from ..core.schemas import PublishContent, PublishResult
from .base import PublisherBase

log = logging.getLogger(__name__)


# —— 小红书 DOM 选择器 ——
# 易变层，被风控/前端改版打中时来这里改。每条都带 fallback 候选。
class _XhsSelectors:
    # 登录页/扫码入口
    LOGIN_TRIGGER = [
        'div.login-container button:has-text("登录")',
        'button:has-text("登录")',
        'a:has-text("登录")',
    ]
    QR_IMG = ['img.qrcode-img', 'div.qrcode img', 'img[alt*="二维码"]']
    # 创作中心
    PUBLISH_TAB_IMAGE = [
        'div.creator-tab:has-text("上传图文")',
        'button:has-text("上传图文")',
        'div:has-text("图文")[role="tab"]',
    ]
    PUBLISH_TAB_VIDEO = [
        'div.creator-tab:has-text("上传视频")',
        'button:has-text("上传视频")',
    ]
    UPLOAD_INPUT = 'input[type="file"]'  # 隐藏的 file input，全页面通用
    TITLE_INPUT = [
        'input[placeholder*="标题"]',
        'input.d-text[placeholder*="标题"]',
    ]
    BODY_EDITOR = [
        'div[contenteditable="true"]',
        'div.ql-editor[contenteditable="true"]',
        'div.editor[contenteditable="true"]',
    ]
    PUBLISH_BTN = [
        'button:has-text("发布")',
        'div.submit:has-text("发布")',
        'button.submit:has-text("发布")',
    ]
    SUCCESS_TOAST = [
        'div:has-text("发布成功")',
        'div.toast:has-text("成功")',
    ]
    RISK_BANNER = [
        'div:has-text("当前账号存在异常")',
        'div:has-text("操作过于频繁")',
        'div:has-text("账号已被限制")',
    ]


# —— URL 常量 ——
_URL_HOME = "https://www.xiaohongshu.com/explore"
_URL_CREATOR_HOME = "https://creator.xiaohongshu.com"
_URL_PUBLISH = "https://creator.xiaohongshu.com/publish/publish"
_URL_USER = "https://www.xiaohongshu.com/user/profile"


def _try_selectors(selectors: list[str]) -> str:
    """返回 css selector 列表的"或"组合，让 locator 命中任一即可。"""
    return ", ".join(selectors)


class XhsCamoufoxPublisher(PublisherBase):
    """小红书 Camoufox 直管发布器。"""

    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.XHS_TOOLKIT  # 复用现有枚举（kind 是分类指引，不是实现锚点）

    # —— 路径与指纹 ——

    def _profile_dir(self, account_id: int) -> Path:
        """每账号一个 Firefox profile（持久化登录态 + 缓存 + Cookie + 指纹）。"""
        d = Path(settings.data_dir) / "browser_profiles" / "xhs" / f"acc_{account_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_fingerprint(self, account_id: int) -> dict:
        with session_scope() as s:
            account = s.get(Account, account_id)
            return get_account_fingerprint(account)

    def _load_proxy(self, account_id: int) -> str:
        with session_scope() as s:
            account = s.get(Account, account_id)
            return get_account_proxy(account)

    # —— 浏览器上下文 ——

    def _camoufox_launch_opts(self, account_id: int) -> dict[str, Any]:
        """组装 AsyncCamoufox 启动参数。"""
        fp = self._load_fingerprint(account_id)
        proxy = self._load_proxy(account_id)

        opts: dict[str, Any] = {
            "headless": settings.browser_headless,
            "humanize": True,           # 真人化鼠标轨迹（核心反行为分析）
            "geoip": True,              # 按出口 IP 自动匹配 timezone/locale/经纬度
            "os": fp["os"],             # per-account 稳定 OS 指纹
            "screen": fp["screen"],     # per-account 稳定屏幕
            "locale": fp["locale"],
            "user_data_dir": str(self._profile_dir(account_id)),
            "persistent_context": True,
        }
        if proxy:
            opts["proxy"] = {"server": proxy}
        return opts

    # —— PublisherBase 实现 ——

    async def login(self, account_id: int, credential: dict) -> bool:
        """打开扫码页等待人工扫码；profile 持久化后续无需重复登录。"""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            log.error("camoufox 未安装：pip install -U 'camoufox[geoip]' && python -m camoufox fetch")
            return False

        # 登录必须有窗口（扫码）
        opts = self._camoufox_launch_opts(account_id)
        opts["headless"] = False
        async with AsyncCamoufox(**opts) as browser:
            page = await browser.new_page()
            await page.goto(_URL_HOME, wait_until="domcontentloaded")
            # 已登录的话直接退出（profile 已持久化）
            if await self._is_logged_in(page):
                log.info(f"xhs acc_{account_id} 已登录（profile 命中）")
                return True
            # 触发登录弹窗
            try:
                await page.locator(_try_selectors(_XhsSelectors.LOGIN_TRIGGER)).first.click(timeout=5000)
            except Exception:
                pass  # 站点可能默认就弹出了
            # 等用户扫码（轮询 cookie；超时 120s）
            for _ in range(60):
                await asyncio.sleep(2)
                if await self._is_logged_in(page):
                    log.info(f"xhs acc_{account_id} 扫码登录成功")
                    return True
            log.warning(f"xhs acc_{account_id} 扫码超时")
            return False

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            return PublishResult(
                success=False,
                error="camoufox 未安装：pip install -U 'camoufox[geoip]' && python -m camoufox fetch",
            )

        # 入参校验
        is_video = bool(content.videos) or content.content_type == ContentType.VIDEO
        if is_video and not content.videos:
            return PublishResult(success=False, error="视频笔记必须提供 video 文件或 URL")
        if not is_video and not content.images:
            return PublishResult(success=False, error="图文笔记必须提供至少一张图片")
        # XHS 不接受 URL 形式上传，需要本地路径
        media_local = (content.videos if is_video else content.images)
        for m in media_local:
            if m.startswith("http"):
                return PublishResult(
                    success=False,
                    error=f"Camoufox 直传需要本地文件，不支持 URL: {m}（请先下载到本地）",
                )

        opts = self._camoufox_launch_opts(account_id)
        async with AsyncCamoufox(**opts) as browser:
            page = await browser.new_page()

            # —— 1. 真人化前置：先逛 30-60s 推荐流 ——
            try:
                await page.goto(_URL_HOME, wait_until="domcontentloaded", timeout=30000)
                if not await self._is_logged_in(page):
                    return PublishResult(
                        success=False,
                        error="登录态失效，请先调用 login()（扫码）",
                    )
                await self._human_browse(page, dwell_seconds=random.uniform(25, 55))
            except Exception as e:
                log.warning(f"前置浏览失败 (不阻断): {e}")

            # —— 2. 走到创作页 ——
            try:
                await page.goto(_URL_PUBLISH, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                return PublishResult(success=False, error=f"打开创作页失败: {e}")

            # —— 3. 风控横幅扫描 ——
            risk = await self._detect_risk_banner(page)
            if risk:
                return PublishResult(
                    success=False,
                    error=f"小红书风控拦截: {risk}（建议暂停该账号 24h+）",
                    raw_response={"risk_banner": risk},
                )

            # —— 4. 切换图文/视频 tab ——
            tab_selectors = (
                _XhsSelectors.PUBLISH_TAB_VIDEO if is_video else _XhsSelectors.PUBLISH_TAB_IMAGE
            )
            try:
                await page.locator(_try_selectors(tab_selectors)).first.click(timeout=5000)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception:
                log.info("tab 切换 selector 未命中，可能站点改版；继续尝试上传")

            # —— 5. 上传 ——
            try:
                upload_input = page.locator(_XhsSelectors.UPLOAD_INPUT).first
                await upload_input.set_input_files(media_local)
            except Exception as e:
                return PublishResult(success=False, error=f"上传媒体失败: {e}")

            # —— 6. 等上传完成 ——
            await asyncio.sleep(random.uniform(3, 6))  # 让上传开始
            for _ in range(60):  # 最多等 60s（视频长 + 网络慢）
                if await page.locator(_try_selectors(_XhsSelectors.TITLE_INPUT)).count() > 0:
                    break
                await asyncio.sleep(1)
            else:
                return PublishResult(success=False, error="等待上传完成超时 60s")

            # —— 7. 填标题、正文 ——
            try:
                await self._human_type(
                    page.locator(_try_selectors(_XhsSelectors.TITLE_INPUT)).first,
                    content.title,
                )
                body_full = self._compose_body(content)
                await self._human_type(
                    page.locator(_try_selectors(_XhsSelectors.BODY_EDITOR)).first,
                    body_full,
                )
            except Exception as e:
                return PublishResult(success=False, error=f"填写标题/正文失败: {e}")

            # —— 8. 真人化中间停顿（看一眼自己写的内容） ——
            await asyncio.sleep(random.uniform(4, 10))

            # —— 9. 点发布 ——
            try:
                await page.locator(_try_selectors(_XhsSelectors.PUBLISH_BTN)).first.click(timeout=8000)
            except Exception as e:
                return PublishResult(success=False, error=f"点击发布失败: {e}")

            # —— 10. 等结果 ——
            ok = False
            url = None
            for _ in range(30):
                await asyncio.sleep(1)
                if await page.locator(_try_selectors(_XhsSelectors.SUCCESS_TOAST)).count() > 0:
                    ok = True
                    break
                # 跳转到笔记详情/创作中心首页一般也表示成功
                if "/explore" in page.url or page.url.startswith(_URL_CREATOR_HOME) and "publish" not in page.url:
                    ok = True
                    url = page.url
                    break

            # —— 11. 真人化后置：刷一会儿推荐流再退 ——
            try:
                await page.goto(_URL_HOME, wait_until="domcontentloaded", timeout=20000)
                await self._human_browse(page, dwell_seconds=random.uniform(15, 30))
            except Exception:
                pass

            if not ok:
                return PublishResult(
                    success=False,
                    error="发布后未检测到成功标志（toast 未出现 / 未跳转）",
                    raw_response={"final_url": page.url},
                )
            return PublishResult(
                success=True,
                platform_url=url,
                raw_response={"final_url": page.url},
            )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """打开个人主页探活：能进 = HEALTHY，跳登录 = EXPIRED，命中限流横幅 = DEGRADED。"""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            return AccountHealth.UNKNOWN

        opts = self._camoufox_launch_opts(account_id)
        opts["headless"] = True  # 探活可以无头
        try:
            async with AsyncCamoufox(**opts) as browser:
                page = await browser.new_page()
                await page.goto(_URL_USER, wait_until="domcontentloaded", timeout=20000)
                if not await self._is_logged_in(page):
                    return AccountHealth.EXPIRED
                if await self._detect_risk_banner(page):
                    return AccountHealth.DEGRADED
                return AccountHealth.HEALTHY
        except Exception as e:
            log.warning(f"health_check 异常 acc_{account_id}: {e}")
            return AccountHealth.UNKNOWN

    # —— 内部工具 ——

    async def _is_logged_in(self, page) -> bool:
        """通过 cookie 名判断登录态。小红书登录后会种 web_session / a1 等。"""
        try:
            cookies = await page.context.cookies("https://www.xiaohongshu.com")
            names = {c.get("name") for c in cookies}
            return bool(names & {"web_session", "a1", "webId"})
        except Exception:
            return False

    async def _detect_risk_banner(self, page) -> str | None:
        try:
            for sel in _XhsSelectors.RISK_BANNER:
                el = page.locator(sel).first
                if await el.count() > 0:
                    txt = (await el.text_content() or "").strip()
                    if txt:
                        return txt
        except Exception:
            pass
        return None

    async def _human_browse(self, page, dwell_seconds: float) -> None:
        """模拟真人浏览：随机滚动 + 不规则停顿。"""
        end = asyncio.get_event_loop().time() + dwell_seconds
        while asyncio.get_event_loop().time() < end:
            try:
                await page.mouse.wheel(0, random.randint(200, 700))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(1.2, 3.5))
            # 偶尔回滚
            if random.random() < 0.1:
                try:
                    await page.mouse.wheel(0, -random.randint(100, 300))
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.8, 2.0))

    async def _human_type(self, locator, text: str) -> None:
        """逐字输入 + 随机间隔（Camoufox 的 humanize 主要管鼠标，键盘节奏自己加）。"""
        await locator.click()
        await asyncio.sleep(random.uniform(0.2, 0.6))
        for ch in text:
            await locator.type(ch, delay=random.uniform(35, 110))
            # 偶尔停下来"想一下"
            if random.random() < 0.04:
                await asyncio.sleep(random.uniform(0.3, 1.2))

    def _compose_body(self, content: PublishContent) -> str:
        """正文 + 标签拼接。XHS 习惯标签放最后，每个 # 前留空格。

        发布前最后一道反 AI 检测净化：即使外部生成器没过 humanize，发布层兜底再洗一次。
        受保护片段（链接/标签/@用户/代码）在 humanize 内部已处理。
        """
        body = content.body or ""
        if settings.xhs_humanize_enabled and body:
            try:
                from ..content.humanize import HumanizeOptions, humanize_for_xhs
                body = humanize_for_xhs(body, HumanizeOptions())
            except Exception as e:
                log.warning(f"humanize 失败 (不阻断发布): {e}")
        if content.tags:
            tag_line = " ".join(f"#{t.lstrip('#')}" for t in content.tags)
            body = f"{body}\n\n{tag_line}" if body else tag_line
        return body
