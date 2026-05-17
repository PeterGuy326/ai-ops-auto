"""发布任务执行器。

职责：拉取 PublishJob → 解密凭证 → 通过 registry 拿 Publisher → 调 publish →
落库结果（成功/失败/重试）→ 触发数据采集。

注意：发布器列表有优先级，fallback 自动切换。
"""
from __future__ import annotations

from datetime import datetime

from ..accounts.manager import check_rate_limit, get_credential, mark_published, update_health
from ..core.db import session_scope
from ..core.enums import AccountHealth, ArticleStatus, JobStatus, Platform
from ..core.models import Article, PublishJob
from ..core.schemas import PublishContent, PublishResult
from ..publishers.registry import default_registry


async def execute_job(job_id: int) -> PublishResult:
    """执行一个 PublishJob。"""
    with session_scope() as s:
        job: PublishJob | None = s.get(PublishJob, job_id)
        if job is None:
            return PublishResult(success=False, error=f"job {job_id} 不存在")

        article: Article | None = s.get(Article, job.article_id)
        if article is None:
            job.status = JobStatus.FAILED
            job.error = "article 缺失"
            return PublishResult(success=False, error=job.error)

        # 风控限流校验（养号期 + 间隔 + 单日上限）
        gate = check_rate_limit(s, job.account_id)
        if not gate.allowed:
            job.status = JobStatus.FAILED
            job.error = f"rate-limit: {gate.reason}"
            return PublishResult(success=False, error=job.error)

        try:
            credential = get_credential(s, job.account_id)
        except ValueError as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            return PublishResult(success=False, error=str(e))

        platform = Platform(job.platform)
        content = _build_content(article)

        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        job.attempts += 1
        s.flush()

    # 跳出 session 调外部工具，避免长事务
    result = await _try_publishers(platform, job.account_id, credential, content)

    with session_scope() as s:
        job = s.get(PublishJob, job_id)
        if job is None:
            return result
        job.finished_at = datetime.utcnow()
        if result.success:
            job.status = JobStatus.SUCCESS
            job.platform_post_id = result.platform_post_id
            job.platform_url = result.platform_url
            job.raw_response = result.raw_response
            mark_published(s, job.account_id)

            article = s.get(Article, job.article_id)
            if article and article.status == ArticleStatus.PUBLISHING:
                article.status = ArticleStatus.PUBLISHED

            # 飞轮闭环：发布成功 → 调度 1h/24h/7d 数据采集
            try:
                from .metrics import schedule_after_publish
                schedule_after_publish(job.id)
            except Exception:
                pass  # 采集失败不影响主流程
        else:
            job.error = result.error or "unknown"
            job.raw_response = result.raw_response
            if job.attempts < job.max_attempts:
                job.status = JobStatus.RETRYING
            else:
                job.status = JobStatus.DEAD
                # 失败联动：先降级到 DEGRADED；近 24h 内连续 3 次 DEAD → 升级到 BANNED
                _escalate_health_on_failure(s, job.account_id)
    return result


async def _try_publishers(
    platform: Platform,
    account_id: int,
    credential: dict,
    content: PublishContent,
) -> PublishResult:
    """按优先级尝试该平台所有 Publisher，第一个成功即返回。"""
    publishers = default_registry.resolve(platform)
    if not publishers:
        return PublishResult(success=False, error=f"未注册 {platform} 的 Publisher")

    last: PublishResult | None = None
    for pub in publishers:
        try:
            result = await pub.publish(account_id, credential, content)
        except NotImplementedError as e:
            result = PublishResult(success=False, error=f"{pub.kind} 未实现: {e}")
        except Exception as e:
            result = PublishResult(success=False, error=f"{pub.kind} 异常: {e}")
        if result.success:
            return result
        last = result
    return last or PublishResult(success=False, error="所有 Publisher 都失败")


def _escalate_health_on_failure(session, account_id: int) -> None:
    """失败联动健康降级：DEAD 默认降到 DEGRADED；24h 内连续 3 次 DEAD 升级到 BANNED。"""
    from datetime import datetime, timedelta
    from sqlalchemy import func, select

    window_start = datetime.utcnow() - timedelta(hours=24)
    recent_dead = session.scalar(
        select(func.count(PublishJob.id))
        .where(PublishJob.account_id == account_id)
        .where(PublishJob.status == JobStatus.DEAD)
        .where(PublishJob.finished_at >= window_start)
    ) or 0

    if recent_dead >= 3:
        update_health(session, account_id, AccountHealth.BANNED)
    else:
        update_health(session, account_id, AccountHealth.DEGRADED)


def _build_content(article: Article) -> PublishContent:
    images = [a.local_path for a in article.assets if a.asset_type == "image"]
    videos = [a.local_path for a in article.assets if a.asset_type == "video"]
    return PublishContent(
        title=article.title,
        body=article.body,
        content_type=article.content_type,
        images=images,
        videos=videos,
        tags=article.extra.get("tags", []) if article.extra else [],
        extra=article.extra or {},
    )
