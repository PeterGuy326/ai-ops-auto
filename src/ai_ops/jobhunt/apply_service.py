"""投递编排服务（P2）—— READY 候选 → 真投递 → 状态机迁移。

对齐 pipeline.py「采集落 DRAFT」的下半场：人工 approve 把 DRAFT→READY 后，
本服务把 READY 的 Application 真投出去（Boss 聊天式发招呼语），落 APPLIED/FAILED/DEAD。

边界（刻意都在编排层、不进 applier，保持 applier 是无状态薄壳）：
  - 账号绑定：按 board 选一个有凭证、当日未超配额的 JobAccount，凭证 Fernet 解密后传入
  - 配额闸：单账号当日 APPLIED 数 ≥ daily_quota 就停（养号防风控）
  - 状态机：成功→APPLIED；失败→attempts+1，未到 max→FAILED（可重试），到顶→DEAD
  - 幂等：只处理 READY；APPLIED/DEAD 不会被重复投

测试友好：applier、store 均可注入；无副作用的 sleep（节流）由调用方/真 applier 承担，
本服务不自己 sleep，便于离线确定性单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..accounts.store import CredentialStore, get_store
from ..core.models import _now
from .accounts import get_credential
from .appliers.base import ApplierBase
from .enums import ApplicationStatus, JobBoard
from .models import Application, JobAccount, JobPosting, ResumeProfile
from .schemas import ApplyResult, JobCandidate


@dataclass
class ExecuteReport:
    """一次 execute 的战报。"""
    applied: int = 0              # 真投出去的
    failed: int = 0              # 本轮失败（可重试，FAILED）
    dead: int = 0               # 重试耗尽置 DEAD
    skipped_no_account: int = 0  # 找不到可用账号
    skipped_quota: int = 0       # 账号当日配额已满
    applied_ids: list[int] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)  # (app_id, error)


def _today_applied_count(session: Session, account_id: int) -> int:
    """某账号今天（UTC 自然日）已投出的数量——配额闸用。"""
    now = _now()
    today_start = datetime(now.year, now.month, now.day)
    return session.scalar(
        select(func.count(Application.id))
        .where(Application.account_id == account_id)
        .where(Application.status == ApplicationStatus.APPLIED)
        .where(Application.applied_at >= today_start)
    ) or 0


def _pick_account(
    session: Session,
    board: JobBoard,
    *,
    account_id: int | None,
    require_credential: bool = True,
) -> JobAccount | None:
    """选投递账号：显式指定优先；否则取该平台账号里当日剩余配额最多的。

    require_credential=False（CDP 模式）时不要求账号存 cookie——登录态来自真 Chrome，
    账号行只用来记配额/绑定。
    """
    if account_id is not None:
        acc = session.get(JobAccount, account_id)
        if acc is None:
            return None
        if require_credential and not acc.encrypted_credential:
            return None
        return acc

    candidates = list(
        session.scalars(
            select(JobAccount)
            .where(JobAccount.board == board)
            .order_by(JobAccount.id)
        )
    )
    # 选剩余配额最多的（含 0：配额满也算「有账号」，由 execute 的配额闸记 skipped_quota，
    # 而不是误报 no_account）。best_remaining 起始 -1 让 remaining=0 也能入选。
    best: JobAccount | None = None
    best_remaining = -1
    for acc in candidates:
        if require_credential and not acc.encrypted_credential:
            continue
        remaining = (acc.daily_quota or 0) - _today_applied_count(session, acc.id)
        if remaining > best_remaining:
            best, best_remaining = acc, remaining
    return best


def _to_candidate(job: JobPosting) -> JobCandidate:
    """ORM JobPosting → 平台无关 JobCandidate（applier.apply 的入参，避免耦合 session）。"""
    return JobCandidate(
        board=job.board,
        external_id=job.external_id,
        url=job.url,
        title=job.title,
        company=job.company,
        location=job.location,
        salary_text=job.salary_text,
        jd_text=job.jd_text,
        tags=list(job.tags or []),
        raw=dict(job.raw or {}),
    )


def _resume_summary(resume: ResumeProfile) -> str:
    return resume.summary or (resume.structured or {}).get("summary", "")


async def execute_applications(
    session: Session,
    *,
    applier: ApplierBase,
    board: JobBoard = JobBoard.BOSS,
    account_id: int | None = None,
    limit: int = 10,
    store: CredentialStore | None = None,
) -> ExecuteReport:
    """把 READY 的 Application 真投出去。

    Args:
        applier: 平台适配器（真投用 BossApplier，离线演练用 FakeApplier）。
        board: 只处理这个平台的 READY 记录。
        account_id: 指定用哪个账号；None 时自动选当日剩余配额最多的。
        limit: 本轮最多投几条。
        store: 凭证解密用；None 时取全局 get_store()（需 FERNET_KEY）。
    """
    report = ExecuteReport()

    ready = list(
        session.scalars(
            select(Application)
            .where(Application.status == ApplicationStatus.READY)
            .where(Application.board == board)
            .order_by(Application.id)
            .limit(limit)
        )
    )
    if not ready:
        return report

    from .browser import cdp_enabled

    use_cdp = cdp_enabled()
    acc = _pick_account(session, board, account_id=account_id, require_credential=not use_cdp)
    if acc is None:
        report.skipped_no_account = len(ready)
        return report

    # CDP 模式登录态来自真 Chrome，不解密 cookie；否则从账号解密取凭证。
    if use_cdp:
        credential: dict = {}
    else:
        st = store or get_store()
        credential = get_credential(session, acc.id, store=st)
    quota = acc.daily_quota or 0
    used = _today_applied_count(session, acc.id)

    for app in ready:
        if quota and used >= quota:
            report.skipped_quota += 1
            continue

        job = session.get(JobPosting, app.job_id)
        resume = session.get(ResumeProfile, app.resume_id)
        if job is None or resume is None:
            app.status = ApplicationStatus.FAILED
            app.error = "关联岗位/简历缺失"
            report.failed += 1
            report.errors.append((app.id, app.error))
            continue

        app.account_id = acc.id
        app.attempts += 1
        try:
            result: ApplyResult = await applier.apply(
                credential=credential,
                job=_to_candidate(job),
                resume_summary=_resume_summary(resume),
                greeting=app.greeting,
            )
        except Exception as e:  # applier 自身已尽量不抛，这里再兜一层
            result = ApplyResult(success=False, error=f"applier 异常: {e}")

        if result.success:
            app.status = ApplicationStatus.APPLIED
            app.applied_at = _now()
            app.error = None
            app.raw = {**(app.raw or {}), **result.raw}
            acc.last_apply_at = _now()
            used += 1
            report.applied += 1
            report.applied_ids.append(app.id)
        else:
            app.error = result.error
            if app.attempts >= app.max_attempts:
                app.status = ApplicationStatus.DEAD
                report.dead += 1
            else:
                app.status = ApplicationStatus.FAILED
                report.failed += 1
            report.errors.append((app.id, result.error or "未知失败"))

        session.flush()

    return report


def approve_applications(
    session: Session,
    *,
    ids: list[int] | None = None,
    min_score: float | None = None,
    board: JobBoard | None = None,
) -> list[int]:
    """人工闸：把 DRAFT 候选勾选为 READY（待投）。返回被推进的 Application id。

    两种用法（可叠加）：
      - ids：精确勾选这些
      - min_score：把匹配分 ≥ 阈值的 DRAFT 一并推进（批量）
    都不给则报空，避免误把全部 DRAFT 放行。
    """
    from .models import JobMatch

    if not ids and min_score is None:
        return []

    q = select(Application).where(Application.status == ApplicationStatus.DRAFT)
    if board is not None:
        q = q.where(Application.board == board)
    if ids:
        q = q.where(Application.id.in_(ids))

    promoted: list[int] = []
    for app in session.scalars(q):
        if min_score is not None and not ids:
            score = session.scalar(
                select(JobMatch.score).where(
                    JobMatch.resume_id == app.resume_id, JobMatch.job_id == app.job_id
                )
            )
            if score is None or score < min_score:
                continue
        app.status = ApplicationStatus.READY
        promoted.append(app.id)
    session.flush()
    return promoted
