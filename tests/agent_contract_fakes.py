"""Deterministic, credential-free renderer doubles for Agent contract tests."""

from __future__ import annotations

from ai_ops.core.enums import AssetType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent
from ai_ops.publishers.base import (
    AgentContractAssetRule,
    AgentContractRendererDescriptor,
    AgentContractRendererUnavailable,
)


class ExactZhihuTestPublisher:
    """Pure renderer double; it never implements or performs an external write."""

    platform = Platform.ZHIHU
    kind = PublisherKind.ZHIHU_CLI
    agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
        renderer_id="tests.zhihu.exact-payload",
        contract_version="1",
        adapter_version="test-1",
        platform=platform,
        publisher_kind=kind,
        accepted_extra_keys=("campaign", "language", "purpose"),
        accepts_tags=False,
        requires_external_account_id=True,
        asset_rules=(
            AgentContractAssetRule(
                asset_type=AssetType.IMAGE,
                min_count=0,
                max_count=9,
            ),
        ),
    )

    def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
        allowed_extra_keys = set(self.agent_contract_renderer_descriptor.accepted_extra_keys)
        if set(content.extra).difference(allowed_extra_keys):
            raise AgentContractRendererUnavailable("unsupported test extra field")
        if content.tags or content.videos:
            raise AgentContractRendererUnavailable("unsupported test content")
        return {
            "action": "publish-test-article",
            "title": content.title,
            "body": content.body,
            "content_type": content.content_type.value,
            "extra": content.extra,
            "image_slots": [
                {"asset_type": AssetType.IMAGE.value, "index": index}
                for index in range(len(content.images))
            ],
        }

    def agent_contract_digest_material(self, content: PublishContent) -> dict[str, object]:
        return {
            "renderer": self.agent_contract_renderer_descriptor.digest_material(),
            "payload": self.render_agent_contract_payload(content),
        }


class ExactRendererTestRegistry:
    """Small registry double with an explicit enabled/disabled boundary."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def resolve(self, platform: Platform) -> list[ExactZhihuTestPublisher]:
        if self._enabled and platform is Platform.ZHIHU:
            return [ExactZhihuTestPublisher()]
        return []


EXACT_RENDERER_REGISTRY = ExactRendererTestRegistry()
NO_EXACT_RENDERER_REGISTRY = ExactRendererTestRegistry(enabled=False)
