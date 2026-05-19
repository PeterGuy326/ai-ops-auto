"""视频剪辑器抽象 —— 与 VideoEngineBase 正交。

Engine：从 brief 造一个新视频（MoneyPrinterTurbo 等）。
Clipper：吃一个已存在的长视频，按 ASR/说话人/时间段切出 N 个短片段。

为什么独立抽象：
  - 接口语义不同：render(brief) vs clip(input_video, segments)
  - 生命周期不同：Clipper 需要先 transcribe 拿字幕，再按文本切
  - 实现技术栈不同：FunClip 走 FunASR/Paraformer，是音频流水线
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.enums import VideoClipperKind
from ..core.schemas import ClipRequest, ClipResult, TranscriptResult


class VideoClipperBase(ABC):
    """视频剪辑器统一接口。"""

    kind: VideoClipperKind

    @abstractmethod
    async def transcribe(self, input_video: str, output_dir: str, lang: str = "zh") -> TranscriptResult:
        """对长视频做 ASR，产出 SRT 字幕 + 结构化 cues。"""

    @abstractmethod
    async def clip(self, request: ClipRequest) -> ClipResult:
        """按 segments 切片，每段产出一个 mp4。"""

    @abstractmethod
    async def health(self) -> bool:
        """实现可用性检查（路径/二进制/进程是否齐备）。"""
