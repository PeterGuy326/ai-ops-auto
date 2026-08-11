"""发布器注册中心 — 平台到 Publisher 的路由 + fallback。

为什么需要：
  - 同一个 Platform 可能有多个 Publisher 实现（主力 + 加固 + 兜底）
  - 主力失败时自动 fallback 到下一个，提高发布成功率
  - 新加平台只需注册新的 Publisher，不动业务代码
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..core.enums import Platform, PublisherKind
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

    @staticmethod
    def kind_value(publisher: PublisherBase) -> str:
        """Normalize enum and plugin/test string kinds to their persisted value."""
        kind = publisher.kind
        return kind.value if isinstance(kind, PublisherKind) else str(kind)

    def resolve_collector(
        self,
        platform: Platform,
        publisher_kind: str = "",
    ) -> PublisherBase | None:
        """Resolve an explicitly capable collector for the actual publish path.

        New jobs must route back to the exact adapter kind that published them.
        A legacy job with no kind may use the first explicitly capable collector,
        but never the base class' unsupported implementation.
        """
        collectors = [
            publisher
            for publisher in self.resolve(platform)
            if bool(getattr(publisher, "supports_metrics", False))
        ]
        if not publisher_kind:
            return collectors[0] if collectors else None
        return next(
            (
                publisher
                for publisher in collectors
                if self.kind_value(publisher) == publisher_kind
            ),
            None,
        )

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

    # SAU 兼容层：只注册当前审计过的 CLI/HTTP contract 并集。
    for p in SAU_PLATFORM_MAP:
        reg.register(p, lambda p=p: SocialAutoUploadPublisher(p), priority=10)

    # YouTube official-API CLI canary. SAU 当前没有已审计的 YouTube contract，
    # 因此这里只存在 receipt-confirmed CLI 单路径，不伪造 fallback。
    if settings.youtube_uploader_enabled:
        from .youtube_cli import YoutubeUploaderPublisher

        reg.register(Platform.YOUTUBE, YoutubeUploaderPublisher, priority=5)

    # 小红书反风控主链路：BROWSER_ENGINE=camoufox 时，XhsCamoufoxPublisher 顶到最高优先级
    if settings.browser_engine == "camoufox":
        from .xhs_camoufox import XhsCamoufoxPublisher
        reg.register(Platform.XIAOHONGSHU, XhsCamoufoxPublisher, priority=5)

    # 小红书加固 — 主力失败时 fallback
    reg.register(Platform.XIAOHONGSHU, XhsSkillsPublisher, priority=20)

    # 知乎 CLI-first canary：第三方 0.2.4 适配器只有显式开启才装配；
    # binary/version/profile/content 预检失败可安全落到现有浏览器适配器。
    # 写进程一旦启动却未确认结果，会由 worker 的 outcome_uncertain 语义阻断 fallback。
    if settings.zhihu_cli_enabled:
        from .zhihu_cli import ZhihuCliPublisher

        reg.register(Platform.ZHIHU, ZhihuCliPublisher, priority=5)

    # 知乎浏览器兜底、头条/公众号自建适配器
    reg.register(Platform.ZHIHU, ZhihuPublisher, priority=10)
    reg.register(Platform.TOUTIAO, ToutiaoPublisher, priority=10)
    reg.register(Platform.WECHAT_MP, WechatMpPublisher, priority=10)

    # 自有博客（GitHub Pages / Hexo）
    reg.register(Platform.GITHUB_PAGES, GitHubPagesPublisher, priority=10)

    # 百家号/搜狐号仍是未经真平台 canary 的 selector Stub。类和 mock 契约保留给
    # 上游协作，但默认 registry 不提供可执行写路径，避免“代码存在”被误解成可运营。
    if settings.baijiahao_publisher_enabled:
        from .baijiahao import BaijiahaoPublisher

        reg.register(Platform.BAIJIAHAO, BaijiahaoPublisher, priority=10)
    if settings.sohuhao_publisher_enabled:
        from .sohuhao import SohuhaoPublisher

        reg.register(Platform.SOHUHAO, SohuhaoPublisher, priority=10)

    return reg


default_registry = build_default_registry()
