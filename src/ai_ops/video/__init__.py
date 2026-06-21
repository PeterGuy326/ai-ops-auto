from .base import VideoEngineBase
from .clipper import FunClipClipper
from .clipper_base import VideoClipperBase
from .happyhorse import HappyHorseEngine
from .kling import KlingEngine
from .money_printer import MoneyPrinterEngine

__all__ = [
    "VideoEngineBase",
    "MoneyPrinterEngine",
    "KlingEngine",
    "HappyHorseEngine",
    "VideoClipperBase",
    "FunClipClipper",
    "build_default_video_engine",
]


def build_default_video_engine() -> VideoEngineBase:
    """按配置选视频引擎（优先级：内网 HappyHorse > 可灵 > 本地 MPT）。

    - 配了 WUKONG_API_KEY → HappyHorse（阿里内网悟空平台，零本地算力、无封控，短剧主力）
    - 配了 KLING_ACCESS_KEY+SECRET → 可灵 Kling（公网备选）
    - 都没配 → 本地 MoneyPrinterTurbo（轻短剧/口播）
    """
    from ..config import settings

    if settings.wukong_api_key:
        return HappyHorseEngine()
    if settings.kling_access_key and settings.kling_secret_key:
        return KlingEngine()
    return MoneyPrinterEngine()
