from .base import PodcastProviderBase
from .listenhub import ListenHubProvider

__all__ = [
    "PodcastProviderBase",
    "ListenHubProvider",
    "build_default_podcast_provider",
]


def build_default_podcast_provider() -> PodcastProviderBase:
    """默认 AI 播客提供方 = ListenHub（云，零本地算力）。"""
    return ListenHubProvider()
