"""发布任务执行器。

职责：拉取 PublishJob → 解密凭证 → 通过 registry 拿 Publisher → 调 publish →
落库结果（成功/失败/重试）→ 触发数据采集。

注意：发布器列表有优先级，fallback 自动切换。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Iterator

from sqlalchemy import and_, or_, select, update

from ..agent_contract.bindings import account_binding_digest
from ..agent_contract.assets import (
    AssetVaultError,
    VaultedAsset,
    copy_verified_vaulted_asset,
)
from ..agent_contract.digest import canonical_sha256, plan_digest
from ..agent_contract.schemas import PublicationTarget
from ..agent_contract.snapshot import (
    parse_stored_content_snapshot,
    publish_content_from_snapshot,
    stored_content_digest,
    validate_stored_content_total,
)
from ..accounts.health_monitor import (
    BAN_PAUSE_HOURS,
    get_paused_until,
    is_paused,
    pause_account,
)
from ..accounts.manager import check_rate_limit, get_credential, mark_published, update_health
from ..core.db import session_scope
from ..core.dedup import is_too_similar
from ..core.enums import AccountHealth, ArticleStatus, AssetType, ContentType, JobStatus, Platform
from ..core.external_identity import normalize_zhihu_external_account_id
from ..core.models import Account, Article, Metrics, PublicationPlan, PublishJob
from ..core.schemas import ApprovedAssetExecution, PublishContent, PublishResult
from ..core.time import as_utc_naive
from ..config import settings
from ..observability import get_logger
from ..observability.sentry import capture_exception
from ..publishers.registry import PublisherRegistry, default_registry
from ..runtime.receipts import (
    new_operation_id,
    read_publish_receipt,
    receipt_data_dir_scope,
    remove_publish_receipt,
    write_publish_receipt,
)
from ..runtime.account_lease import (
    AccountOperationLease,
    AccountOperationLeaseTimeout,
)

# parse_count 已沉到 core/parsers（TD-Z3-debt 闭环, 2026 Q2）：
# 通用 UI 数字解析（"1.2万" / "3.5k" → int）是基础设施层，不该绑在 publisher 实现里。
# 上 sprint 用 `from ..publishers.toutiao import _parse_count` 是反向依赖（L5 调 L4），
# 本次改为从 core 正向 import，scheduler 和 publisher 双向解耦。
# 留 `_parse_count` 别名 → 模块内 _coerce_count 调用零改动。
from ..core.parsers import parse_count as _parse_count

logger = get_logger(__name__)

# 发布前置兜底污点词清单（命中即 fail-fast，防止 TODO / 未替换占位符 / 错版本号溜出）。
# 注：暂不进 config.py（Task B 在那条战线，避免合并冲突），下个 sprint 再迁移。
TAINT_PATTERNS: tuple[str, ...] = ("TODO", "未替换占位符", "过期版本号", "XXX")

# simhash 拦截阈值：与该账号 7d 内已发布 article.body 的 hamming 距离 < 此值即视为重复。
# 对齐 docs/anti-risk.md §63 设定的"相似度 > 0.85"，64 位 simhash 下约 8 bit。
SIMHASH_HAMMING_THRESHOLD = settings.simhash_hamming_threshold
SIMHASH_LOOKBACK_DAYS = 7

# A PublishJob is claimed with one conditional UPDATE before any publisher is
# entered.  Keeping this list deliberately small makes every other state
# terminal/non-runnable from execute_job's point of view.
CLAIMABLE_JOB_STATUSES: tuple[JobStatus, ...] = (
    JobStatus.PENDING,
    JobStatus.RETRYING,
)
NONTERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRYING}
)

INTERRUPTED_EXECUTION_ERROR = "发布执行被中断，平台结果未知；请先在平台核验，再手动重发"
TIMED_OUT_EXECUTION_ERROR = "发布执行超时，平台结果未知；请先在平台核验，再手动重发"
PERSISTENCE_EXECUTION_ERROR = "发布结果写库失败，平台结果未知；请先在平台核验，再手动重发"
PERSISTENCE_CONFIRMED_EFFECT_ERROR = (
    "平台写入回执已确认，但控制面写库失败；已保留发布标识，请人工对账，禁止重发"
)
UNCONFIRMED_EXECUTION_ERROR = "发布器未能确认写入结果，平台结果未知；请先在平台核验，再手动重发"
PREWRITE_CANCELLED_ERROR = "发布尚未开始：等待账号操作锁时任务被取消"
EXACT_ASSET_MATERIALIZATION_ERROR = (
    "Agent contract approved assets failed secure execution materialization"
)


class ExactAssetMaterializationError(RuntimeError):
    """Raised before a Publisher sees an unmaterialized approved asset."""


# Carries the already-redacted adapter result across the final DB transaction.
# ContextVar keeps concurrent async jobs isolated.  If commit/finalization
# raises, execute_job can still preserve a confirmed post ID/URL instead of
# replacing the only receipt with a generic "unknown" marker.
_FINALIZING_RESULT: ContextVar[PublishResult | None] = ContextVar(
    "ai_ops_finalizing_publish_result",
    default=None,
)


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Explicit dependencies for an isolated/manual worker execution.

    Production callers omit this object and retain the existing module-level
    dependencies.  Offline demos and tests can instead run the
    real claim/finalize state machine without temporarily replacing the global
    database session, publisher registry, notifications, or metric scheduler.
    """

    session_scope_factory: Callable[[], Any]
    registry: PublisherRegistry
    schedule_after_publish: Callable[[int], object]
    notify_success: Callable[[dict], object]
    notify_failed: Callable[[dict], object]
    similarity_checker: Callable[..., bool]
    rate_limit_checker: Callable[..., Any]
    account_lease_factory: Callable[..., Any]
    receipt_writer: Callable[..., object]
    receipt_reader: Callable[[int, str], dict | None]
    receipt_remover: Callable[[int, str], object]
    receipt_data_dir: str | Path
    report_exception: Callable[..., object]
    job_execution_timeout_seconds: float = 30.0
    account_operation_lock_timeout_seconds: float = 5.0


def _execution_session_scope(context: WorkerExecutionContext | None):
    return context.session_scope_factory() if context is not None else session_scope()


def _report_worker_exception(
    execution_context: WorkerExecutionContext | None,
    exc: BaseException,
    **details,
) -> None:
    reporter = (
        capture_exception if execution_context is None else execution_context.report_exception
    )
    try:
        reporter(exc, **details)
    except Exception:
        logger.exception(
            "worker exception reporter failed",
            extra={"scope": details.get("scope")},
        )


def _retry_delay_seconds(attempts: int) -> int:
    """Return deterministic exponential retry delay for a completed attempt."""
    base = max(1, int(getattr(settings, "job_retry_base_seconds", 60)))
    # Cap at one hour: a broken publisher must not create an ever-growing delay,
    # while the account still gets enough breathing room after repeated failures.
    return min(base * (2 ** max(0, attempts - 1)), 3600)


def _claim_job(session, job_id: int, *, now: datetime) -> PublishJob | None:
    """Atomically move one runnable job to RUNNING and increment attempts.

    This is a database compare-and-swap, not an ORM read-then-write.  It works on
    both SQLite and PostgreSQL and ensures concurrent callbacks/scanners cannot
    both enter the external publisher.
    """
    account_id = session.scalar(select(PublishJob.account_id).where(PublishJob.id == job_id))
    if account_id is None:
        return None

    # Serialize admission for one account on databases that support row locks.
    # This closes the gap where two different jobs pass min-interval/quota checks
    # before either one records a successful publish. SQLite remains supported by
    # its single-writer behavior and the documented single-worker topology.
    session.scalar(select(Account.id).where(Account.id == account_id).with_for_update())
    other_running = session.scalar(
        select(PublishJob.id)
        .where(PublishJob.account_id == account_id)
        .where(PublishJob.status == JobStatus.RUNNING)
        .where(PublishJob.id != job_id)
        .limit(1)
    )
    if other_running is not None:
        return None

    claim_time = as_utc_naive(now) or datetime.utcnow()
    claimed = session.execute(
        update(PublishJob)
        .where(PublishJob.id == job_id)
        .where(PublishJob.status.in_(CLAIMABLE_JOB_STATUSES))
        # Manual and legacy HTTP execution calls reach this same CAS. Exact
        # jobs cannot bypass their human-approved not-before timestamp, and a
        # missing immutable timestamp fails closed.
        .where(
            or_(
                PublishJob.plan_id.is_(None),
                and_(
                    PublishJob.approved_planned_for.is_not(None),
                    PublishJob.approved_planned_for <= claim_time,
                ),
            )
        )
        .values(
            status=JobStatus.RUNNING,
            started_at=claim_time,
            finished_at=None,
            attempts=PublishJob.attempts + 1,
        )
    )
    if claimed.rowcount != 1:
        return None
    job = session.get(PublishJob, job_id)
    if job is not None:
        session.refresh(job)
    return job


def _sync_article_status(session, article_id: int) -> ArticleStatus | None:
    """Aggregate all fan-out job states into the owning Article lifecycle."""
    # Serialize sibling aggregation so the last completing fan-out branch always
    # observes prior committed siblings and performs the terminal Article update.
    article = session.scalar(
        select(Article)
        .where(Article.id == article_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if article is None:
        return None

    # SessionLocal has autoflush=False; flush the just-finished job before reading
    # sibling states, otherwise aggregation can observe its previous status.
    session.flush()
    raw_statuses = session.scalars(
        select(PublishJob.status)
        .where(PublishJob.article_id == article_id)
        # A failed job replaced by republish_job is historical evidence, not an
        # active fan-out branch.  Counting it would keep a successfully replaced
        # article DEAD forever.
        .where(PublishJob.superseded_by_job_id.is_(None))
    ).all()
    statuses = [JobStatus(value) for value in raw_statuses]
    if not statuses:
        return article.status

    if all(status == JobStatus.SUCCESS for status in statuses):
        article.status = ArticleStatus.PUBLISHED
    elif any(status in NONTERMINAL_JOB_STATUSES for status in statuses):
        # A DEAD sibling does not make the article terminal while another fan-out
        # job is still runnable.  Operators should see that work remains active.
        article.status = ArticleStatus.PUBLISHING
    elif any(status == JobStatus.DEAD for status in statuses):
        article.status = ArticleStatus.DEAD
    elif any(status == JobStatus.FAILED for status in statuses):
        article.status = ArticleStatus.FAILED
    else:  # defensive fallback for future JobStatus values
        article.status = ArticleStatus.FAILED
    session.flush()
    return article.status


def _finish_failed_attempt(
    session,
    job: PublishJob,
    error: str,
    *,
    now: datetime | None = None,
    retryable: bool = True,
) -> None:
    """Persist one failed attempt, including its durable next execution time."""
    finished_at = now or datetime.utcnow()
    job.error = error
    job.finished_at = finished_at
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.RETRYING
        job.scheduled_at = finished_at + timedelta(seconds=_retry_delay_seconds(job.attempts))
    elif retryable:
        job.status = JobStatus.DEAD
        job.scheduled_at = None
        # Health escalation counts recent DEAD rows; autoflush is disabled, so
        # make the current exhausted attempt visible to that aggregate query.
        session.flush()
        _escalate_health_on_failure(session, job.account_id)
    else:
        job.status = JobStatus.FAILED
        job.scheduled_at = None
    _sync_article_status(session, job.article_id)


def _defer_without_consuming_attempt(
    session,
    job: PublishJob,
    error: str,
    *,
    retry_at: datetime,
) -> None:
    """Persist a policy deferral without spending a publisher attempt."""
    now = datetime.utcnow()
    job.attempts = max(0, job.attempts - 1)  # undo the CAS reservation
    job.status = JobStatus.RETRYING
    job.error = error
    job.finished_at = now
    job.scheduled_at = max(retry_at, now + timedelta(seconds=1))
    _sync_article_status(session, job.article_id)


def mark_running_job_uncertain(
    job_id: int,
    error: str,
    *,
    now: datetime | None = None,
    started_before: datetime | None = None,
    known_result: PublishResult | None = None,
    execution_context: WorkerExecutionContext | None = None,
) -> bool:
    """Fail-close one interrupted RUNNING job without blindly publishing again.

    An external platform may have accepted a post before this process was
    cancelled or crashed. Retrying that same row automatically could therefore
    duplicate an irreversible side effect. The conditional update also makes a
    late recovery pass harmless when another execution already completed.
    """
    finished_at = now or datetime.utcnow()
    with _execution_session_scope(execution_context) as session:
        row = session.execute(
            select(
                PublishJob.article_id,
                PublishJob.raw_response,
                PublishJob.publisher_kind,
                PublishJob.platform_post_id,
                PublishJob.platform_url,
            ).where(PublishJob.id == job_id)
        ).one_or_none()
        if row is None:
            return False
        article_id, prior_raw, prior_kind, prior_post_id, prior_url = row
        raw_response = dict(prior_raw or {})
        operation_id = raw_response.get("operation_id")
        if isinstance(operation_id, str):
            try:
                receipt = (
                    read_publish_receipt(job_id, operation_id)
                    if execution_context is None
                    else execution_context.receipt_reader(job_id, operation_id)
                )
            except Exception as exc:
                logger.warning(
                    "worker receipt recovery failed closed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
                receipt = None
        else:
            receipt = None

        platform_post_id: str | None = prior_post_id
        platform_url: str | None = prior_url
        publisher_kind = str(prior_kind or "")[:64]
        effect_applied = False
        outcome_uncertain = True
        if known_result is not None:
            raw_response.update(dict(known_result.raw_response or {}))
            platform_post_id = known_result.platform_post_id
            platform_url = known_result.platform_url
            publisher_kind = str(raw_response.get("publisher_kind") or "")
            effect_applied = bool(known_result.effect_applied)
            outcome_uncertain = bool(known_result.outcome_uncertain)
        elif receipt is not None:
            receipt_raw = receipt.get("raw_response")
            if isinstance(receipt_raw, dict):
                raw_response.update(receipt_raw)
            post_id = receipt.get("platform_post_id")
            post_url = receipt.get("platform_url")
            platform_post_id = post_id if isinstance(post_id, str) else None
            platform_url = post_url if isinstance(post_url, str) else None
            publisher_kind = str(receipt.get("publisher_kind") or "")[:64]
            effect_applied = bool(receipt.get("effect_applied"))
            outcome_uncertain = bool(receipt.get("outcome_uncertain"))
            raw_response["receipt_recovered"] = True

        # With no side-effect receipt, an interrupted RUNNING call is unknown.
        # A known effect is still terminal and requires reconciliation, but its
        # post identity must not be erased or mislabeled as safe to retry.
        if effect_applied:
            error = PERSISTENCE_CONFIRMED_EFFECT_ERROR
        raw_response["outcome_uncertain"] = outcome_uncertain
        raw_response["effect_applied"] = effect_applied
        raw_response["reconciliation_required"] = True

        conditions = [
            PublishJob.id == job_id,
            PublishJob.status == JobStatus.RUNNING,
        ]
        if started_before is not None:
            conditions.append(
                (PublishJob.started_at.is_(None)) | (PublishJob.started_at <= started_before)
            )
        updated = session.execute(
            update(PublishJob)
            .where(*conditions)
            .values(
                status=JobStatus.FAILED,
                error=error,
                finished_at=finished_at,
                scheduled_at=None,
                raw_response=raw_response,
                platform_post_id=platform_post_id,
                platform_url=platform_url,
                publisher_kind=publisher_kind,
            )
        )
        if updated.rowcount != 1:
            return False
        _sync_article_status(session, article_id)
        return True


def _best_effort_mark_uncertain(
    job_id: int,
    error: str,
    *,
    scope: str,
    known_result: PublishResult | None = None,
    execution_context: WorkerExecutionContext | None = None,
) -> bool:
    """Preserve the caller's control flow even if reconciliation storage fails."""
    try:
        return mark_running_job_uncertain(
            job_id,
            error,
            known_result=known_result,
            execution_context=execution_context,
        )
    except Exception as reconciliation_error:
        logger.exception(
            "worker could not persist unknown platform outcome",
            extra={"job_id": job_id, "scope": scope},
        )
        _report_worker_exception(
            execution_context,
            reconciliation_error,
            scope=scope,
            job_id=job_id,
        )
        return False


def schedule_job_runs(jobs, *, default_when: datetime | None = None) -> list[tuple[int, datetime]]:
    """Persist jittered run times and optionally register in-memory callbacks.

    The durable ``scheduled_at`` value is authoritative.  This prevents the
    database scanner from observing the pre-jitter planned time and publishing
    early.  APScheduler is only a latency optimization when it is running in the
    dedicated worker; restart recovery never depends on its in-memory callback.
    """
    if not bool(getattr(settings, "auto_publish_enabled", False)):
        logger.info("schedule_job_runs: auto publish disabled; jobs remain pending")
        return []

    from .queue import queue
    from .jitter import jitter_publish_time

    planned_base = default_when or datetime.utcnow()
    out: list[tuple[int, datetime]] = []
    for j in jobs:
        when = as_utc_naive(getattr(j, "scheduled_at", None) or planned_base)
        try:
            actual = jitter_publish_time(when)
        except Exception as e:
            logger.warning(
                "schedule_job_runs: jitter failed for job %s (%s); using planned time",
                getattr(j, "id", "?"),
                e,
            )
            actual = when

        # Jobs returned by distributor.distribute are attached to the caller's
        # session, so this assignment is committed with the API transaction.
        # It also keeps detached/CLI objects truthful for their caller.
        j.scheduled_at = actual
        out.append((j.id, actual))

        if not bool(getattr(getattr(queue, "_scheduler", None), "running", False)):
            continue
        try:
            queue.schedule_once(
                actual,
                (lambda jid=j.id: execute_job(jid)),
                job_id=f"pub-{j.id}",
            )
        except Exception as e:  # 无 loop / 调度器未启 → 跳过，不影响记录
            logger.warning("schedule_job_runs: skipped job %s (%s)", getattr(j, "id", "?"), e)
    return out


async def execute_job(
    job_id: int,
    *,
    execution_context: WorkerExecutionContext | None = None,
) -> PublishResult:
    """Run one job and fail-close unexpected post-claim persistence errors."""
    result_token = _FINALIZING_RESULT.set(None)
    try:
        return await _execute_job_once(job_id, execution_context=execution_context)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        known_result = _FINALIZING_RESULT.get()
        persistence_error = (
            PERSISTENCE_CONFIRMED_EFFECT_ERROR
            if known_result is not None and known_result.effect_applied
            else PERSISTENCE_EXECUTION_ERROR
        )
        marked = _best_effort_mark_uncertain(
            job_id,
            persistence_error,
            scope="worker.finalize_unknown_outcome",
            known_result=known_result,
            execution_context=execution_context,
        )
        logger.exception(
            "worker execution crashed outside publisher boundary",
            extra={"job_id": job_id, "marked_failed": marked},
        )
        _report_worker_exception(
            execution_context,
            exc,
            scope="worker.execute_job",
            job_id=job_id,
        )
        if known_result is not None:
            raw_response = dict(known_result.raw_response or {})
            raw_response["reconciliation_required"] = True
            raw_response["persistence_failed"] = True
            return PublishResult(
                success=False,
                effect_applied=known_result.effect_applied,
                retryable=False,
                outcome_uncertain=known_result.outcome_uncertain,
                platform_post_id=known_result.platform_post_id,
                platform_url=known_result.platform_url,
                error=persistence_error,
                raw_response=raw_response,
            )
        return PublishResult(
            success=False,
            retryable=False,
            outcome_uncertain=True,
            error=PERSISTENCE_EXECUTION_ERROR,
        )
    finally:
        _FINALIZING_RESULT.reset(result_token)


async def _execute_job_once(
    job_id: int,
    *,
    execution_context: WorkerExecutionContext | None = None,
) -> PublishResult:
    """Execute one job after a database-level conditional claim.

    Explicit calls are intentionally allowed even when automatic publishing is
    disabled.  The API/CLI authorization layer owns that human-triggered action;
    only automatic scheduling/scanning is guarded by ``auto_publish_enabled``.
    """
    with _execution_session_scope(execution_context) as s:
        existing: PublishJob | None = s.get(PublishJob, job_id)
        if existing is None:
            return PublishResult(success=False, error=f"job {job_id} 不存在")

        claim_time = datetime.utcnow()
        job = _claim_job(s, job_id, now=claim_time)
        if job is None:
            # Another worker already claimed it, or it is terminal.  This path
            # never reaches credential loading/the external publisher.
            s.refresh(existing)
            approved_not_before = as_utc_naive(existing.approved_planned_for)
            if (
                existing.status in CLAIMABLE_JOB_STATUSES
                and existing.plan_id is not None
                and (approved_not_before is None or claim_time < approved_not_before)
            ):
                return PublishResult(
                    success=False,
                    retryable=False,
                    error=f"job {job_id} 尚未到审批计划执行时间，或审批时间绑定缺失",
                )
            return PublishResult(
                success=False,
                error=f"job {job_id} 当前状态 {existing.status}，不可执行",
            )

        article: Article | None = s.get(Article, job.article_id)
        if article is None:
            error = "article 缺失"
            _finish_failed_attempt(s, job, error, retryable=False)
            return PublishResult(success=False, error=error)

        # Claim is the point where an article leaves SCHEDULED.  Fan-out
        # aggregation keeps it PUBLISHING until every sibling has succeeded.
        _sync_article_status(s, article.id)

        # 风控限流校验（养号期 + 间隔 + 单日上限）
        rate_limit_checker = (
            check_rate_limit if execution_context is None else execution_context.rate_limit_checker
        )
        gate = rate_limit_checker(s, job.account_id, exclude_job_id=job.id)
        if not gate.allowed:
            error = f"rate-limit: {gate.reason}"
            if gate.retry_at is not None:
                _defer_without_consuming_attempt(
                    s,
                    job,
                    error,
                    retry_at=gate.retry_at,
                )
            else:
                _finish_failed_attempt(s, job, error, retryable=False)
            return PublishResult(success=False, error=error)

        # 风控降权暂停期检查（health_monitor 写入 account.profile["paused_until"]）
        account = s.get(Account, job.account_id)
        if account is not None and is_paused(account):
            until = get_paused_until(account)
            error = f"账号暂停中至 {until.isoformat() if until else 'unknown'}"
            if until is not None:
                _defer_without_consuming_attempt(s, job, error, retry_at=until)
            else:
                _finish_failed_attempt(s, job, error)
            return PublishResult(success=False, error=error)

        # Health is an execution gate, not merely a dashboard label. Jobs may
        # have been scheduled before a health check changed the account, so the
        # worker must re-check immediately before loading credentials or
        # entering a publisher. UNKNOWN remains allowed for newly-added
        # accounts; DEGRADED/BANNED/EXPIRED require an operator/health check to
        # restore the account before a new job is created or manually retried.
        if account is None:
            error = "account 缺失"
            _finish_failed_attempt(s, job, error, retryable=False)
            return PublishResult(success=False, retryable=False, error=error)
        account_health = AccountHealth(account.health)
        if account_health not in {AccountHealth.HEALTHY, AccountHealth.UNKNOWN}:
            error = f"账号健康状态 {account_health.value}，禁止发布"
            _finish_failed_attempt(s, job, error, retryable=False)
            return PublishResult(success=False, retryable=False, error=error)

        contract_content: PublishContent | None = None
        if job.plan_id is not None:
            try:
                contract_content = _build_verified_contract_content(s, job, account)
            except Exception as exc:
                # Never reflect snapshot, filesystem, account profile, or
                # credential-binding details into the public job error.
                logger.warning(
                    "worker.contract_snapshot_rejected",
                    extra={"job_id": job.id, "error_type": type(exc).__name__},
                )
                error = "Agent contract approval snapshot failed verification"
                _finish_failed_attempt(s, job, error, retryable=False)
                return PublishResult(success=False, retryable=False, error=error)

        # 内容层前置兜底：TAINT 词 + simhash 查重。
        # 任何一个命中即 fail-fast，不再消耗下游的解密 / 浏览器开销。
        if execution_context is None:
            ok, pre_err = _pre_publish_check(
                s,
                job,
                article,
                body_override=(contract_content.body if contract_content else None),
            )
        else:
            ok, pre_err = _pre_publish_check(
                s,
                job,
                article,
                body_override=(contract_content.body if contract_content else None),
                similarity_checker=execution_context.similarity_checker,
                exception_reporter=execution_context.report_exception,
            )
        if not ok:
            _finish_failed_attempt(
                s,
                job,
                pre_err or "内容检查失败",
                retryable=False,
            )
            return PublishResult(success=False, error=pre_err)

        try:
            credential = get_credential(s, job.account_id)
        except ValueError:
            # 部分适配器（SAU / Camoufox / GitHub Pages）依赖 account_name、
            # 持久化浏览器 profile 或本机 git 状态，本来就没有 cookie/token。
            # 凭证是否必需由 publisher 判定；编排层传空 dict 让它给出
            # 平台特定结果，不在 registry 之前误杀磁盘态适配器。
            credential = {}

        try:
            platform = Platform(job.platform)
        except ValueError:
            error = f"未知平台: {job.platform}"
            _finish_failed_attempt(s, job, error, retryable=False)
            return PublishResult(success=False, error=error)
        operation_id = new_operation_id()
        claim_response = dict(job.raw_response or {})
        claim_response.update(
            {
                "operation_id": operation_id,
                "operation_attempt": job.attempts,
            }
        )
        job.raw_response = claim_response
        content = contract_content or _build_content(article)
        content.job_id = job.id
        content.operation_id = operation_id

        # 小红书图文：发布前对图片做反指纹处理（EXIF/微裁剪/微旋转/调色）
        # 仅对 XIAOHONGSHU + IMAGE_TEXT 执行，规避其它平台回归
        if (
            platform == Platform.XIAOHONGSHU
            and content.content_type == ContentType.IMAGE_TEXT
            and content.images
            and job.plan_id is None
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
                _report_worker_exception(
                    execution_context,
                    e,
                    scope="worker.image_anti_fingerprint",
                    job_id=job.id,
                    account_id=job.account_id,
                )

        # Snapshot primitives before the session closes.  The CAS transaction is
        # committed before the external call so other workers observe RUNNING.
        account_id = job.account_id

    # 跳出 session 调外部工具，避免长事务
    execution_uncertain = False
    write_lease_acquired = False
    try:
        timeout_seconds = float(
            getattr(settings, "job_execution_timeout_seconds", 1800)
            if execution_context is None
            else execution_context.job_execution_timeout_seconds
        )
        lock_timeout = float(
            getattr(settings, "account_operation_lock_timeout_seconds", 120)
            if execution_context is None
            else execution_context.account_operation_lock_timeout_seconds
        )
        lease = (
            AccountOperationLease(account_id, timeout_seconds=lock_timeout)
            if execution_context is None
            else execution_context.account_lease_factory(
                account_id,
                timeout_seconds=lock_timeout,
            )
        )
        async with lease:
            write_lease_acquired = True
            if execution_context is None:
                publish_call = _try_publishers_with_materialized_assets(
                    platform,
                    account_id,
                    credential,
                    content,
                )
                if timeout_seconds > 0:
                    result = await asyncio.wait_for(publish_call, timeout=timeout_seconds)
                else:
                    result = await publish_call
            else:
                with receipt_data_dir_scope(execution_context.receipt_data_dir):
                    publish_call = _try_publishers_with_materialized_assets(
                        platform,
                        account_id,
                        credential,
                        content,
                        registry=execution_context.registry,
                        receipt_writer=execution_context.receipt_writer,
                    )
                    if timeout_seconds > 0:
                        result = await asyncio.wait_for(
                            publish_call,
                            timeout=timeout_seconds,
                        )
                    else:
                        result = await publish_call
    except asyncio.CancelledError:
        if not write_lease_acquired:
            _best_effort_mark_uncertain(
                job_id,
                PREWRITE_CANCELLED_ERROR,
                scope="worker.cancel_before_publish",
                known_result=PublishResult(
                    success=False,
                    effect_applied=False,
                    retryable=False,
                    outcome_uncertain=False,
                    error=PREWRITE_CANCELLED_ERROR,
                    raw_response={"write_started": False},
                ),
                execution_context=execution_context,
            )
            raise
        marked = _best_effort_mark_uncertain(
            job_id,
            INTERRUPTED_EXECUTION_ERROR,
            scope="worker.cancel_unknown_outcome",
            execution_context=execution_context,
        )
        logger.warning(
            "worker execution cancelled; platform outcome is unknown",
            extra={"job_id": job_id, "marked_failed": marked},
        )
        raise
    except ExactAssetMaterializationError:
        result = PublishResult(
            success=False,
            effect_applied=False,
            retryable=False,
            outcome_uncertain=False,
            error=EXACT_ASSET_MATERIALIZATION_ERROR,
            raw_response={"write_started": False, "asset_materialization_failed": True},
        )
    except AccountOperationLeaseTimeout:
        result = PublishResult(
            success=False,
            effect_applied=False,
            retryable=True,
            outcome_uncertain=False,
            error="账号 profile 正被其他操作占用，稍后重试",
            raw_response={"write_started": False, "account_operation_busy": True},
        )
    except TimeoutError:
        execution_uncertain = True
        result = PublishResult(
            success=False,
            outcome_uncertain=True,
            error=TIMED_OUT_EXECUTION_ERROR,
        )
    except Exception as e:  # defensive: lock or injected/custom registry implementations
        if not write_lease_acquired:
            result = PublishResult(
                success=False,
                effect_applied=False,
                retryable=True,
                outcome_uncertain=False,
                error=f"账号操作锁不可用: {type(e).__name__}",
                raw_response={"write_started": False, "account_operation_lock_error": True},
            )
        else:
            # The coroutine was already entered, so an arbitrary failure cannot
            # prove that no external write happened. Do not retry/fallback, and
            # do not persist exception text that may embed CLI output/credentials.
            result = PublishResult(
                success=False,
                retryable=False,
                outcome_uncertain=True,
                error=f"publisher 调用状态无法确认: {type(e).__name__}",
            )

    if result.success and not result.effect_applied:
        result = result.model_copy(
            update={
                "success": False,
                "retryable": False,
                "error": result.error or "Publisher 只生成了预览，没有执行对外发布",
            }
        )

    # Make the parsed receipt available to execute_job before entering the
    # final transaction.  `_try_publishers` has already written the same result
    # to a durable sidecar for process-crash/stale-job recovery.
    _FINALIZING_RESULT.set(result)

    with _execution_session_scope(execution_context) as s:
        job = s.get(PublishJob, job_id)
        if job is None:
            return result
        # Only the claimant may complete a RUNNING job.  A future cancellation or
        # admin transition must not be overwritten by a late publisher response.
        if job.status != JobStatus.RUNNING:
            return PublishResult(
                success=False,
                error=f"job {job_id} 完成时状态已变为 {job.status}",
            )

        finished_at = datetime.utcnow()
        job.finished_at = finished_at
        # `_try_publishers` stamps the adapter that actually ended the fallback
        # chain. Persist that execution fact (not the originally planned/first
        # registry entry) so metrics route back to the same implementation.
        actual_publisher_kind = (result.raw_response or {}).get("publisher_kind")
        if isinstance(actual_publisher_kind, str) and 0 < len(actual_publisher_kind) <= 64:
            job.publisher_kind = actual_publisher_kind
        if result.success:
            job.status = JobStatus.SUCCESS
            job.error = None
            job.platform_post_id = result.platform_post_id
            job.platform_url = result.platform_url
            job.raw_response = result.raw_response
            mark_published(s, job.account_id)

            # 闭环最后一公里：把 publisher 主动塞进 raw_response 的第一份指标快照落库。
            # 不接入 = publisher 白做；接入后 dashboard / report 立刻有数（不用等 1h 飞轮）。
            # 同 session 内 add，依靠 session_scope commit。
            # 双层防御：helper 内已 try/except + capture；这里再套一层，防 helper
            # 被未来重构 / mock 替换破坏自吞契约后把 publish 主流程拖垮
            try:
                _persist_initial_metrics(
                    s,
                    job.id,
                    (result.raw_response or {}).get("initial_metadata") or {},
                    exception_reporter=(
                        capture_exception
                        if execution_context is None
                        else execution_context.report_exception
                    ),
                )
            except Exception as e:
                logger.warning(
                    "worker.persist_initial_metrics_outer: swallowed",
                    extra={"job_id": job.id, "error": str(e)},
                )
                _report_worker_exception(
                    execution_context,
                    e,
                    scope="worker.persist_initial_metrics_outer",
                    job_id=job.id,
                )

            _sync_article_status(s, job.article_id)
            article = s.get(Article, job.article_id)

            # 飞轮闭环：发布成功 → 调度 1h/24h/7d 数据采集
            try:
                if execution_context is None:
                    from .metrics import schedule_after_publish

                    schedule_after_publish(job.id)
                else:
                    execution_context.schedule_after_publish(job.id)
            except Exception as e:
                # 采集失败不影响主流程——但必须留观测痕迹，否则飞轮长期断掉无人知
                logger.warning(
                    "worker.schedule_metrics: swallowed",
                    extra={"job_id": job.id, "error": str(e)},
                )
                _report_worker_exception(
                    execution_context,
                    e,
                    scope="worker.schedule_metrics",
                    job_id=job.id,
                )
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
            raw_response = dict(result.raw_response or {})
            if result.outcome_uncertain:
                raw_response["outcome_uncertain"] = True
            if result.effect_applied:
                raw_response["effect_applied"] = True
                job.platform_post_id = result.platform_post_id
                job.platform_url = result.platform_url
            job.raw_response = raw_response
            failure_error = result.error or "unknown"
            if result.outcome_uncertain and "平台结果未知" not in failure_error:
                failure_error = f"{UNCONFIRMED_EXECUTION_ERROR}（{failure_error}）"
            _finish_failed_attempt(
                s,
                job,
                failure_error,
                now=finished_at,
                retryable=(
                    result.retryable and not execution_uncertain and not result.outcome_uncertain
                ),
            )
            if job.status == JobStatus.DEAD:
                # 自动重发钩子（publishing-sop §五 / §八"笔记发了发现内容错"自动通道）：
                # 默认关（AUTO_REPUBLISH_ON_DEAD=False）——避免 publisher 真挂时无限建 v2 → v3 → ...
                # 风暴。本钩子仅"建 v2 + 标 v1 superseded"，**不真触发 v2 执行**：
                # 让 scheduler.queue 按既有节奏拉起，复用风控 / 限流 / dedup 全套兜底。
                # Exact contract job 必须新建 plan 并重新独立审批，绝不降级成
                # planless legacy v2。异常吞 + capture：自动重发是辅助通道，挂了
                # 不能拖累 job 状态本身的落库。
                if AUTO_REPUBLISH_ON_DEAD and job.plan_id is not None:
                    logger.info(
                        "worker.auto_republish: exact job requires a new approved plan",
                        extra={"job_id": job.id, "plan_id": job.plan_id},
                    )
                elif AUTO_REPUBLISH_ON_DEAD:
                    try:
                        v2 = republish_job(s, job.id, reason="auto_retry_exhausted")
                        logger.info(
                            "worker.auto_republish: created v2",
                            extra={"old_job_id": job.id, "new_job_id": v2.id},
                        )
                    except Exception as e:
                        logger.warning(
                            "worker.auto_republish: swallowed",
                            extra={"job_id": job.id, "error": str(e)},
                        )
                        _report_worker_exception(
                            execution_context,
                            e,
                            scope="worker.auto_republish",
                            job_id=job.id,
                        )
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
        if execution_context is None:
            remove_publish_receipt(job_id, operation_id)
        else:
            execution_context.receipt_remover(job_id, operation_id)
    except Exception as exc:
        # Receipt cleanup is best-effort after the database result committed.
        # It must never turn a durable SUCCESS/FAILED result into a false error.
        logger.warning(
            "worker receipt cleanup failed after commit",
            extra={"job_id": job_id, "error_type": type(exc).__name__},
        )
    try:
        if execution_context is None:
            from ..notify import publish_failed, publish_success

        else:
            publish_success = execution_context.notify_success
            publish_failed = execution_context.notify_failed
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
        _report_worker_exception(
            execution_context,
            e,
            scope="worker.notify",
            job_id=job_id,
            kind=notify_snapshot.get("kind"),
        )

    return result


def _validated_exact_asset_manifest(
    content: PublishContent,
) -> list[ApprovedAssetExecution]:
    """Match the private approval manifest to every publisher-facing path."""

    manifest = list(content.approved_assets)
    supported_types = {AssetType.IMAGE, AssetType.VIDEO}
    if any(asset.asset_type not in supported_types for asset in manifest):
        raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)

    image_assets = [asset for asset in manifest if asset.asset_type == AssetType.IMAGE]
    video_assets = [asset for asset in manifest if asset.asset_type == AssetType.VIDEO]
    if [asset.storage_path for asset in image_assets] != list(content.images):
        raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
    if [asset.storage_path for asset in video_assets] != list(content.videos):
        raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
    if len(manifest) != len(content.images) + len(content.videos):
        raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)

    for asset in manifest:
        try:
            filename = Path(asset.storage_path).name
        except (OSError, TypeError, ValueError):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR) from None
        if filename != f"{asset.sha256}{asset.storage_suffix}":
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
    return manifest


def _copy_verified_asset_to_execution_file(
    asset: ApprovedAssetExecution,
    *,
    directory_fd: int,
    destination_name: str,
) -> None:
    """Copy one approved inode through the same FD that was securely verified."""

    destination_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        destination_fd = os.open(destination_name, flags, 0o400, dir_fd=directory_fd)
        copied = copy_verified_vaulted_asset(
            VaultedAsset(
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                vault_path=Path(asset.storage_path),
            ),
            destination_fd=destination_fd,
            vault_root=settings.agent_asset_vault_root,
            max_bytes=settings.agent_asset_max_bytes,
        )
        if copied.sha256 != asset.sha256 or copied.size_bytes != asset.size_bytes:
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)

        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o400)
        destination_stat = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_size != copied.size_bytes
            or destination_stat.st_mode & 0o222
        ):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
        visible_stat = os.stat(destination_name, dir_fd=directory_fd, follow_symlinks=False)
        if (visible_stat.st_dev, visible_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
    finally:
        if destination_fd is not None:
            try:
                os.close(destination_fd)
            except OSError:
                pass


def _cleanup_exact_asset_temporary_directory(
    temporary: tempfile.TemporaryDirectory[str],
) -> None:
    try:
        temporary.cleanup()
    except OSError:
        # Cleanup must not replace a path-free pre-write rejection or erase a
        # known external result after the Publisher returned.
        logger.exception("worker exact asset execution directory cleanup failed")


@contextmanager
def _materialized_exact_assets(content: PublishContent) -> Iterator[PublishContent]:
    """Yield immutable private execution copies for an exact-contract publish.

    Legacy jobs are returned unchanged. Exact jobs first match every path to the
    approved digest/size/suffix manifest, then securely open and hash the vault
    inode. The already-open, rewound FD is copied into a new 0700 directory
    below the configured vault so audited adapters retain their controlled-root
    boundary. Each destination is created with O_EXCL and changed to 0400.
    """

    if not content.exact_approval:
        yield content
        return

    manifest = _validated_exact_asset_manifest(content)
    if not manifest:
        yield content
        return

    temporary: tempfile.TemporaryDirectory[str] | None = None
    vault_root_fd: int | None = None
    directory_fd: int | None = None
    try:
        configured_root = Path(settings.agent_asset_vault_root).expanduser()
        if not hasattr(os, "getuid"):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)
        root_lexical_stat = os.lstat(configured_root)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        vault_root_fd = os.open(configured_root, directory_flags)
        root_opened_stat = os.fstat(vault_root_fd)
        vault_root = configured_root.resolve(strict=True)
        root_resolved_stat = os.stat(vault_root, follow_symlinks=False)
        root_identity = (root_opened_stat.st_dev, root_opened_stat.st_ino)
        if (
            not stat.S_ISDIR(root_lexical_stat.st_mode)
            or stat.S_ISLNK(root_lexical_stat.st_mode)
            or not stat.S_ISDIR(root_opened_stat.st_mode)
            or not stat.S_ISDIR(root_resolved_stat.st_mode)
            or (root_lexical_stat.st_dev, root_lexical_stat.st_ino) != root_identity
            or (root_resolved_stat.st_dev, root_resolved_stat.st_ino) != root_identity
            or stat.S_IMODE(root_opened_stat.st_mode) != 0o700
            or root_opened_stat.st_uid != os.getuid()
        ):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)

        temporary = tempfile.TemporaryDirectory(prefix=".agent-execution-", dir=vault_root)
        execution_directory = Path(temporary.name)
        os.chmod(execution_directory, 0o700)
        lexical_stat = os.lstat(execution_directory)
        directory_fd = os.open(execution_directory, directory_flags)
        opened_stat = os.fstat(directory_fd)
        opened_parent_stat = os.stat("..", dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(lexical_stat.st_mode)
            or stat.S_ISLNK(lexical_stat.st_mode)
            or not stat.S_ISDIR(opened_stat.st_mode)
            or (lexical_stat.st_dev, lexical_stat.st_ino)
            != (opened_stat.st_dev, opened_stat.st_ino)
            or (opened_parent_stat.st_dev, opened_parent_stat.st_ino) != root_identity
            or stat.S_IMODE(opened_stat.st_mode) != 0o700
            or opened_stat.st_uid != os.getuid()
        ):
            raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR)

        materialized: list[tuple[ApprovedAssetExecution, str]] = []
        for ordinal, asset in enumerate(manifest):
            destination_name = f"{asset.asset_type.value}-{ordinal:03d}{asset.storage_suffix}"
            _copy_verified_asset_to_execution_file(
                asset,
                directory_fd=directory_fd,
                destination_name=destination_name,
            )
            materialized.append((asset, str(execution_directory / destination_name)))
        os.fsync(directory_fd)
        execution_content = content.model_copy(
            update={
                "images": [
                    path for asset, path in materialized if asset.asset_type == AssetType.IMAGE
                ],
                "videos": [
                    path for asset, path in materialized if asset.asset_type == AssetType.VIDEO
                ],
                # The manifest has served its scheduler-only purpose. Do not
                # expose the original vault paths to the external adapter.
                "approved_assets": [],
            }
        )
    except ExactAssetMaterializationError:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            directory_fd = None
        if vault_root_fd is not None:
            try:
                os.close(vault_root_fd)
            except OSError:
                pass
            vault_root_fd = None
        if temporary is not None:
            _cleanup_exact_asset_temporary_directory(temporary)
        raise
    except (AssetVaultError, OSError, RuntimeError, TypeError, ValueError):
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            directory_fd = None
        if vault_root_fd is not None:
            try:
                os.close(vault_root_fd)
            except OSError:
                pass
            vault_root_fd = None
        if temporary is not None:
            _cleanup_exact_asset_temporary_directory(temporary)
        raise ExactAssetMaterializationError(EXACT_ASSET_MATERIALIZATION_ERROR) from None

    assert temporary is not None
    try:
        yield execution_content
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if vault_root_fd is not None:
            try:
                os.close(vault_root_fd)
            except OSError:
                pass
        _cleanup_exact_asset_temporary_directory(temporary)


async def _try_publishers_with_materialized_assets(
    platform: Platform,
    account_id: int,
    credential: dict,
    content: PublishContent,
    *,
    registry: PublisherRegistry | None = None,
    receipt_writer: Callable[..., object] | None = None,
) -> PublishResult:
    """Materialize approved assets off-loop, retaining them through Publisher exit."""

    async def publish(execution_content: PublishContent) -> PublishResult:
        if registry is None and receipt_writer is None:
            # Keep the production call shape compatible with injected legacy
            # worker tests and operators that replace this internal boundary.
            return await _try_publishers(
                platform,
                account_id,
                credential,
                execution_content,
            )
        return await _try_publishers(
            platform,
            account_id,
            credential,
            execution_content,
            registry=registry,
            receipt_writer=receipt_writer,
        )

    if not content.exact_approval:
        return await publish(content)

    materialization = _materialized_exact_assets(content)
    enter_task = asyncio.create_task(asyncio.to_thread(materialization.__enter__))
    execution_content, enter_cancellation = await _await_shielded_task_completion(enter_task)
    if enter_cancellation is not None:
        try:
            await _exit_materialized_exact_assets(materialization)
        finally:
            raise enter_cancellation

    try:
        return await publish(execution_content)
    finally:
        await _exit_materialized_exact_assets(materialization)


async def _await_shielded_task_completion(
    task: asyncio.Task[Any],
) -> tuple[Any, asyncio.CancelledError | None]:
    """Wait for a non-cancellable thread task while remembering caller cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            if task.done() and task.cancelled():
                if cancellation is not None:
                    raise cancellation
                raise
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is not None:
                raise cancellation
            raise


async def _exit_materialized_exact_assets(materialization: Any) -> None:
    cleanup_task = asyncio.create_task(
        asyncio.to_thread(materialization.__exit__, None, None, None)
    )
    _, cleanup_cancellation = await _await_shielded_task_completion(cleanup_task)
    if cleanup_cancellation is not None:
        raise cleanup_cancellation


async def _try_publishers(
    platform: Platform,
    account_id: int,
    credential: dict,
    content: PublishContent,
    *,
    registry: PublisherRegistry | None = None,
    receipt_writer: Callable[..., object] | None = None,
) -> PublishResult:
    """按优先级尝试该平台所有 Publisher，第一个成功即返回。"""
    active_registry = registry if registry is not None else default_registry
    publishers = active_registry.resolve(platform)
    if not publishers:
        return PublishResult(success=False, error=f"未注册 {platform} 的 Publisher")

    if content.exact_approval:
        required_kind = content.approved_publisher_kind
        expected_payload_digest = content.approved_renderer_payload_digest
        if not required_kind or not expected_payload_digest:
            return PublishResult(
                success=False,
                retryable=False,
                error="Agent contract job is missing its approved renderer binding",
            )
        publishers = [
            publisher
            for publisher in publishers
            if getattr(publisher.kind, "value", str(publisher.kind)) == required_kind
        ]
        if len(publishers) != 1:
            return PublishResult(
                success=False,
                retryable=False,
                error="Approved Agent renderer is unavailable or ambiguous",
            )
        try:
            material = publishers[0].agent_contract_digest_material(content)
            current_payload_digest = canonical_sha256(material)
        except Exception:
            return PublishResult(
                success=False,
                retryable=False,
                error="Approved Agent renderer failed payload verification",
            )
        if current_payload_digest != expected_payload_digest:
            return PublishResult(
                success=False,
                retryable=False,
                error="Approved Agent renderer payload changed after planning",
            )

    last: PublishResult | None = None
    for pub in publishers:
        try:
            result = await pub.publish(account_id, credential, content)
        except NotImplementedError as e:
            result = PublishResult(success=False, error=f"{pub.kind} 未实现: {e}")
        except Exception as e:
            # An adapter exception does not prove that its external write never
            # started.  Treat the boundary as unknown; otherwise the next
            # Publisher or durable retry could create a duplicate post.
            result = PublishResult(
                success=False,
                retryable=False,
                outcome_uncertain=True,
                error=f"{pub.kind} 异常后写入状态未知（{type(e).__name__}）",
                raw_response={
                    "adapter": str(pub.kind),
                    "exception_type": type(e).__name__,
                    "outcome": "unknown",
                },
            )
        raw_response = dict(result.raw_response or {})
        publisher_kind = getattr(pub.kind, "value", str(pub.kind))
        raw_response["publisher_kind"] = publisher_kind
        result = result.model_copy(update={"raw_response": raw_response})
        active_receipt_writer = write_publish_receipt if receipt_writer is None else receipt_writer
        try:
            active_receipt_writer(
                job_id=content.job_id,
                operation_id=content.operation_id,
                publisher_kind=publisher_kind,
                result=result,
            )
        except Exception as exc:
            # The built-in writer already swallows storage failures. Custom
            # execution contexts receive the same best-effort contract here.
            logger.warning(
                "worker receipt journal failed",
                extra={
                    "job_id": content.job_id,
                    "error_type": type(exc).__name__,
                },
            )
        if result.success or result.effect_applied or result.outcome_uncertain:
            return result
        last = result
    return last or PublishResult(success=False, error="所有 Publisher 都失败")


def _escalate_health_on_failure(session, account_id: int) -> None:
    """失败联动健康降级：DEAD 默认降到 DEGRADED；24h 内连续 3 次 DEAD 升级到 BANNED。"""
    from datetime import datetime, timedelta
    from sqlalchemy import func, select

    window_start = datetime.utcnow() - timedelta(hours=24)
    recent_dead = (
        session.scalar(
            select(func.count(PublishJob.id))
            .where(PublishJob.account_id == account_id)
            .where(PublishJob.status == JobStatus.DEAD)
            .where(PublishJob.finished_at >= window_start)
        )
        or 0
    )

    if recent_dead >= 3:
        pause_account(
            session,
            account_id,
            hours=BAN_PAUSE_HOURS,
            health=AccountHealth.BANNED,
            reason=f"24h 内连续 {recent_dead} 次发布 DEAD",
        )
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


def _build_verified_contract_content(
    session,
    job: PublishJob,
    account: Account,
) -> PublishContent:
    """Load one exact approved payload and fail closed on every drift vector."""

    plan = session.get(PublicationPlan, job.plan_id)
    if (
        plan is None
        or plan.state != "scheduled"
        or plan.article_id != job.article_id
        or as_utc_naive(job.approved_planned_for) != as_utc_naive(plan.planned_for)
    ):
        raise ValueError("contract plan is not executable")

    snapshot = parse_stored_content_snapshot(plan.content_snapshot)
    if snapshot.content_id != plan.article_id:
        raise ValueError("contract content identity changed")
    validate_stored_content_total(
        snapshot,
        max_total_bytes=settings.agent_asset_max_total_bytes,
    )
    if any(asset.size_bytes > settings.agent_asset_max_bytes for asset in snapshot.assets):
        raise ValueError("contract asset metadata exceeds the per-file limit")
    content_hash = stored_content_digest(snapshot)
    if content_hash != plan.content_digest:
        raise ValueError("contract content digest changed")

    targets = [PublicationTarget.model_validate(value) for value in plan.targets]
    if not targets:
        raise ValueError("contract targets are missing")
    recomputed_plan_digest = plan_digest(
        content_digest=content_hash,
        targets=targets,
        planned_for=plan.planned_for,
    )
    if recomputed_plan_digest != plan.plan_digest:
        raise ValueError("contract plan digest changed")

    matching_targets = [target for target in targets if target.account_id == job.account_id]
    if len(matching_targets) != 1:
        raise ValueError("contract job target is not approved")
    target = matching_targets[0]
    if Platform(job.platform) != target.platform or Platform(account.platform) != target.platform:
        raise ValueError("contract target platform changed")
    if account_binding_digest(account) != target.account_binding_digest:
        raise ValueError("contract target binding changed")
    if target.approved_external_account_id is not None:
        profile = account.profile
        raw_external_account_id = (
            profile.get("external_account_id") if isinstance(profile, dict) else None
        )
        try:
            current_external_account_id = normalize_zhihu_external_account_id(
                raw_external_account_id
            )
        except ValueError:
            raise ValueError("contract target external identity is invalid") from None
        if current_external_account_id != target.approved_external_account_id:
            raise ValueError("contract target external identity changed")

    matching_approvals = [
        approval
        for approval in plan.approval_requests
        if approval.status == "approved" and approval.plan_digest == plan.plan_digest
    ]
    if len(matching_approvals) != 1:
        raise ValueError("contract approval is missing")
    approval = matching_approvals[0]
    if (
        approval.decided_by_type != "human"
        or approval.decided_at is None
        or approval.decided_by in {approval.requested_by, plan.created_by}
    ):
        raise ValueError("contract approval is not independent")
    content = publish_content_from_snapshot(snapshot)
    content.approved_publisher_kind = target.execution.renderer.publisher_kind.value
    content.approved_renderer_payload_digest = target.execution.payload_digest
    content.approved_external_account_id = target.approved_external_account_id
    content.approved_assets = [
        ApprovedAssetExecution(
            asset_type=asset.asset_type,
            storage_path=asset.storage_path,
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            storage_suffix=asset.storage_suffix,
        )
        for asset in snapshot.assets
    ]
    return content


def _pre_publish_check(
    session,
    job: PublishJob,
    article: Article,
    *,
    body_override: str | None = None,
    similarity_checker=None,
    exception_reporter=None,
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
        exception_reporter: 可注入的异常上报函数；未传时在调用时解析生产
            ``capture_exception``，避免把导入时对象冻结进默认参数。

    Returns:
        (ok, error_message)：ok=False 时 error_message 给 worker 写入 job.error。

    职责单一：只判断"能不能发"，不动 job / article 任何字段——状态机由调用方处理。
    """
    body = article.body or "" if body_override is None else body_override

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
        # 静默放行 + 观测兜底——dedup 长期失效 = 重复内容溢出 + 平台限流风险升级
        logger.warning(
            "worker.simhash_check: swallowed",
            extra={"job_id": job.id, "account_id": job.account_id, "error": str(e)},
        )
        active_reporter = capture_exception if exception_reporter is None else exception_reporter
        try:
            active_reporter(
                e,
                scope="worker.simhash_check",
                job_id=job.id,
                account_id=job.account_id,
            )
        except Exception:
            logger.exception(
                "worker simhash exception reporter failed",
                extra={"job_id": job.id},
            )
        return True, None
    if too_similar:
        return False, (
            f"simhash 重复: 与账号 {job.account_id} 近 "
            f"{SIMHASH_LOOKBACK_DAYS}d 已发布内容相似度过高"
            f"（hamming < {SIMHASH_HAMMING_THRESHOLD}）"
        )

    return True, None


# ---------------------------------------------------------------------------
# initial_metadata → Metrics 落库（TD-Z3, 2026 Q2）
# ---------------------------------------------------------------------------


def _coerce_count(value) -> int:
    """把 initial_metadata 里的 count 字段统一收敛为 int。

    宽容输入：
      - int → 直接返回（其他 publisher 后续可能直接返 int）
      - str → 走 _parse_count（兼容 "1.2万" / "3.5k" 等 UI 缩写，头条当前路径）
      - None / 其他 → 0
    """
    if isinstance(value, bool):
        # bool 是 int 子类，必须先排除——不然 True/False 会被当 1/0 静默吃掉
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _parse_count(value)
    return 0


def _persist_initial_metrics(
    session,
    job_id: int,
    initial_metadata: dict,
    *,
    exception_reporter=capture_exception,
) -> "Metrics | None":
    """publish 成功后落第一份 Metrics 快照。

    数据流闭环：publisher._do_publish 抓到的 view/comment/like 已塞进
    raw_response["initial_metadata"]——本函数把它真正写到 Metrics 表，
    省下游 collect_metrics 飞轮 1h 后才出第一份数据的等待窗口。

    Args:
        session: SQLAlchemy session（worker 已持有；这里不开新连接、不 commit，
            commit 由 worker 外层 session_scope 统一管）。
        job_id: PublishJob.id，作为 Metrics.job_id 外键。
        initial_metadata: publisher 塞进 raw_response 的 dict，常见字段：
            {url, view_count, comment_count, like_count, share_count, publish_time}
            字段值可能是 int 或 UI 字符串（如 "1.2万"），统一走 _coerce_count 收敛。

    Returns:
        Metrics 实例（已 add 进 session），或 None（数据为空 / 全 0 / 异常）。

    短路策略：
      - initial_metadata 为空 dict → 返回 None（其它 publisher 不返 metadata 即此路径）
      - 所有计数都解析为 0 → 返回 None（避免污染数据；下游飞轮 1h 后还会跑）

    容错策略：
      - 任何异常 → logger.warning + capture_exception + 返回 None
      - publish 主流程不受影响（哪怕 Metrics 表写挂了，job 已落 SUCCESS）
    """
    if not initial_metadata:
        return None

    try:
        views = _coerce_count(initial_metadata.get("view_count"))
        likes = _coerce_count(initial_metadata.get("like_count"))
        comments = _coerce_count(initial_metadata.get("comment_count"))
        shares = _coerce_count(initial_metadata.get("share_count"))

        if views == 0 and likes == 0 and comments == 0 and shares == 0:
            # 全 0 → 不落库。这是新发布常态（刚发出去还没人看到），让飞轮 1h 后再落
            # 第一行非 0 数据；避免 dashboard 看到"发了 = 全 0"的歧义信号
            return None

        metric = Metrics(
            job_id=job_id,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            raw=dict(initial_metadata),  # 浅拷贝隔离，避免后续修改 raw_response 时联动
            # Round 6 / TD-Z3-followup-2：显式标 source="initial"——让 scheduler/metrics.py
            # 的 24h 触发判定能基于 source 计数排除掉这条初始快照（飞轮 count 只数 scheduled）。
            source="initial",
        )
        session.add(metric)
        session.flush()
        return metric
    except Exception as e:
        # 落库失败不影响 publish 主流程——但飞轮永远 1h 后才有第一份数据 = 仪表盘
        # 体感差。必须 capture 让 Sentry 兜底告警
        logger.warning(
            "worker.persist_initial_metrics: swallowed",
            extra={"job_id": job_id, "error": str(e)},
        )
        try:
            exception_reporter(
                e,
                scope="worker.persist_initial_metrics",
                job_id=job_id,
            )
        except Exception:
            logger.exception(
                "worker metrics exception reporter failed",
                extra={"job_id": job_id},
            )
        return None


# ---------------------------------------------------------------------------
# 重发覆盖追踪 helper（publishing-sop §五 / §九 #7）
# ---------------------------------------------------------------------------


def _mark_job_superseded(session, old_job_id: int, new_job_id: int) -> bool:
    """把旧 PublishJob 标记为被新 job 覆盖。

    使用场景（本 Task 暂不创建调用方，仅暴露字段 + helper 给后续重发流程用）：
      - worker / 运营手动创建新 job 替代旧 job（旧 job 内容错 / 失败需重发）
      - 调用方先创建新 job，再调本 helper 把旧 job.superseded_by_job_id 指向新 job
      - 后台 UI / 周报 / 数据分析据此追踪"旧 job 被谁覆盖"，运营复盘有据

    Args:
        session: SQLAlchemy session（调用方负责 commit；本函数不开新连接、不 commit）
        old_job_id: 被覆盖的旧 PublishJob.id
        new_job_id: 覆盖它的新 PublishJob.id

    Returns:
        True  = 旧 job 存在且字段已 set
        False = 旧 job 不存在（调用方应日志告警）

    防御：
        - old == new → 拒绝（自指 = 数据污染）。返回 False 不抛，让上游决定降级
        - 不校验 new_job_id 是否真存在（FK 在 DB 侧兜底；helper 保持薄）
    """
    if old_job_id == new_job_id:
        # 自指防御：旧 job 指向自己 = 语义错乱。不抛异常以免阻塞主流程，
        # 但返回 False 让调用方有机会观测到。
        logger.warning(
            "worker._mark_job_superseded: refused self-reference",
            extra={"old_job_id": old_job_id, "new_job_id": new_job_id},
        )
        return False

    old = session.get(PublishJob, old_job_id)
    if old is None:
        logger.warning(
            "worker._mark_job_superseded: old job not found",
            extra={"old_job_id": old_job_id, "new_job_id": new_job_id},
        )
        return False

    old.superseded_by_job_id = new_job_id
    session.flush()
    return True


# ---------------------------------------------------------------------------
# 重发覆盖主流程（publishing-sop §五"重发覆盖语义" / §八风险表）
# ---------------------------------------------------------------------------

# 自动重发开关：默认关。打开 = execute_job 把 attempts >= max_attempts 的 job 标 DEAD 后
# 自动建 v2 PublishJob（v1.superseded_by_job_id 指向 v2）。
#
# 默认关的原因：
#   - publisher 真坏掉时（cookies 失效 / 平台改版 / 网络断），重试只会无限建 v2 → v3 → ...
#     形成风暴，反而把账号刷成 BANNED；
#   - 自动重发应该被外层"健康度评估 + 人工 review"门控；
#   - 运营拿到失败告警后手动调 POST /jobs/{id}/republish 才是当前推荐路径。
#
# 何时打开：accounts.health_monitor 接入"按账号自动判断是否值得重发"之后（follow-up）。
AUTO_REPUBLISH_ON_DEAD = False

# 允许重发的旧 job 状态白名单：只有真"跑挂了"的 job 才允许覆盖重发。
# - SUCCESS：已发布成功，重发 = 重复发，应走平台手动删 + 重新建 article 路径
# - PENDING / RUNNING / RETRYING：job 还在进行中，重发会形成竞态（多个 worker 抢同一 article）
# 只放行 FAILED / DEAD —— 前者是单轮失败、后者是耗尽重试。
_REPUBLISHABLE_STATUSES = (JobStatus.FAILED, JobStatus.DEAD)


def republish_job(
    session,
    old_job_id: int,
    *,
    reason: str = "manual",
    platform_checked: bool = False,
) -> PublishJob:
    """基于失败的旧 PublishJob 创建 v2，并把旧 job 标记为 superseded。

    主流程入口（publishing-sop §五"重发覆盖语义"的物理载体）：
      - 人工触发：POST /jobs/{id}/republish（运营 UI 按钮）→ reason="manual"
      - 自动触发：execute_job 在 max_attempts 耗尽时调（AUTO_REPUBLISH_ON_DEAD=True 时）
        → reason="auto_retry_exhausted"

    本函数只"建 v2 + 标 v1 superseded"，**不真触发 v2 执行**——让 scheduler 拉起，
    复用现有风控 / 限流 / dedup / 健康度评估全套兜底，避免重发流程绕开主路径。

    Args:
        session: SQLAlchemy session（**不在函数内 commit**，commit 由调用方的 session_scope
            / API 的 get_session 统一管；保持 helper 薄）
        old_job_id: 被覆盖的旧 PublishJob.id（必须存在且 status ∈ {FAILED, DEAD}）
        reason: 重发原因，写入 v2.raw_response["republish_reason"]，用于运营复盘。
            约定值："manual" | "auto_retry_exhausted"；其它字符串也接受（向前兼容）

    Returns:
        新建的 v2 PublishJob 实例（已 add 进 session 并 flush，id 已分配）

    Raises:
        ValueError: 旧 job 不存在 / 状态不在白名单 / 属于 Agent contract。
            Exact job 必须创建新 plan 并重新独立审批，不能降级为 planless v2。
            调用方负责转译为 HTTP 400 等。

    数据契约（v2 vs v1）：
      - 复用：article_id / account_id / platform / publisher_kind / max_attempts
      - 重置：status=PENDING, attempts=0, started_at/finished_at/platform_*/error=None
      - 新写：raw_response = {"republish_reason": reason, "republished_from": old_id}
      - 关联：v1.superseded_by_job_id = v2.id（via _mark_job_superseded）
    """
    old = session.get(PublishJob, old_job_id)
    if old is None:
        raise ValueError(f"job {old_job_id} not found")

    if old.plan_id is not None:
        raise ValueError(
            "Agent contract jobs require a new publication plan and independent "
            "approval before republishing"
        )

    if old.status not in _REPUBLISHABLE_STATUSES:
        raise ValueError(f"can only republish FAILED/DEAD jobs, got {old.status}")
    prior_result = old.raw_response or {}
    needs_platform_check = bool(
        prior_result.get("outcome_uncertain")
        or prior_result.get("effect_applied")
        or "平台结果未知" in (old.error or "")
    )
    if needs_platform_check and not platform_checked:
        raise ValueError(
            "the platform may already contain this post; verify it first and set "
            "platform_checked=true before republishing"
        )

    new_job = PublishJob(
        article_id=old.article_id,
        account_id=old.account_id,
        platform=old.platform,
        publisher_kind=old.publisher_kind,
        status=JobStatus.PENDING,
        attempts=0,
        max_attempts=old.max_attempts,
        raw_response={
            "republish_reason": reason,
            "republished_from": old.id,
            "platform_checked": platform_checked,
        },
    )
    session.add(new_job)
    session.flush()  # 拿 new_job.id

    # 标 v1 superseded（helper 自带"老 job 不存在则降级"，但此处老 job 100% 存在）
    _mark_job_superseded(session, old.id, new_job.id)
    return new_job
