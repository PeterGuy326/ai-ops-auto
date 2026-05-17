"""publisher registry 路由 + fallback 行为单测。"""
from __future__ import annotations

import asyncio

import pytest

from ai_ops.core.enums import AccountHealth, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers.base import PublisherBase
from ai_ops.publishers.registry import PublisherRegistry


class _AlwaysFail(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    async def login(self, account_id, credential):
        return False

    async def publish(self, account_id, credential, content):
        return PublishResult(success=False, error="模拟失败")

    async def health_check(self, account_id, credential):
        return AccountHealth.DEGRADED


class _AlwaysOk(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.XHS_TOOLKIT

    async def login(self, account_id, credential):
        return True

    async def publish(self, account_id, credential, content):
        return PublishResult(success=True, platform_post_id="x1", platform_url="https://xhs/x1")

    async def health_check(self, account_id, credential):
        return AccountHealth.HEALTHY


def test_registry_priority_order():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    reg.register(Platform.XIAOHONGSHU, _AlwaysFail, priority=10)

    pubs = reg.resolve(Platform.XIAOHONGSHU)
    # priority 10 排在 20 之前
    assert pubs[0].kind == PublisherKind.SOCIAL_AUTO_UPLOAD
    assert pubs[1].kind == PublisherKind.XHS_TOOLKIT


def test_registry_supported_platforms():
    reg = PublisherRegistry()
    reg.register(Platform.DOUYIN, _AlwaysOk)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk)
    assert set(reg.supported_platforms()) == {Platform.DOUYIN, Platform.XIAOHONGSHU}


def test_fallback_chain_simulated():
    """模拟 worker 的 fallback 逻辑：第一个失败，第二个成功。"""
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysFail, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)

    async def run():
        content = PublishContent(title="t", body="b", content_type="image_text")
        for pub in reg.resolve(Platform.XIAOHONGSHU):
            res = await pub.publish(1, {}, content)
            if res.success:
                return res
        return None

    res = asyncio.run(run())
    assert res is not None and res.success
    assert res.platform_post_id == "x1"
