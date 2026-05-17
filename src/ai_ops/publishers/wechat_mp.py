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

阶段 1：支持登录 + 健康检查；发布草稿留 TODO（公众号图文编辑器是 iframe + 复杂富文本，
       建议后续单独迭代）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs
from .base import PublisherBase


LOGIN_URL = "https://mp.weixin.qq.com/"
HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"
APPMSG_NEW_URL = (
    "https://mp.weixin.qq.com/cgi-bin/appmsg"
    "?t=media/appmsg_edit_v2&action=edit&type=77&createType=0&token=&lang=zh_CN"
)


def _default_profile_dir(account_id: int) -> Path:
    base = settings.data_dir / "browser_profiles"
    base.mkdir(parents=True, exist_ok=True)
    return (base / f"wechat_mp_{account_id}").resolve()


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
        """阶段 1：仅支持保存草稿。

        公众号图文编辑器是 iframe + 自研富文本，自动化复杂度高，
        阶段 1 先打通登录 + 健康检查 + 状态机走通，正式发布留 TODO。
        """
        if content.content_type == ContentType.VIDEO:
            return PublishResult(
                success=False,
                error="公众号视频走视频号路径（Platform.WECHAT_VIDEO），本 publisher 仅做图文",
            )
        return PublishResult(
            success=False,
            error="公众号自动发布草稿尚在路线图，请先用本 publisher 做登录态维护",
        )

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
