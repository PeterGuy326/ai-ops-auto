"""Public Publisher API with lazy registry exports.

Plugin packages must be able to import the SDK without constructing the process
global registry (which could recursively load the same plugin).  Registry names
therefore stay lazy while the side-effect-free base and SDK types are eager.
"""

from __future__ import annotations

from typing import Any

from ..core.enums import AccountHealth, Platform
from ..core.schemas import PublishContent, PublishResult
from .base import (
    AgentContractAssetRule,
    AgentContractRendererDescriptor,
    AgentContractRendererUnavailable,
    PublisherBase,
)
from .plugin_sdk import (
    PUBLISHER_PLUGIN_API_VERSION,
    PUBLISHER_PLUGIN_ENTRY_POINT_GROUP,
    PublisherPlugin,
    PublisherPluginCapability,
    PublisherPluginManifest,
)


def __getattr__(name: str) -> Any:
    if name == "PublisherRegistry":
        # Import only the requested symbol. While ``default_registry`` is being
        # constructed, an allowlisted plugin may itself import this public type;
        # asking the partially initialized module for ``default_registry`` too
        # would turn that valid import into a circular-import failure.
        from .registry import PublisherRegistry

        return PublisherRegistry
    if name == "default_registry":
        from .registry import default_registry

        return default_registry
    raise AttributeError(name)


__all__ = [
    "AgentContractAssetRule",
    "AgentContractRendererDescriptor",
    "AgentContractRendererUnavailable",
    "AccountHealth",
    "PUBLISHER_PLUGIN_API_VERSION",
    "PUBLISHER_PLUGIN_ENTRY_POINT_GROUP",
    "PublisherBase",
    "PublisherRegistry",
    "PublisherPlugin",
    "PublisherPluginCapability",
    "PublisherPluginManifest",
    "Platform",
    "PublishContent",
    "PublishResult",
    "default_registry",
]
