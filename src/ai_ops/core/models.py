from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Integer, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AccountHealth,
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
)


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # 专题分类（用字符串而非 enum，方便后续扩展；常见值：general/tech/exam/sports/lifestyle）
    category: Mapped[str] = mapped_column(String(32), default="general", server_default="general")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    persona: Mapped[dict] = mapped_column(JSON, default=dict)
    target_platforms: Mapped[list] = mapped_column(JSON, default=list)
    heat_score: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    articles: Mapped[list["Article"]] = relationship(back_populates="topic")
    accounts: Mapped[list["Account"]] = relationship(back_populates="topic")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"))
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[ContentType] = mapped_column(String(32))
    status: Mapped[ArticleStatus] = mapped_column(String(32), default=ArticleStatus.DRAFT)
    target_platforms: Mapped[list] = mapped_column(JSON, default=list)
    target_account_ids: Mapped[list] = mapped_column(JSON, default=list)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    topic: Mapped["Topic"] = relationship(back_populates="articles")
    assets: Mapped[list["Asset"]] = relationship(back_populates="article")
    jobs: Mapped[list["PublishJob"]] = relationship(back_populates="article")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articles.id"), nullable=True)
    asset_type: Mapped[AssetType] = mapped_column(String(32))
    source: Mapped[AssetSource] = mapped_column(String(32))
    local_path: Mapped[str] = mapped_column(String(512))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    article: Mapped[Optional["Article"]] = relationship(back_populates="assets")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(String(32))
    nickname: Mapped[str] = mapped_column(String(128))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    # 账号绑定的专题（nullable=True 兼容存量；profile.group/tags 仍可用作软分组的二级维度）
    topic_id: Mapped[Optional[int]] = mapped_column(ForeignKey("topics.id"), nullable=True)
    encrypted_credential: Mapped[bytes] = mapped_column(default=b"")
    health: Mapped[AccountHealth] = mapped_column(String(32), default=AccountHealth.UNKNOWN)
    risk_level: Mapped[int] = mapped_column(Integer, default=0)
    daily_quota: Mapped[int] = mapped_column(Integer, default=5)
    last_publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_health_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    topic: Mapped[Optional["Topic"]] = relationship(back_populates="accounts")


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    platform: Mapped[Platform] = mapped_column(String(32))
    status: Mapped[JobStatus] = mapped_column(String(32), default=JobStatus.PENDING)
    publisher_kind: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    platform_post_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    platform_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    article: Mapped["Article"] = relationship(back_populates="jobs")


class Metrics(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id"))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
