"""FastAPI 入口 — 编排层对外接口。

设计原则：路由只做转译 + 调 service 层，不写业务逻辑。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from ..accounts import manager as account_mgr
from ..content import manager as content_mgr
from ..core.db import SessionLocal, init_db
from ..core.enums import ArticleStatus, JobStatus, Platform
from ..core.models import Account, Article, PublishJob, Topic
from ..core.schemas import (
    AccountIn,
    AccountOut,
    AccountUpdate,
    ArticleIn,
    ArticleOut,
    JobOut,
    TopicIn,
    TopicOut,
)
# 触发 lint：JobStatus 用于 dashboard 路由的统计
from ..scheduler.queue import queue
from ..scheduler.worker import execute_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    queue.start()
    try:
        from ..scheduler.health import schedule_daily_health_check
        schedule_daily_health_check()  # 默认每天 02:00
    except Exception:
        pass  # 调度注册失败不阻塞启动
    yield
    queue.shutdown()


app = FastAPI(title="ai-ops-auto", version="0.1.0", lifespan=lifespan)


class StripApiPrefixMiddleware(BaseHTTPMiddleware):
    """让 /api/* 路径在内部 dispatch 时等同于 /*。

    意义：前端不论 dev（vite proxy）还是 prod（StaticFiles serve），
    fetch /api/topics 都能命中现有的 @app.get("/topics") 路由。
    """
    async def dispatch(self, request, call_next):
        path = request.scope.get("path", "")
        if path.startswith("/api/"):
            request.scope["path"] = path[4:]
            request.scope["raw_path"] = request.scope["path"].encode("utf-8")
        return await call_next(request)


app.add_middleware(StripApiPrefixMiddleware)

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Mount React build 产物（如果有）
_FRONTEND_DIST = Path(__file__).parent.parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    app.mount("/admin", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="admin")


def get_session() -> Session:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------- Topics ----------------

@app.post("/topics", response_model=TopicOut)
def api_create_topic(data: TopicIn, s: Session = Depends(get_session)):
    return content_mgr.create_topic(s, data)


@app.get("/topics", response_model=list[TopicOut])
def api_list_topics(s: Session = Depends(get_session)):
    return content_mgr.list_topics(s)


# ---------------- Articles ----------------

@app.get("/articles", response_model=list[ArticleOut])
def api_list_articles(limit: int = 100, s: Session = Depends(get_session)):
    return content_mgr.list_articles(s, limit=limit)


@app.post("/articles", response_model=ArticleOut)
def api_create_article(data: ArticleIn, s: Session = Depends(get_session)):
    return content_mgr.create_article(s, data)


@app.post("/articles/{article_id}/transition", response_model=ArticleOut)
def api_transition_article(
    article_id: int,
    target: ArticleStatus,
    s: Session = Depends(get_session),
):
    try:
        return content_mgr.transition_status(s, article_id, target)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- Accounts ----------------

@app.post("/accounts", response_model=AccountOut)
def api_create_account(data: AccountIn, s: Session = Depends(get_session)):
    return account_mgr.create_account(s, data)


@app.get("/accounts", response_model=list[AccountOut])
def api_list_accounts(
    platform: Optional[Platform] = None,
    s: Session = Depends(get_session),
):
    return account_mgr.list_accounts(s, platform=platform)


@app.patch("/accounts/{account_id}", response_model=AccountOut)
def api_update_account(account_id: int, data: AccountUpdate, s: Session = Depends(get_session)):
    try:
        return account_mgr.update_account(s, account_id, data)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/accounts/{account_id}")
def api_delete_account(account_id: int, s: Session = Depends(get_session)):
    if not account_mgr.delete_account(s, account_id):
        raise HTTPException(404, f"account {account_id} 不存在")
    return {"ok": True, "deleted": account_id}


@app.post("/accounts/import")
def api_import_accounts(items: list[AccountIn], s: Session = Depends(get_session)):
    """批量导入账号（从 cookie 文件 / 其它系统迁移过来时用）。"""
    created = []
    failed: list[dict] = []
    for i, item in enumerate(items):
        try:
            created.append(account_mgr.create_account(s, item))
        except Exception as e:
            failed.append({"index": i, "nickname": item.nickname, "error": str(e)})
    return {"created": len(created), "failed": failed, "ids": [a.id for a in created]}


@app.post("/accounts/{account_id}/login")
async def api_login_account(account_id: int):
    """触发 publisher.login()，启动扫码流程。

    返回最终是否成功登录；过程中产生的 cookie 由 publisher 写回 credential
    （SSE 推送二维码图见 /accounts/{id}/login/stream，留为 P5+ 增量）。
    """
    import asyncio
    from ..accounts.manager import get_credential
    from ..core.db import session_scope
    from ..core.models import Account
    from ..core.enums import Platform
    from ..publishers.registry import default_registry

    with session_scope() as s:
        a = s.get(Account, account_id)
        if a is None:
            raise HTTPException(404, f"account {account_id} 不存在")
        try:
            cred = get_credential(s, account_id)
        except Exception:
            cred = {}
        platform = Platform(a.platform)

    pubs = default_registry.resolve(platform)
    if not pubs:
        raise HTTPException(400, f"无 {platform} publisher")

    try:
        ok = await asyncio.wait_for(pubs[0].login(account_id, cred), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(408, "登录超时（5 分钟内未完成扫码）")

    # publisher.login 会把 cookies 写回 cred dict — 这里加密落库
    if ok and cred.get("cookies"):
        with session_scope() as s:
            from ..accounts.store import get_store
            a = s.get(Account, account_id)
            if a is not None:
                a.encrypted_credential = get_store().encrypt(cred)

    return {"ok": ok, "account_id": account_id}


@app.get("/accounts/{account_id}/dispatch-preview")
def api_dispatch_preview(account_id: int, count: int = 1, s: Session = Depends(get_session)):
    """预览分发结果（不实际发布），用于 UI 上验证策略。

    注意：account_id 这里是平台标识占位（不优雅但不破坏路由命名）；
    更稳的是 /platforms/{platform}/dispatch?count=...，留 follow-up。
    """
    from ..accounts.dispatcher import pick_accounts
    a = s.get(Account, account_id)
    if a is None:
        raise HTTPException(404)
    cands = pick_accounts(s, Platform(a.platform), count=count)
    return [{"account_id": c.account_id, "nickname": c.nickname, "weight": c.weight,
             "health": c.health, "last_publish_at": c.last_publish_at} for c in cands]


# ---------------- Jobs ----------------

@app.get("/jobs", response_model=list[JobOut])
def api_list_jobs(limit: int = 100, s: Session = Depends(get_session)):
    return [
        JobOut(
            id=j.id,
            article_id=j.article_id,
            account_id=j.account_id,
            platform=j.platform,
            status=j.status,
            attempts=j.attempts,
            platform_post_id=j.platform_post_id,
            platform_url=j.platform_url,
            error=j.error,
            scheduled_at=j.scheduled_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
        )
        for j in s.execute(
            select(PublishJob).order_by(PublishJob.id.desc()).limit(limit)
        ).scalars().all()
    ]


@app.post("/jobs/{job_id}/run")
async def api_run_job(job_id: int):
    """同步触发一个 PublishJob（用于调试 / 手动重跑）。"""
    result = await execute_job(job_id)
    return result.model_dump()


@app.post("/jobs/{job_id}/collect")
async def api_collect_metrics(job_id: int):
    """手动触发一次数据采集（不等飞轮调度）。"""
    from ..scheduler.metrics import collect_one
    return await collect_one(job_id)


@app.get("/topics/heat-rank")
def api_heat_rank(limit: int = 10, s: Session = Depends(get_session)):
    """按 heat_score 倒排取热门主题（数据回流飞轮的反馈输入）。"""
    from ..content.heat_engine import top_topics
    return [
        {"id": t.id, "name": t.name, "heat_score": t.heat_score, "keywords": t.keywords}
        for t in top_topics(s, limit=limit)
    ]


@app.get("/health")
def health():
    return {"ok": True}


# ============== Web UI ==============

@app.get("/ui", response_class=HTMLResponse)
def ui_dashboard(request: Request, s: Session = Depends(get_session)):
    from ..content.heat_engine import top_topics

    counts = {
        "topics": s.scalar(select(func.count(Topic.id))) or 0,
        "articles": s.scalar(select(func.count(Article.id))) or 0,
        "accounts": s.scalar(select(func.count(Account.id))) or 0,
        "jobs": s.scalar(select(func.count(PublishJob.id))) or 0,
        "published": s.scalar(select(func.count(Article.id)).where(Article.status == ArticleStatus.PUBLISHED)) or 0,
        "failed": s.scalar(select(func.count(PublishJob.id)).where(PublishJob.status == JobStatus.DEAD)) or 0,
    }
    recent_jobs = s.execute(
        select(PublishJob).order_by(PublishJob.created_at.desc()).limit(10)
    ).scalars().all()
    return _templates.TemplateResponse("dashboard.html", {
        "request": request,
        "counts": counts,
        "hot_topics": top_topics(s, limit=5),
        "recent_jobs": recent_jobs,
    })


@app.get("/ui/topics", response_class=HTMLResponse)
def ui_topics(request: Request, s: Session = Depends(get_session)):
    rows = [
        {"id": t.id, "name": t.name, "keywords": ", ".join(t.keywords or []),
         "heat_score": round(t.heat_score, 3), "created_at": t.created_at}
        for t in s.execute(select(Topic).order_by(Topic.id.desc())).scalars().all()
    ]
    return _templates.TemplateResponse("list.html", {
        "request": request, "title": "主题",
        "columns": ["id", "name", "keywords", "heat_score", "created_at"],
        "rows": rows, "empty_hint": "POST /topics 创建第一个主题",
    })


@app.get("/ui/articles", response_class=HTMLResponse)
def ui_articles(request: Request, s: Session = Depends(get_session)):
    rows = [
        {"id": a.id, "topic_id": a.topic_id, "title": a.title,
         "content_type": a.content_type, "status": a.status,
         "scheduled_at": a.scheduled_at, "created_at": a.created_at}
        for a in s.execute(select(Article).order_by(Article.id.desc())).scalars().all()
    ]
    return _templates.TemplateResponse("list.html", {
        "request": request, "title": "文章",
        "columns": ["id", "topic_id", "title", "content_type", "status", "scheduled_at", "created_at"],
        "rows": rows, "empty_hint": "POST /articles 创建一篇",
    })


@app.get("/ui/accounts", response_class=HTMLResponse)
def ui_accounts(request: Request, s: Session = Depends(get_session)):
    rows = [
        {"id": a.id, "platform": a.platform, "nickname": a.nickname,
         "health": a.health, "daily_quota": a.daily_quota,
         "last_publish_at": a.last_publish_at, "created_at": a.created_at}
        for a in s.execute(select(Account).order_by(Account.id.desc())).scalars().all()
    ]
    return _templates.TemplateResponse("list.html", {
        "request": request, "title": "账号",
        "columns": ["id", "platform", "nickname", "health", "daily_quota", "last_publish_at", "created_at"],
        "rows": rows, "empty_hint": "POST /accounts 添加账号",
    })


@app.get("/ui/jobs", response_class=HTMLResponse)
def ui_jobs(request: Request, s: Session = Depends(get_session)):
    rows = [
        {"id": j.id, "article_id": j.article_id, "account_id": j.account_id,
         "platform": j.platform, "status": j.status,
         "attempts": f"{j.attempts}/{j.max_attempts}",
         "started_at": j.started_at, "platform_url": j.platform_url or "-"}
        for j in s.execute(select(PublishJob).order_by(PublishJob.id.desc()).limit(50)).scalars().all()
    ]
    return _templates.TemplateResponse("list.html", {
        "request": request, "title": "任务",
        "columns": ["id", "article_id", "account_id", "platform", "status", "attempts", "started_at", "platform_url"],
        "rows": rows, "empty_hint": "POST /jobs/{id}/run 触发任务",
    })
