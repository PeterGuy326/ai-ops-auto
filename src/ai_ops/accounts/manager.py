"""多账号管理 + 养号期 + 限流校验。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..core.enums import AccountHealth, JobStatus
from ..core.models import Account, PublishJob, Topic
from ..core.schemas import AccountIn, AccountOut
from .store import get_store


# 反风控固定指纹候选（少量真实组合，避免极端冷门）
_OS_POOL = ["windows", "macos"]
_SCREEN_POOL = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 2560, "height": 1440},
]


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
    if data.proxy:
        profile["proxy"] = data.proxy

    # 校验 topic_id 存在性（避免悬空外键）
    if data.topic_id is not None:
        if session.get(Topic, data.topic_id) is None:
            raise ValueError(f"topic {data.topic_id} 不存在")

    a = Account(
        platform=data.platform,
        nickname=data.nickname,
        profile=profile,
        topic_id=data.topic_id,
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
    if data.topic_id is not None:
        # -1 作哨兵：清空绑定（None 表示"不变"，所以用 -1 显式 unset）
        if data.topic_id == -1:
            a.topic_id = None
        else:
            if session.get(Topic, data.topic_id) is None:
                raise ValueError(f"topic {data.topic_id} 不存在")
            a.topic_id = data.topic_id

    profile = dict(a.profile or {})
    if data.tags is not None:
        profile["tags"] = data.tags
    if data.group is not None:
        profile["group"] = data.group
    if data.weight is not None:
        profile["weight"] = data.weight
    if data.proxy is not None:
        # 空串 = 显式清空；非 None 才覆盖
        if data.proxy == "":
            profile.pop("proxy", None)
        else:
            profile["proxy"] = data.proxy
    a.profile = profile

    session.flush()
    return _to_out(a)


def delete_account(session: Session, account_id: int) -> bool:
    a = session.get(Account, account_id)
    if a is None:
        return False
    session.delete(a)
    return True


def list_accounts(session: Session, platform=None, by_topic: int | None = None) -> list[AccountOut]:
    q = session.query(Account)
    if platform is not None:
        q = q.filter(Account.platform == platform)
    if by_topic is not None:
        q = q.filter(Account.topic_id == by_topic)
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


def get_account_proxy(account: Account) -> str:
    """优先 account.profile.proxy，回退到 settings.browser_proxy（共享代理仅作兜底）。

    一机一号一 IP 是反风控核心，强烈建议每个账号配独立代理。
    """
    if account is None:
        return settings.browser_proxy or ""
    return (account.profile or {}).get("proxy") or settings.browser_proxy or ""


def get_account_fingerprint(account: Account) -> dict:
    """按 account.id 派生稳定指纹（OS + 屏幕分辨率），保证同账号每次 launch 指纹一致。

    返回字段对齐 camoufox.AsyncCamoufox 的 launch 参数：
      - os: list[str]，单元素
      - screen: dict{width,height}
      - locale: str
    """
    if account is None or account.id is None:
        h = b"\x00" * 32
    else:
        h = hashlib.sha256(f"xhs:fp:{account.id}".encode()).digest()
    os_choice = _OS_POOL[h[0] % len(_OS_POOL)]
    screen = _SCREEN_POOL[h[1] % len(_SCREEN_POOL)]
    return {
        "os": [os_choice],
        "screen": screen,
        "locale": "zh-CN",
    }


def _to_out(a: Account) -> AccountOut:
    return AccountOut(
        id=a.id,
        platform=a.platform,
        nickname=a.nickname,
        profile=a.profile,
        topic_id=a.topic_id,
        health=a.health,
        risk_level=a.risk_level,
        daily_quota=a.daily_quota,
        last_publish_at=a.last_publish_at,
        last_health_check_at=a.last_health_check_at,
        created_at=a.created_at,
    )
