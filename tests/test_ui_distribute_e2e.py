"""端到端 HTTP/UI 冒烟：素材中台 + 历史导入 + 账号详情页，打通真实 app。

覆盖（走真实 ai_ops.api.main.app）：
  - POST /accounts/{id}/import-published   历史回填
  - POST /articles + approve + distribute  入库→审核→按账号分发
  - GET  /accounts/{id}/jobs               按账号留痕（历史+新发）
  - GET  /ui/accounts/{id}                 账号详情页渲染（含历史/系统标记）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_ops.api.main import app, get_session
from ai_ops.config import settings
from ai_ops.core.models import Base


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override():
        s = TestingSession()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(settings, "api_key", "")  # dev 模式，免 header
    app.dependency_overrides[get_session] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_full_flow_history_and_distribute(client):
    # 1) 建主题 + 账号
    tid = client.post("/topics", json={"name": "逆袭短剧", "category": "drama"}).json()["id"]
    acc = client.post("/accounts", json={"platform": "douyin", "nickname": "抖音老号"}).json()
    aid = acc["id"]

    # 2) 历史回填（之前发的内容）
    r = client.post(f"/accounts/{aid}/import-published", json=[
        {"title": "去年爆款", "platform_post_id": "dy-1", "platform_url": "https://douyin.com/v/1"},
        {"title": "前年图文", "platform_post_id": "dy-2"},
    ])
    assert r.status_code == 200 and len(r.json()) == 2
    assert all(j["status"] == "success" for j in r.json())

    # 3) 新素材：建 → 审核 → 按账号分发
    art = client.post("/articles", json={
        "topic_id": tid, "title": "逆袭短剧·第一集", "content_type": "video",
        "target_platforms": ["douyin"],
    }).json()
    art_id = art["id"]
    assert art["status"] == "draft"

    # DRAFT 直接分发应被拒
    assert client.post(f"/articles/{art_id}/distribute", json=[aid]).status_code == 400
    # 审核通过 → READY
    assert client.post(f"/articles/{art_id}/approve").json()["status"] == "ready"
    # 分发 → 建 1 条 job
    jobs = client.post(f"/articles/{art_id}/distribute", json=[aid]).json()
    assert len(jobs) == 1 and jobs[0]["account_id"] == aid and jobs[0]["platform"] == "douyin"

    # 4) 按账号留痕：2 条历史 + 1 条新发 = 3
    recs = client.get(f"/accounts/{aid}/jobs").json()
    assert len(recs) == 3

    # 5) 账号详情页渲染
    page = client.get(f"/ui/accounts/{aid}")
    assert page.status_code == 200
    html = page.text
    assert "抖音老号" in html
    assert "去年爆款" in html and "逆袭短剧·第一集" in html
    assert "历史" in html and "系统" in html  # 来源标记
