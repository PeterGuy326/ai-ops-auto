from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .enums import (
    AccountHealth,
    ArticleStatus,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
)


class TopicIn(BaseModel):
    name: str
    category: str = Field(
        default="general",
        description="专题分类（常见值：general/tech/exam/sports/lifestyle）",
    )
    keywords: list[str] = Field(default_factory=list)
    persona: dict = Field(default_factory=dict)
    target_platforms: list[Platform] = Field(default_factory=list)
    notes: str = ""


class TopicUpdate(BaseModel):
    """PATCH /topics/{id}：所有字段可选。"""
    name: Optional[str] = None
    category: Optional[str] = None
    keywords: Optional[list[str]] = None
    persona: Optional[dict] = None
    target_platforms: Optional[list[Platform]] = None
    notes: Optional[str] = None


class TopicOut(TopicIn):
    id: int
    heat_score: float
    created_at: datetime


class TopicStats(BaseModel):
    """GET /topics 列表项：带账号/文章统计。"""
    id: int
    name: str
    category: str
    keywords: list[str] = Field(default_factory=list)
    target_platforms: list[Platform] = Field(default_factory=list)
    heat_score: float
    notes: str = ""
    account_count: int = 0
    article_count: int = 0
    created_at: datetime


class AssetRef(BaseModel):
    id: Optional[int] = None
    asset_type: AssetType
    local_path: str
    meta: dict = Field(default_factory=dict)


class ArticleIn(BaseModel):
    topic_id: int
    title: str
    body: str = ""
    content_type: ContentType
    target_platforms: list[Platform] = Field(default_factory=list)
    target_account_ids: list[int] = Field(default_factory=list)
    scheduled_at: Optional[datetime] = None
    extra: dict = Field(default_factory=dict)


class ArticleOut(ArticleIn):
    id: int
    status: ArticleStatus
    created_at: datetime
    updated_at: datetime
    assets: list[AssetRef] = Field(default_factory=list)


class AccountIn(BaseModel):
    platform: Platform
    nickname: str
    profile: dict = Field(default_factory=dict)
    daily_quota: int = 5
    topic_id: Optional[int] = Field(
        default=None,
        description="账号绑定的专题 id（None=未绑定，存量账号兼容）",
    )
    credential_plain: dict = Field(
        default_factory=dict,
        description="明文凭证（cookie/token），落库时加密",
    )
    tags: list[str] = Field(default_factory=list, description="账号标签：人设/赛道/地域，存到 profile.tags")
    group: str = Field(default="", description="账号分组，用于跨账号分发策略")
    weight: int = Field(default=1, ge=1, le=100, description="分发权重，默认 1")
    proxy: str = Field(
        default="",
        description="账号专属代理（一机一号一IP 是反风控核心）。格式：http://user:pass@host:port",
    )


class AccountUpdate(BaseModel):
    """PATCH /accounts/{id} 用，所有字段可选。"""
    nickname: Optional[str] = None
    daily_quota: Optional[int] = None
    topic_id: Optional[int] = Field(
        default=None,
        description="重新绑定专题；None=不变；-1=清空绑定",
    )
    tags: Optional[list[str]] = None
    group: Optional[str] = None
    weight: Optional[int] = Field(default=None, ge=1, le=100)
    credential_plain: Optional[dict] = Field(default=None, description="如提供则覆盖加密凭证")
    proxy: Optional[str] = Field(default=None, description="账号专属代理；None 表示不变，空串表示清空")


class AccountOut(BaseModel):
    id: int
    platform: Platform
    nickname: str
    profile: dict
    topic_id: Optional[int] = None
    health: AccountHealth
    risk_level: int
    daily_quota: int
    last_publish_at: Optional[datetime]
    last_health_check_at: Optional[datetime]
    created_at: datetime

    @property
    def tags(self) -> list[str]:
        return self.profile.get("tags", [])

    @property
    def group(self) -> str:
        return self.profile.get("group", "")

    @property
    def weight(self) -> int:
        return self.profile.get("weight", 1)

    @property
    def proxy(self) -> str:
        return self.profile.get("proxy", "")


class PublishContent(BaseModel):
    """喂给 PublisherBase.publish 的标准化内容。"""
    title: str
    body: str
    content_type: ContentType
    images: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


class PublishResult(BaseModel):
    success: bool
    platform_post_id: Optional[str] = None
    platform_url: Optional[str] = None
    error: Optional[str] = None
    raw_response: dict = Field(default_factory=dict)


class VideoBrief(BaseModel):
    """喂给 VideoEngineBase.render 的标准化任务。"""
    theme: str
    script: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    duration_seconds: int = 60
    voice: Optional[str] = None
    bgm: Optional[str] = None
    resolution: str = "1080x1920"
    extra: dict = Field(default_factory=dict)


class VideoArtifact(BaseModel):
    video_path: str
    cover_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    duration_seconds: float
    meta: dict = Field(default_factory=dict)


class TranscriptCue(BaseModel):
    """一条 SRT 字幕 cue（ASR 转写产物）。"""
    index: int
    start_ms: int
    end_ms: int
    text: str


class TranscriptResult(BaseModel):
    """clipper.transcribe() 的产物：字幕 + 原始文本。"""
    srt_path: str
    cues: list[TranscriptCue] = Field(default_factory=list)
    full_text: str = ""
    meta: dict = Field(default_factory=dict)


class ClipSegment(BaseModel):
    """单次剪辑请求段——按文本匹配 OR 按时间段，二选一。
    dest_text 优先；同时给则以 dest_text 为准（FunClip Stage 2 语义）。
    """
    dest_text: Optional[str] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    # 前后扩展（毫秒），对应 FunClip --start_ost / --end_ost
    start_ost_ms: int = 0
    end_ost_ms: int = 0


class ClipRequest(BaseModel):
    """喂给 clipper.clip() 的统一请求。"""
    input_video: str
    segments: list[ClipSegment] = Field(default_factory=list)
    output_dir: str = "./data/clips"
    lang: str = "zh"
    hotwords: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


class ClipArtifact(BaseModel):
    """单条切片产物。"""
    video_path: str
    dest_text: Optional[str] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    meta: dict = Field(default_factory=dict)


class ClipResult(BaseModel):
    """clipper.clip() 的产物：N 个切片 + transcript（如有）。"""
    clips: list[ClipArtifact] = Field(default_factory=list)
    transcript: Optional[TranscriptResult] = None
    meta: dict = Field(default_factory=dict)


class ClipPublishRequest(BaseModel):
    """clip→publish 流水线入参：切片任务 + 目标平台 + 文案。"""
    clip_request: ClipRequest
    platforms: list[Platform]
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)


class PublishPlanItem(BaseModel):
    """一条发布计划项：目标平台 + 待发标准化内容 + 溯源切片路径。"""
    platform: Platform
    content: PublishContent
    source_clip_path: str


class ClipPublishPlan(BaseModel):
    """clip→publish 流水线产物——dry-run 发布计划。

    只编排到「内容就绪」为止，不触发真发布：真发布走 PublishJob + worker，
    以免绕过 rate limit / 风控间隔 / metrics 闭环。
    """
    items: list[PublishPlanItem] = Field(default_factory=list)
    clip_count: int = 0
    meta: dict = Field(default_factory=dict)


# ============ AI 播客（ListenHub 这类云播客）============
class PodcastSpeaker(BaseModel):
    """播客说话人——speaker_id 由 provider 的音色列表给出。"""
    speaker_id: str
    name: str = ""


class PodcastBrief(BaseModel):
    """喂给 PodcastProviderBase.generate 的标准化任务。"""
    query: str                                   # 主题 / prompt
    speakers: list[PodcastSpeaker] = Field(default_factory=list)
    language: str = "zh"
    mode: str = "deep"                           # quick / deep / debate
    # 参考素材（URL 或文本），对应 ListenHub sources
    source_urls: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


class PodcastArtifact(BaseModel):
    """播客生成产物：音频 + 文稿。"""
    episode_id: str
    title: str = ""
    audio_url: Optional[str] = None              # 远端 MP3（可能有时效）
    audio_stream_url: Optional[str] = None       # m3u8 流
    audio_path: Optional[str] = None             # 下载到本地后的路径
    scripts: list[dict] = Field(default_factory=list)  # [{speakerId,speakerName,content}]
    credits: Optional[int] = None
    duration_seconds: Optional[float] = None
    meta: dict = Field(default_factory=dict)


class JobOut(BaseModel):
    id: int
    article_id: int
    account_id: int
    platform: Platform
    status: JobStatus
    attempts: int
    platform_post_id: Optional[str]
    platform_url: Optional[str]
    error: Optional[str]
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
