"""内容（主题 + 文章 + 物料）管理。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.enums import ArticleStatus
from ..core.models import Account, Article, Asset, Topic
from ..core.schemas import (
    ArticleIn,
    ArticleOut,
    AssetRef,
    TopicIn,
    TopicOut,
    TopicStats,
    TopicUpdate,
)


def create_topic(session: Session, data: TopicIn) -> TopicOut:
    t = Topic(**data.model_dump())
    session.add(t)
    session.flush()
    return TopicOut(
        id=t.id,
        heat_score=t.heat_score,
        created_at=t.created_at,
        **data.model_dump(),
    )


def update_topic(session: Session, topic_id: int, patch: TopicUpdate) -> TopicOut:
    t = session.get(Topic, topic_id)
    if t is None:
        raise ValueError(f"topic {topic_id} not found")
    data = patch.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(t, k, v)
    session.flush()
    return TopicOut(
        id=t.id,
        name=t.name,
        category=t.category,
        keywords=t.keywords,
        persona=t.persona,
        target_platforms=t.target_platforms,
        notes=t.notes,
        heat_score=t.heat_score,
        created_at=t.created_at,
    )


def list_topics(session: Session) -> list[TopicOut]:
    return [
        TopicOut(
            id=t.id,
            name=t.name,
            category=t.category,
            keywords=t.keywords,
            persona=t.persona,
            target_platforms=t.target_platforms,
            notes=t.notes,
            heat_score=t.heat_score,
            created_at=t.created_at,
        )
        for t in session.query(Topic).all()
    ]


def list_topic_stats(session: Session) -> list[TopicStats]:
    """带账号 / 文章统计的 topic 列表（GET /topics 用）。

    用两次 group-by 聚合而非每行 N+1 查询，列表页 O(1) 次 IO。
    """
    acct_counts = dict(
        session.execute(
            select(Account.topic_id, func.count(Account.id))
            .where(Account.topic_id.isnot(None))
            .group_by(Account.topic_id)
        ).all()
    )
    art_counts = dict(
        session.execute(
            select(Article.topic_id, func.count(Article.id))
            .group_by(Article.topic_id)
        ).all()
    )
    return [
        TopicStats(
            id=t.id,
            name=t.name,
            category=t.category,
            keywords=t.keywords or [],
            target_platforms=t.target_platforms or [],
            heat_score=t.heat_score,
            notes=t.notes,
            account_count=int(acct_counts.get(t.id, 0)),
            article_count=int(art_counts.get(t.id, 0)),
            created_at=t.created_at,
        )
        for t in session.query(Topic).order_by(Topic.id.asc()).all()
    ]


def list_articles(
    session: Session, limit: int = 100, topic_id: int | None = None
) -> list[ArticleOut]:
    q = session.query(Article)
    if topic_id is not None:
        q = q.filter(Article.topic_id == topic_id)
    return [
        _to_article_out(a)
        for a in q.order_by(Article.id.desc()).limit(limit).all()
    ]


def create_article(session: Session, data: ArticleIn) -> ArticleOut:
    a = Article(status=ArticleStatus.DRAFT, **data.model_dump())
    session.add(a)
    session.flush()
    return _to_article_out(a)


def transition_status(session: Session, article_id: int, target: ArticleStatus) -> ArticleOut:
    a = session.get(Article, article_id)
    if a is None:
        raise ValueError(f"article {article_id} not found")
    if not _can_transition(a.status, target):
        raise ValueError(f"非法状态转换 {a.status} → {target}")
    a.status = target
    session.flush()
    return _to_article_out(a)


def attach_asset(session: Session, article_id: int, asset: AssetRef) -> AssetRef:
    obj = Asset(
        article_id=article_id,
        asset_type=asset.asset_type,
        source=asset.meta.get("source", "user_upload"),
        local_path=asset.local_path,
        meta=asset.meta,
    )
    session.add(obj)
    session.flush()
    return AssetRef(id=obj.id, asset_type=obj.asset_type, local_path=obj.local_path, meta=obj.meta)


# ---------------- 内部 ----------------

_ALLOWED_TRANSITIONS: dict[ArticleStatus, set[ArticleStatus]] = {
    ArticleStatus.DRAFT: {ArticleStatus.READY},
    ArticleStatus.READY: {ArticleStatus.SCHEDULED, ArticleStatus.DRAFT},
    ArticleStatus.SCHEDULED: {ArticleStatus.PUBLISHING, ArticleStatus.READY},
    ArticleStatus.PUBLISHING: {ArticleStatus.PUBLISHED, ArticleStatus.FAILED},
    ArticleStatus.FAILED: {ArticleStatus.SCHEDULED, ArticleStatus.DEAD},
    ArticleStatus.PUBLISHED: set(),
    ArticleStatus.DEAD: set(),
}


def _can_transition(src: ArticleStatus, dst: ArticleStatus) -> bool:
    return dst in _ALLOWED_TRANSITIONS.get(src, set())


def _to_article_out(a: Article) -> ArticleOut:
    return ArticleOut(
        id=a.id,
        topic_id=a.topic_id,
        title=a.title,
        body=a.body,
        content_type=a.content_type,
        status=a.status,
        target_platforms=a.target_platforms,
        target_account_ids=a.target_account_ids,
        scheduled_at=a.scheduled_at,
        extra=a.extra,
        created_at=a.created_at,
        updated_at=a.updated_at,
        assets=[
            AssetRef(id=x.id, asset_type=x.asset_type, local_path=x.local_path, meta=x.meta)
            for x in a.assets
        ],
    )
