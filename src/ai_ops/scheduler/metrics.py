"""Durable post-publication metrics collection and feedback execution.

闭环：发布成功 → 持久化 1h/24h/7d 任务 → 租约执行 → 写 Metrics → 反馈健康度。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import secrets

from sqlalchemy import and_, case, or_, select, update

from ..accounts.manager import get_credential
from ..config import EXTERNAL_OPERATION_FINALIZE_MARGIN_SECONDS, settings
from ..core.db import session_scope
from ..core.db_clock import database_utc_now
from ..core.enums import JobStatus, MetricsTaskStatus, Platform
from ..core.models import AgentOperation, Metrics, MetricsCollectionTask, PublishJob
from ..core.time import as_utc_naive
from ..publishers.plugin_sdk import (
    PublisherPluginResolutionError,
    is_publisher_plugin_instance,
)
from ..publishers.registry import default_registry
from ..observability import get_logger
from ..observability.sentry import (
    capture_exception,
    redacted_external_exception,
    safe_exception_type,
)

logger = get_logger(__name__)


# 发布后采集时间点
DEFAULT_INTERVALS_SECONDS = (3600, 86400, 604800)  # 1h / 24h / 7d
# Maximum lateness before a current observation can no longer honestly
# represent its intended window: 1h→+1h, 24h→+6h, 7d→+24h.
DEFAULT_DEADLINE_GRACE_SECONDS = (3600, 21600, 86400)

# 24h 健康度评估触发节点对应的 interval index（与 DEFAULT_INTERVALS_SECONDS 对齐）。
# TD-P0-debt2：上一轮 P0 把"24h 节点"从裸 `metric_count == 2` 改成"cutoff + count",
# 解决了 P0 但仍隐含"第 2 个飞轮节点 = 24h"。如未来给 DEFAULT_INTERVALS_SECONDS 加
# 30min 实时档位（如 (1800, 3600, 86400, 604800)），第 2 个就是 1h 节点了——P0 再次触发。
# 把判定升级成显式 interval_index，配合此常量解耦：
#   - 改飞轮档位时，记得同时更新这两个常量（test_health_eval_interval_index_constant_exists 守护）
#   - DEFAULT_INTERVALS_SECONDS[HEALTH_EVAL_INTERVAL_INDEX] 必须语义上等于 24h（86400）
HEALTH_EVAL_INTERVAL_INDEX = 1


@dataclass(frozen=True, slots=True)
class MetricsTaskClaim:
    """Primitive ownership values safe to carry outside a DB session."""

    task_id: int
    job_id: int
    interval_index: int
    lease_token: str


def ensure_metrics_collection_tasks(
    session,
    job_id: int,
    *,
    anchor: datetime,
    intervals: tuple[int, ...] = DEFAULT_INTERVALS_SECONDS,
    deadline_graces: tuple[int, ...] = DEFAULT_DEADLINE_GRACE_SECONDS,
    max_attempts: int | None = None,
) -> list[MetricsCollectionTask]:
    """Create the immutable per-window ledger rows in the caller transaction.

    Publication finalization calls this with its existing session so the
    SUCCESS row and its collection intent commit atomically. Repeated calls are
    idempotent; an existing row with different immutable window data fails
    closed instead of silently moving approved evidence windows.
    """
    normalized_anchor = as_utc_naive(anchor)
    if normalized_anchor is None:
        raise ValueError("metrics task anchor is required")
    normalized_intervals = tuple(int(value) for value in intervals)
    normalized_graces = tuple(int(value) for value in deadline_graces)
    if (
        normalized_intervals != DEFAULT_INTERVALS_SECONDS
        or normalized_graces != DEFAULT_DEADLINE_GRACE_SECONDS
    ):
        raise ValueError("durable metrics tasks require the fixed 1h/24h/7d contract")
    bounded_attempts = int(
        max_attempts
        if max_attempts is not None
        else getattr(settings, "metrics_task_max_attempts", 5)
    )
    if not 1 <= bounded_attempts <= 20:
        raise ValueError("metrics task max_attempts must be between 1 and 20")

    existing = {
        task.interval_index: task
        for task in session.scalars(
            select(MetricsCollectionTask)
            .where(MetricsCollectionTask.job_id == job_id)
            .order_by(MetricsCollectionTask.interval_index.asc())
        ).all()
    }
    tasks: list[MetricsCollectionTask] = []
    for interval_index, window_seconds in enumerate(normalized_intervals):
        due_at = normalized_anchor + timedelta(seconds=window_seconds)
        collection_deadline_at = due_at + timedelta(seconds=normalized_graces[interval_index])
        task = existing.get(interval_index)
        if task is not None:
            if (
                task.window_seconds != window_seconds
                or task.due_at != due_at
                or task.collection_deadline_at != collection_deadline_at
            ):
                raise RuntimeError("metrics task immutable window mismatch")
            tasks.append(task)
            continue
        task = MetricsCollectionTask(
            job_id=job_id,
            interval_index=interval_index,
            window_seconds=window_seconds,
            due_at=due_at,
            collection_deadline_at=collection_deadline_at,
            next_attempt_at=due_at,
            status=MetricsTaskStatus.QUEUED,
            attempts=0,
            max_attempts=bounded_attempts,
        )
        session.add(task)
        tasks.append(task)
    session.flush()
    return tasks


def backfill_missing_metrics_collection_tasks(*, limit: int = 100) -> list[int]:
    """Boundedly repair successful legacy jobs that predate the durable ledger."""
    safe_limit = max(1, min(int(limit), 1000))
    missing_window = or_(
        *(
            ~select(MetricsCollectionTask.id)
            .where(
                MetricsCollectionTask.job_id == PublishJob.id,
                MetricsCollectionTask.interval_index == interval_index,
            )
            .exists()
            for interval_index in range(len(DEFAULT_INTERVALS_SECONDS))
        )
    )
    repaired: list[int] = []
    repair_time = datetime.utcnow()
    with session_scope() as session:
        quarantine_value = PublishJob.raw_response["metrics_task_backfill_quarantined"]
        if session.get_bind().dialect.name == "sqlite":
            # SQLite JSON booleans are integer 1/0 and do not cast malformed
            # strings while comparing.
            not_quarantined = quarantine_value.as_boolean().is_not(True)
        else:
            # PostgreSQL's ->> text extraction avoids a BOOLEAN cast. A custom
            # adapter value such as "oops" therefore cannot abort the scanner.
            not_quarantined = quarantine_value.as_string().is_distinct_from("true")
        rows = session.execute(
            select(PublishJob.id, PublishJob.finished_at)
            .where(
                PublishJob.status == JobStatus.SUCCESS,
                PublishJob.platform_post_id.is_not(None),
                PublishJob.finished_at.is_not(None),
                not_quarantined,
                missing_window,
            )
            .order_by(PublishJob.finished_at.asc(), PublishJob.id.asc())
            .limit(safe_limit)
        ).all()
        for job_id, finished_at in rows:
            try:
                # Isolate corrupt legacy rows and concurrent unique-key races:
                # one bad publication must not roll back every repair in this
                # bounded batch.
                with session.begin_nested():
                    tasks = ensure_metrics_collection_tasks(
                        session,
                        job_id,
                        anchor=finished_at,
                    )
                    for task in tasks:
                        if (
                            task.status == MetricsTaskStatus.QUEUED
                            and task.collection_deadline_at <= repair_time
                        ):
                            task.status = MetricsTaskStatus.FAILED
                            task.next_attempt_at = None
                            task.last_error = (
                                "metrics collection window expired before durable backfill"
                            )
                            task.finished_at = repair_time
                            task.updated_at = repair_time
            except Exception as exc:
                job = session.get(PublishJob, job_id)
                if job is not None:
                    raw_response = dict(job.raw_response or {})
                    raw_response["metrics_task_backfill_required"] = True
                    if (
                        isinstance(exc, RuntimeError)
                        and str(exc) == "metrics task immutable window mismatch"
                    ):
                        # This is structural corruption, not a transient race.
                        # Keep it out of the oldest-first page so newer legacy
                        # publications are not starved forever.
                        raw_response["metrics_task_backfill_quarantined"] = True
                    job.raw_response = raw_response
                logger.exception(
                    "durable metrics task backfill failed for one job",
                    extra={"job_id": job_id},
                )
                try:
                    capture_exception(
                        exc,
                        scope="scheduler.metrics.backfill",
                        job_id=job_id,
                    )
                except Exception:
                    logger.exception(
                        "durable metrics backfill failure could not be reported",
                        extra={"job_id": job_id},
                    )
                continue
            job = session.get(PublishJob, job_id)
            if job is not None and (job.raw_response or {}).get("metrics_task_backfill_required"):
                raw_response = dict(job.raw_response or {})
                raw_response.pop("metrics_task_backfill_required", None)
                raw_response.pop("metrics_task_backfill_quarantined", None)
                job.raw_response = raw_response
            repaired.append(job_id)
    return repaired


def get_due_metrics_collection_task_ids(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[int]:
    """Return queued or abandoned task IDs; the caller must still claim them."""
    cutoff = as_utc_naive(now) or datetime.utcnow()
    safe_limit = max(1, min(int(limit), 1000))
    eligible = or_(
        and_(
            MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
            MetricsCollectionTask.next_attempt_at <= cutoff,
            MetricsCollectionTask.collection_deadline_at > cutoff,
        ),
        and_(
            MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
            MetricsCollectionTask.lease_expires_at <= cutoff,
            MetricsCollectionTask.collection_deadline_at > cutoff,
        ),
    )
    due_order = case(
        (
            MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
            MetricsCollectionTask.next_attempt_at,
        ),
        else_=MetricsCollectionTask.lease_expires_at,
    )
    with session_scope() as session:
        return list(
            session.scalars(
                select(MetricsCollectionTask.id)
                .join(PublishJob, PublishJob.id == MetricsCollectionTask.job_id)
                .where(
                    PublishJob.status == JobStatus.SUCCESS,
                    MetricsCollectionTask.attempts < MetricsCollectionTask.max_attempts,
                    eligible,
                )
                .order_by(due_order.asc(), MetricsCollectionTask.id.asc())
                .limit(safe_limit)
            ).all()
        )


def reconcile_exhausted_metrics_collection_tasks(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[int]:
    """Make a final crashed attempt terminal once its lease has expired."""
    cutoff = as_utc_naive(now) or datetime.utcnow()
    safe_limit = max(1, min(int(limit), 1000))
    with session_scope() as session:
        rows = list(
            session.execute(
                select(
                    MetricsCollectionTask.id,
                    MetricsCollectionTask.collection_deadline_at,
                )
                .where(
                    MetricsCollectionTask.status.in_(
                        (MetricsTaskStatus.QUEUED, MetricsTaskStatus.CLAIMED)
                    ),
                    or_(
                        MetricsCollectionTask.collection_deadline_at <= cutoff,
                        and_(
                            MetricsCollectionTask.attempts >= MetricsCollectionTask.max_attempts,
                            or_(
                                and_(
                                    MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
                                    MetricsCollectionTask.next_attempt_at <= cutoff,
                                ),
                                and_(
                                    MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                                    MetricsCollectionTask.lease_expires_at <= cutoff,
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(MetricsCollectionTask.id.asc())
                .limit(safe_limit)
            ).all()
        )
        reconciled: list[int] = []
        for task_id, deadline_at in rows:
            deadline_expired = deadline_at <= cutoff
            last_error = (
                "metrics collection window expired before evidence was captured"
                if deadline_expired
                else "metrics owner lease expired after final attempt"
            )
            transitioned = session.execute(
                update(MetricsCollectionTask)
                .where(
                    MetricsCollectionTask.id == task_id,
                    MetricsCollectionTask.status.in_(
                        (MetricsTaskStatus.QUEUED, MetricsTaskStatus.CLAIMED)
                    ),
                    or_(
                        MetricsCollectionTask.collection_deadline_at <= cutoff,
                        and_(
                            MetricsCollectionTask.attempts >= MetricsCollectionTask.max_attempts,
                            or_(
                                and_(
                                    MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
                                    MetricsCollectionTask.next_attempt_at <= cutoff,
                                ),
                                and_(
                                    MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                                    MetricsCollectionTask.lease_expires_at <= cutoff,
                                ),
                            ),
                        ),
                    ),
                )
                .values(
                    status=MetricsTaskStatus.FAILED,
                    next_attempt_at=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=last_error,
                    finished_at=cutoff,
                    updated_at=cutoff,
                )
                .execution_options(synchronize_session=False)
            )
            if transitioned.rowcount == 1:
                reconciled.append(task_id)
    return reconciled


def _claim_metrics_collection_task(
    task_id: int,
    *,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> MetricsTaskClaim | None:
    claim_time = as_utc_naive(now) or datetime.utcnow()
    duration = int(
        lease_seconds
        if lease_seconds is not None
        else getattr(settings, "metrics_task_lease_seconds", 300)
    )
    lease_token = secrets.token_hex(32)
    lease_expires_at = claim_time + timedelta(seconds=max(2, duration))
    eligible = or_(
        and_(
            MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
            MetricsCollectionTask.next_attempt_at <= claim_time,
            MetricsCollectionTask.collection_deadline_at > claim_time,
        ),
        and_(
            MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
            MetricsCollectionTask.lease_expires_at <= claim_time,
            MetricsCollectionTask.collection_deadline_at > claim_time,
        ),
    )
    with session_scope() as session:
        claimed = session.execute(
            update(MetricsCollectionTask)
            .where(
                MetricsCollectionTask.id == task_id,
                MetricsCollectionTask.attempts < MetricsCollectionTask.max_attempts,
                MetricsCollectionTask.collection_deadline_at > claim_time,
                eligible,
            )
            .values(
                status=MetricsTaskStatus.CLAIMED,
                next_attempt_at=None,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                started_at=claim_time,
                finished_at=None,
                updated_at=claim_time,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            return None
        row = session.execute(
            select(
                MetricsCollectionTask.job_id,
                MetricsCollectionTask.interval_index,
            ).where(MetricsCollectionTask.id == task_id)
        ).one()
        return MetricsTaskClaim(
            task_id=task_id,
            job_id=row.job_id,
            interval_index=row.interval_index,
            lease_token=lease_token,
        )


def _begin_metrics_collection_attempt(
    claim: MetricsTaskClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """Count an attempt immediately before the external collector is entered."""
    started = as_utc_naive(now) or datetime.utcnow()
    with session_scope() as session:
        ownership_clock = (
            started
            if now is not None
            else database_utc_now(
                session,
                after_seconds=(
                    int(getattr(settings, "metrics_task_collection_timeout_seconds", 120))
                    + EXTERNAL_OPERATION_FINALIZE_MARGIN_SECONDS
                ),
            )
        )
        updated = session.execute(
            update(MetricsCollectionTask)
            .where(
                MetricsCollectionTask.id == claim.task_id,
                MetricsCollectionTask.job_id == claim.job_id,
                MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                MetricsCollectionTask.lease_token == claim.lease_token,
                MetricsCollectionTask.lease_expires_at > ownership_clock,
                MetricsCollectionTask.collection_deadline_at > ownership_clock,
                MetricsCollectionTask.attempts < MetricsCollectionTask.max_attempts,
            )
            .values(
                attempts=MetricsCollectionTask.attempts + 1,
                updated_at=started,
            )
            .execution_options(synchronize_session=False)
        )
        return updated.rowcount == 1


def _metrics_task_retry_delay_seconds(attempts: int) -> int:
    base = max(1, int(getattr(settings, "metrics_task_retry_base_seconds", 300)))
    return min(base * (2 ** max(0, int(attempts) - 1)), 6 * 60 * 60)


def _defer_unclaimed_metrics_collection_task(
    task_id: int,
    reason: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Move a still-due, unowned task out of the scanner's hot prefix.

    The account/profile lease is acquired before the durable task claim.  If
    that local resource is busy, leaving the row at its original due timestamp
    makes the same first ``limit`` rows win every subsequent scan and can
    starve unrelated accounts forever.  This CAS defers only a still-queued row
    (or an already-expired owner) and deliberately consumes no collection
    attempt.
    """
    transition_time = as_utc_naive(now) or datetime.utcnow()
    safe_reason = str(reason).strip()[:1000] or "account operation lease is unavailable"
    with session_scope() as session:
        row = session.execute(
            select(
                MetricsCollectionTask.status,
                MetricsCollectionTask.attempts,
                MetricsCollectionTask.max_attempts,
                MetricsCollectionTask.next_attempt_at,
                MetricsCollectionTask.lease_token,
                MetricsCollectionTask.lease_expires_at,
                MetricsCollectionTask.collection_deadline_at,
            ).where(MetricsCollectionTask.id == task_id)
        ).one_or_none()
        if row is None or row.attempts >= row.max_attempts:
            return False
        if row.collection_deadline_at <= transition_time:
            return False

        if row.status == MetricsTaskStatus.QUEUED:
            if row.next_attempt_at is None or row.next_attempt_at > transition_time:
                return False
            owner_predicate = and_(
                MetricsCollectionTask.status == MetricsTaskStatus.QUEUED,
                MetricsCollectionTask.next_attempt_at == row.next_attempt_at,
                MetricsCollectionTask.lease_token.is_(None),
                MetricsCollectionTask.lease_expires_at.is_(None),
            )
        elif row.status == MetricsTaskStatus.CLAIMED:
            if row.lease_expires_at is None or row.lease_expires_at > transition_time:
                return False
            owner_predicate = and_(
                MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                MetricsCollectionTask.lease_token == row.lease_token,
                MetricsCollectionTask.lease_expires_at == row.lease_expires_at,
            )
        else:
            return False

        desired_retry_at = transition_time + timedelta(
            seconds=_metrics_task_retry_delay_seconds(row.attempts)
        )
        if desired_retry_at >= row.collection_deadline_at:
            # Preserve one final opportunity inside the immutable evidence
            # window without creating an immediate hot-loop near its boundary.
            desired_retry_at = transition_time + (
                (row.collection_deadline_at - transition_time) / 2
            )
        if desired_retry_at <= transition_time:
            return False

        deferred = session.execute(
            update(MetricsCollectionTask)
            .where(
                MetricsCollectionTask.id == task_id,
                MetricsCollectionTask.attempts == row.attempts,
                MetricsCollectionTask.max_attempts == row.max_attempts,
                MetricsCollectionTask.collection_deadline_at == row.collection_deadline_at,
                MetricsCollectionTask.collection_deadline_at > transition_time,
                owner_predicate,
            )
            .values(
                status=MetricsTaskStatus.QUEUED,
                next_attempt_at=desired_retry_at,
                lease_token=None,
                lease_expires_at=None,
                last_error=safe_reason,
                finished_at=None,
                updated_at=transition_time,
            )
            .execution_options(synchronize_session=False)
        )
        return deferred.rowcount == 1


def _retry_or_fail_metrics_collection_task(
    claim: MetricsTaskClaim,
    reason: str,
    *,
    now: datetime | None = None,
    terminal: bool = False,
) -> MetricsTaskStatus | None:
    """Release only a still-current, unexpired owner; stale owners are inert."""
    safe_reason = str(reason).strip()[:1000] or "metrics collection unavailable"
    with session_scope() as session:
        row = session.execute(
            select(
                MetricsCollectionTask.attempts,
                MetricsCollectionTask.max_attempts,
                MetricsCollectionTask.collection_deadline_at,
            ).where(
                MetricsCollectionTask.id == claim.task_id,
                MetricsCollectionTask.job_id == claim.job_id,
            )
        ).one_or_none()
        if row is None:
            return None
        transition_time = as_utc_naive(now) or datetime.utcnow()
        retry_at = transition_time + timedelta(
            seconds=_metrics_task_retry_delay_seconds(row.attempts)
        )
        should_fail = (
            terminal
            or row.attempts >= row.max_attempts
            or transition_time >= row.collection_deadline_at
            or retry_at >= row.collection_deadline_at
        )
        next_status = MetricsTaskStatus.FAILED if should_fail else MetricsTaskStatus.QUEUED
        ownership_clock = transition_time if now is not None else database_utc_now(session)
        transitioned = session.execute(
            update(MetricsCollectionTask)
            .where(
                MetricsCollectionTask.id == claim.task_id,
                MetricsCollectionTask.job_id == claim.job_id,
                MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                MetricsCollectionTask.lease_token == claim.lease_token,
                MetricsCollectionTask.lease_expires_at > ownership_clock,
                MetricsCollectionTask.attempts == row.attempts,
                MetricsCollectionTask.max_attempts == row.max_attempts,
                MetricsCollectionTask.collection_deadline_at == row.collection_deadline_at,
            )
            .values(
                status=next_status,
                next_attempt_at=None if should_fail else retry_at,
                lease_token=None,
                lease_expires_at=None,
                last_error=safe_reason,
                finished_at=transition_time if should_fail else None,
                updated_at=transition_time,
            )
            .execution_options(synchronize_session=False)
        )
        return next_status if transitioned.rowcount == 1 else None


def _collection_skip_reason(data: object) -> str | None:
    """Recognize an unavailable collection without converting it to zeroes."""
    if not isinstance(data, dict):
        return "collector 返回了无效结果"
    if data.get("skipped"):
        # Adapter-provided text may contain argv, stderr, profile paths, or
        # credential fragments.  It is not safe for the durable task ledger.
        return "collector 跳过采集"

    raw = data.get("raw")
    if isinstance(raw, dict):
        if raw.get("error"):
            return "collector 报告采集错误"
        if raw.get("not_found"):
            return "collector 未找到目标内容"
        if raw.get("http_status") not in (None, 200):
            return f"collector HTTP 状态 {raw['http_status']}"

    required_counts = {"likes", "comments", "shares", "views"}
    if not required_counts.issubset(data):
        return "collector 缺少标准指标字段"
    for field in required_counts:
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int):
            return "collector 返回了无效计数"
        if not 0 <= value <= 2_147_483_647:
            return "collector 返回了越界计数"
    return None


def _project_plugin_metrics(data: object) -> object:
    """Keep only normalized counters from an in-process third-party adapter."""

    # Require an exact dict so a hostile mapping subclass cannot run custom
    # accessors while this trust-boundary projection reads the four fields.
    if type(data) is not dict:
        return None
    return {
        "likes": data.get("likes"),
        "comments": data.get("comments"),
        "shares": data.get("shares"),
        "views": data.get("views"),
        "raw": {},
    }


async def collect_one(
    job_id: int,
    *,
    interval_index: int | None = None,
    source: str = "scheduled",
    agent_operation_id: int | None = None,
    agent_operation_lease_token: str | None = None,
    collection_task_id: int | None = None,
    collection_task_lease_token: str | None = None,
    account_lease_held: bool = False,
    expected_account_id: int | None = None,
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
        - "scheduled"（默认）：持久任务扫描器执行路径
        - "manual"：API /jobs/{id}/collect 端点手动触发（API 调用方显式传）
        - 写入 Metrics.source 字段，让 24h 触发判定能基于 source 计数排除非飞轮行
          （Round 6 / TD-Z3-followup-2 / TD-P0-debt2）。
    agent_operation_id : int | None, keyword-only
        v1 手动回采的持久幂等操作 ID。存在时，Metrics 行与该操作唯一绑定；
        崩溃恢复会复用已落库快照，不重复访问外部 collector。
    agent_operation_lease_token : str | None, keyword-only
        与 ``agent_operation_id`` 配对的当前 owner token。指标落库前用条件
        UPDATE 获取行锁并校验未过期 ownership，阻止 stale owner 越权写入。
    collection_task_id / collection_task_lease_token : optional pair
        持久化 1h/24h/7d task owner。成功快照与 task 状态在同一事务提交。
    account_lease_held : bool, keyword-only
        Durable runner 已在 claim 前获取账号 profile 锁时传 True；其它入口由
        本函数获取同一跨进程锁，避免 metrics 与 publish/health 并发碰 profile。

    keyword-only 防误传：避免后续加参数时位置漂移导致悄悄 break。
    """
    if (agent_operation_id is None) != (agent_operation_lease_token is None):
        return {"skipped": True, "reason": "Agent metrics operation lease is incomplete"}
    if (collection_task_id is None) != (collection_task_lease_token is None):
        return {"skipped": True, "reason": "Scheduled metrics task lease is incomplete"}
    if agent_operation_id is not None and collection_task_id is not None:
        return {"skipped": True, "reason": "Metrics collection has multiple ledger owners"}
    if collection_task_id is not None and (source != "scheduled" or interval_index is None):
        return {"skipped": True, "reason": "Scheduled metrics task binding is invalid"}

    collection_window_seconds: int | None = None
    with session_scope() as s:
        if agent_operation_id is not None:
            existing = s.scalar(
                select(Metrics).where(Metrics.agent_operation_id == agent_operation_id)
            )
            if existing is not None:
                if existing.job_id != job_id:
                    return {
                        "skipped": True,
                        "reason": "Agent metrics operation is bound to a different job",
                    }
                return {
                    "likes": existing.likes,
                    "comments": existing.comments,
                    "shares": existing.shares,
                    "views": existing.views,
                    "raw": existing.raw,
                    "replayed_from_ledger": True,
                }
        if collection_task_id is not None:
            existing = s.scalar(
                select(Metrics).where(Metrics.collection_task_id == collection_task_id)
            )
            if existing is not None:
                if existing.job_id != job_id:
                    return {
                        "skipped": True,
                        "reason": "Scheduled metrics task is bound to a different job",
                        "retryable": False,
                    }
                return {
                    "likes": existing.likes,
                    "comments": existing.comments,
                    "shares": existing.shares,
                    "views": existing.views,
                    "raw": existing.raw,
                    "replayed_from_task_ledger": True,
                }
            task = s.get(MetricsCollectionTask, collection_task_id)
            task_now = datetime.utcnow()
            if task is None or task.job_id != job_id:
                return {
                    "skipped": True,
                    "reason": "Scheduled metrics task is bound to a different job",
                    "retryable": False,
                }
            if task.interval_index != interval_index:
                return {
                    "skipped": True,
                    "reason": "Scheduled metrics task window binding changed",
                    "retryable": False,
                }
            if (
                task.status != MetricsTaskStatus.CLAIMED
                or task.lease_token != collection_task_lease_token
                or task.lease_expires_at is None
                or task.lease_expires_at <= task_now
                or task.collection_deadline_at <= task_now
            ):
                return {"skipped": True, "reason": "Scheduled metrics task lease was lost"}
            collection_window_seconds = task.window_seconds
        job = s.get(PublishJob, job_id)
        if job is None or not job.platform_post_id:
            return {
                "skipped": True,
                "reason": "job 不存在或没有 platform_post_id",
                "retryable": False,
            }
        if (
            account_lease_held
            and expected_account_id is not None
            and expected_account_id != job.account_id
        ):
            return {
                "skipped": True,
                "reason": "Scheduled metrics task account binding changed",
            }

        platform = Platform(job.platform)
        publisher_kind = (job.publisher_kind or "").strip()
        try:
            publisher = default_registry.resolve_collector(platform, publisher_kind)
        except PublisherPluginResolutionError:
            return {
                "skipped": True,
                "reason": "Publisher plugin configuration is invalid",
                "error_code": "publisher_plugin_configuration_invalid",
                "publisher_kind": publisher_kind,
                "retryable": False,
            }
        if publisher is None:
            if publisher_kind:
                reason = f"publisher {publisher_kind} 不支持 metrics 采集"
            else:
                reason = f"无 {platform.value} 明确支持 metrics 的 publisher"
            return {
                "skipped": True,
                "reason": reason,
                "publisher_kind": publisher_kind,
                "retryable": False,
            }
        plugin_publisher = is_publisher_plugin_instance(publisher)

        try:
            credential = get_credential(s, job.account_id)
        except ValueError:
            return {"skipped": True, "reason": "凭证缺失"}

        post_id = job.platform_post_id
        post_url = job.platform_url
        article_id = job.article_id
        account_id = job.account_id

    async def invoke_collector() -> dict:
        if collection_task_id is not None and not _begin_metrics_collection_attempt(
            MetricsTaskClaim(
                task_id=collection_task_id,
                job_id=job_id,
                interval_index=interval_index,
                lease_token=collection_task_lease_token,
            )
        ):
            return {"skipped": True, "reason": "Scheduled metrics task lease was lost"}
        collection = publisher.collect_metrics(post_id, post_url, credential)
        if agent_operation_id is not None:
            # AgentControlPlane owns the timeout and the UNAVAILABLE response.
            # Applying the scheduled-task timeout here would couple two
            # independent settings and change the public failure semantics.
            return await collection
        timeout_setting = (
            "metrics_task_collection_timeout_seconds"
            if collection_task_id is not None
            else "agent_metrics_collection_timeout_seconds"
        )
        return await asyncio.wait_for(
            collection,
            timeout=float(getattr(settings, timeout_setting, 120)),
        )

    # 跳出事务调外部接口；所有会共享 cookie/profile 的入口使用同一账号锁。
    try:
        if account_lease_held:
            data = await invoke_collector()
        else:
            from ..runtime.account_lease import (
                AccountOperationLease,
                AccountOperationLeaseTimeout,
            )

            try:
                async with AccountOperationLease(
                    account_id,
                    timeout_seconds=float(
                        getattr(settings, "account_operation_lock_timeout_seconds", 120)
                    ),
                ):
                    data = await invoke_collector()
            except AccountOperationLeaseTimeout:
                return {
                    "skipped": True,
                    "reason": "账号 profile 正被其他操作占用，稍后重试",
                }
    except TimeoutError:
        return {
            "skipped": True,
            "reason": "metrics collector timed out",
            "publisher_kind": publisher_kind,
        }
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and not plugin_publisher:
            raise
        # A collector exception is missing evidence, not evidence of zero
        # engagement. Keep it out of Metrics and account-health evaluation.
        exception_type = safe_exception_type(exc)
        logger.warning(
            "metrics collector failed; snapshot skipped",
            extra={
                "job_id": job_id,
                "publisher_kind": publisher_kind,
                "exception_type": exception_type,
            },
        )
        try:
            capture_exception(
                redacted_external_exception(exc),
                scope="metrics.collect_one",
                job_id=job_id,
                publisher_kind=publisher_kind,
                exception_type=exception_type,
            )
        except Exception:
            logger.exception(
                "metrics collector failure could not be reported",
                extra={"job_id": job_id, "publisher_kind": publisher_kind},
            )
        return {
            "skipped": True,
            "reason": f"collector 执行失败（{exception_type}）",
            "publisher_kind": publisher_kind,
        }
    if plugin_publisher:
        # Freeze plugin identity before the await and project before reading any
        # adapter-provided skip/raw metadata. The plugin may mutate its own
        # marker during collection; that cannot reopen the durable data path.
        data = _project_plugin_metrics(data)
    collection_skip_reason = _collection_skip_reason(data)
    if collection_skip_reason is not None:
        contract_errors = {
            "collector 返回了无效结果",
            "collector 缺少标准指标字段",
            "collector 返回了无效计数",
            "collector 返回了越界计数",
        }
        skipped_result = {
            "skipped": True,
            "reason": collection_skip_reason,
            "publisher_kind": publisher_kind,
        }
        if collection_skip_reason in contract_errors:
            skipped_result["retryable"] = False
        return skipped_result

    with session_scope() as s:
        collected_at = datetime.utcnow()
        if agent_operation_id is not None:
            fenced = s.execute(
                update(AgentOperation)
                .where(
                    AgentOperation.id == agent_operation_id,
                    AgentOperation.operation == "collect_metrics",
                    AgentOperation.lease_token == agent_operation_lease_token,
                    AgentOperation.lease_expires_at > database_utc_now(s),
                    AgentOperation.response_json.is_(None),
                )
                # A no-op value still obtains the database row/write lock until
                # the Metrics insert commits in this same transaction.
                .values(updated_at=AgentOperation.updated_at)
                .execution_options(synchronize_session=False)
            )
            if fenced.rowcount != 1:
                return {"skipped": True, "reason": "Agent metrics operation lease was lost"}
        if collection_task_id is not None:
            existing = s.scalar(
                select(Metrics).where(Metrics.collection_task_id == collection_task_id)
            )
            if existing is not None:
                if existing.job_id != job_id:
                    return {
                        "skipped": True,
                        "reason": "Scheduled metrics task is bound to a different job",
                        "retryable": False,
                    }
                return {
                    "likes": existing.likes,
                    "comments": existing.comments,
                    "shares": existing.shares,
                    "views": existing.views,
                    "raw": existing.raw,
                    "replayed_from_task_ledger": True,
                }
            fenced_task = s.execute(
                update(MetricsCollectionTask)
                .where(
                    MetricsCollectionTask.id == collection_task_id,
                    MetricsCollectionTask.job_id == job_id,
                    MetricsCollectionTask.interval_index == interval_index,
                    MetricsCollectionTask.status == MetricsTaskStatus.CLAIMED,
                    MetricsCollectionTask.lease_token == collection_task_lease_token,
                    MetricsCollectionTask.lease_expires_at > database_utc_now(s),
                    MetricsCollectionTask.collection_deadline_at > database_utc_now(s),
                    MetricsCollectionTask.attempts > 0,
                )
                .values(
                    status=MetricsTaskStatus.SUCCEEDED,
                    next_attempt_at=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                    finished_at=collected_at,
                    updated_at=collected_at,
                )
                .execution_options(synchronize_session=False)
            )
            if fenced_task.rowcount != 1:
                replay = s.scalar(
                    select(Metrics).where(Metrics.collection_task_id == collection_task_id)
                )
                if replay is not None and replay.job_id == job_id:
                    return {
                        "likes": replay.likes,
                        "comments": replay.comments,
                        "shares": replay.shares,
                        "views": replay.views,
                        "raw": replay.raw,
                        "replayed_from_task_ledger": True,
                    }
                return {"skipped": True, "reason": "Scheduled metrics task lease was lost"}
        m = Metrics(
            job_id=job_id,
            agent_operation_id=agent_operation_id,
            collection_task_id=collection_task_id,
            collected_at=collected_at,
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
        if collection_window_seconds is not None:
            is_health_eval_node = collection_window_seconds == 86400
        elif interval_index is not None:
            # 优先级 1：显式飞轮路径
            is_health_eval_node = interval_index == HEALTH_EVAL_INTERVAL_INDEX
        else:
            # 检查是否已有 source 区分（至少 1 条非 "scheduled" → 走优先级 2）
            from sqlalchemy import func

            non_scheduled_exists = (
                s.scalar(
                    select(func.count(Metrics.id)).where(
                        Metrics.job_id == job_id, Metrics.source != "scheduled"
                    )
                )
                or 0
            )

            if non_scheduled_exists > 0:
                # 优先级 2：source-based scheduled count（owner 终态）
                scheduled_count = (
                    s.scalar(
                        select(func.count(Metrics.id)).where(
                            Metrics.job_id == job_id, Metrics.source == "scheduled"
                        )
                    )
                    or 0
                )
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
                    metric_count = s.query(Metrics).filter(Metrics.job_id == job_id).count()
                is_health_eval_node = metric_count == 2

        # For durable 24h tasks, the exact metric, account-health write, task
        # terminal state, and snapshot commit share one transaction.  Unlike
        # the legacy/manual compatibility path below, an evaluation failure is
        # deliberately retryable: swallowing it would leave a SUCCEEDED task
        # whose promised feedback was never applied.
        if collection_task_id is not None and is_health_eval_node:
            try:
                from ..accounts.health_monitor import evaluate_after_metrics

                action = evaluate_after_metrics(s, job_id, metric_id=m.id)
                data["health_action"] = {
                    "decision": action.decision,
                    "reason": action.reason,
                }
            except Exception as e:
                logger.warning(
                    "scheduler.metrics.health_eval: failed; transaction will retry",
                    extra={"job_id": job_id, "exception_type": type(e).__name__},
                )
                try:
                    capture_exception(e, scope="scheduler.metrics.health_eval", job_id=job_id)
                except Exception:
                    logger.exception(
                        "metrics health-eval failure could not be reported",
                        extra={"job_id": job_id},
                    )
                raise

    # 24h 节点：触发健康度评估（曝光异常 → 降级 + 暂停）
    if is_health_eval_node and collection_task_id is None:
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
                "scheduler.metrics.health_eval: failed",
                extra={"job_id": job_id, "exception_type": type(e).__name__},
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
            "scheduler.metrics.heat_refresh: failed",
            extra={
                "job_id": job_id,
                "article_id": article_id,
                "exception_type": type(e).__name__,
            },
        )
        capture_exception(
            e,
            scope="scheduler.metrics.heat_refresh",
            job_id=job_id,
            article_id=article_id,
        )

    return data


def _metrics_task_account_id(task_id: int) -> int | None:
    with session_scope() as session:
        return session.scalar(
            select(PublishJob.account_id)
            .join(
                MetricsCollectionTask,
                MetricsCollectionTask.job_id == PublishJob.id,
            )
            .where(MetricsCollectionTask.id == task_id)
        )


async def run_metrics_collection_task(task_id: int) -> dict:
    """Claim and execute one durable collection task with profile isolation."""
    account_id = _metrics_task_account_id(task_id)
    if account_id is None:
        return {"skipped": True, "reason": "metrics task does not exist"}

    from ..runtime.account_lease import AccountOperationLease, AccountOperationLeaseTimeout

    claim: MetricsTaskClaim | None = None
    result: dict | None = None
    try:
        async with AccountOperationLease(
            account_id,
            timeout_seconds=float(
                getattr(settings, "metrics_task_account_lock_timeout_seconds", 1)
            ),
        ):
            # Claim only after the shared profile is available. Waiting for a
            # publish/health operation therefore consumes neither an attempt nor
            # a task lease, and same-account backlog runs serially.
            claim = _claim_metrics_collection_task(task_id)
            if claim is None:
                return {"skipped": True, "reason": "metrics task is not claimable"}
            try:
                result = await collect_one(
                    claim.job_id,
                    interval_index=claim.interval_index,
                    source="scheduled",
                    collection_task_id=claim.task_id,
                    collection_task_lease_token=claim.lease_token,
                    account_lease_held=True,
                    expected_account_id=account_id,
                )
            except asyncio.CancelledError:
                try:
                    _retry_or_fail_metrics_collection_task(
                        claim,
                        "metrics collection was cancelled",
                    )
                except Exception:
                    # Cancellation must retain its identity. Lease expiry is the
                    # durable fallback if this best-effort release also fails.
                    logger.exception(
                        "cancelled metrics owner could not be requeued",
                        extra={"task_id": task_id, "job_id": claim.job_id},
                    )
                raise
            except Exception as exc:
                logger.exception(
                    "durable metrics task crashed",
                    extra={"task_id": task_id, "job_id": claim.job_id},
                )
                try:
                    capture_exception(
                        exc,
                        scope="scheduler.metrics.task",
                        task_id=task_id,
                        job_id=claim.job_id,
                    )
                except Exception:
                    logger.exception(
                        "durable metrics task failure could not be reported",
                        extra={"task_id": task_id},
                    )
                state = _retry_or_fail_metrics_collection_task(
                    claim,
                    f"collector execution failed ({type(exc).__name__})",
                )
                return {
                    "skipped": True,
                    "reason": "durable metrics task crashed",
                    "task_state": state.value if state is not None else "stale_owner",
                }
    except AccountOperationLeaseTimeout:
        deferred = _defer_unclaimed_metrics_collection_task(
            task_id,
            "account operation lease is busy",
        )
        return {
            "skipped": True,
            "reason": "account operation lease is busy",
            "task_state": (MetricsTaskStatus.QUEUED.value if deferred else "concurrent_owner"),
        }
    except OSError:
        if result is not None:
            # The task may already have committed before lock cleanup failed.
            # Preserve that result and let the post-processing below run.
            logger.warning(
                "account operation lease cleanup failed after metrics collection",
                extra={"task_id": task_id},
            )
        elif claim is not None:
            state = _retry_or_fail_metrics_collection_task(
                claim,
                "account operation lease cleanup failed",
            )
            return {
                "skipped": True,
                "reason": "account operation lease is unavailable",
                "task_state": state.value if state is not None else "stale_owner",
            }
        else:
            deferred = _defer_unclaimed_metrics_collection_task(
                task_id,
                "account operation lease is unavailable",
            )
            return {
                "skipped": True,
                "reason": "account operation lease is unavailable",
                "task_state": (MetricsTaskStatus.QUEUED.value if deferred else "concurrent_owner"),
            }

    if result is None:
        return {"skipped": True, "reason": "metrics collection produced no result"}
    if result.get("skipped"):
        reason = str(result.get("reason") or "metrics collection unavailable")
        if reason == "Scheduled metrics task lease was lost":
            return result
        state = _retry_or_fail_metrics_collection_task(
            claim,
            reason,
            terminal=result.get("retryable") is False,
        )
        result = dict(result)
        result["task_state"] = state.value if state is not None else "stale_owner"
        return result
    return result


async def scan_due_metrics_collection_tasks(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[int, dict]:
    """Repair, reconcile, and execute one bounded durable metrics batch."""
    safe_limit = max(1, min(int(limit), 1000))
    backfill_missing_metrics_collection_tasks(limit=safe_limit)
    reconcile_exhausted_metrics_collection_tasks(now=now, limit=safe_limit)
    task_ids = get_due_metrics_collection_task_ids(now=now, limit=safe_limit)
    if not task_ids:
        return {}

    concurrency = max(
        1,
        int(getattr(settings, "metrics_task_max_concurrency", 4)),
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(durable_task_id: int) -> tuple[int, dict]:
        async with semaphore:
            try:
                result = await run_metrics_collection_task(durable_task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One corrupt row, account-lock backend, or custom collector
                # integration must not prevent unrelated due tasks from being
                # attempted in the same bounded scan.
                logger.exception(
                    "durable metrics batch item crashed",
                    extra={"task_id": durable_task_id},
                )
                try:
                    capture_exception(
                        exc,
                        scope="scheduler.metrics.scan_item",
                        task_id=durable_task_id,
                    )
                except Exception:
                    logger.exception(
                        "durable metrics batch failure could not be reported",
                        extra={"task_id": durable_task_id},
                    )
                result = {
                    "skipped": True,
                    "reason": "durable metrics task crashed",
                    "task_state": "runner_error",
                }
            return durable_task_id, result

    pairs = await asyncio.gather(*(run_one(task_id) for task_id in task_ids))
    return dict(pairs)


def schedule_after_publish(
    job_id: int,
    intervals: tuple[int, ...] = DEFAULT_INTERVALS_SECONDS,
) -> list[str]:
    """Return durable task IDs; retained as a non-scheduling compatibility API.

    APScheduler date callbacks are intentionally gone. The database scanner is
    the only executor, so worker restart cannot lose collection intent and
    concurrent owners cannot commit duplicate snapshots. An external read can
    still repeat after a crash between the read and its database commit.
    """
    if tuple(intervals) != DEFAULT_INTERVALS_SECONDS:
        raise ValueError("durable metrics tasks require the fixed 1h/24h/7d contract")
    with session_scope() as session:
        task_ids = session.scalars(
            select(MetricsCollectionTask.id)
            .where(MetricsCollectionTask.job_id == job_id)
            .order_by(MetricsCollectionTask.interval_index.asc())
        ).all()
    return [f"metrics-task-{task_id}" for task_id in task_ids]
