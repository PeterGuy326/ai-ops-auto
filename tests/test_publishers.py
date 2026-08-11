"""publisher registry 路由 + fallback 行为单测。"""
from __future__ import annotations

import asyncio


from ai_ops.core.enums import AccountHealth, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers.base import PublisherBase
from ai_ops.publishers.registry import PublisherRegistry
from ai_ops.scheduler import worker as worker_mod


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


class _Uncertain(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    async def login(self, account_id, credential):
        return False

    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=False,
            outcome_uncertain=True,
            error="platform outcome unknown",
        )

    async def health_check(self, account_id, credential):
        return AccountHealth.UNKNOWN


class _KnownPartialEffect(_Uncertain):
    async def publish(self, account_id, credential, content):
        return PublishResult(
            success=False,
            effect_applied=True,
            platform_post_id="already-created",
            error="post created but follow-up failed",
        )


class _RaisesUnexpectedly(_Uncertain):
    async def publish(self, account_id, credential, content):
        raise RuntimeError("exception text may contain a secret")


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


def test_worker_fallback_stops_on_uncertain_outcome(monkeypatch):
    """A second Publisher could duplicate a write that the first cannot confirm."""
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _Uncertain, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.outcome_uncertain is True


def test_worker_fallback_stops_after_known_partial_effect(monkeypatch):
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _KnownPartialEffect, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.effect_applied is True
    assert result.platform_post_id == "already-created"


def test_worker_unexpected_exception_is_unknown_and_stops_fallback(monkeypatch):
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _RaisesUnexpectedly, priority=10)
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk, priority=20)
    monkeypatch.setattr(worker_mod, "default_registry", reg)

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
        )
    )

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["exception_type"] == "RuntimeError"
    assert "secret" not in result.model_dump_json()


def test_custom_receipt_writer_failure_does_not_erase_confirmed_result():
    reg = PublisherRegistry()
    reg.register(Platform.XIAOHONGSHU, _AlwaysOk)

    def broken_receipt_writer(**kwargs):
        raise OSError("isolated receipt store unavailable")

    result = asyncio.run(
        worker_mod._try_publishers(
            Platform.XIAOHONGSHU,
            1,
            {},
            PublishContent(title="t", body="b", content_type="image_text"),
            registry=reg,
            receipt_writer=broken_receipt_writer,
        )
    )

    assert result.success is True
    assert result.platform_post_id == "x1"
