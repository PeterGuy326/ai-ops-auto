"""AI 播客生成提供方抽象（云 API，本地零算力）。

与 VideoEngineBase 同构：吃一个 PodcastBrief，产出 PodcastArtifact（音频+文稿）。
实现可换（ListenHub / AutoContentAPI / 自组装 LLM+TTS），编排层不动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.enums import PodcastProviderKind
from ..core.schemas import PodcastArtifact, PodcastBrief


class PodcastProviderBase(ABC):
    """AI 播客提供方统一接口。"""

    kind: PodcastProviderKind

    @abstractmethod
    async def generate(self, brief: PodcastBrief) -> PodcastArtifact:
        """根据 brief 生成播客成品（异步任务内部轮询到完成）。"""

    @abstractmethod
    async def health(self) -> bool:
        """可用性检查（key 是否配齐）。"""
