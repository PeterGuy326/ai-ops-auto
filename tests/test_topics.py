"""专题（Topic）一等公民 + Account 绑定专题的单测。

覆盖：
  - 创建 topic（带 category）
  - 更新 topic（PATCH）
  - 创建 account 绑定 topic
  - list_accounts(by_topic=...) 命中
  - list_topic_stats 统计正确
  - 创建 account 时 topic_id 不存在 → ValueError
  - list_articles(topic_id=...) 过滤
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.accounts import manager as account_mgr
from ai_ops.content import manager as content_mgr
from ai_ops.core.enums import ContentType, Platform
from ai_ops.core.models import Base
from ai_ops.core.schemas import (
    AccountIn,
    AccountUpdate,
    ArticleIn,
    TopicIn,
    TopicUpdate,
)


@pytest.fixture()
def session():
    """in-memory sqlite，每个测试独立 schema。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()
        engine.dispose()


def test_create_topic_with_category(session):
    t = content_mgr.create_topic(
        session,
        TopicIn(name="dws", category="tech", keywords=["钉钉", "AI表格"]),
    )
    assert t.id is not None
    assert t.name == "dws"
    assert t.category == "tech"
    assert t.keywords == ["钉钉", "AI表格"]


def test_create_topic_default_category(session):
    t = content_mgr.create_topic(session, TopicIn(name="misc"))
    assert t.category == "general"


def test_update_topic(session):
    t = content_mgr.create_topic(session, TopicIn(name="软考", category="exam"))
    updated = content_mgr.update_topic(
        session, t.id, TopicUpdate(name="软考备考", category="exam"),
    )
    assert updated.name == "软考备考"
    assert updated.category == "exam"


def test_update_topic_not_found(session):
    with pytest.raises(ValueError, match="not found"):
        content_mgr.update_topic(session, 99999, TopicUpdate(name="x"))


def test_create_account_bound_to_topic(session):
    t = content_mgr.create_topic(session, TopicIn(name="dws", category="tech"))
    a = account_mgr.create_account(
        session,
        AccountIn(
            platform=Platform.XIAOHONGSHU,
            nickname="dws_xhs_01",
            topic_id=t.id,
        ),
    )
    assert a.topic_id == t.id


def test_create_account_invalid_topic(session):
    with pytest.raises(ValueError, match="topic 999 不存在"):
        account_mgr.create_account(
            session,
            AccountIn(
                platform=Platform.XIAOHONGSHU,
                nickname="orphan",
                topic_id=999,
            ),
        )


def test_list_accounts_by_topic(session):
    t1 = content_mgr.create_topic(session, TopicIn(name="dws", category="tech"))
    t2 = content_mgr.create_topic(session, TopicIn(name="软考", category="exam"))

    account_mgr.create_account(
        session, AccountIn(platform=Platform.XIAOHONGSHU, nickname="a1", topic_id=t1.id)
    )
    account_mgr.create_account(
        session, AccountIn(platform=Platform.XIAOHONGSHU, nickname="a2", topic_id=t1.id)
    )
    account_mgr.create_account(
        session, AccountIn(platform=Platform.ZHIHU, nickname="b1", topic_id=t2.id)
    )
    account_mgr.create_account(
        session, AccountIn(platform=Platform.ZHIHU, nickname="orphan", topic_id=None)
    )

    t1_accounts = account_mgr.list_accounts(session, by_topic=t1.id)
    assert {a.nickname for a in t1_accounts} == {"a1", "a2"}

    t2_accounts = account_mgr.list_accounts(session, by_topic=t2.id)
    assert {a.nickname for a in t2_accounts} == {"b1"}

    # 跨 platform + topic 组合过滤
    cross = account_mgr.list_accounts(session, platform=Platform.ZHIHU, by_topic=t2.id)
    assert {a.nickname for a in cross} == {"b1"}


def test_update_account_topic_rebind(session):
    t1 = content_mgr.create_topic(session, TopicIn(name="dws", category="tech"))
    t2 = content_mgr.create_topic(session, TopicIn(name="软考", category="exam"))
    a = account_mgr.create_account(
        session, AccountIn(platform=Platform.XIAOHONGSHU, nickname="a", topic_id=t1.id)
    )

    # rebind
    updated = account_mgr.update_account(session, a.id, AccountUpdate(topic_id=t2.id))
    assert updated.topic_id == t2.id

    # clear (-1 哨兵)
    cleared = account_mgr.update_account(session, a.id, AccountUpdate(topic_id=-1))
    assert cleared.topic_id is None


def test_topic_stats(session):
    t1 = content_mgr.create_topic(session, TopicIn(name="dws", category="tech"))
    t2 = content_mgr.create_topic(session, TopicIn(name="软考", category="exam"))

    account_mgr.create_account(
        session, AccountIn(platform=Platform.XIAOHONGSHU, nickname="a1", topic_id=t1.id)
    )
    account_mgr.create_account(
        session, AccountIn(platform=Platform.XIAOHONGSHU, nickname="a2", topic_id=t1.id)
    )
    content_mgr.create_article(
        session,
        ArticleIn(topic_id=t1.id, title="dws-01", content_type=ContentType.IMAGE_TEXT),
    )

    stats = content_mgr.list_topic_stats(session)
    stats_by_name = {s.name: s for s in stats}
    assert stats_by_name["dws"].account_count == 2
    assert stats_by_name["dws"].article_count == 1
    assert stats_by_name["dws"].category == "tech"
    assert stats_by_name["软考"].account_count == 0
    assert stats_by_name["软考"].article_count == 0


def test_list_articles_by_topic(session):
    t1 = content_mgr.create_topic(session, TopicIn(name="dws", category="tech"))
    t2 = content_mgr.create_topic(session, TopicIn(name="软考", category="exam"))
    content_mgr.create_article(
        session,
        ArticleIn(topic_id=t1.id, title="dws-01", content_type=ContentType.IMAGE_TEXT),
    )
    content_mgr.create_article(
        session,
        ArticleIn(topic_id=t1.id, title="dws-02", content_type=ContentType.IMAGE_TEXT),
    )
    content_mgr.create_article(
        session,
        ArticleIn(topic_id=t2.id, title="exam-01", content_type=ContentType.LONG_ARTICLE),
    )

    t1_arts = content_mgr.list_articles(session, topic_id=t1.id)
    assert {a.title for a in t1_arts} == {"dws-01", "dws-02"}

    t2_arts = content_mgr.list_articles(session, topic_id=t2.id)
    assert {a.title for a in t2_arts} == {"exam-01"}

    all_arts = content_mgr.list_articles(session)
    assert len(all_arts) == 3
