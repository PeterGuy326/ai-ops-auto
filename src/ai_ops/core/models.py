from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    # Asset order affects carousels/covers and is part of the approval digest.
    # Primary-key order preserves insertion order across session reloads.
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="article",
        order_by="Asset.id",
    )
    jobs: Mapped[list["PublishJob"]] = relationship(back_populates="article")
    publication_plans: Mapped[list["PublicationPlan"]] = relationship(back_populates="article")


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "(storage_kind IS NULL AND content_sha256 IS NULL AND size_bytes IS NULL) OR "
            "(storage_kind IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND size_bytes IS NOT NULL AND storage_kind = 'agent_vault_v1' "
            "AND length(content_sha256) = 64 AND size_bytes >= 0)",
            name="ck_assets_vault_metadata_complete",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[Optional[int]] = mapped_column(ForeignKey("articles.id"), nullable=True)
    asset_type: Mapped[AssetType] = mapped_column(String(32))
    source: Mapped[AssetSource] = mapped_column(String(32))
    local_path: Mapped[str] = mapped_column(String(512))
    # Contract-v1 assets are copied into the controlled content-addressed vault.
    # Legacy assets keep these fields NULL and cannot be used by the exact
    # approval path until they are ingested into that vault.
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    storage_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
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


class PublicationPlan(Base):
    """Immutable publication intent that an approval decision can bind to.

    ``content_digest`` identifies the staged content snapshot, while
    ``plan_digest`` also covers targets and timing.  Service code must recompute
    and compare both before scheduling; the database keeps the durable evidence.
    """

    __tablename__ = "publication_plans"
    __table_args__ = (
        CheckConstraint(
            "state IN ('draft', 'approval_pending', 'approved', 'rejected', "
            "'scheduled', 'cancelled', 'expired')",
            name="ck_publication_plans_state",
        ),
        CheckConstraint(
            "length(content_digest) = 64",
            name="ck_publication_plans_content_digest_sha256",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_publication_plans_plan_digest_sha256",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Immutable, credential-free payload reviewed by the human principal and
    # consumed by contract jobs.  Workers never rebuild a v1 payload from the
    # mutable Article row after approval.
    content_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    targets: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Immediate execution is represented by an explicit current timestamp.  A
    # nullable value would let approval bind every future execution time.
    planned_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    article: Mapped["Article"] = relationship(back_populates="publication_plans")
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(back_populates="plan")
    jobs: Mapped[list["PublishJob"]] = relationship(back_populates="plan")


class ApprovalRequest(Base):
    """An immutable decision trail for one exact publication plan digest."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')",
            name="ck_approval_requests_status",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_approval_requests_plan_digest_sha256",
        ),
        CheckConstraint(
            "((decided_by IS NULL AND decided_by_type IS NULL) OR "
            "(decided_by IS NOT NULL AND decided_by_type IS NOT NULL))",
            name="ck_approval_requests_decider_identity",
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'rejected') OR "
            "(decided_by IS NOT NULL AND decided_by_type IS NOT NULL "
            "AND decided_at IS NOT NULL)",
            name="ck_approval_requests_decision_complete",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("publication_plans.id"),
        nullable=False,
        index=True,
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by_type: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    decided_by_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    plan: Mapped["PublicationPlan"] = relationship(back_populates="approval_requests")


class AgentOperation(Base):
    """Replay ledger for an Agent mutation's idempotency key.

    A row with null response fields is a claimed operation. Database-only
    mutations fill it in the same transaction; bounded external reads use the
    explicit expiring lease and a uniquely linked normalized result.
    """

    __tablename__ = "agent_operations"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "operation",
            "idempotency_key",
            name="uq_agent_operations_principal_operation_key",
        ),
        CheckConstraint(
            "length(request_digest) = 64",
            name="ck_agent_operations_request_digest_sha256",
        ),
        CheckConstraint(
            "response_status_code IS NULL OR "
            "(response_status_code >= 100 AND response_status_code <= 599)",
            name="ck_agent_operations_response_status_code",
        ),
        CheckConstraint(
            "(response_status_code IS NULL AND response_json IS NULL) OR "
            "(response_status_code IS NOT NULL AND response_json IS NOT NULL)",
            name="ck_agent_operations_response_complete",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND length(lease_token) = 64)",
            name="ck_agent_operations_lease_complete",
        ),
        CheckConstraint(
            "response_json IS NULL OR (lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_agent_operations_completed_not_leased",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_json: Mapped[Optional[dict]] = mapped_column(
        JSON(none_as_null=True),
        nullable=True,
    )
    # External reads commit an unfinished operation before leaving the DB.
    # Ownership and expiry let a retry reclaim crashes without allowing an old
    # worker to overwrite the eventual replay response.
    lease_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_now,
        onupdate=_now,
        nullable=False,
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "account_id",
            name="uq_publish_jobs_plan_account",
        ),
        CheckConstraint(
            "plan_id IS NULL OR approved_planned_for IS NOT NULL",
            name="ck_publish_jobs_contract_planned_for",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    # Nullable keeps every legacy/manual/backfill job valid.  Contract-v1 jobs
    # set it and gain one-job-per-target idempotency at the database boundary.
    plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("publication_plans.id"),
        nullable=True,
    )
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
    # Immutable not-before timestamp copied from the approved PublicationPlan.
    # Contract verification reads this field; ``scheduled_at`` remains the
    # mutable next-attempt timestamp shared with legacy jobs and may move after
    # a retry or policy deferral.
    approved_planned_for: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )
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
    plan: Mapped[Optional["PublicationPlan"]] = relationship(back_populates="jobs")


class Metrics(Base):
    __tablename__ = "metrics"
    __table_args__ = (
        UniqueConstraint(
            "agent_operation_id",
            name="uq_metrics_agent_operation_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id"))
    # Manual Agent collections bind their normalized snapshot to the durable
    # idempotency ledger.  A retry after response-finalization failure reuses
    # this row rather than calling the external collector again.
    agent_operation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_operations.id"),
        nullable=True,
    )
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
