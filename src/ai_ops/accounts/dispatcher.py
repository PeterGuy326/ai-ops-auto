"""跨账号分发策略 — 同一文章发给多个账号时，按什么规则选账号。

底层逻辑：
  1. 强约束：过滤 BANNED + 养号期内 + 今日配额已满 + 不健康
  2. 排序：按 health (HEALTHY > DEGRADED) → 按 last_publish_at 升序（轮转）
  3. 限量：count 决定取几个；按 weight 加权选择

支持按 group 筛选（"AI赛道" / "情感号" / "北京区域"）。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..core.enums import AccountHealth, JobStatus, Platform
from ..core.models import Account, PublishJob


@dataclass(slots=True)
class DispatchCandidate:
    account_id: int
    nickname: str
    weight: int
    health: str
    last_publish_at: datetime | None


def pick_accounts(
    session: Session,
    platform: Platform,
    *,
    count: int = 1,
    group: str | None = None,
    allow_degraded: bool = False,
) -> list[DispatchCandidate]:
    """按策略选 count 个账号。

    过滤规则：
      - 平台匹配
      - health: 默认只取 HEALTHY/UNKNOWN；allow_degraded=True 时也取 DEGRADED
      - 排除养号期内
      - 排除今日配额已满
      - 可选按 group 过滤
    排序：
      - last_publish_at 升序（最久没发的优先），加权重抽样
    """
    accepted_health: set[str] = {AccountHealth.HEALTHY.value, AccountHealth.UNKNOWN.value}
    if allow_degraded:
        accepted_health.add(AccountHealth.DEGRADED.value)

    candidates_q = select(Account).where(
        Account.platform == platform,
        Account.health.in_(accepted_health),
    )
    candidates = list(session.execute(candidates_q).scalars().all())

    # 群组过滤
    if group:
        candidates = [a for a in candidates if (a.profile or {}).get("group") == group]

    # 养号期 / 今日配额过滤
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    nurture_cutoff = now - timedelta(days=settings.nurture_days)

    filtered: list[Account] = []
    for a in candidates:
        if settings.nurture_days > 0 and a.created_at > nurture_cutoff:
            continue
        today_count = session.scalar(
            select(__import__("sqlalchemy").func.count(PublishJob.id))
            .where(PublishJob.account_id == a.id)
            .where(PublishJob.started_at >= today_start)
            .where(PublishJob.status.in_([JobStatus.SUCCESS, JobStatus.RUNNING, JobStatus.RETRYING]))
        ) or 0
        cap = min(a.daily_quota or settings.publish_max_per_day, settings.publish_max_per_day)
        if today_count >= cap:
            continue
        filtered.append(a)

    if not filtered:
        return []

    # 排序：last_publish_at 升序 + healthy 优先
    def sort_key(a: Account):
        health_rank = 0 if a.health == AccountHealth.HEALTHY else 1
        last_ts = a.last_publish_at.timestamp() if a.last_publish_at else 0
        return (health_rank, last_ts)
    filtered.sort(key=sort_key)

    # 加权抽样（不重复）
    pool = filtered[:max(count * 3, count)]  # 候选池
    weights = [int((a.profile or {}).get("weight", 1)) for a in pool]
    picked: list[Account] = []
    while pool and len(picked) < count:
        chosen = random.choices(pool, weights=weights, k=1)[0]
        idx = pool.index(chosen)
        picked.append(pool.pop(idx))
        weights.pop(idx)

    return [
        DispatchCandidate(
            account_id=a.id,
            nickname=a.nickname,
            weight=int((a.profile or {}).get("weight", 1)),
            health=a.health,
            last_publish_at=a.last_publish_at,
        )
        for a in picked
    ]
