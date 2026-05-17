"""多账号管理 + 养号期 + 限流校验。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..core.enums import AccountHealth, JobStatus
from ..core.models import Account, PublishJob
from ..core.schemas import AccountIn, AccountOut
from .store import get_store


@dataclass(slots=True)
class RateCheckResult:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def create_account(session: Session, data: AccountIn) -> AccountOut:
    blob = get_store().encrypt(data.credential_plain) if data.credential_plain else b""
    # tags/group/weight 合并进 profile（避免动 ORM schema）
    profile = dict(data.profile)
    if data.tags:
        profile["tags"] = data.tags
    if data.group:
        profile["group"] = data.group
    if data.weight != 1:
        profile["weight"] = data.weight

    a = Account(
        platform=data.platform,
        nickname=data.nickname,
        profile=profile,
        encrypted_credential=blob,
        daily_quota=data.daily_quota,
        health=AccountHealth.UNKNOWN,
    )
    session.add(a)
    session.flush()
    return _to_out(a)


def update_account(session: Session, account_id: int, data) -> AccountOut:
    """PATCH /accounts/{id}：部分更新（含覆盖加密凭证）。"""
    a = session.get(Account, account_id)
    if a is None:
        raise ValueError(f"account {account_id} not found")

    if data.nickname is not None:
        a.nickname = data.nickname
    if data.daily_quota is not None:
        a.daily_quota = data.daily_quota
    if data.credential_plain is not None:
        a.encrypted_credential = get_store().encrypt(data.credential_plain)

    profile = dict(a.profile or {})
    if data.tags is not None:
        profile["tags"] = data.tags
    if data.group is not None:
        profile["group"] = data.group
    if data.weight is not None:
        profile["weight"] = data.weight
    a.profile = profile

    session.flush()
    return _to_out(a)


def delete_account(session: Session, account_id: int) -> bool:
    a = session.get(Account, account_id)
    if a is None:
        return False
    session.delete(a)
    return True


def list_accounts(session: Session, platform=None) -> list[AccountOut]:
    q = session.query(Account)
    if platform is not None:
        q = q.filter(Account.platform == platform)
    return [_to_out(a) for a in q.all()]


def get_credential(session: Session, account_id: int) -> dict:
    a = session.get(Account, account_id)
    if a is None or not a.encrypted_credential:
        raise ValueError(f"account {account_id} 没有凭证")
    return get_store().decrypt(a.encrypted_credential)


def update_health(session: Session, account_id: int, health: AccountHealth) -> None:
    a = session.get(Account, account_id)
    if a is None:
        return
    a.health = health
    a.last_health_check_at = datetime.utcnow()


def mark_published(session: Session, account_id: int) -> None:
    a = session.get(Account, account_id)
    if a is None:
        return
    a.last_publish_at = datetime.utcnow()


def is_in_nurture_period(account: Account, days: int | None = None) -> bool:
    """新账号 nurture_days 天内禁止发布（养号期）。"""
    threshold = days if days is not None else settings.nurture_days
    if threshold <= 0:
        return False
    return account.created_at + timedelta(days=threshold) > datetime.utcnow()


def check_rate_limit(session: Session, account_id: int) -> RateCheckResult:
    """发布前限流校验：养号期 + 最小间隔 + 单日上限。

    返回 RateCheckResult，allowed=False 时附带 reason 写入 job.error。
    """
    account = session.get(Account, account_id)
    if account is None:
        return RateCheckResult(False, f"account {account_id} 不存在")

    # 1. 养号期
    if is_in_nurture_period(account):
        days_left = (
            account.created_at + timedelta(days=settings.nurture_days) - datetime.utcnow()
        ).days + 1
        return RateCheckResult(
            False, f"养号期未结束（剩余 ~{days_left} 天，配置 nurture_days={settings.nurture_days}）"
        )

    now = datetime.utcnow()

    # 2. 最小间隔
    if account.last_publish_at:
        elapsed = (now - account.last_publish_at).total_seconds()
        if elapsed < settings.publish_min_interval_seconds:
            wait = settings.publish_min_interval_seconds - int(elapsed)
            return RateCheckResult(
                False, f"距上次发布仅 {int(elapsed)}s，最小间隔 {settings.publish_min_interval_seconds}s（还需等 {wait}s）"
            )

    # 3. 单日上限（按当天成功 + 进行中的 job 计数）
    today_start = datetime(now.year, now.month, now.day)
    cap = min(account.daily_quota or settings.publish_max_per_day, settings.publish_max_per_day)
    today_count = session.query(func.count(PublishJob.id)).filter(
        PublishJob.account_id == account_id,
        PublishJob.started_at >= today_start,
        PublishJob.status.in_([JobStatus.SUCCESS, JobStatus.RUNNING, JobStatus.RETRYING]),
    ).scalar() or 0
    if today_count >= cap:
        return RateCheckResult(
            False, f"今日发布数 {today_count} 已达上限 {cap}（min(daily_quota, publish_max_per_day)）"
        )

    return RateCheckResult(True, "ok")


def _to_out(a: Account) -> AccountOut:
    return AccountOut(
        id=a.id,
        platform=a.platform,
        nickname=a.nickname,
        profile=a.profile,
        health=a.health,
        risk_level=a.risk_level,
        daily_quota=a.daily_quota,
        last_publish_at=a.last_publish_at,
        last_health_check_at=a.last_health_check_at,
        created_at=a.created_at,
    )
