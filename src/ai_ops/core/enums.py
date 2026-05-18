from enum import Enum


class Platform(str, Enum):
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    ZHIHU = "zhihu"
    TOUTIAO = "toutiao"
    BILIBILI = "bilibili"
    KUAISHOU = "kuaishou"
    WECHAT_VIDEO = "wechat_video"
    WECHAT_MP = "wechat_mp"  # 微信公众号图文
    BAIJIAHAO = "baijiahao"
    SOHUHAO = "sohuhao"  # 搜狐号（门户系媒体）
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    GITHUB_PAGES = "github_pages"  # 自有博客（Hexo / Jekyll / Hugo / 纯静态）


class ContentType(str, Enum):
    IMAGE_TEXT = "image_text"
    VIDEO = "video"
    LONG_ARTICLE = "long_article"


class ArticleStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD = "dead"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD = "dead"


class AssetType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


class AssetSource(str, Enum):
    USER_UPLOAD = "user_upload"
    AI_GENERATED = "ai_generated"
    STOCK_LIBRARY = "stock_library"
    EXTERNAL_TOOL = "external_tool"


class AccountHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BANNED = "banned"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class PublisherKind(str, Enum):
    """发布器实现类型，决定底层调用哪个外部工具。"""
    SOCIAL_AUTO_UPLOAD = "social_auto_upload"
    XHS_TOOLKIT = "xhs_toolkit"
    XHS_AI_PUBLISHER = "xhs_ai_publisher"
    SHORT_VIDEO_AUTO = "short_video_auto"
    HEXO = "hexo"
    JEKYLL = "jekyll"
    HUGO = "hugo"


class VideoEngineKind(str, Enum):
    MONEY_PRINTER_TURBO = "money_printer_turbo"
    NARRATO_AI = "narrato_ai"
    FFMPEG_RAW = "ffmpeg_raw"
