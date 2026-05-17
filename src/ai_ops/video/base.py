from abc import ABC, abstractmethod

from ..core.enums import VideoEngineKind
from ..core.schemas import VideoArtifact, VideoBrief


class VideoEngineBase(ABC):
    """视频引擎统一接口。"""

    kind: VideoEngineKind

    @abstractmethod
    async def render(self, brief: VideoBrief) -> VideoArtifact:
        """根据 brief 生成视频。"""

    @abstractmethod
    async def health(self) -> bool:
        """引擎可用性检查。"""
