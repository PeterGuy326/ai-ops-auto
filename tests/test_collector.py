"""历史采集器单测 —— 导出文件(CSV/JSON) → 回填历史发布(列名容错)。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.content import collector
from ai_ops.core.enums import ContentType, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, PublishJob


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def account(session):
    a = Account(platform=Platform.DOUYIN, nickname="抖音老号", profile={})
    session.add(a)
    session.flush()
    return a


def test_import_from_rows_chinese_columns(session, account):
    rows = [
        {"标题": "去年爆款", "作品链接": "https://douyin.com/v/1", "作品id": "dy1",
         "发布时间": "2025-11-01 12:30:00", "类型": "视频", "文案": "历史文案A"},
        {"标题": "图文一篇", "链接": "https://douyin.com/v/2", "id": "dy2",
         "发表时间": "2025-12-02", "类型": "图文"},
    ]
    jobs = collector.import_from_rows(session, account.id, rows)
    assert len(jobs) == 2
    assert all(j.status == JobStatus.SUCCESS and j.account_id == account.id for j in jobs)
    arts = {session.get(Article, j.article_id).title for j in jobs}
    assert arts == {"去年爆款", "图文一篇"}
    # 类型映射
    types = {session.get(Article, j.article_id).content_type for j in jobs}
    assert ContentType.VIDEO in types and ContentType.IMAGE_TEXT in types


def test_import_idempotent_via_collector(session, account):
    rows = [{"标题": "同条", "作品id": "x1"}]
    collector.import_from_rows(session, account.id, rows)
    collector.import_from_rows(session, account.id, rows)  # 再导一次
    assert session.query(PublishJob).filter_by(account_id=account.id).count() == 1


def test_import_from_csv(session, account, tmp_path):
    p = tmp_path / "posts.csv"
    p.write_text("标题,作品链接,作品id,发布时间\n爆款A,https://x/1,a1,2025-10-01\n爆款B,https://x/2,a2,2025-10-02\n", encoding="utf-8")
    jobs = collector.import_from_csv(session, account.id, p)
    assert len(jobs) == 2
    assert {j.platform_post_id for j in jobs} == {"a1", "a2"}


def test_import_from_json_wrapped(session, account, tmp_path):
    p = tmp_path / "posts.json"
    p.write_text(json.dumps({"data": [
        {"title": "J1", "post_id": "j1", "url": "https://x/j1"},
        {"title": "J2", "post_id": "j2"},
    ]}), encoding="utf-8")
    jobs = collector.import_from_json(session, account.id, p)
    assert len(jobs) == 2
    assert {session.get(Article, j.article_id).title for j in jobs} == {"J1", "J2"}
