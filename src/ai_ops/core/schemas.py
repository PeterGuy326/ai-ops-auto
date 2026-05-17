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
    keywords: list[str] = Field(default_factory=list)
    persona: dict = Field(default_factory=dict)
    target_platforms: list[Platform] = Field(default_factory=list)
    notes: str = ""


class TopicOut(TopicIn):
    id: int
    heat_score: float
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
    credential_plain: dict = Field(
        default_factory=dict,
        description="明文凭证（cookie/token），落库时加密",
    )
    tags: list[str] = Field(default_factory=list, description="账号标签：人设/赛道/地域，存到 profile.tags")
    group: str = Field(default="", description="账号分组，用于跨账号分发策略")
    weight: int = Field(default=1, ge=1, le=100, description="分发权重，默认 1")


class AccountUpdate(BaseModel):
    """PATCH /accounts/{id} 用，所有字段可选。"""
    nickname: Optional[str] = None
    daily_quota: Optional[int] = None
    tags: Optional[list[str]] = None
    group: Optional[str] = None
    weight: Optional[int] = Field(default=None, ge=1, le=100)
    credential_plain: Optional[dict] = Field(default=None, description="如提供则覆盖加密凭证")


class AccountOut(BaseModel):
    id: int
    platform: Platform
    nickname: str
    profile: dict
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
