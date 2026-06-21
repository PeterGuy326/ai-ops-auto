"""素材分发中台 —— 「入库待审 → 人工审核 → 按账号扇出分发 → 留痕」。

底层逻辑（对齐 Article 状态机，刻意为之）：
  DRAFT(待审) → READY(审过) → SCHEDULED(已分发) → PUBLISHING → PUBLISHED

  - **不直发**：所有生成产物（文章/视频/博客/播客）先 `stage_to_library` 落成
    Article(status=DRAFT)，进素材库等人工审核（转 READY）。
  - **审后分发**：`distribute` 只接受 READY 的素材，按目标账号扇出成 N 条
    PublishJob（每条 = 一个账号在一个平台的分发记录），随后素材转 SCHEDULED。
  - **按账号留痕**：PublishJob 挂 account_id + platform + status + platform_url，
    `list_account_jobs` 即可按个人账号查全部分发记录。真发布仍由 scheduler.worker
    消费 PublishJob（含 rate-limit / 风控间隔 / metrics 闭环），本模块不绕过。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import (
    ArticleStatus,
    AssetSource,
    AssetType,
    ContentType,
    JobStatus,
    Platform,
)
from ..core.models import Account, Article, Asset, PublishJob

# content_type → 该类素材的「主资产」类型（用于把文件挂成 Asset）
_VIDEO_TYPES = {ContentType.VIDEO}
_AUDIO_TYPES = {ContentType.AUDIO}


def stage_to_library(
    session: Session,
    *,
    topic_id: int,
    title: str,
    content_type: ContentType,
    body: str = "",
    video_paths: Sequence[str] = (),
    image_paths: Sequence[str] = (),
    audio_paths: Sequence[str] = (),
    target_platforms: Sequence[Platform] = (),
    source: AssetSource = AssetSource.AI_GENERATED,
    extra: Optional[dict] = None,
) -> Article:
    """把一份生成产物落进素材库，状态 = DRAFT(待审)。

    文章/视频/博客/播客通用：正文进 body，文件进 Asset。**不分发**——等审核。
    """
    art = Article(
        topic_id=topic_id,
        title=title,
        body=body,
        content_type=content_type,
        status=ArticleStatus.DRAFT,
        target_platforms=[
            p.value if isinstance(p, Platform) else p for p in target_platforms
        ],
        extra=extra or {},
    )
    session.add(art)
    session.flush()

    for path in video_paths:
        session.add(Asset(article_id=art.id, asset_type=AssetType.VIDEO, source=source, local_path=path, meta={}))
    for path in image_paths:
        session.add(Asset(article_id=art.id, asset_type=AssetType.IMAGE, source=source, local_path=path, meta={}))
    for path in audio_paths:
        session.add(Asset(article_id=art.id, asset_type=AssetType.AUDIO, source=source, local_path=path, meta={}))
    session.flush()
    return art


def approve(session: Session, article_id: int) -> Article:
    """人工审核通过：DRAFT → READY。"""
    art = session.get(Article, article_id)
    if art is None:
        raise ValueError(f"素材 {article_id} 不存在")
    if art.status != ArticleStatus.DRAFT:
        raise ValueError(f"只有 DRAFT(待审) 可审核通过，当前 {art.status}")
    art.status = ArticleStatus.READY
    session.flush()
    return art


def distribute(
    session: Session,
    article_id: int,
    account_ids: Optional[Sequence[int]] = None,
    *,
    scheduled_at: Optional[datetime] = None,
    require_ready: bool = True,
) -> list[PublishJob]:
    """把审过的素材按账号扇出成分发记录（PublishJob）。

    - 审核闸：require_ready=True 时，仅 READY 素材可分发（DRAFT 直接拒，防止误直发）。
    - 目标账号：显式 account_ids 优先；否则按素材 target_platforms 取该平台所有账号。
    - 每个账号建一条 PublishJob（platform 取自账号），即「按个人账号留痕」。
    - 分发后素材转 SCHEDULED；真发布由 worker 消费（不在此直发，保留风控闭环）。
    """
    art = session.get(Article, article_id)
    if art is None:
        raise ValueError(f"素材 {article_id} 不存在")
    if require_ready and art.status != ArticleStatus.READY:
        raise ValueError(
            f"素材未审核通过（需 READY，当前 {art.status}），不能分发。请先 approve。"
        )

    accounts = _resolve_accounts(session, art, account_ids)
    if not accounts:
        raise ValueError("没有可分发的目标账号（检查 target_platforms / account_ids）")

    jobs: list[PublishJob] = []
    for acc in accounts:
        job = PublishJob(
            article_id=art.id,
            account_id=acc.id,
            platform=acc.platform,
            status=JobStatus.PENDING,
            scheduled_at=scheduled_at,
        )
        session.add(job)
        jobs.append(job)
    session.flush()

    if art.status == ArticleStatus.READY:
        art.status = ArticleStatus.SCHEDULED
        session.flush()
    return jobs


def list_account_jobs(
    session: Session, account_id: int, *, limit: int = 100
) -> list[PublishJob]:
    """按个人账号查全部分发记录（最新优先）——留痕查询。"""
    q = (
        select(PublishJob)
        .where(PublishJob.account_id == account_id)
        .order_by(PublishJob.created_at.desc())
        .limit(limit)
    )
    return list(session.execute(q).scalars().all())


def _resolve_accounts(
    session: Session, art: Article, account_ids: Optional[Sequence[int]]
) -> list[Account]:
    if account_ids:
        rows = session.execute(
            select(Account).where(Account.id.in_(list(account_ids)))
        ).scalars().all()
        return list(rows)
    # 未指定账号：按素材 target_platforms 取这些平台下所有账号
    platforms = [Platform(p) if not isinstance(p, Platform) else p for p in (art.target_platforms or [])]
    if not platforms:
        return []
    rows = session.execute(
        select(Account).where(Account.platform.in_([p.value for p in platforms]))
    ).scalars().all()
    return list(rows)
