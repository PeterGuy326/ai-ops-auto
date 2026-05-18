"""发布任务执行器。

职责：拉取 PublishJob → 解密凭证 → 通过 registry 拿 Publisher → 调 publish →
落库结果（成功/失败/重试）→ 触发数据采集。

注意：发布器列表有优先级，fallback 自动切换。
"""
from __future__ import annotations

from datetime import datetime

from ..accounts.health_monitor import get_paused_until, is_paused
from ..accounts.manager import check_rate_limit, get_credential, mark_published, update_health
from ..core.db import session_scope
from ..core.dedup import is_too_similar
from ..core.enums import AccountHealth, ArticleStatus, ContentType, JobStatus, Platform
from ..core.models import Account, Article, PublishJob
from ..core.schemas import PublishContent, PublishResult
from ..observability import get_logger
from ..observability.sentry import capture_exception
from ..publishers.registry import default_registry

logger = get_logger(__name__)

# 发布前置兜底污点词清单（命中即 fail-fast，防止 TODO / 未替换占位符 / 错版本号溜出）。
# 注：暂不进 config.py（Task B 在那条战线，避免合并冲突），下个 sprint 再迁移。
TAINT_PATTERNS: tuple[str, ...] = ("TODO", "未替换占位符", "过期版本号", "XXX")

# simhash 拦截阈值：与该账号 7d 内已发布 article.body 的 hamming 距离 < 此值即视为重复。
# 对齐 docs/anti-risk.md §63 设定的"相似度 > 0.85"，64 位 simhash 下约 8 bit。
SIMHASH_HAMMING_THRESHOLD = 8
SIMHASH_LOOKBACK_DAYS = 7


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

        # 风控降权暂停期检查（health_monitor 写入 account.profile["paused_until"]）
        account = s.get(Account, job.account_id)
        if account is not None and is_paused(account):
            until = get_paused_until(account)
            job.status = JobStatus.FAILED
            job.error = f"账号暂停中至 {until.isoformat() if until else 'unknown'}"
            return PublishResult(success=False, error=job.error)

        # 内容层前置兜底：TAINT 词 + simhash 查重。
        # 任何一个命中即 fail-fast，不再消耗下游的解密 / 浏览器开销。
        ok, pre_err = _pre_publish_check(s, job, article)
        if not ok:
            job.status = JobStatus.FAILED
            job.error = pre_err
            job.finished_at = datetime.utcnow()
            return PublishResult(success=False, error=pre_err)

        try:
            credential = get_credential(s, job.account_id)
        except ValueError as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            return PublishResult(success=False, error=str(e))

        platform = Platform(job.platform)
        content = _build_content(article)

        # 小红书图文：发布前对图片做反指纹处理（EXIF/微裁剪/微旋转/调色）
        # 仅对 XIAOHONGSHU + IMAGE_TEXT 执行，规避其它平台回归
        if (
            platform == Platform.XIAOHONGSHU
            and content.content_type == ContentType.IMAGE_TEXT
            and content.images
        ):
            try:
                from ..content.asset_processor import process_images
                content.images = process_images(content.images, job.account_id)
            except Exception as e:
                # 处理失败不阻断发布，沿用原图——但事故必须可观测，不能闷声
                logger.warning(
                    "worker.image_anti_fingerprint: swallowed",
                    extra={"job_id": job.id, "account_id": job.account_id, "error": str(e)},
                )
                capture_exception(
                    e,
                    scope="worker.image_anti_fingerprint",
                    job_id=job.id,
                    account_id=job.account_id,
                )

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
            except Exception as e:
                # 采集失败不影响主流程——但必须留观测痕迹，否则飞轮长期断掉无人知
                logger.warning(
                    "worker.schedule_metrics: swallowed",
                    extra={"job_id": job.id, "error": str(e)},
                )
                capture_exception(e, scope="worker.schedule_metrics", job_id=job.id)
            # 通知模块快照（Task B）：在 session 内拼好数据，出块后再发——
            # 避免 notify 调用失败/慢回写影响 job 状态落库
            notify_snapshot = {
                "kind": "success",
                "id": job.id,
                "account_id": job.account_id,
                "platform": job.platform,
                "platform_url": job.platform_url,
                "title": (article.title if article else "（无标题）"),
            }
        else:
            job.error = result.error or "unknown"
            job.raw_response = result.raw_response
            if job.attempts < job.max_attempts:
                job.status = JobStatus.RETRYING
            else:
                job.status = JobStatus.DEAD
                # 失败联动：先降级到 DEGRADED；近 24h 内连续 3 次 DEAD → 升级到 BANNED
                _escalate_health_on_failure(s, job.account_id)
            # 通知模块快照（Task B）：失败也快照，session 外调 notify.publish_failed
            notify_snapshot = {
                "kind": "failed",
                "id": job.id,
                "account_id": job.account_id,
                "platform": job.platform,
                "error": job.error,
            }

    # 出 session 后异步通知——session_scope 已 commit，notify 异常不会回滚 job 状态
    try:
        from ..notify import publish_success, publish_failed
        if notify_snapshot["kind"] == "success":
            publish_success(notify_snapshot)
        else:
            publish_failed(notify_snapshot)
    except Exception as e:
        # 通知是辅助通道，任何异常都不能影响主业务返回值——
        # 但通知静默失败 = 运营群再也收不到消息，必须 capture 让 Sentry 兜底告警
        logger.warning(
            "worker.notify: swallowed",
            extra={
                "job_id": job_id,
                "kind": notify_snapshot.get("kind"),
                "error": str(e),
            },
        )
        capture_exception(
            e,
            scope="worker.notify",
            job_id=job_id,
            kind=notify_snapshot.get("kind"),
        )

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


def _pre_publish_check(
    session,
    job: PublishJob,
    article: Article,
    *,
    similarity_checker=None,
) -> tuple[bool, str | None]:
    """发布前置内容兜底：TAINT 词 grep + simhash 查重。

    Args:
        session: SQLAlchemy session（worker 已持有；这里不开新连接）。当前 TAINT 检查
            只读 article.body，simhash 通过 similarity_checker 走（默认调
            ``core.dedup.is_too_similar``，内部自带 session_scope）。
        job: PublishJob，提供 account_id 作为 simhash 查重的 scope key。
        article: Article，提供 body 作为待检测文本。
        similarity_checker: 可注入的相似度检测函数（签名同 is_too_similar），
            主要给单测注入 mock 用；生产路径默认 = is_too_similar。

    Returns:
        (ok, error_message)：ok=False 时 error_message 给 worker 写入 job.error。

    职责单一：只判断"能不能发"，不动 job / article 任何字段——状态机由调用方处理。
    """
    body = (article.body or "")

    # TAINT 词 grep：命中第一个即返回，避免拼接所有命中浪费日志位
    for pattern in TAINT_PATTERNS:
        if pattern in body:
            return False, f"污点拦截: 正文含 {pattern}"

    # simhash 查重：空 body 直接放行（不报错，让下游自己决定要不要发空内容）
    if not body.strip():
        return True, None

    checker = similarity_checker if similarity_checker is not None else is_too_similar
    try:
        too_similar = checker(
            text=body,
            account_id=job.account_id,
            days=SIMHASH_LOOKBACK_DAYS,
            threshold=SIMHASH_HAMMING_THRESHOLD,
        )
    except Exception as e:
        # 查重失败不阻断主流程：宁可发出去也不要因为 dedup bug 卡住运营节奏
        # （生产路径用 is_too_similar 内部已 try 兜底；这里再加一层防御）
        return True, None  # 静默放行，错误已被吞——下个 sprint 接入观测后再考虑改 hard-fail
    if too_similar:
        return False, (
            f"simhash 重复: 与账号 {job.account_id} 近 "
            f"{SIMHASH_LOOKBACK_DAYS}d 已发布内容相似度过高"
            f"（hamming < {SIMHASH_HAMMING_THRESHOLD}）"
        )

    return True, None
