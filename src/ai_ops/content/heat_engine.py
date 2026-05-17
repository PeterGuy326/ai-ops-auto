"""主题热度引擎 — 把 Metrics 聚合成 Topic.heat_score，驱动下一轮选题。

打分公式（可调）：
  score = log10(views + 1) * 0.4 + log10(likes + 1) * 0.4 + log10(comments + 1) * 0.2

聚合：取每个 article 最新 metrics，按 topic 求 mean。
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import session_scope
from ..core.models import Article, Metrics, PublishJob, Topic


def metric_score(metrics: Metrics) -> float:
    """单条 metrics 打分。log 平滑大数（避免一两条爆款拉偏全局）。"""
    return (
        math.log10(metrics.views + 1) * 0.4
        + math.log10(metrics.likes + 1) * 0.4
        + math.log10(metrics.comments + 1) * 0.2
    )


def latest_metrics_for_article(session: Session, article_id: int) -> list[Metrics]:
    """取 article 下所有 job 的最新一条 metrics。"""
    jobs = session.execute(
        select(PublishJob).where(PublishJob.article_id == article_id)
    ).scalars().all()

    out: list[Metrics] = []
    for j in jobs:
        latest = session.execute(
            select(Metrics)
            .where(Metrics.job_id == j.id)
            .order_by(Metrics.collected_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is not None:
            out.append(latest)
    return out


def recompute_topic_heat(session: Session, topic_id: int) -> float:
    """重算单个 topic 的 heat_score。"""
    topic = session.get(Topic, topic_id)
    if topic is None:
        return 0.0

    articles = session.execute(
        select(Article).where(Article.topic_id == topic_id)
    ).scalars().all()

    scores: list[float] = []
    for a in articles:
        for m in latest_metrics_for_article(session, a.id):
            scores.append(metric_score(m))

    new_score = sum(scores) / len(scores) if scores else 0.0
    topic.heat_score = new_score
    return new_score


def recompute_topic_heat_for_article(article_id: int) -> float:
    """根据 article 反查 topic 触发重算。供 metrics 采集后调用。"""
    with session_scope() as s:
        a = s.get(Article, article_id)
        if a is None:
            return 0.0
        return recompute_topic_heat(s, a.topic_id)


def top_topics(session: Session, limit: int = 10) -> list[Topic]:
    """按 heat_score 倒序取热门主题，供生成器下一轮选题。"""
    return list(
        session.execute(
            select(Topic).order_by(Topic.heat_score.desc()).limit(limit)
        ).scalars().all()
    )
