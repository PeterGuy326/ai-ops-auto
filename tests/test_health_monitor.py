"""health_monitor 单测：baseline / pause / is_paused / evaluate_after_metrics。

用 in-memory SQLite 起独立 engine，不打主库。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ai_ops.accounts.health_monitor import (
    compute_baseline,
    evaluate_after_metrics,
    get_paused_until,
    is_paused,
    pause_account,
)
from ai_ops.core.enums import AccountHealth, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, Metrics, PublishJob, Topic


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s: Session = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_topic(s) -> Topic:
    t = Topic(name=f"topic_{id(s)}", keywords=[], persona={}, target_platforms=[])
    s.add(t)
    s.flush()
    return t


def _mk_article(s, topic_id: int) -> Article:
    a = Article(topic_id=topic_id, title="t", body="b", content_type="image_text")
    s.add(a)
    s.flush()
    return a


def _mk_account(s) -> Account:
    a = Account(platform=Platform.XIAOHONGSHU, nickname="acc1", profile={}, health=AccountHealth.HEALTHY)
    s.add(a)
    s.flush()
    return a


def _mk_success_job_with_metric(
    s, account_id: int, article_id: int, views: int, days_ago: float = 0
) -> tuple[PublishJob, Metrics]:
    finished = datetime.utcnow() - timedelta(days=days_ago)
    job = PublishJob(
        article_id=article_id,
        account_id=account_id,
        platform=Platform.XIAOHONGSHU,
        status=JobStatus.SUCCESS,
        started_at=finished - timedelta(minutes=5),
        finished_at=finished,
    )
    s.add(job)
    s.flush()
    metric = Metrics(
        job_id=job.id,
        likes=int(views * 0.05),
        comments=int(views * 0.01),
        views=views,
        collected_at=finished + timedelta(hours=24),
    )
    s.add(metric)
    s.flush()
    return job, metric


# ---------------------------------------------------------------------------- #
# compute_baseline
# ---------------------------------------------------------------------------- #
def test_compute_baseline_empty(db_session):
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    baseline = compute_baseline(db_session, acc.id, lookback_days=7)
    assert baseline["sample_size"] == 0
    assert baseline["views"] == 0


def test_compute_baseline_median(db_session):
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    art = _mk_article(db_session, topic.id)
    # 三次：views=1000 / 2000 / 3000  → median=2000
    for v, days_ago in [(1000, 1), (2000, 2), (3000, 3)]:
        _mk_success_job_with_metric(db_session, acc.id, art.id, v, days_ago=days_ago)

    baseline = compute_baseline(db_session, acc.id, lookback_days=7)
    assert baseline["sample_size"] == 3
    assert baseline["views"] == 2000


# ---------------------------------------------------------------------------- #
# pause_account / is_paused
# ---------------------------------------------------------------------------- #
def test_pause_and_is_paused(db_session):
    acc = _mk_account(db_session)

    until = pause_account(db_session, acc.id, hours=48, reason="manual test")
    db_session.flush()

    acc2 = db_session.get(Account, acc.id)
    assert is_paused(acc2) is True
    assert acc2.health == AccountHealth.DEGRADED
    assert acc2.profile.get("paused_until") == until.isoformat()
    assert acc2.profile.get("paused_reason") == "manual test"

    got_until = get_paused_until(acc2)
    assert got_until is not None
    assert abs((got_until - until).total_seconds()) < 1


def test_is_paused_expired(db_session):
    acc = _mk_account(db_session)
    # 手动写一个已过期的 paused_until
    expired = datetime.utcnow() - timedelta(minutes=1)
    profile = dict(acc.profile or {})
    profile["paused_until"] = expired.isoformat()
    acc.profile = profile
    db_session.flush()

    acc2 = db_session.get(Account, acc.id)
    assert is_paused(acc2) is False
    assert get_paused_until(acc2) is None


def test_is_paused_no_field(db_session):
    acc = _mk_account(db_session)
    assert is_paused(acc) is False


def test_is_paused_corrupt_value(db_session):
    acc = _mk_account(db_session)
    acc.profile = {"paused_until": "not-an-isoformat-string"}
    db_session.flush()
    acc2 = db_session.get(Account, acc.id)
    # 解析失败 → False（放行，避免误锁）
    assert is_paused(acc2) is False


# ---------------------------------------------------------------------------- #
# evaluate_after_metrics
# ---------------------------------------------------------------------------- #
def test_evaluate_skip_no_baseline(db_session):
    """新号没历史 → skip 不动它。"""
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    art = _mk_article(db_session, topic.id)
    job, _ = _mk_success_job_with_metric(db_session, acc.id, art.id, views=100)

    action = evaluate_after_metrics(db_session, job.id)
    assert action.decision == "skip"
    assert "样本不足" in action.reason


def test_evaluate_healthy(db_session):
    """有 baseline 但本次曝光正常 → healthy。"""
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    art = _mk_article(db_session, topic.id)
    # baseline: 3 个历史 views=2000
    for days_ago in [3, 4, 5]:
        _mk_success_job_with_metric(db_session, acc.id, art.id, 2000, days_ago=days_ago)
    # 当前 job：views=1800（高于阈值 2000*0.2=400）
    current_job, _ = _mk_success_job_with_metric(db_session, acc.id, art.id, 1800, days_ago=0)

    action = evaluate_after_metrics(db_session, current_job.id)
    assert action.decision == "healthy"
    assert action.baseline["views"] == 2000


def test_evaluate_degraded_after_3_low(db_session):
    """近 3 次都低曝光 → DEGRADED + pause 48h。"""
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    art = _mk_article(db_session, topic.id)
    # baseline：5 个高 views 在 lookback 窗口内（7 天内），中位数 5000
    # 注：lookback_days=7 默认；近 3 次低 views 也在窗口内，但占少数所以 median 仍是 5000
    for days_ago in [3, 4, 5, 6, 7]:
        _mk_success_job_with_metric(db_session, acc.id, art.id, 5000, days_ago=days_ago)
    # 近 3 次：days_ago=0/0.5/1，全部低曝光 views=100（< 5000 * 0.2 = 1000）
    low_jobs = [
        _mk_success_job_with_metric(db_session, acc.id, art.id, 100, days_ago=days)[0]
        for days in [2, 1, 0]
    ]
    # baseline 总样本 = 5 + 3 = 8，sorted views: 100,100,100,5000,5000,5000,5000,5000
    # median = (5000+5000)/2 = 5000

    action = evaluate_after_metrics(db_session, low_jobs[-1].id)
    assert action.decision == "degraded", f"reason: {action.reason}, baseline: {action.baseline}"
    assert action.paused_until is not None
    # 验证 48h 量级（允许 ±1 小时误差）
    diff_h = (action.paused_until - datetime.utcnow()).total_seconds() / 3600
    assert 47 < diff_h < 49

    # 账号状态已更新
    acc2 = db_session.get(Account, acc.id)
    assert acc2.health == AccountHealth.DEGRADED
    assert is_paused(acc2)


def test_evaluate_banned_after_5_low(db_session):
    """连续 5 次低曝光 → BANNED + pause 7d。"""
    topic = _mk_topic(db_session)
    acc = _mk_account(db_session)
    art = _mk_article(db_session, topic.id)
    # baseline：7 个高 views 在 7d 内，再加 5 个低；总 12 个，sorted median 仍 = 5000
    for days_ago in [3, 4, 5, 6, 7]:
        _mk_success_job_with_metric(db_session, acc.id, art.id, 5000, days_ago=days_ago)
    # 再补 2 个高让 high count > low count
    _mk_success_job_with_metric(db_session, acc.id, art.id, 5000, days_ago=6)
    _mk_success_job_with_metric(db_session, acc.id, art.id, 5000, days_ago=7)
    # 近 5 次：days_ago=0..2.4，全部低曝光
    low_jobs = [
        _mk_success_job_with_metric(db_session, acc.id, art.id, 50, days_ago=d)[0]
        for d in [2.4, 1.8, 1.2, 0.6, 0]
    ]

    action = evaluate_after_metrics(db_session, low_jobs[-1].id)
    assert action.decision == "banned", f"reason: {action.reason}, baseline: {action.baseline}"
    acc2 = db_session.get(Account, acc.id)
    assert acc2.health == AccountHealth.BANNED
    # 7 天量级
    diff_h = (action.paused_until - datetime.utcnow()).total_seconds() / 3600
    assert 167 < diff_h < 169
