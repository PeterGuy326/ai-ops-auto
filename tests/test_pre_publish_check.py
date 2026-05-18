"""tests/test_pre_publish_check.py — worker._pre_publish_check 单测。

策略：
  - 用 in-memory SQLite 起独立 engine（不污染主库），仿照 test_health_monitor.py
  - 不调真实 is_too_similar（依赖全局 engine），用 similarity_checker 注入 mock
  - 覆盖：TAINT 命中 / simhash 命中 / 双双不命中放行 / dedup 异常静默放行 / 空 body 放行
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.core.enums import AccountHealth, ContentType, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic
from ai_ops.scheduler.worker import (
    SIMHASH_HAMMING_THRESHOLD,
    SIMHASH_LOOKBACK_DAYS,
    TAINT_PATTERNS,
    _pre_publish_check,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


_TOPIC_COUNTER = {"n": 0}


def _mk_setup(s, body: str) -> tuple[PublishJob, Article]:
    _TOPIC_COUNTER["n"] += 1
    topic = Topic(
        name=f"t_{_TOPIC_COUNTER['n']}",
        keywords=[], persona={}, target_platforms=[],
    )
    s.add(topic)
    s.flush()
    acc = Account(
        platform=Platform.XIAOHONGSHU,
        nickname="acc1",
        profile={},
        health=AccountHealth.HEALTHY,
    )
    s.add(acc)
    s.flush()
    article = Article(
        topic_id=topic.id, title="t", body=body, content_type=ContentType.IMAGE_TEXT
    )
    s.add(article)
    s.flush()
    job = PublishJob(
        article_id=article.id,
        account_id=acc.id,
        platform=Platform.XIAOHONGSHU,
        status=JobStatus.PENDING,
    )
    s.add(job)
    s.flush()
    return job, article


def test_taint_pattern_blocks_todo(db_session):
    job, article = _mk_setup(db_session, "正文内容\nTODO: 待补充链接\n剩下的话")
    ok, err = _pre_publish_check(job=job, article=article, session=db_session)
    assert ok is False
    assert err is not None
    assert "污点拦截" in err
    assert "TODO" in err


def test_taint_pattern_blocks_placeholder(db_session):
    job, article = _mk_setup(db_session, "干净开头。未替换占位符\n收尾。")
    ok, err = _pre_publish_check(job=job, article=article, session=db_session)
    assert ok is False
    assert "未替换占位符" in err


def test_taint_all_patterns_covered(db_session):
    # 每个 TAINT_PATTERNS 都验一遍——免得日后改清单又漏掉某个
    for pat in TAINT_PATTERNS:
        job, article = _mk_setup(db_session, f"前文 {pat} 后文")
        ok, err = _pre_publish_check(job=job, article=article, session=db_session)
        assert ok is False, f"pattern {pat!r} 未拦截"
        assert pat in err


def test_simhash_block_uses_injected_checker(db_session):
    """注入 mock checker 返回 True，应判 simhash 重复。"""
    job, article = _mk_setup(db_session, "全新原创内容，绝无 TAINT。")
    captured = {}

    def fake_checker(*, text, account_id, days, threshold):
        captured["text"] = text
        captured["account_id"] = account_id
        captured["days"] = days
        captured["threshold"] = threshold
        return True

    ok, err = _pre_publish_check(
        job=job, article=article, session=db_session, similarity_checker=fake_checker
    )
    assert ok is False
    assert "simhash 重复" in err
    # 参数透传正确：阈值/天数与 worker 常量对齐
    assert captured["days"] == SIMHASH_LOOKBACK_DAYS
    assert captured["threshold"] == SIMHASH_HAMMING_THRESHOLD
    assert captured["account_id"] == job.account_id
    assert captured["text"] == article.body


def test_clean_body_passes(db_session):
    job, article = _mk_setup(db_session, "完全干净的原创内容，无任何 taint。")
    ok, err = _pre_publish_check(
        job=job,
        article=article,
        session=db_session,
        similarity_checker=lambda **kw: False,
    )
    assert ok is True
    assert err is None


def test_empty_body_passes(db_session):
    job, article = _mk_setup(db_session, "")
    # checker 不应被调用（body 空就 short-circuit）
    called = {"n": 0}

    def boom(**kw):
        called["n"] += 1
        return True

    ok, err = _pre_publish_check(
        job=job, article=article, session=db_session, similarity_checker=boom
    )
    assert ok is True
    assert err is None
    assert called["n"] == 0


def test_checker_exception_swallowed(db_session):
    """dedup 内部炸了，不应阻断发布——观测靠日志/上游告警，不靠 hard-fail。"""
    job, article = _mk_setup(db_session, "干净正文")

    def explode(**kw):
        raise RuntimeError("dedup 模块炸了")

    ok, err = _pre_publish_check(
        job=job, article=article, session=db_session, similarity_checker=explode
    )
    assert ok is True
    assert err is None
