"""Installed-wheel fixture proving that disabled Publisher plugins stay unloaded."""

from __future__ import annotations

import os
from pathlib import Path

from ai_ops.publishers import (
    AccountHealth,
    Platform,
    PublishResult,
    PublisherBase,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
)


sentinel = os.environ.get("AI_OPS_PLUGIN_SENTINEL")
if sentinel:
    Path(sentinel).write_text("fixture plugin imported\n", encoding="utf-8")


class FixturePublisher(PublisherBase):
    platform = Platform.ZHIHU
    kind = "fixture_zhihu"

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        return PublishResult(success=True, platform_post_id="fixture")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


def publisher_plugin() -> PublisherPlugin:
    return PublisherPlugin(
        manifest=PublisherPluginManifest(
            plugin_id="fixture.zhihu",
            plugin_version="1.0.0",
            api_version=1,
            platform=Platform.ZHIHU,
            publisher_kind="fixture_zhihu",
            adapter_version="fixture-1",
            capabilities=(
                PublisherPluginCapability.HEALTH_CHECK,
                PublisherPluginCapability.LOGIN,
                PublisherPluginCapability.PUBLISH,
            ),
        ),
        factory=FixturePublisher,
    )


__all__ = ["FixturePublisher", "publisher_plugin"]
