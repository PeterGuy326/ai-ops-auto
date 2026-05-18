"""发布器注册中心 — 平台到 Publisher 的路由 + fallback。

为什么需要：
  - 同一个 Platform 可能有多个 Publisher 实现（主力 + 加固 + 兜底）
  - 主力失败时自动 fallback 到下一个，提高发布成功率
  - 新加平台只需注册新的 Publisher，不动业务代码
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..core.enums import Platform
from .base import PublisherBase


class PublisherRegistry:
    def __init__(self) -> None:
        # platform -> [(priority, factory)]，priority 越小越先尝试
        self._slots: dict[Platform, list[tuple[int, Callable[[], PublisherBase]]]] = defaultdict(list)

    def register(
        self,
        platform: Platform,
        factory: Callable[[], PublisherBase],
        priority: int = 100,
    ) -> None:
        self._slots[platform].append((priority, factory))
        self._slots[platform].sort(key=lambda t: t[0])

    def resolve(self, platform: Platform) -> list[PublisherBase]:
        """返回该平台所有 Publisher，按优先级排序。调用方依次尝试。"""
        return [factory() for _, factory in self._slots.get(platform, [])]

    def supported_platforms(self) -> list[Platform]:
        return list(self._slots.keys())


def build_default_registry() -> PublisherRegistry:
    """默认装配——按选型决策注册。"""
    from ..config import settings
    from .github_pages import GitHubPagesPublisher
    from .social_auto_upload import SAU_PLATFORM_MAP, SocialAutoUploadPublisher
    from .toutiao import ToutiaoPublisher
    from .wechat_mp import WechatMpPublisher
    from .xhs_skills import XhsSkillsPublisher
    from .zhihu import ZhihuPublisher

    reg = PublisherRegistry()

    # SAU 主力 — 覆盖 7 个平台
    for p in SAU_PLATFORM_MAP:
        reg.register(p, lambda p=p: SocialAutoUploadPublisher(p), priority=10)

    # 小红书反风控主链路：BROWSER_ENGINE=camoufox 时，XhsCamoufoxPublisher 顶到最高优先级
    if settings.browser_engine == "camoufox":
        from .xhs_camoufox import XhsCamoufoxPublisher
        reg.register(Platform.XIAOHONGSHU, XhsCamoufoxPublisher, priority=5)

    # 小红书加固 — 主力失败时 fallback
    reg.register(Platform.XIAOHONGSHU, XhsSkillsPublisher, priority=20)

    # 知乎、头条 — 开源缺口，自建
    reg.register(Platform.ZHIHU, ZhihuPublisher, priority=10)
    reg.register(Platform.TOUTIAO, ToutiaoPublisher, priority=10)
    reg.register(Platform.WECHAT_MP, WechatMpPublisher, priority=10)

    # 自有博客（GitHub Pages / Hexo）
    reg.register(Platform.GITHUB_PAGES, GitHubPagesPublisher, priority=10)

    # 百家号（Round 2A）— 开源缺口，自建；百度 SEO 流量管道，与头条号互补
    from .baijiahao import BaijiahaoPublisher
    reg.register(Platform.BAIJIAHAO, BaijiahaoPublisher, priority=10)

    return reg


default_registry = build_default_registry()


# ---------------- Round 2B 增量：搜狐号 ----------------
# 与 baijiahao Round 2A 并行 P7 任务，物理分区在 registry.py 末尾各加各的注册块，
# 互不干涉、互不重复 import；git merge 时按"先后追加"语义保留两块即可。
from .sohuhao import SohuhaoPublisher  # noqa: E402
default_registry.register(Platform.SOHUHAO, SohuhaoPublisher, priority=10)
