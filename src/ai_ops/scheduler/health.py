"""定时健康检查 daemon — 每天扫普通账号与已到期 BANNED 账号。

接入：lifespan startup 调 schedule_daily_health_check。
默认 02:00 跑（凌晨人少，反爬窗口）。
到期 BANNED 账号只做只读探活，只有明确 HEALTHY 才解除封禁。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib

from sqlalchemy import select

from ..accounts.health_monitor import (
    is_ban_probe_due,
    record_banned_probe,
    recover_banned_account,
)
from ..accounts.manager import get_credential, update_health
from ..config import settings
from ..core.db import session_scope
from ..core.enums import AccountHealth, JobStatus, Platform
from ..core.models import Account, PublishJob
from ..publishers.registry import default_registry
from ..publishers.plugin_sdk import (
    PublisherPluginResolutionError,
    is_publisher_plugin_instance,
)
from ..runtime.account_lease import (
    AccountOperationLease,
    AccountOperationLeaseTimeout,
)
from ..observability import get_logger
from ..observability.sentry import (
    capture_exception,
    redacted_external_exception,
    safe_exception_type,
)
from .queue import queue

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _AccountProbePlan:
    """Version snapshot used to reject a stale external health result."""

    account_id: int
    platform: Platform
    credential: dict
    recovering_ban: bool
    health: AccountHealth
    last_publish_at: datetime | None
    last_health_check_at: datetime | None
    credential_digest: bytes
    probe_started_at: datetime


def _credential_digest(account: Account) -> bytes:
    """Compare credential generations without retaining plaintext in the plan."""
    return hashlib.sha256(bytes(account.encrypted_credential or b"")).digest()


def _has_running_publish(session, account_id: int) -> bool:
    return (
        session.scalar(
            select(PublishJob.id)
            .where(PublishJob.account_id == account_id)
            .where(PublishJob.status == JobStatus.RUNNING)
            .limit(1)
        )
        is not None
    )


def _publish_started_since(session, account_id: int, since: datetime) -> bool:
    """Catch a publish that started and completed while the probe was in flight."""
    return (
        session.scalar(
            select(PublishJob.id)
            .where(PublishJob.account_id == account_id)
            .where(PublishJob.started_at.is_not(None))
            .where(PublishJob.started_at >= since)
            .limit(1)
        )
        is not None
    )


def _prepare_probe(account_id: int) -> _AccountProbePlan | None:
    """Take a fresh account snapshot immediately before touching its profile.

    Publish and health additionally share a kernel-backed account lease. Login
    and metrics participation remains a Phase 1 extension; the database checks
    here also prevent a stale probe from overwriting newer account state.
    """
    probe_started_at = datetime.utcnow()
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            return None
        recovering_ban = AccountHealth(account.health) == AccountHealth.BANNED
        if recovering_ban and not is_ban_probe_due(account):
            return None
        if _has_running_publish(session, account_id):
            logger.info(
                "scheduler.health: skipped account with running publish",
                extra={"account_id": account_id},
            )
            return None
        try:
            credential = get_credential(session, account_id)
        except Exception as exc:
            # Some disk-profile adapters intentionally have no database credential.
            logger.warning(
                "scheduler.health.credential_load: swallowed",
                extra={"account_id": account_id, "exception_type": type(exc).__name__},
            )
            capture_exception(
                exc,
                scope="scheduler.health.credential_load",
                account_id=account_id,
            )
            credential = {}
        return _AccountProbePlan(
            account_id=account_id,
            platform=Platform(account.platform),
            credential=credential,
            recovering_ban=recovering_ban,
            health=AccountHealth(account.health),
            last_publish_at=account.last_publish_at,
            last_health_check_at=account.last_health_check_at,
            credential_digest=_credential_digest(account),
            probe_started_at=probe_started_at,
        )


def _probe_is_stale(session, account: Account, plan: _AccountProbePlan) -> bool:
    """Return true when publication/account state changed during the probe."""
    return bool(
        _has_running_publish(session, plan.account_id)
        or _publish_started_since(session, plan.account_id, plan.probe_started_at)
        or AccountHealth(account.health) != plan.health
        or account.last_publish_at != plan.last_publish_at
        or account.last_health_check_at != plan.last_health_check_at
        or _credential_digest(account) != plan.credential_digest
    )


async def check_all_accounts() -> dict:
    """全量健康检查。返回 {account_id: health}。"""
    results: dict[int, str] = {}

    with session_scope() as s:
        account_ids = list(s.scalars(select(Account.id)).all())

    for account_id in account_ids:
        # Accounts are processed sequentially, so the concurrency check and
        # credential/version snapshot must be fresh here rather than at list time.
        plan = _prepare_probe(account_id)
        if plan is None:
            continue
        try:
            pubs = default_registry.resolve(plan.platform)
        except PublisherPluginResolutionError as exc:
            logger.error(
                "scheduler.health: Publisher plugin routing blocked",
                extra={
                    "account_id": account_id,
                    "error_code": exc.code,
                },
            )
            results[account_id] = AccountHealth.UNKNOWN.value
            continue
        if not pubs:
            continue
        health = AccountHealth.UNKNOWN
        try:
            # Health never waits behind a publish. If it wins the race first,
            # the worker waits on the same lease; profile access is serialized.
            async with AccountOperationLease(account_id, timeout_seconds=0):
                for pub in pubs:
                    plugin_publisher = is_publisher_plugin_instance(pub)
                    try:
                        candidate = AccountHealth(
                            await asyncio.wait_for(
                                pub.health_check(account_id, plan.credential),
                                timeout=float(
                                    getattr(settings, "health_probe_timeout_seconds", 60)
                                ),
                            )
                        )
                    except (Exception, SystemExit) as e:
                        if isinstance(e, SystemExit) and not plugin_publisher:
                            raise
                        # 探活炸了不阻断 fallback/后续账号，但必须 capture。
                        exception_type = safe_exception_type(e)
                        logger.warning(
                            "scheduler.health.check: swallowed",
                            extra={
                                "account_id": account_id,
                                "platform": str(plan.platform),
                                "exception_type": exception_type,
                            },
                        )
                        capture_exception(
                            redacted_external_exception(e),
                            scope="scheduler.health.check",
                            account_id=account_id,
                            platform=str(plan.platform),
                            exception_type=exception_type,
                        )
                        continue
                    health = candidate
                    # UNKNOWN 表示该实现不可用或网络结果不明，可以让同账号的下一种
                    # 适配技术继续做只读探活；EXPIRED/BANNED 等确定结果不可覆盖。
                    if candidate != AccountHealth.UNKNOWN:
                        break
        except AccountOperationLeaseTimeout:
            logger.info(
                "scheduler.health: account operation lease busy; probe skipped",
                extra={"account_id": account_id},
            )
            continue
        except OSError as exc:
            logger.warning(
                "scheduler.health: account operation lease unavailable",
                extra={"account_id": account_id, "exception_type": type(exc).__name__},
            )
            capture_exception(
                exc,
                scope="scheduler.health.account_lease",
                account_id=account_id,
            )
            continue

        with session_scope() as s:
            # PostgreSQL workers take the same Account row lock while claiming a
            # job. Holding it for this short validation/write transaction closes
            # the final check-to-commit race without a long external DB lock.
            acc = s.scalar(
                select(Account)
                .where(Account.id == account_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if acc is None or _probe_is_stale(s, acc, plan):
                logger.info(
                    "scheduler.health: discarded stale probe result",
                    extra={"account_id": account_id},
                )
                continue

            if plan.recovering_ban:
                # Re-check inside the write transaction.  A newer ban/pause may
                # have been applied while the external health probe was running.
                if AccountHealth(acc.health) != AccountHealth.BANNED:
                    effective_health = AccountHealth(acc.health)
                elif health == AccountHealth.HEALTHY and recover_banned_account(s, account_id):
                    effective_health = AccountHealth.HEALTHY
                else:
                    # UNKNOWN and every explicit unhealthy result are
                    # fail-closed: record the probe, retain BANNED, and retry on
                    # the next daily health cycle.
                    record_banned_probe(s, account_id, health)
                    effective_health = AccountHealth.BANNED
            else:
                update_health(s, account_id, health)
                effective_health = health

            # 通知模块（Task B）：登录态失效/被封 → 推 IM 提醒账号负责人
            # 在 session 内组装 snapshot，避免 detached account 在出块后查询失败
            notify_snapshot = None
            # A due-but-unrecovered BANNED account was already notified when it
            # entered that state; do not create a daily notification loop.
            if not plan.recovering_ban and effective_health in (
                AccountHealth.EXPIRED,
                AccountHealth.BANNED,
            ):
                acc = s.get(Account, account_id)
                if acc is not None:
                    notify_snapshot = {
                        "id": acc.id,
                        "nickname": acc.nickname,
                        "platform": acc.platform,
                        "health": effective_health,
                    }
        results[account_id] = effective_health.value

        # 出 session 后调通知，notify 内部容错——不影响下一个账号的探活循环
        if notify_snapshot is not None:
            try:
                from ..notify import account_expired

                account_expired(notify_snapshot)
            except Exception as e:
                # 通知是辅助通道，失败不能阻断探活循环——但必须 capture，
                # 否则账号被封后运营群收不到提醒，损失全在生产侧
                logger.warning(
                    "scheduler.health.notify: swallowed",
                    extra={
                        "account_id": account_id,
                        "health": notify_snapshot.get("health").value
                        if hasattr(notify_snapshot.get("health"), "value")
                        else str(notify_snapshot.get("health")),
                        "exception_type": type(e).__name__,
                    },
                )
                capture_exception(
                    e,
                    scope="scheduler.health.notify",
                    account_id=account_id,
                )

    return {
        "checked_at": datetime.utcnow().isoformat(),
        "count": len(results),
        "results": results,
    }


def schedule_daily_health_check(cron: str = "0 2 * * *") -> str:
    """注册每日健康检查（默认 02:00 凌晨）。"""
    return queue.schedule_cron(
        cron,
        check_all_accounts,
        job_id="daily-health-check",
    )
