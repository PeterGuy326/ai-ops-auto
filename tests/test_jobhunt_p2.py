"""jobhunt 专题 P2 验收测试 —— 人工 approve(DRAFT→READY) → 真投递编排(READY→APPLIED)。

纯离线、确定性：
  - Applier 用 FakeApplier（永远成功）/ 自定义 FailingApplier（永远失败）
  - 凭证用临时 Fernet key 注入的 CredentialStore，无需配 FERNET_KEY env
  - 不碰真浏览器、不 sleep

覆盖：
  1. approve：ids 精确放行 / min_score 批量放行 / 空参不误放全部
  2. execute happy path：READY → APPLIED，绑定 account、写 applied_at、bump last_apply_at
  3. 配额闸：daily_quota 限制单账号当日投递数
  4. 失败路径：applier 失败 → FAILED；重试到 max_attempts → DEAD
  5. 无可用账号：READY 原样不动，计 skipped_no_account
  6. 账号自动选择：跳过无凭证账号，选当日剩余配额最多的
"""
from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.accounts.store import CredentialStore
from ai_ops.core.models import Base
from ai_ops.jobhunt import models as jh_models  # noqa: F401  注册四表
from ai_ops.jobhunt.accounts import create_account
from ai_ops.jobhunt.apply_service import (
    approve_applications,
    execute_applications,
)
from ai_ops.jobhunt.appliers.base import ApplierBase
from ai_ops.jobhunt.appliers.fake import FakeApplier
from ai_ops.jobhunt.enums import ApplicationStatus, JobBoard
from ai_ops.jobhunt.models import (
    Application,
    JobAccount,
    JobMatch,
    JobPosting,
    ResumeProfile,
)
from ai_ops.jobhunt.schemas import ApplyResult


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def store():
    return CredentialStore(key=Fernet.generate_key().decode())


_COOKIES = [{"name": "bst", "value": "tok", "domain": ".zhipin.com", "path": "/"}]


@pytest.fixture()
def resume(session):
    r = ResumeProfile(
        name="后端-2026",
        raw_text="",
        structured={"summary": "6 年 Go 后端", "skills": ["Go"]},
        summary="6 年 Go 后端",
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _make_job(session, ext: str, title: str = "后端工程师") -> JobPosting:
    job = JobPosting(
        board=JobBoard.BOSS,
        external_id=ext,
        url=f"https://www.zhipin.com/job_detail/{ext}.html",
        title=title,
        company="示例公司",
        location="杭州",
    )
    session.add(job)
    session.flush()
    return job


def _make_app(
    session, resume, job, *, status=ApplicationStatus.READY, greeting="你好，我很合适"
) -> Application:
    app = Application(
        resume_id=resume.id,
        job_id=job.id,
        board=JobBoard.BOSS,
        status=status,
        greeting=greeting,
    )
    session.add(app)
    session.flush()
    return app


def _make_match(session, resume, job, score: float) -> JobMatch:
    m = JobMatch(resume_id=resume.id, job_id=job.id, score=score)
    session.add(m)
    session.flush()
    return m


class FailingApplier(ApplierBase):
    """永远失败的 applier，用于验证失败/重试状态机。"""

    board = JobBoard.BOSS

    async def search_jobs(self, query, *, credential=None):
        return []

    async def apply(self, *, credential, job, resume_summary, greeting) -> ApplyResult:
        return ApplyResult(success=False, error="模拟投递失败")


# ---------------------------------------------------------------------------
# 1) approve：DRAFT → READY
# ---------------------------------------------------------------------------
def test_approve_by_ids(session, resume):
    a = _make_app(session, resume, _make_job(session, "j1"), status=ApplicationStatus.DRAFT)
    b = _make_app(session, resume, _make_job(session, "j2"), status=ApplicationStatus.DRAFT)

    promoted = approve_applications(session, ids=[a.id])

    assert promoted == [a.id]
    assert a.status == ApplicationStatus.READY
    assert b.status == ApplicationStatus.DRAFT  # 没勾的不动


def test_approve_by_min_score(session, resume):
    j_hi, j_lo = _make_job(session, "hi"), _make_job(session, "lo")
    a_hi = _make_app(session, resume, j_hi, status=ApplicationStatus.DRAFT)
    a_lo = _make_app(session, resume, j_lo, status=ApplicationStatus.DRAFT)
    _make_match(session, resume, j_hi, 85.0)
    _make_match(session, resume, j_lo, 50.0)

    promoted = approve_applications(session, min_score=75.0)

    assert promoted == [a_hi.id]
    assert a_hi.status == ApplicationStatus.READY
    assert a_lo.status == ApplicationStatus.DRAFT  # 分不够不放行


def test_approve_empty_args_promotes_nothing(session, resume):
    a = _make_app(session, resume, _make_job(session, "j1"), status=ApplicationStatus.DRAFT)
    assert approve_applications(session) == []
    assert a.status == ApplicationStatus.DRAFT  # 不给条件 = 不误放全部


# ---------------------------------------------------------------------------
# 2) execute happy path
# ---------------------------------------------------------------------------
async def test_execute_happy_path(session, resume, store):
    acc = create_account(session, JobBoard.BOSS, "号1", _COOKIES, store=store)
    app = _make_app(session, resume, _make_job(session, "j1"))

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.applied == 1
    assert rep.applied_ids == [app.id]
    assert app.status == ApplicationStatus.APPLIED
    assert app.account_id == acc.id
    assert app.applied_at is not None
    assert app.attempts == 1
    assert app.error is None
    assert acc.last_apply_at is not None


async def test_execute_only_touches_ready(session, resume, store):
    create_account(session, JobBoard.BOSS, "号1", _COOKIES, store=store)
    draft = _make_app(session, resume, _make_job(session, "d"), status=ApplicationStatus.DRAFT)
    ready = _make_app(session, resume, _make_job(session, "r"), status=ApplicationStatus.READY)

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.applied == 1
    assert ready.status == ApplicationStatus.APPLIED
    assert draft.status == ApplicationStatus.DRAFT  # DRAFT 不会被投


# ---------------------------------------------------------------------------
# 3) 配额闸
# ---------------------------------------------------------------------------
async def test_execute_respects_daily_quota(session, resume, store):
    create_account(session, JobBoard.BOSS, "号1", _COOKIES, daily_quota=1, store=store)
    a = _make_app(session, resume, _make_job(session, "j1"))
    b = _make_app(session, resume, _make_job(session, "j2"))

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.applied == 1
    assert rep.skipped_quota == 1
    statuses = sorted([a.status, b.status], key=lambda s: s.value)
    assert ApplicationStatus.APPLIED in statuses
    assert ApplicationStatus.READY in statuses  # 超配额那条留在 READY 等下次


async def test_quota_counts_prior_applied_today(session, resume, store):
    acc = create_account(session, JobBoard.BOSS, "号1", _COOKIES, daily_quota=2, store=store)
    # 今天已投 2 条（占满配额）
    for ext in ("old1", "old2"):
        done = _make_app(session, resume, _make_job(session, ext), status=ApplicationStatus.APPLIED)
        done.account_id = acc.id
        done.applied_at = datetime.utcnow()
    session.flush()
    _make_app(session, resume, _make_job(session, "new"))

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.applied == 0
    assert rep.skipped_quota == 1


# ---------------------------------------------------------------------------
# 4) 失败 → FAILED → DEAD
# ---------------------------------------------------------------------------
async def test_execute_failure_marks_failed_then_retriable(session, resume, store):
    create_account(session, JobBoard.BOSS, "号1", _COOKIES, store=store)
    app = _make_app(session, resume, _make_job(session, "j1"))

    rep = await execute_applications(session, applier=FailingApplier(), store=store)

    assert rep.failed == 1 and rep.applied == 0
    assert app.status == ApplicationStatus.FAILED  # attempts=1 < max=3，可重试
    assert app.attempts == 1
    assert app.error == "模拟投递失败"


async def test_execute_failure_exhausts_to_dead(session, resume, store):
    create_account(session, JobBoard.BOSS, "号1", _COOKIES, daily_quota=99, store=store)
    app = _make_app(session, resume, _make_job(session, "j1"))
    app.attempts = 2  # 再失败一次就到 max_attempts=3

    rep = await execute_applications(session, applier=FailingApplier(), store=store)

    assert rep.dead == 1
    assert app.status == ApplicationStatus.DEAD
    assert app.attempts == 3


# ---------------------------------------------------------------------------
# 5) 无可用账号
# ---------------------------------------------------------------------------
async def test_execute_no_account_skips(session, resume, store):
    app = _make_app(session, resume, _make_job(session, "j1"))

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.skipped_no_account == 1
    assert rep.applied == 0
    assert app.status == ApplicationStatus.READY  # 没账号别动它


# ---------------------------------------------------------------------------
# 6) 账号自动选择
# ---------------------------------------------------------------------------
async def test_execute_cdp_mode_uses_credentialless_account(session, resume, monkeypatch):
    """CDP 模式：账号无 cookie 也能投（登录态来自真 Chrome），不需要 store 解密。"""
    import ai_ops.jobhunt.browser as br
    from ai_ops.jobhunt.accounts import create_cdp_account

    monkeypatch.setattr(br, "cdp_enabled", lambda: True)
    acc = create_cdp_account(session, JobBoard.BOSS, "CDP号")
    assert acc.encrypted_credential == b""  # 确实没存 cookie
    app = _make_app(session, resume, _make_job(session, "j1"))

    rep = await execute_applications(session, applier=FakeApplier())  # 注意：没传 store

    assert rep.applied == 1
    assert app.status == ApplicationStatus.APPLIED
    assert app.account_id == acc.id


async def test_pick_account_skips_credential_less(session, resume, store):
    # 无凭证账号（直接造，不走 create_account）
    bare = JobAccount(board=JobBoard.BOSS, nickname="空号", daily_quota=99)
    session.add(bare)
    good = create_account(session, JobBoard.BOSS, "好号", _COOKIES, daily_quota=99, store=store)
    app = _make_app(session, resume, _make_job(session, "j1"))

    rep = await execute_applications(session, applier=FakeApplier(), store=store)

    assert rep.applied == 1
    assert app.account_id == good.id  # 跳过无凭证的空号
