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

# 24h 健康度评估触发节点对应的 interval index（与 DEFAULT_INTERVALS_SECONDS 对齐）。
# TD-P0-debt2：上一轮 P0 把"24h 节点"从裸 `metric_count == 2` 改成"cutoff + count",
# 解决了 P0 但仍隐含"第 2 个飞轮节点 = 24h"。如未来给 DEFAULT_INTERVALS_SECONDS 加
# 30min 实时档位（如 (1800, 3600, 86400, 604800)），第 2 个就是 1h 节点了——P0 再次触发。
# 把判定升级成显式 interval_index，配合此常量解耦：
#   - 改飞轮档位时，记得同时更新这两个常量（test_health_eval_interval_index_constant_exists 守护）
#   - DEFAULT_INTERVALS_SECONDS[HEALTH_EVAL_INTERVAL_INDEX] 必须语义上等于 24h（86400）
HEALTH_EVAL_INTERVAL_INDEX = 1


async def collect_one(
    job_id: int,
    *,
    interval_index: int | None = None,
    source: str = "scheduled",
) -> dict:
    """采集单个 job 的最新数据，写 Metrics 表，触发热度重算。

    Parameters
    ----------
    job_id : int
        要采集的 PublishJob.id
    interval_index : int | None, keyword-only
        - None（默认）：手动触发 / 不知道是哪一档飞轮，走 source-based / cutoff 兜底路径。
          兼容 api/main.py 的手动触发、observability 测试桩、上 sprint P0 守护测试。
        - int：第 N 档飞轮（0-indexed，与 DEFAULT_INTERVALS_SECONDS 对齐）。
          触发判定改为 `interval_index == HEALTH_EVAL_INTERVAL_INDEX` 显式比对，
          跳过 cutoff 查询（既省一次 SQL 也让"显式=不依赖时间"语义干净）。
    source : str, keyword-only
        - "scheduled"（默认）：飞轮 schedule_after_publish 回调路径
        - "manual"：API /jobs/{id}/collect 端点手动触发（API 调用方显式传）
        - 写入 Metrics.source 字段，让 24h 触发判定能基于 source 计数排除非飞轮行
          （Round 6 / TD-Z3-followup-2 / TD-P0-debt2）。

    keyword-only 防误传：避免后续加参数时位置漂移导致悄悄 break。
    """
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
            # Round 6：source 由调用方决定。飞轮回调默认 "scheduled"；API 手动触发传 "manual"。
            source=source,
        )
        s.add(m)
        s.flush()

        # 触发节点判定 — 三段优先级（Round 6 / TD-Z3-followup-2 / TD-P0-debt2）：
        #
        # 优先级 1：显式 interval_index（飞轮调度路径，最稳）
        #   schedule_after_publish 回调直接告诉我们"我是第几档"，不需要回表数 metric。
        #
        # 优先级 2：source-based scheduled count（owner 终态判定）
        #   表里至少有 1 条非 "scheduled" 的 metric（说明 initial / manual 写入已生效），
        #   直接按 source='scheduled' 计数：count == HEALTH_EVAL_INTERVAL_INDEX + 1 触发
        #   （+1 因为本次 collect_one 写的 scheduled 行已 flush 进库，含当前）。
        #   这是 owner 设计的终态——任何后续给 Metrics 加写入入口（backfill / external API）
        #   都不污染触发判定，因为非飞轮入口都会标自己的 source。
        #
        # 优先级 3：cutoff + count 兜底（守护测试 + 生产 ALTER 瞬间）
        #   表里所有 metric source 都是 "scheduled"（默认 server_default 兜底状态）：
        #     - 守护测试场景：_seed_metric 不传 source → 默认 "scheduled"
        #     - 生产 ALTER 瞬间：老 initial 行被 server_default 一刀切标 "scheduled"
        #   这种状态下 source 区分尚未生效，降级到 TD-Z3-followup-A 的 cutoff + count 路径
        #   （cutoff=finished_at+30min 把"接近 finished_at"的老 initial 行排除掉）。
        #   新数据陆续写入（worker 落 initial / API 落 manual）后，自动过渡到优先级 2。
        if interval_index is not None:
            # 优先级 1：显式飞轮路径
            is_health_eval_node = interval_index == HEALTH_EVAL_INTERVAL_INDEX
        else:
            # 检查是否已有 source 区分（至少 1 条非 "scheduled" → 走优先级 2）
            from sqlalchemy import func, select
            non_scheduled_exists = s.scalar(
                select(func.count(Metrics.id))
                .where(Metrics.job_id == job_id, Metrics.source != "scheduled")
            ) or 0

            if non_scheduled_exists > 0:
                # 优先级 2：source-based scheduled count（owner 终态）
                scheduled_count = s.scalar(
                    select(func.count(Metrics.id))
                    .where(Metrics.job_id == job_id, Metrics.source == "scheduled")
                ) or 0
                # +1 因为本次 collect_one 写的 scheduled 行已 flush 进库，含当前
                is_health_eval_node = scheduled_count == HEALTH_EVAL_INTERVAL_INDEX + 1
            else:
                # 优先级 3：cutoff + count 兜底（守护测试 / 迁移瞬间）
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
                is_health_eval_node = metric_count == 2

    # 24h 节点：触发健康度评估（曝光异常 → 降级 + 暂停）
    if is_health_eval_node:
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
    """发布成功后调度 N 次采集任务。返回 scheduler job ids。

    每次回调把"我是第几档"（interval_index）显式传给 collect_one——
    让 24h 节点判定不再依赖"恰好第 2 条 metric 落库"这种隐式约定。

    闭包陷阱注意：for 内 lambda 必须用默认参数 early-binding 捕获 jid + i，
    否则 Python late-binding 会让 3 个 lambda 全部捕获最后一次的 (job_id, idx)。
    """
    import asyncio

    ids = []
    for idx, delay in enumerate(intervals):
        when = datetime.utcnow() + timedelta(seconds=delay)
        sid = queue.schedule_once(
            when,
            (lambda jid=job_id, i=idx: asyncio.create_task(collect_one(jid, interval_index=i))),
            job_id=f"metrics-{job_id}-{delay}",
        )
        ids.append(sid)
    return ids
