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
from .queue import queue


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
            except Exception:
                cred = {}
            plan.append((a.id, Platform(a.platform), cred))

    for account_id, platform, credential in plan:
        try:
            pubs = default_registry.resolve(platform)
            if not pubs:
                continue
            health = await pubs[0].health_check(account_id, credential)
        except Exception:
            health = AccountHealth.UNKNOWN

        with session_scope() as s:
            update_health(s, account_id, health)
        results[account_id] = health.value if hasattr(health, "value") else str(health)

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
