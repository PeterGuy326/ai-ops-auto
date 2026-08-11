"""Deterministic fake backends used only by the offline demo.

These classes deliberately contain no HTTP, browser, subprocess, filesystem,
or credential-loading code.  A caller must instantiate them directly; they are
not registered in :mod:`ai_ops.publishers.registry`.
"""

from __future__ import annotations

from hashlib import sha256
import json

from ..core.enums import AccountHealth, Platform
from ..core.schemas import PublishContent, PublishResult
from ..publishers.base import PublisherBase


FAKE_PUBLISHER_KIND = "offline_demo_fake"
FAKE_METRICS_KIND = "offline_demo_metrics"


def _require_empty_credential(credential: dict) -> None:
    """Make the zero-credential demo contract executable, not documentary."""
    if credential:
        raise ValueError("offline demo backends do not accept credentials")


class FakeMetricsBackend:
    """Return a stable engagement snapshot without contacting any service."""

    kind = FAKE_METRICS_KIND

    async def collect(
        self,
        post_id: str,
        post_url: str | None,
        credential: dict,
    ) -> dict:
        _require_empty_credential(credential)
        if not post_id.startswith("demo-"):
            raise ValueError("offline demo metrics require a demo post id")
        if post_url is not None and not post_url.startswith("demo://"):
            raise ValueError("offline demo metrics require a demo URL")
        return {
            "views": 128,
            "likes": 17,
            "comments": 4,
            "shares": 3,
            "raw": {
                "backend": self.kind,
                "demo": True,
                "deterministic": True,
                "offline": True,
                "synthetic": True,
            },
        }


class FakePublisher(PublisherBase):
    """A receipt-producing publisher with no external side effects."""

    platform = Platform.ZHIHU
    # Production kinds are an enum, while test/plugin kinds are intentionally
    # allowed to be strings by PublisherRegistry.kind_value().  Keeping this
    # demo-only kind outside PublisherKind prevents it looking production-ready.
    kind = FAKE_PUBLISHER_KIND
    supports_metrics = True

    def __init__(self, metrics_backend: FakeMetricsBackend | None = None) -> None:
        self.metrics_backend = metrics_backend or FakeMetricsBackend()

    async def login(self, account_id: int, credential: dict) -> bool:
        _require_empty_credential(credential)
        return account_id > 0

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        _require_empty_credential(credential)
        canonical = json.dumps(
            {
                "account_id": account_id,
                "body": content.body,
                "content_type": content.content_type.value,
                "tags": list(content.tags),
                "title": content.title,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        post_id = f"demo-{sha256(canonical.encode('utf-8')).hexdigest()[:12]}"
        return PublishResult(
            success=True,
            effect_applied=True,
            retryable=False,
            outcome_uncertain=False,
            platform_post_id=post_id,
            platform_url=f"demo://posts/{post_id}",
            raw_response={
                "backend": self.kind,
                "demo": True,
                "deterministic": True,
                "offline": True,
                "synthetic": True,
                "initial_metadata": {
                    "view_count": 1,
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                },
            },
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        _require_empty_credential(credential)
        return AccountHealth.HEALTHY if account_id > 0 else AccountHealth.UNKNOWN

    async def collect_metrics(
        self,
        post_id: str,
        post_url: str | None,
        credential: dict,
    ) -> dict:
        return await self.metrics_backend.collect(post_id, post_url, credential)
