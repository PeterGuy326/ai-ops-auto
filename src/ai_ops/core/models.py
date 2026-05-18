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
    # 重发覆盖追踪（publishing-sop §五 / §九 #7）：
    # 当 worker / 运营创建新 PublishJob 替代旧（失败/内容错）job 时，
    # 调用 _mark_job_superseded(s, old.id, new.id) 把旧 job 的此字段指向新 job——
    # 后台 UI / 周报 / 数据分析据此追踪"哪条失败 job 后来被谁覆盖了"，运营复盘有据。
    # nullable=True：默认 None 表示"未被覆盖"（成功路径 + 当前在跑的路径都属此态）。
    # self-FK：FK 目标即 publish_jobs.id（本表自引用）。
    superseded_by_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("publish_jobs.id"), nullable=True
    )

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
    # 采集来源标签（Round 6, TD-Z3-followup-2 / TD-P0-debt2）：
    #   - "initial"  = worker._persist_initial_metrics 落第一份发布快照（≈ finished_at）
    #   - "scheduled" = scheduler/metrics.collect_one 飞轮采集（1h/24h/7d 等档位）
    #   - "manual"   = api/main.py /jobs/{id}/collect 端点手动触发
    #   - 预留：external（第三方数据回填）/ backfill 等
    # 设计目的：让 24h 健康度评估触发判定不再"二阶推导"（按计数 / 时间窗反推），
    # 直接按 source='scheduled' 计数——任何后续给 Metrics 加写入入口都不污染触发判定。
    # 双层默认：
    #   - default="scheduled"     ORM 写入侧兜底（业务 / 测试 Metrics(...) 不传 source 时）
    #   - server_default="scheduled" DB ALTER ADD 时给老行兜底（避免 NOT NULL 升级失败）
    # 生产 ALTER 瞬间的语义不一致（老 initial 行被一刀切标 scheduled）由
    # scheduler/metrics.py 的三段优先级（interval_index → source-based → cutoff 兜底）兜住，
    # 详见 scheduler/metrics.collect_one 触发判定块。
    source: Mapped[str] = mapped_column(
        String(16),
        default="scheduled",
        server_default="scheduled",
        nullable=False,
    )
