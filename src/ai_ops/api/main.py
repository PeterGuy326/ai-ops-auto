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
from ..observability import init_observability
from .auth import require_api_key
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
    TopicStats,
    TopicUpdate,
)
# 触发 lint：JobStatus 用于 dashboard 路由的统计
from ..scheduler.queue import queue
from ..scheduler.worker import execute_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Task G · 可观测性必须最先初始化，确保后续 init_db / queue 的日志走结构化通道
    try:
        init_observability()
    except Exception as e:
        # observability 自身炸了不能阻塞主启动；裸 print 兜底（此时日志可能未就绪）
        import sys
        print(f'[startup] observability init failed (swallowed): {e}', file=sys.stderr)

    # === DEPLOY-CHECK · 启动期非阻塞自检 ===
    # 底层逻辑：生产环境最常见的"启动起来但配错了"——FERNET_KEY/API_KEY 没设——
    # 不能 raise（会让 dev / pytest 全废，运维也没机会进容器排查），
    # 但必须显眼 log 提示，否则运维不知道。结构化日志已就绪，warning 级别能进 ELK。
    try:
        from ..config import settings as _deploy_settings
        from ..observability import get_logger as _deploy_get_logger
        _deploy_logger = _deploy_get_logger("ai_ops.deploy_check")
        if not (_deploy_settings.fernet_key or "").strip():
            _deploy_logger.error(
                "[DEPLOY-CHECK] FERNET_KEY 未设置！cookies/凭证加密依赖此密钥，"
                "生产环境必须通过 env FERNET_KEY=... 注入。生成命令："
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        if not (_deploy_settings.api_key or "").strip():
            _deploy_logger.warning(
                "[DEPLOY-CHECK] API_KEY 未设置 —— 当前为 dev 自动放行模式，"
                "生产环境必须设非空值（X-API-Key 鉴权），否则任何人可写数据。"
            )
        if not (_deploy_settings.feishu_webhook_url or "").strip():
            _deploy_logger.warning(
                "[DEPLOY-CHECK] FEISHU_WEBHOOK_URL 未设置 —— 失败/风控告警不会通知，"
                "运维变盲人。建议配上群机器人 webhook（详见 docs/deployment.md §8.2）"
            )
    except Exception as _deploy_err:
        # 自检自己炸了绝不能阻塞启动 —— 裸 print 兜底
        import sys as _sys
        print(f"[DEPLOY-CHECK] self-check failed (swallowed): {_deploy_err}", file=_sys.stderr)

    # === SCHEMA-CHECK · Round 5 启动期 schema 漂移自检 + 自动升级 ===
    # 底层逻辑：dev/prod schema parity。早期 dev DB 是 `Base.metadata.create_all()` 建的，
    # 后续 model 加字段后 create_all 不会自动 ALTER，启动 INSERT 直接炸（Round 5 P9 事故重现）。
    # 生产已由 Dockerfile entrypoint 自动跑 alembic upgrade head 兜底（auto_upgrade_db=False 默认）；
    # dev 设 AUTO_UPGRADE_DB=true 后此处自动愈合。与 DEPLOY-CHECK 一致：**不 raise**，
    # 启动继续，但日志显眼，开发者第一时间能看到。
    try:
        from ..core.db import check_schema_drift as _check_schema_drift
        from ..core.db import try_auto_upgrade as _try_auto_upgrade
        from ..config import settings as _schema_settings
        from ..observability import get_logger as _schema_get_logger
        _schema_logger = _schema_get_logger("ai_ops.schema_check")
        _drift = _check_schema_drift()
        if _drift["in_sync"]:
            _schema_logger.info(
                "[SCHEMA-CHECK] schema 与 code 对齐 (rev=%s)", _drift["code_head"]
            )
        elif _schema_settings.auto_upgrade_db:
            _schema_logger.warning(
                "[SCHEMA-CHECK] schema 漂移：db=%s code=%s missing=%s，"
                "auto_upgrade_db=True，尝试自动升级…",
                _drift["db_head"], _drift["code_head"], _drift["missing_migrations"],
            )
            _up = _try_auto_upgrade()
            if _up["ok"]:
                _schema_logger.warning(
                    "[SCHEMA-CHECK] 已自动升级 schema %s -> %s（dev 模式）",
                    _up["from_rev"], _up["to_rev"],
                )
            else:
                _schema_logger.error(
                    "[SCHEMA-CHECK] 自动升级失败 (reason=%s, error=%s)，启动可能炸，"
                    "运维请手动跑：alembic upgrade head",
                    _up["reason"], _up["error"],
                )
        else:
            _schema_logger.error(
                "[SCHEMA-CHECK] schema 漂移：DB 在 %s，代码在 %s，待跑 migration: %s。"
                "请跑 `alembic upgrade head`（生产）或设 AUTO_UPGRADE_DB=true 后重启（dev）。"
                "启动继续，但相关表的 INSERT/SELECT 可能直接炸（详见 docs/deployment.md）。",
                _drift["db_head"], _drift["code_head"], _drift["missing_migrations"],
            )
    except Exception as _schema_err:
        # 自检自己炸了绝不阻塞启动 —— 裸 print 兜底（与 DEPLOY-CHECK 一致）
        import sys as _sys
        print(f"[SCHEMA-CHECK] self-check failed (swallowed): {_schema_err}", file=_sys.stderr)

    init_db()
    queue.start()
    try:
        from ..scheduler.health import schedule_daily_health_check
        schedule_daily_health_check()  # 默认每天 02:00
    except Exception:
        pass  # 调度注册失败不阻塞启动
    # === 数据回流自动出报 cron（daily 18:00 / weekly Mon 09:00）===
    try:
        from ..reports.cron import schedule_report_crons
        schedule_report_crons()
    except Exception:
        pass  # 报表 cron 注册失败不阻塞启动
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

@app.post("/topics", response_model=TopicOut, dependencies=[Depends(require_api_key)])
def api_create_topic(data: TopicIn, s: Session = Depends(get_session)):
    return content_mgr.create_topic(s, data)


@app.get("/topics", response_model=list[TopicStats], dependencies=[Depends(require_api_key)])
def api_list_topics(s: Session = Depends(get_session)):
    """带 account_count / article_count 统计的 topic 列表。

    兼容性：响应体在原 TopicOut 基础上**新增**统计字段，删除了 `persona`（前端列表不用）。
    若前端需要完整字段，请用 `GET /topics/{id}` 获取单条（留为 P5+ 增量，目前没有）。
    """
    return content_mgr.list_topic_stats(s)


@app.patch("/topics/{topic_id}", response_model=TopicOut, dependencies=[Depends(require_api_key)])
def api_update_topic(topic_id: int, data: TopicUpdate, s: Session = Depends(get_session)):
    try:
        return content_mgr.update_topic(s, topic_id, data)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------- Articles ----------------

@app.get("/articles", response_model=list[ArticleOut], dependencies=[Depends(require_api_key)])
def api_list_articles(
    limit: int = 100,
    topic_id: Optional[int] = None,
    s: Session = Depends(get_session),
):
    return content_mgr.list_articles(s, limit=limit, topic_id=topic_id)


@app.post("/articles", response_model=ArticleOut, dependencies=[Depends(require_api_key)])
def api_create_article(data: ArticleIn, s: Session = Depends(get_session)):
    return content_mgr.create_article(s, data)


@app.post("/articles/{article_id}/transition", response_model=ArticleOut, dependencies=[Depends(require_api_key)])
def api_transition_article(
    article_id: int,
    target: ArticleStatus,
    s: Session = Depends(get_session),
):
    try:
        return content_mgr.transition_status(s, article_id, target)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- 素材分发中台（审核 → 按账号分发 → 留痕）----------------

def _job_out(j: PublishJob) -> JobOut:
    return JobOut(
        id=j.id, article_id=j.article_id, account_id=j.account_id, platform=j.platform,
        status=j.status, attempts=j.attempts, platform_post_id=j.platform_post_id,
        platform_url=j.platform_url, error=j.error, scheduled_at=j.scheduled_at,
        started_at=j.started_at, finished_at=j.finished_at,
    )


@app.post("/articles/{article_id}/approve", response_model=ArticleOut, dependencies=[Depends(require_api_key)])
def api_approve_article(article_id: int, s: Session = Depends(get_session)):
    """人工审核通过：DRAFT(待审) → READY(可分发)。"""
    from ..content import distributor

    try:
        art = distributor.approve(s, article_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return content_mgr._to_article_out(art)


@app.post("/articles/{article_id}/distribute", response_model=list[JobOut], dependencies=[Depends(require_api_key)])
def api_distribute_article(
    article_id: int,
    account_ids: Optional[list[int]] = None,
    s: Session = Depends(get_session),
):
    """把审过的素材按账号扇出成分发记录（PublishJob）。

    - 仅 READY 素材可分发（DRAFT 会 400，防误直发）。
    - account_ids 为空 → 按素材 target_platforms 自动选号。
    - 真发布由 scheduler.worker 消费（含风控闭环），本接口只建记录。
    """
    from ..content import distributor

    try:
        jobs = distributor.distribute(s, article_id, account_ids=account_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [_job_out(j) for j in jobs]


@app.get("/accounts/{account_id}/jobs", response_model=list[JobOut], dependencies=[Depends(require_api_key)])
def api_account_jobs(account_id: int, limit: int = 100, s: Session = Depends(get_session)):
    """按个人账号查全部分发记录（留痕）。"""
    from ..content import distributor

    return [_job_out(j) for j in distributor.list_account_jobs(s, account_id, limit=limit)]


# ---------------- Accounts ----------------

@app.post("/accounts", response_model=AccountOut, dependencies=[Depends(require_api_key)])
def api_create_account(data: AccountIn, s: Session = Depends(get_session)):
    return account_mgr.create_account(s, data)


@app.get("/accounts", response_model=list[AccountOut], dependencies=[Depends(require_api_key)])
def api_list_accounts(
    platform: Optional[Platform] = None,
    topic_id: Optional[int] = None,
    s: Session = Depends(get_session),
):
    return account_mgr.list_accounts(s, platform=platform, by_topic=topic_id)


@app.patch("/accounts/{account_id}", response_model=AccountOut, dependencies=[Depends(require_api_key)])
def api_update_account(account_id: int, data: AccountUpdate, s: Session = Depends(get_session)):
    try:
        return account_mgr.update_account(s, account_id, data)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def api_delete_account(account_id: int, s: Session = Depends(get_session)):
    if not account_mgr.delete_account(s, account_id):
        raise HTTPException(404, f"account {account_id} 不存在")
    return {"ok": True, "deleted": account_id}


@app.post("/accounts/import", dependencies=[Depends(require_api_key)])
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


@app.post("/accounts/{account_id}/login", dependencies=[Depends(require_api_key)])
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

    # publisher.login 会把凭证写回 cred dict（cookies-based / profile_dir-based 都可能）
    # — 只要登录成功且 cred 非空就 Fernet 加密落库，兼容所有 publisher
    if ok and cred:
        with session_scope() as s:
            from ..accounts.store import get_store
            a = s.get(Account, account_id)
            if a is not None:
                a.encrypted_credential = get_store().encrypt(cred)

    return {"ok": ok, "account_id": account_id}


@app.get("/accounts/{account_id}/dispatch-preview", dependencies=[Depends(require_api_key)])
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

@app.get("/jobs", response_model=list[JobOut], dependencies=[Depends(require_api_key)])
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


@app.post("/jobs/{job_id}/run", dependencies=[Depends(require_api_key)])
async def api_run_job(job_id: int):
    """同步触发一个 PublishJob（用于调试 / 手动重跑）。"""
    result = await execute_job(job_id)
    return result.model_dump()


@app.post(
    "/jobs/{job_id}/republish",
    response_model=JobOut,
    dependencies=[Depends(require_api_key)],
)
def api_republish_job(job_id: int, s: Session = Depends(get_session)):
    """重发覆盖（publishing-sop §五）：基于失败 job 创建 v2 + 标 v1 superseded。

    - 入参：旧 job_id（必须 status ∈ {FAILED, DEAD}）
    - 行为：建 v2（status=PENDING）+ v1.superseded_by_job_id ← v2.id；**不立刻执行 v2**，
      由 scheduler 按现有节奏拉起，复用风控 / 限流 / dedup 全套兜底。
    - 错误：旧 job 不存在 / 状态不允许重发 → 400
    """
    from ..scheduler.worker import republish_job

    try:
        new_job = republish_job(s, job_id, reason="manual")
    except ValueError as e:
        raise HTTPException(400, str(e))

    return JobOut(
        id=new_job.id,
        article_id=new_job.article_id,
        account_id=new_job.account_id,
        platform=new_job.platform,
        status=new_job.status,
        attempts=new_job.attempts,
        platform_post_id=new_job.platform_post_id,
        platform_url=new_job.platform_url,
        error=new_job.error,
        scheduled_at=new_job.scheduled_at,
        started_at=new_job.started_at,
        finished_at=new_job.finished_at,
    )


@app.post("/jobs/{job_id}/collect", dependencies=[Depends(require_api_key)])
async def api_collect_metrics(job_id: int):
    """手动触发一次数据采集（不等飞轮调度）。

    Round 6 / TD-Z3-followup-2：传 source="manual" 让 Metrics 行被显式标记，
    24h 触发判定的 source-based 优先级（priority 2）会自动排除这条非飞轮行，
    避免运营手动复采污染健康度评估节点。
    """
    from ..scheduler.metrics import collect_one
    return await collect_one(job_id, source="manual")


@app.get("/topics/heat-rank", dependencies=[Depends(require_api_key)])
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
