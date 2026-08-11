"""发布器注册中心 — 平台到 Publisher 的路由 + fallback。

为什么需要：
  - 同一个 Platform 可能有多个 Publisher 实现（主力 + 加固 + 兜底）
  - 主力失败时自动 fallback 到下一个，提高发布成功率
  - 新加平台只需注册新的 Publisher，不动业务代码
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import logging
from typing import Callable

from ..core.enums import Platform
from .base import PublisherBase
from .plugin_sdk import (
    PublisherPluginError,
    PublisherPluginErrorCode,
    PublisherPluginResolutionError,
    PublisherPluginValidationReport,
    instantiate_validated_publisher,
    publisher_kind_value,
    safe_plugin_exception_type,
    validate_enabled_publisher_plugins,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PublisherRegistration:
    priority: int
    sort_key: str
    factory: Callable[[], PublisherBase]


class PublisherRegistry:
    def __init__(self) -> None:
        # platform -> registrations，priority 越小越先尝试；相同优先级用稳定
        # registration id 排序，避免 entry-point 枚举顺序影响真实写路径。
        self._slots: dict[Platform, list[_PublisherRegistration]] = defaultdict(list)
        self._sequence = 0
        self._plugin_validation_report = PublisherPluginValidationReport((), (), ())

    def register(
        self,
        platform: Platform,
        factory: Callable[[], PublisherBase],
        priority: int = 100,
        *,
        registration_id: str | None = None,
    ) -> None:
        self._sequence += 1
        sort_key = registration_id or f"local:{self._sequence:08d}"
        self._slots[platform].append(
            _PublisherRegistration(
                priority=priority,
                sort_key=sort_key,
                factory=factory,
            )
        )
        self._slots[platform].sort(key=lambda item: (item.priority, item.sort_key))

    def set_plugin_validation_report(self, report: PublisherPluginValidationReport) -> None:
        self._plugin_validation_report = report

    @property
    def plugin_validation_report(self) -> PublisherPluginValidationReport:
        return self._plugin_validation_report

    def resolve(self, platform: Platform) -> list[PublisherBase]:
        """返回该平台所有 Publisher，按优先级排序。调用方依次尝试。"""
        if not self._plugin_validation_report.ok:
            # Registry construction remains import-safe so doctor/API reads are
            # available, while external routing fails closed instead of silently
            # choosing a different write path than the operator configured.
            raise PublisherPluginResolutionError()
        publishers: list[PublisherBase] = []
        for registration in self._slots.get(platform, []):
            try:
                publisher = registration.factory()
            except PublisherPluginResolutionError:
                # Plugin factories are wrapped with per-construction manifest
                # validation. Preserve that stable reason for worker/doctor
                # diagnostics instead of relabeling it as a generic factory
                # exception.
                raise
            except (Exception, SystemExit) as exc:
                # Construction happens only during explicit routing, not import.
                # Never fall through to another write path after a configured
                # factory changes underneath its validated manifest.
                exception_type = safe_plugin_exception_type(exc)
                logger.error(
                    "publisher factory unavailable",
                    extra={
                        "registration_id": registration.sort_key,
                        "exception_type": exception_type,
                    },
                )
                raise PublisherPluginResolutionError("publisher_factory_failed") from None
            if not isinstance(publisher, PublisherBase):
                logger.error(
                    "publisher factory returned invalid type",
                    extra={"registration_id": registration.sort_key},
                )
                raise PublisherPluginResolutionError("publisher_factory_returned_invalid_type")
            publishers.append(publisher)
        return publishers

    @staticmethod
    def kind_value(publisher: PublisherBase) -> str:
        """Normalize enum and plugin/test string kinds to their persisted value."""
        return publisher_kind_value(publisher.kind)

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
            (publisher for publisher in collectors if self.kind_value(publisher) == publisher_kind),
            None,
        )

    def supported_platforms(self) -> list[Platform]:
        return list(self._slots.keys())


def build_default_registry(
    *,
    config=None,
    plugin_entry_points=None,
) -> PublisherRegistry:
    """默认装配——按选型决策注册。"""
    from ..config import settings
    from .github_pages import GitHubPagesPublisher
    from .social_auto_upload import SAU_PLATFORM_MAP, SocialAutoUploadPublisher
    from .toutiao import ToutiaoPublisher
    from .wechat_mp import WechatMpPublisher
    from .xhs_skills import XhsSkillsPublisher
    from .zhihu import ZhihuPublisher

    active_config = settings if config is None else config
    reg = PublisherRegistry()

    # SAU 兼容层：只注册当前审计过的 CLI/HTTP contract 并集。
    for p in SAU_PLATFORM_MAP:
        reg.register(
            p,
            lambda p=p: SocialAutoUploadPublisher(p),
            priority=10,
            registration_id=f"builtin:social_auto_upload:{p.value}",
        )

    # YouTube official-API CLI canary. SAU 当前没有已审计的 YouTube contract，
    # 因此这里只存在 receipt-confirmed CLI 单路径，不伪造 fallback。
    if active_config.youtube_uploader_enabled:
        from .youtube_cli import YoutubeUploaderPublisher

        reg.register(
            Platform.YOUTUBE,
            YoutubeUploaderPublisher,
            priority=5,
            registration_id="builtin:youtube_uploader",
        )

    # 小红书反风控主链路：BROWSER_ENGINE=camoufox 时，XhsCamoufoxPublisher 顶到最高优先级
    if active_config.browser_engine == "camoufox":
        from .xhs_camoufox import XhsCamoufoxPublisher

        reg.register(
            Platform.XIAOHONGSHU,
            XhsCamoufoxPublisher,
            priority=5,
            registration_id="builtin:xhs_camoufox",
        )

    # 小红书加固 — 主力失败时 fallback
    reg.register(
        Platform.XIAOHONGSHU,
        XhsSkillsPublisher,
        priority=20,
        registration_id="builtin:xhs_skills",
    )

    # 知乎 CLI-first canary：第三方 0.2.4 适配器只有显式开启才装配；
    # binary/version/profile/content 预检失败可安全落到现有浏览器适配器。
    # 写进程一旦启动却未确认结果，会由 worker 的 outcome_uncertain 语义阻断 fallback。
    if active_config.zhihu_cli_enabled:
        from .zhihu_cli import ZhihuCliPublisher

        reg.register(
            Platform.ZHIHU,
            ZhihuCliPublisher,
            priority=5,
            registration_id="builtin:zhihu_cli",
        )

    # 知乎浏览器兜底、头条/公众号自建适配器
    reg.register(
        Platform.ZHIHU,
        ZhihuPublisher,
        priority=10,
        registration_id="builtin:zhihu_browser",
    )
    reg.register(
        Platform.TOUTIAO,
        ToutiaoPublisher,
        priority=10,
        registration_id="builtin:toutiao_browser",
    )
    reg.register(
        Platform.WECHAT_MP,
        WechatMpPublisher,
        priority=10,
        registration_id="builtin:wechat_mp_browser",
    )

    # 自有博客（GitHub Pages / Hexo）
    reg.register(
        Platform.GITHUB_PAGES,
        GitHubPagesPublisher,
        priority=10,
        registration_id="builtin:github_pages",
    )

    # 百家号/搜狐号仍是未经真平台 canary 的 selector Stub。类和 mock 契约保留给
    # 上游协作，但默认 registry 不提供可执行写路径，避免“代码存在”被误解成可运营。
    if active_config.baijiahao_publisher_enabled:
        from .baijiahao import BaijiahaoPublisher

        reg.register(
            Platform.BAIJIAHAO,
            BaijiahaoPublisher,
            priority=10,
            registration_id="builtin:baijiahao_browser",
        )
    if active_config.sohuhao_publisher_enabled:
        from .sohuhao import SohuhaoPublisher

        reg.register(
            Platform.SOHUHAO,
            SohuhaoPublisher,
            priority=10,
            registration_id="builtin:sohuhao_browser",
        )

    enabled_plugins = tuple(getattr(active_config, "publisher_plugin_allowlist", ()) or ())
    try:
        plugin_report = validate_enabled_publisher_plugins(
            enabled_plugins,
            entry_points=plugin_entry_points,
        )
    except (Exception, SystemExit) as exc:
        plugin_report = PublisherPluginValidationReport(
            enabled_selectors=tuple(sorted(enabled_plugins)),
            loaded=(),
            errors=(
                PublisherPluginError(
                    selector="publisher-plugin-selection",
                    code=PublisherPluginErrorCode.LOAD_FAILED,
                    exception_type=safe_plugin_exception_type(exc),
                ),
            ),
        )
    for loaded in plugin_report.loaded:
        manifest = loaded.plugin.manifest
        reg.register(
            manifest.platform,
            lambda loaded=loaded: instantiate_validated_publisher(
                loaded.selector,
                loaded.plugin,
            ),
            priority=manifest.priority,
            registration_id=f"plugin:{loaded.selector}",
        )
    reg.set_plugin_validation_report(plugin_report)

    return reg


default_registry = build_default_registry()
