"""jobhunt 专题 ORM 模型 —— 4 张新表。

复用 core.models.Base（同一个 metadata），这样 init_db / alembic 都能扫到。
注意：本模块必须被 import 一次才会注册到 Base.metadata——
core/db.py 与 alembic/env.py 已显式 import（见各自文件注释）。

外键策略：
  - resume / job / match 之间用真 FK（同库强关联，删除少见）
  - Application.account_id 暂用裸 Integer（不 FK 到 accounts）：
    accounts.platform 是 core.enums.Platform（内容平台），与招聘平台语义不同；
    P2 真投递接入账号体系时再决定是否复用 accounts 表或新建 job_accounts。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.enums import AccountHealth, AssetType  # noqa: F401  (DOCUMENT: 原始简历存为 Asset)
from ..core.models import Base, _now
from .enums import ApplicationStatus, JobBoard


class ResumeProfile(Base):
    """一份简历的结构化结果（决策：一份通用简历 + 千岗千面的打招呼语）。

    raw_asset_id 指向 assets 表里那条 DOCUMENT 资产（原始 PDF/Word）。
    structured 是 LLM 抽出来的完整结构（schema 见 resume_parser.RESUME_SCHEMA_HINT），
    顶层冗余几个高频字段（target_titles / expected_cities / skills）方便 matcher 和岗位搜索直接取。
    """
    __tablename__ = "resume_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))  # 给这份简历起的名（如「后端-2026版」）
    raw_asset_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assets.id"), nullable=True
    )
    raw_text: Mapped[str] = mapped_column(Text, default="")  # 从文件抽出的纯文本
    structured: Mapped[dict] = mapped_column(JSON, default=dict)  # LLM 结构化全量

    # —— 顶层冗余字段（matcher / 岗位搜索高频取用，避免每次解 JSON）——
    summary: Mapped[str] = mapped_column(Text, default="")
    years_of_experience: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_titles: Mapped[list] = mapped_column(JSON, default=list)
    expected_cities: Mapped[list] = mapped_column(JSON, default=list)
    expected_salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expected_salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    search_keywords: Mapped[list] = mapped_column(JSON, default=list)  # 岗位搜索用关键词

    is_active: Mapped[bool] = mapped_column(default=True)  # 当前主用简历
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    matches: Mapped[list["JobMatch"]] = relationship(back_populates="resume")
    applications: Mapped[list["Application"]] = relationship(back_populates="resume")


class JobPosting(Base):
    """从招聘平台爬来的单个岗位（JD）。P1 才会真正写入，P0 只建表。

    (board, external_id) 唯一：同平台同岗位只存一份，重复采集走 upsert。
    external_id 平台没给时回退用 url 的稳定 hash，仍保证去重。
    """
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("board", "external_id", name="uq_job_board_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    board: Mapped[JobBoard] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))  # 平台侧岗位 id（或 url hash）
    url: Mapped[str] = mapped_column(String(512), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    company: Mapped[str] = mapped_column(String(256), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    salary_text: Mapped[str] = mapped_column(String(128), default="")  # 原始「25-40K·14薪」
    jd_text: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # 平台原始结构留档
    crawled_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    matches: Mapped[list["JobMatch"]] = relationship(back_populates="job")
    applications: Mapped[list["Application"]] = relationship(back_populates="job")


class JobMatch(Base):
    """简历 × 岗位 的匹配打分结果（LLM 给分 + 命中点/差距/理由）。P1 落库。"""
    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("resume_id", "job_id", name="uq_match_resume_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_profiles.id"))
    job_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"))
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    verdict: Mapped[str] = mapped_column(String(16), default="weak")  # MatchVerdict 值
    matched_points: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")  # 打分用的 LLM
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    resume: Mapped["ResumeProfile"] = relationship(back_populates="matches")
    job: Mapped["JobPosting"] = relationship(back_populates="matches")


class JobAccount(Base):
    """招聘平台账号 —— 与内容平台 accounts 表刻意分离（用户决策）。

    单开一张表的理由：core.models.Account.platform 是内容平台 Platform 枚举，
    dispatcher / health_monitor 都按它工作；把 boss 塞进去会互相污染。
    凭证复用 accounts/store.py 的 CredentialStore（Fernet 加密），不裸存 cookie。

    encrypted_credential 解密后结构：{"cookies": [{name,value,domain,path}, ...]}
    """
    __tablename__ = "job_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    board: Mapped[JobBoard] = mapped_column(String(32))
    nickname: Mapped[str] = mapped_column(String(128))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # tags / notes / proxy 等
    encrypted_credential: Mapped[bytes] = mapped_column(default=b"")
    health: Mapped[AccountHealth] = mapped_column(String(32), default=AccountHealth.UNKNOWN)
    # Boss 反爬严，日投递上限给得保守（养号后可调高）
    daily_quota: Mapped[int] = mapped_column(Integer, default=30)
    last_apply_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Application(Base):
    """投递记录 —— 新流水线里 core.models.PublishJob 的对应物。

    一条 = 用某份简历、（P2 起）经某账号，向某岗位投递一次。
    greeting 是这次投递用的个性化打招呼语 / 求职理由。
    """
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("resume_id", "job_id", name="uq_app_resume_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resume_profiles.id"))
    job_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"))
    match_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_matches.id"), nullable=True)
    board: Mapped[JobBoard] = mapped_column(String(32))
    # P2 真投递时绑定的 job_accounts.id（保持裸 Integer 不加 DB FK：
    # 避免 batch-alter 已建的 applications 表；引用完整性由 manager 层保证）
    account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    status: Mapped[ApplicationStatus] = mapped_column(
        String(32), default=ApplicationStatus.DRAFT
    )
    greeting: Mapped[str] = mapped_column(Text, default="")  # 个性化打招呼语
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    hr_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # HR 首条回复
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    resume: Mapped["ResumeProfile"] = relationship(back_populates="applications")
    job: Mapped["JobPosting"] = relationship(back_populates="applications")
