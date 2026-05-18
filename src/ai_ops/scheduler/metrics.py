"""数据回流采集 + 调度。

闭环：发布成功 → 调度 3 次采集（1h/24h/7d）→ 写 Metrics 表 → 触发热度重算。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..accounts.manager import get_credential
from ..core.db import session_scope
from ..core.enums import Platform
from ..core.models import Article, Metrics, PublishJob
from ..publishers.registry import default_registry
from ..observability import get_logger
from ..observability.sentry import capture_exception
from .queue import queue

logger = get_logger(__name__)


# 发布后采集时间点
DEFAULT_INTERVALS_SECONDS = (3600, 86400, 604800)  # 1h / 24h / 7d


async def collect_one(job_id: int) -> dict:
    """采集单个 job 的最新数据，写 Metrics 表，触发热度重算。"""
    with session_scope() as s:
        job = s.get(PublishJob, job_id)
        if job is None or not job.platform_post_id:
            return {"skipped": True, "reason": "job 不存在或没有 platform_post_id"}

        try:
            credential = get_credential(s, job.account_id)
        except ValueError:
            return {"skipped": True, "reason": "凭证缺失"}

        platform = Platform(job.platform)
        publishers = default_registry.resolve(platform)
        if not publishers:
            return {"skipped": True, "reason": f"无 {platform} publisher"}
        publisher = publishers[0]
        post_id = job.platform_post_id
        post_url = job.platform_url
        article_id = job.article_id

    # 跳出事务调外部接口
    data = await publisher.collect_metrics(post_id, post_url, credential)

    with session_scope() as s:
        m = Metrics(
            job_id=job_id,
            likes=data.get("likes", 0),
            comments=data.get("comments", 0),
            shares=data.get("shares", 0),
            views=data.get("views", 0),
            raw=data.get("raw", {}),
        )
        s.add(m)
        s.flush()
        # 触发节点判定：用"真实时间窗"代替"metric 计数"——
        # TD-Z3 后 Metrics 表会有 initial 行（≈ job.finished_at，publish 完成瞬间落库），
        # 飞轮采集（1h/24h/7d）的 collected_at 是飞轮真正跑的时刻；
        # 取 finished_at + 30min 做 cutoff，只数飞轮采集、排除 initial 行——
        # 30min 阈值兼顾调度抖动（1h 飞轮可能 ±10 分钟跑）；
        # 否则 publish + 1h 就会被误判为 24h 节点，刚发 1h 数据没起来就误降权 / 暂停账号。
        job = s.get(PublishJob, job_id)
        job_anchor = (job.finished_at or job.created_at) if job is not None else None
        if job_anchor is not None:
            cutoff = job_anchor + timedelta(minutes=30)
            metric_count = (
                s.query(Metrics)
                .filter(Metrics.job_id == job_id, Metrics.collected_at > cutoff)
                .count()
            )
        else:
            # 极端兜底：job 被并发删 / 时间字段全空。退回旧行为不阻塞主流程；
            # 这条路径理论不可达（能跑到 collect_one 的 job 都已 finished）。
            metric_count = (
                s.query(Metrics).filter(Metrics.job_id == job_id).count()
            )

    # 24h 节点：触发健康度评估（曝光异常 → 降级 + 暂停）
    if metric_count == 2:
        try:
            from ..accounts.health_monitor import evaluate_after_metrics
            with session_scope() as s2:
                action = evaluate_after_metrics(s2, job_id)
                data["health_action"] = {
                    "decision": action.decision,
                    "reason": action.reason,
                }
        except Exception as e:
            # 健康评估失败不影响采集主流程——但 24h 节点降级逻辑长期失效会让风控判
            # 断慢半拍，必须 capture 让 Sentry 兜底告警
            logger.warning(
                "scheduler.metrics.health_eval: swallowed",
                extra={"job_id": job_id, "error": str(e)},
            )
            capture_exception(e, scope="scheduler.metrics.health_eval", job_id=job_id)

    # 异步刷新主题热度（fire and forget）
    try:
        from ..content.heat_engine import recompute_topic_heat_for_article
        recompute_topic_heat_for_article(article_id)
    except Exception as e:
        # 热度刷新失败不影响采集主路径——但飞轮上的内容选题环节会拿到旧热度，
        # 选题质量长期劣化无人察觉。必须 capture
        logger.warning(
            "scheduler.metrics.heat_refresh: swallowed",
            extra={"job_id": job_id, "article_id": article_id, "error": str(e)},
        )
        capture_exception(
            e,
            scope="scheduler.metrics.heat_refresh",
            job_id=job_id,
            article_id=article_id,
        )

    return data


def schedule_after_publish(
    job_id: int,
    intervals: tuple[int, ...] = DEFAULT_INTERVALS_SECONDS,
) -> list[str]:
    """发布成功后调度 N 次采集任务。返回 scheduler job ids。"""
    import asyncio

    ids = []
    for delay in intervals:
        when = datetime.utcnow() + timedelta(seconds=delay)
        sid = queue.schedule_once(
            when,
            (lambda jid=job_id: asyncio.create_task(collect_one(jid))),
            job_id=f"metrics-{job_id}-{delay}",
        )
        ids.append(sid)
    return ids
