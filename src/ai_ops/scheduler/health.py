"""定时健康检查 daemon — 每天扫所有非 BANNED 账号，调对应 publisher.health_check。

接入：lifespan startup 调 schedule_daily_health_check。
默认 02:00 跑（凌晨人少，反爬窗口）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import select

from ..accounts.manager import get_credential, update_health
from ..core.db import session_scope
from ..core.enums import AccountHealth, Platform
from ..core.models import Account
from ..publishers.registry import default_registry
from ..observability import get_logger
from ..observability.sentry import capture_exception
from .queue import queue

logger = get_logger(__name__)


async def check_all_accounts() -> dict:
    """全量健康检查。返回 {account_id: health}。"""
    results: dict[int, str] = {}

    with session_scope() as s:
        accounts = list(
            s.execute(
                select(Account).where(Account.health != AccountHealth.BANNED)
            ).scalars().all()
        )
        # 提前拷贝凭证，避免长事务
        plan = []
        for a in accounts:
            try:
                cred = get_credential(s, a.id)
            except Exception as e:
                # 凭证拿不到 = 该账号本轮跳过探活（cred={} 兜底），但失败必须可观测——
                # 否则一批账号集体掉凭证时 health daemon 静默退化，事故只能事后翻日志
                logger.warning(
                    "scheduler.health.credential_load: swallowed",
                    extra={"account_id": a.id, "error": str(e)},
                )
                capture_exception(e, scope="scheduler.health.credential_load", account_id=a.id)
                cred = {}
            plan.append((a.id, Platform(a.platform), cred))

    for account_id, platform, credential in plan:
        try:
            pubs = default_registry.resolve(platform)
            if not pubs:
                continue
            health = await pubs[0].health_check(account_id, credential)
        except Exception as e:
            # 探活炸了不阻断后续账号——但必须 capture，否则 publisher 集体罢工无人知
            logger.warning(
                "scheduler.health.check: swallowed",
                extra={"account_id": account_id, "platform": str(platform), "error": str(e)},
            )
            capture_exception(
                e,
                scope="scheduler.health.check",
                account_id=account_id,
                platform=str(platform),
            )
            health = AccountHealth.UNKNOWN

        with session_scope() as s:
            update_health(s, account_id, health)
            # 通知模块（Task B）：登录态失效/被封 → 推 IM 提醒账号负责人
            # 在 session 内组装 snapshot，避免 detached account 在出块后查询失败
            notify_snapshot = None
            if health in (AccountHealth.EXPIRED, AccountHealth.BANNED):
                acc = s.get(Account, account_id)
                if acc is not None:
                    notify_snapshot = {
                        "id": acc.id,
                        "nickname": acc.nickname,
                        "platform": acc.platform,
                        "health": health,
                    }
        results[account_id] = health.value if hasattr(health, "value") else str(health)

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
                        "error": str(e),
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
        lambda: asyncio.create_task(check_all_accounts()),
        job_id="daily-health-check",
    )
