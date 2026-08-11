from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ..core.enums import AccountHealth, AssetType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult


class AgentContractRendererUnavailable(ValueError):
    """Raised when a Publisher cannot safely project an approved payload."""


@dataclass(frozen=True, slots=True)
class AgentContractAssetRule:
    """One ordered asset constraint exposed to the Agent planning boundary."""

    asset_type: AssetType
    min_count: int = 0
    max_count: int | None = None

    def __post_init__(self) -> None:
        if self.min_count < 0:
            raise ValueError("min_count must be non-negative")
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count must be greater than or equal to min_count")

    def digest_material(self) -> dict[str, object]:
        """Return the stable JSON projection covered by a renderer digest."""

        return {
            "asset_type": self.asset_type.value,
            "min_count": self.min_count,
            "max_count": self.max_count,
        }


@dataclass(frozen=True, slots=True)
class AgentContractRendererDescriptor:
    """Static, credential-free identity and input boundary for one renderer.

    A missing descriptor is deliberately the default.  Agent planning can use
    :meth:`digest_material` together with the projected payload, while comparing
    ``asset_rules`` against the complete approved asset manifest before any
    Publisher is selected for execution.
    """

    renderer_id: str
    contract_version: str
    adapter_version: str
    platform: Platform
    # Core adapters normally use PublisherKind. Third-party plugins may use a
    # namespaced string because the persisted job column is intentionally a
    # bounded string rather than a database enum.
    publisher_kind: PublisherKind | str
    accepted_extra_keys: tuple[str, ...] = ()
    accepts_tags: bool = False
    requires_external_account_id: bool = False
    asset_rules: tuple[AgentContractAssetRule, ...] = ()

    def __post_init__(self) -> None:
        if not self.renderer_id or not self.contract_version or not self.adapter_version:
            raise ValueError("renderer identity and versions must be non-empty")
        if tuple(sorted(set(self.accepted_extra_keys))) != self.accepted_extra_keys:
            raise ValueError("accepted_extra_keys must be unique and sorted")
        asset_types = [rule.asset_type for rule in self.asset_rules]
        if len(set(asset_types)) != len(asset_types):
            raise ValueError("asset_rules must not repeat an asset type")

    def digest_material(self) -> dict[str, object]:
        """Return deterministic JSON data that identifies this renderer contract."""

        return {
            "renderer_id": self.renderer_id,
            "contract_version": self.contract_version,
            "adapter_version": self.adapter_version,
            "platform": self.platform.value,
            "publisher_kind": (
                self.publisher_kind.value
                if isinstance(self.publisher_kind, PublisherKind)
                else str(self.publisher_kind)
            ),
            "accepted_extra_keys": list(self.accepted_extra_keys),
            "accepts_tags": self.accepts_tags,
            "requires_external_account_id": self.requires_external_account_id,
            "asset_rules": [rule.digest_material() for rule in self.asset_rules],
        }


class PublisherBase(ABC):
    """所有平台发布器的统一接口。

    实现类是外部工具的薄壳 wrapper——不要在这里写发布的核心逻辑（反爬/签名/上传），
    那些归属于集成的开源工具（social-auto-upload / xhs-toolkit / ...）。
    """

    platform: Platform
    kind: PublisherKind | str
    # Metrics routing is opt-in. A synthetic zero from an unsupported adapter
    # is indistinguishable from a real zero-view post and can poison health.
    supports_metrics: bool = False
    # Agent execution is a stricter boundary than legacy publication.  Adapters
    # opt in only after providing a pure, path-free payload projection whose
    # implementation is also reused at the external write boundary.
    agent_contract_renderer_descriptor: ClassVar[AgentContractRendererDescriptor | None] = None

    @property
    def supports_agent_contract_renderer(self) -> bool:
        return self.agent_contract_renderer_descriptor is not None

    def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
        """Project the final adapter payload without credentials or host paths.

        The base implementation is intentionally fail-closed.  A subclass must
        both publish a static descriptor and override this pure projection.
        """

        del content
        raise AgentContractRendererUnavailable(
            f"{type(self).__name__} does not support Agent contract rendering"
        )

    def agent_contract_digest_material(self, content: PublishContent) -> dict[str, object]:
        """Return descriptor plus payload data suitable for canonical hashing."""

        descriptor = self.agent_contract_renderer_descriptor
        if descriptor is None:
            raise AgentContractRendererUnavailable(
                f"{type(self).__name__} does not support Agent contract rendering"
            )
        return {
            "renderer": descriptor.digest_material(),
            "payload": self.render_agent_contract_payload(content),
        }

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
