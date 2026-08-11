from abc import ABC, abstractmethod

from ..core.enums import AccountHealth, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult


class PublisherBase(ABC):
    """所有平台发布器的统一接口。

    实现类是外部工具的薄壳 wrapper——不要在这里写发布的核心逻辑（反爬/签名/上传），
    那些归属于集成的开源工具（social-auto-upload / xhs-toolkit / ...）。
    """

    platform: Platform
    kind: PublisherKind
    # Metrics routing is opt-in. A synthetic zero from an unsupported adapter
    # is indistinguishable from a real zero-view post and can poison health.
    supports_metrics: bool = False

    @abstractmethod
    async def login(self, account_id: int, credential: dict) -> bool:
        """触发外部工具完成登录（通常落 cookie/token）。"""

    @abstractmethod
    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """单次发布。content 已是平台无关的标准化结构，由 wrapper 翻译成工具需要的格式。"""

    @abstractmethod
    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """登录态/风控感知。"""

    async def collect_metrics(
        self,
        post_id: str,
        post_url: str | None,
        credential: dict,
    ) -> dict:
        """采集已发布内容的互动数据。

        Adapters must override this method and set ``supports_metrics=True``.
        Unsupported collection is a routing decision, never a synthetic zero.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support metrics collection")
