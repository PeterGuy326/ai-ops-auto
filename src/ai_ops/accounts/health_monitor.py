"""账号健康度自动闭环：metrics 异常 → 降级 + 暂停。

闭环路径：
  publish 成功 → schedule_after_publish 调度 1h/24h/7d 三次 collect_one
  → 24h 这次回流后，调 evaluate_after_metrics(job_id)
  → 比对该账号近 7 天 baseline，若 views 跌破 20% → 累计触发
  → 近 3 次都触发 → DEGRADED + paused_until=now+48h
  → 连续 5 次都触发 → BANNED + paused_until=now+7d

设计要点：
  - paused_until 写入 account.profile["paused_until"]（ISO 字符串），不动 ORM schema
  - 与 P7-A 在改的 manager.py 解耦：我只 *读* account.profile 和 *调* update_health
  - "近 N 次发布"窗口口径：按 PublishJob.started_at desc 取最近 N 个 SUCCESS job，
    再看每个 job 的"24h 节点 metric"是否曾被标 low_views
  - 触发判定不依赖额外字段，metrics 异常状态用 job.raw_response 不污染——直接当场比对最新 metric
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..core.enums import AccountHealth, JobStatus
from ..core.models import Account, Metrics, PublishJob


# 触发条件阈值 —— 调保守
LOW_VIEW_RATIO = 0.2          # 当次 views < baseline.views * 0.2 视为低曝光
MIN_BASELINE_SAMPLES = 3      # baseline 至少要 3 个历史 metric 样本，否则 skip
DEGRADE_TRIGGER_WINDOW = 3    # 最近 3 次 24h 节点都低曝光 → DEGRADED
BAN_TRIGGER_WINDOW = 5        # 连续 5 次 → BANNED
DEGRADE_PAUSE_HOURS = 48
BAN_PAUSE_HOURS = 24 * 7      # 7 天


Decision = Literal["healthy", "degraded", "banned", "skip"]


@dataclass(slots=True)
class HealthAction:
    """evaluate_after_metrics 的返回结果。"""
    account_id: int
    decision: Decision
    reason: str
    baseline: dict | None = None
    current: dict | None = None
    paused_until: Optional[datetime] = None


# ---------------------------------------------------------------------------- #
# baseline 计算
# ---------------------------------------------------------------------------- #
def compute_baseline(session: Session, account_id: int, lookback_days: int = 7) -> dict:
    """算该账号过去 N 天发布的中位数指标（views/likes/comments）。

    口径：取每个 SUCCESS job 的"最新一条 metric"（通常是 7d 那条；不足 24h 的取最新）。
    返回 {views, likes, comments, sample_size}；sample_size < MIN_BASELINE_SAMPLES 时调用方需 skip。
    """
    since = datetime.utcnow() - timedelta(days=lookback_days)

    jobs = (
        session.query(PublishJob)
        .filter(
            PublishJob.account_id == account_id,
            PublishJob.status == JobStatus.SUCCESS,
            PublishJob.finished_at >= since,
        )
        .order_by(desc(PublishJob.finished_at))
        .all()
    )

    views_list: list[int] = []
    likes_list: list[int] = []
    comments_list: list[int] = []
    for j in jobs:
        latest = (
            session.query(Metrics)
            .filter(Metrics.job_id == j.id)
            .order_by(desc(Metrics.collected_at))
            .first()
        )
        if latest is None:
            continue
        views_list.append(latest.views or 0)
        likes_list.append(latest.likes or 0)
        comments_list.append(latest.comments or 0)

    sample = len(views_list)
    if sample == 0:
        return {"views": 0, "likes": 0, "comments": 0, "sample_size": 0}

    return {
        "views": int(statistics.median(views_list)),
        "likes": int(statistics.median(likes_list)),
        "comments": int(statistics.median(comments_list)),
        "sample_size": sample,
    }


# ---------------------------------------------------------------------------- #
# pause / is_paused
# ---------------------------------------------------------------------------- #
def pause_account(
    session: Session,
    account_id: int,
    hours: int = DEGRADE_PAUSE_HOURS,
    *,
    health: AccountHealth = AccountHealth.DEGRADED,
    reason: str = "",
) -> datetime:
    """暂停账号 N 小时 + 调 update_health。返回 paused_until 时间。

    边界约定：
      - 只写 account.profile["paused_until"]（ISO 字符串），不动 ORM schema
      - update_health 复用 manager.py 暴露的函数（不在本模块二次实现）
    """
    from .manager import update_health  # 局部 import 防 circular

    a = session.get(Account, account_id)
    if a is None:
        raise ValueError(f"account {account_id} not found")

    until = datetime.utcnow() + timedelta(hours=hours)
    profile = dict(a.profile or {})
    profile["paused_until"] = until.isoformat()
    if reason:
        profile["paused_reason"] = reason
    a.profile = profile

    update_health(session, account_id, health)
    return until


def is_paused(account: Account) -> bool:
    """读 profile["paused_until"]，过期自动失效。

    注意：纯读，不写库；调用方应在 worker.py:execute_job 前做检查。
    解析失败/字段缺失 → False（默认放行，避免把正常账号误锁）。
    """
    if account is None or not account.profile:
        return False
    raw = account.profile.get("paused_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    return datetime.utcnow() < until


def get_paused_until(account: Account) -> Optional[datetime]:
    """供 worker.py 在 error message 里展示。"""
    if account is None or not account.profile:
        return None
    raw = account.profile.get("paused_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return until if datetime.utcnow() < until else None


# ---------------------------------------------------------------------------- #
# 核心：metrics 回流后评估
# ---------------------------------------------------------------------------- #
def evaluate_after_metrics(session: Session, job_id: int) -> HealthAction:
    """采集到一个 job 的最新 metric 后调用。返回处置动作（已落库）。

    流程：
      1. 拿 job → account_id
      2. 算 baseline（过去 7 天该账号中位数）
      3. 比对当前 metric.views 是否跌破 LOW_VIEW_RATIO * baseline.views
      4. 若 yes，回溯该账号最近 N 次 SUCCESS job，看连续触发次数
         - >= BAN_TRIGGER_WINDOW → BANNED + pause 7d
         - >= DEGRADE_TRIGGER_WINDOW → DEGRADED + pause 48h
         - 否则不动，返回 healthy（仅本次低，可能噪声）
    """
    job = session.get(PublishJob, job_id)
    if job is None:
        return HealthAction(account_id=0, decision="skip", reason=f"job {job_id} 不存在")

    account_id = job.account_id

    # 当前 metric：取该 job 最新一条
    current_metric = (
        session.query(Metrics)
        .filter(Metrics.job_id == job_id)
        .order_by(desc(Metrics.collected_at))
        .first()
    )
    if current_metric is None:
        return HealthAction(
            account_id=account_id, decision="skip", reason="该 job 还没采集到任何 metric"
        )

    current = {
        "views": current_metric.views or 0,
        "likes": current_metric.likes or 0,
        "comments": current_metric.comments or 0,
    }

    baseline = compute_baseline(session, account_id, lookback_days=7)
    if baseline["sample_size"] < MIN_BASELINE_SAMPLES:
        return HealthAction(
            account_id=account_id,
            decision="skip",
            reason=f"baseline 样本不足（{baseline['sample_size']} < {MIN_BASELINE_SAMPLES}）",
            baseline=baseline,
            current=current,
        )

    threshold_views = baseline["views"] * LOW_VIEW_RATIO
    this_low = current["views"] < threshold_views

    if not this_low:
        return HealthAction(
            account_id=account_id,
            decision="healthy",
            reason=f"views={current['views']} >= 阈值 {threshold_views:.1f}（baseline {baseline['views']}）",
            baseline=baseline,
            current=current,
        )

    # 当前 low：回溯近 N 次 SUCCESS job 的最新 metric，看是否连续低
    recent_count = _count_recent_low_views(session, account_id, baseline["views"], BAN_TRIGGER_WINDOW)

    if recent_count >= BAN_TRIGGER_WINDOW:
        until = pause_account(
            session,
            account_id,
            hours=BAN_PAUSE_HOURS,
            health=AccountHealth.BANNED,
            reason=f"连续 {recent_count} 次低曝光",
        )
        return HealthAction(
            account_id=account_id,
            decision="banned",
            reason=f"连续 {recent_count} 次低曝光（>= {BAN_TRIGGER_WINDOW}），BANNED + pause {BAN_PAUSE_HOURS}h",
            baseline=baseline,
            current=current,
            paused_until=until,
        )

    if recent_count >= DEGRADE_TRIGGER_WINDOW:
        until = pause_account(
            session,
            account_id,
            hours=DEGRADE_PAUSE_HOURS,
            health=AccountHealth.DEGRADED,
            reason=f"连续 {recent_count} 次低曝光",
        )
        return HealthAction(
            account_id=account_id,
            decision="degraded",
            reason=f"连续 {recent_count} 次低曝光（>= {DEGRADE_TRIGGER_WINDOW}），DEGRADED + pause {DEGRADE_PAUSE_HOURS}h",
            baseline=baseline,
            current=current,
            paused_until=until,
        )

    return HealthAction(
        account_id=account_id,
        decision="healthy",
        reason=f"本次低曝光但累计仅 {recent_count} 次，未达降级线",
        baseline=baseline,
        current=current,
    )


def _count_recent_low_views(
    session: Session, account_id: int, baseline_views: int, window: int
) -> int:
    """回溯该账号最近 `window` 个 SUCCESS job，看其最新 metric 低于阈值的个数。

    口径：低于 baseline_views * LOW_VIEW_RATIO 即算 low。
    """
    threshold = baseline_views * LOW_VIEW_RATIO
    jobs = (
        session.query(PublishJob)
        .filter(
            PublishJob.account_id == account_id,
            PublishJob.status == JobStatus.SUCCESS,
        )
        .order_by(desc(PublishJob.finished_at))
        .limit(window)
        .all()
    )
    count = 0
    for j in jobs:
        latest = (
            session.query(Metrics)
            .filter(Metrics.job_id == j.id)
            .order_by(desc(Metrics.collected_at))
            .first()
        )
        if latest is None:
            continue
        if (latest.views or 0) < threshold:
            count += 1
    return count
