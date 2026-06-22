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


def test_ui_interactive_approve_distribute(client):
    """后台可点操作：素材详情页 → 审核按钮 → 分发按钮（纯表单 POST）。"""
    tid = client.post("/topics", json={"name": "短剧", "category": "drama"}).json()["id"]
    aid = client.post("/accounts", json={"platform": "douyin", "nickname": "抖音号X"}).json()["id"]
    art_id = client.post("/articles", json={
        "topic_id": tid, "title": "可点短剧", "content_type": "video", "target_platforms": ["douyin"],
    }).json()["id"]

    # 详情页（DRAFT）：应有"审核通过"按钮
    page = client.get(f"/ui/articles/{art_id}")
    assert page.status_code == 200 and "审核通过" in page.text

    # 点"审核通过"（表单 POST，303 重定向回详情）
    r = client.post(f"/ui/articles/{art_id}/approve", follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/ui/articles/{art_id}")
    assert "分发到所选账号" in page.text and "抖音号X" in page.text  # READY 显示分发表单+候选账号

    # 点"分发"（勾选账号）
    r = client.post(f"/ui/articles/{art_id}/distribute", data={"account_ids": [aid]}, follow_redirects=False)
    assert r.status_code == 303
    recs = client.get(f"/accounts/{aid}/jobs").json()
    assert len(recs) == 1 and recs[0]["article_id"] == art_id

    # 素材列表标题可点进详情
    assert "/ui/articles/" in client.get("/ui/articles").text


def test_ui_library_filters(client):
    """素材库统一入口：按类型/状态筛选 + 待审专属视图。"""
    tid = client.post("/topics", json={"name": "混合", "category": "g"}).json()["id"]
    # 造不同类型 + 不同状态的素材
    v = client.post("/articles", json={"topic_id": tid, "title": "视频草稿", "content_type": "video"}).json()["id"]
    client.post("/articles", json={"topic_id": tid, "title": "长文草稿", "content_type": "long_article"})
    client.post("/articles", json={"topic_id": tid, "title": "音频草稿", "content_type": "audio"})
    client.post(f"/articles/{v}/approve")  # 视频 → READY

    # 全部：3 条都在
    allp = client.get("/ui/articles").text
    assert "视频草稿" in allp and "长文草稿" in allp and "音频草稿" in allp
    assert "素材库" in allp  # 标题改名

    # 按类型筛选：只看视频
    vid = client.get("/ui/articles?content_type=video").text
    assert "视频草稿" in vid and "长文草稿" not in vid and "音频草稿" not in vid

    # 待审专属视图(draft)：视频已 READY 不在，长文/音频在
    draft = client.get("/ui/articles?status=draft").text
    assert "长文草稿" in draft and "音频草稿" in draft and "视频草稿" not in draft


def test_ui_dashboard_operations(client):
    """运营看板：待审计数 + 今日分发 + 各平台账号健康。"""
    tid = client.post("/topics", json={"name": "看板", "category": "g"}).json()["id"]
    acc = client.post("/accounts", json={"platform": "douyin", "nickname": "看板号"}).json()["id"]
    # 2 个待审 + 1 个走到分发
    client.post("/articles", json={"topic_id": tid, "title": "待审1", "content_type": "video"})
    client.post("/articles", json={"topic_id": tid, "title": "待审2", "content_type": "audio"})
    art = client.post("/articles", json={"topic_id": tid, "title": "要发", "content_type": "video", "target_platforms": ["douyin"]}).json()["id"]
    client.post(f"/articles/{art}/approve")
    client.post(f"/articles/{art}/distribute", json=[acc])

    page = client.get("/ui")
    assert page.status_code == 200
    html = page.text
    assert "运营看板" in html
    assert "🕒 待审素材" in html and "各平台账号健康" in html
    assert "douyin" in html  # 平台健康行
    # 待审视图链接可达
    assert '/ui/articles?status=draft' in html
