"""tests/test_republish.py — 重发覆盖主流程（publishing-sop §五的物理载体）。

覆盖矩阵：
  1. v2 字段复制语义：article_id / account_id / platform / publisher_kind / max_attempts 复用
  2. v1.superseded_by_job_id → v2.id（_mark_job_superseded 真生效）
  3. 状态白名单：FAILED / DEAD 放行；SUCCESS / PENDING / RUNNING / RETRYING 拒
  4. 缺失旧 job 抛 ValueError
  5. raw_response 元数据（republish_reason + republished_from）写入
  6. AUTO_REPUBLISH_ON_DEAD 默认关：execute_job 走到 DEAD 时**不**自动建 v2

走的 session 套路：复用 tests/test_superseded_by.py 同款（SessionLocal.configure(bind=engine)
+ Base.metadata.create_all），不引入临时 sessionmaker，与生产路径同构。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic
from ai_ops.core.schemas import PublishResult
from ai_ops.scheduler import worker as worker_mod
from ai_ops.scheduler.worker import (
    AUTO_REPUBLISH_ON_DEAD,
    execute_job,
    republish_job,
)


# ---------------------------------------------------------------------------
# Fixtures（与 test_superseded_by.py 同款，单测内部不复用避免跨文件耦合）
# ---------------------------------------------------------------------------


@pytest.fixture
def session_in_memory():
    """in-memory SQLite 上的 production SessionLocal。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)

    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _mk_topic_article_account(s) -> tuple[Topic, Article, Account]:
    topic = Topic(name="t-republish", keywords=[], persona={}, target_platforms=[])
    s.add(topic)
    s.flush()
    art = Article(
        topic_id=topic.id,
        title="title",
        body="body",
        content_type=ContentType.IMAGE_TEXT,
        status=ArticleStatus.READY,
    )
    s.add(art)
    acc = Account(
        platform=Platform.XIAOHONGSHU,
        nickname="nick",
        profile={},
        health=AccountHealth.HEALTHY,
    )
    s.add(acc)
    s.flush()
    return topic, art, acc


def _mk_job(
    s,
    art_id: int,
    acc_id: int,
    *,
    status: JobStatus = JobStatus.PENDING,
    attempts: int = 0,
    max_attempts: int = 3,
    publisher_kind: str = "xhs_camoufox",
    platform: Platform = Platform.XIAOHONGSHU,
) -> PublishJob:
    job = PublishJob(
        article_id=art_id,
        account_id=acc_id,
        platform=platform,
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        publisher_kind=publisher_kind,
    )
    s.add(job)
    s.flush()
    return job


# ---------------------------------------------------------------------------
# Case 1: v2 字段复制语义
# ---------------------------------------------------------------------------


def test_republish_creates_new_job_with_same_fields(session_in_memory) -> None:
    """v2 job 应复用 article_id / account_id / platform / publisher_kind / max_attempts；
    status=PENDING, attempts=0, started_at/finished_at/error 等运行时字段重置。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(
            s, art.id, acc.id,
            status=JobStatus.DEAD,
            attempts=3,
            max_attempts=3,
            publisher_kind="xhs_camoufox",
        )
        v1.error = "publisher 挂了"
        s.commit()
        v1_id = v1.id

        v2 = republish_job(s, v1_id, reason="manual")
        s.commit()

        # 字段复用断言
        assert v2.article_id == art.id
        assert v2.account_id == acc.id
        assert v2.platform == Platform.XIAOHONGSHU
        assert v2.publisher_kind == "xhs_camoufox"
        assert v2.max_attempts == 3, "max_attempts 应复用旧值"

        # 运行时字段重置断言
        assert v2.status == JobStatus.PENDING, "v2 初始状态必须 PENDING"
        assert v2.attempts == 0, "v2 attempts 必须重置为 0"
        assert v2.started_at is None
        assert v2.finished_at is None
        assert v2.error is None
        assert v2.platform_post_id is None
        assert v2.platform_url is None

        # v2 自己不应被标 superseded
        assert v2.superseded_by_job_id is None


# ---------------------------------------------------------------------------
# Case 2: v1.superseded_by_job_id → v2.id
# ---------------------------------------------------------------------------


def test_republish_marks_old_superseded_by_new(session_in_memory) -> None:
    """republish 调完后旧 job.superseded_by_job_id 必须指向新 job.id。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=JobStatus.FAILED, attempts=1)
        s.commit()
        v1_id = v1.id

        v2 = republish_job(s, v1_id, reason="manual")
        s.commit()

        # 另一 session 读出来双重验证
        with SessionLocal() as s2:
            fresh_v1 = s2.get(PublishJob, v1_id)
            assert fresh_v1.superseded_by_job_id == v2.id, (
                f"v1.superseded_by_job_id 应={v2.id}，实际={fresh_v1.superseded_by_job_id}"
            )


# ---------------------------------------------------------------------------
# Case 3: 状态白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_status",
    [JobStatus.SUCCESS, JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRYING],
)
def test_republish_rejects_non_failed_jobs(session_in_memory, bad_status) -> None:
    """白名单外的 status 必须 ValueError，不允许覆盖重发。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=bad_status)
        s.commit()

        with pytest.raises(ValueError, match="can only republish FAILED/DEAD"):
            republish_job(s, v1.id, reason="manual")


def test_republish_rejects_success_job(session_in_memory) -> None:
    """SUCCESS 显式拒（即使被 parametrize 覆盖，留作独立用例方便定位）。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=JobStatus.SUCCESS, attempts=1)
        s.commit()
        with pytest.raises(ValueError):
            republish_job(s, v1.id)


def test_republish_rejects_pending_job(session_in_memory) -> None:
    """PENDING 显式拒（在跑的 job 不允许并发建 v2，避免竞态）。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=JobStatus.PENDING)
        s.commit()
        with pytest.raises(ValueError):
            republish_job(s, v1.id)


# ---------------------------------------------------------------------------
# Case 4: 缺失旧 job
# ---------------------------------------------------------------------------


def test_republish_rejects_missing_job(session_in_memory) -> None:
    """旧 job 不存在 → ValueError("job {id} not found")。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        with pytest.raises(ValueError, match="not found"):
            republish_job(s, 99999, reason="manual")


# ---------------------------------------------------------------------------
# Case 5: raw_response 元数据
# ---------------------------------------------------------------------------


def test_republish_sets_raw_response_metadata(session_in_memory) -> None:
    """v2.raw_response 必须含 republish_reason + republished_from。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=JobStatus.DEAD, attempts=3)
        s.commit()
        v1_id = v1.id

        v2 = republish_job(s, v1_id, reason="auto_retry_exhausted")
        s.commit()

        assert v2.raw_response is not None
        assert v2.raw_response.get("republish_reason") == "auto_retry_exhausted"
        assert v2.raw_response.get("republished_from") == v1_id


# ---------------------------------------------------------------------------
# Case 6: 默认 manual reason 兜底
# ---------------------------------------------------------------------------


def test_republish_default_reason_is_manual(session_in_memory) -> None:
    """不传 reason 时默认 'manual'（API 入口最常见路径）。"""
    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(s, art.id, acc.id, status=JobStatus.FAILED)
        s.commit()

        v2 = republish_job(s, v1.id)
        s.commit()
        assert v2.raw_response.get("republish_reason") == "manual"


# ---------------------------------------------------------------------------
# Case 7 (bonus): AUTO_REPUBLISH_ON_DEAD 默认关
# ---------------------------------------------------------------------------


def test_auto_republish_off_by_default() -> None:
    """常量自身的契约：默认 False，避免重试风暴。

    不靠走 execute_job 端到端来验（worker 路径依赖 publisher / 浏览器栈），
    直接断言常量本身的值。配合下面 test_auto_republish_branch_not_triggered_when_off
    覆盖"分支逻辑也确实不进"。
    """
    assert AUTO_REPUBLISH_ON_DEAD is False, (
        "AUTO_REPUBLISH_ON_DEAD 必须默认 False，避免 publisher 真挂时风暴"
    )


def test_auto_republish_branch_not_triggered_when_off(
    session_in_memory, monkeypatch
) -> None:
    """execute_job 走完 attempts >= max_attempts → DEAD 时，常量 False → 不调 republish_job。

    路径：
      1. 桩掉 worker 外部依赖（get_credential / check_rate_limit / notify / metrics 等）
         — 借用 test_worker_integration.py 同款套路，避免 ValueError("no credential") 等
         FAILED 短路在 DEAD 之前
      2. mock _try_publishers 永返失败 + 监视 republish_job
      3. attempts=2 / max_attempts=3 → execute_job 入口 +=1 = 3，失败后 3>=3 进 DEAD 分支
      4. 断言 republish_job 调用 0 次 + v1.status=DEAD + superseded_by 仍 None
    """
    from ai_ops.accounts.manager import RateCheckResult

    SessionLocal = session_in_memory
    with SessionLocal() as s:
        _, art, acc = _mk_topic_article_account(s)
        v1 = _mk_job(
            s, art.id, acc.id,
            status=JobStatus.PENDING,
            attempts=2,
            max_attempts=3,
        )
        s.commit()
        v1_id = v1.id

    # 桩掉外部依赖（同 test_worker_integration._patch_worker_externals 套路）
    monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda s, aid: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "mark_published", lambda s, aid: None)
    monkeypatch.setattr(worker_mod, "is_paused", lambda acc: False)

    from ai_ops.scheduler import metrics as metrics_mod
    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])

    import ai_ops.notify as notify_mod
    monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda snap: None)

    async def _fake_try_publishers(platform, account_id, credential, content):
        return PublishResult(success=False, error="forced failure for test")

    monkeypatch.setattr(worker_mod, "_try_publishers", _fake_try_publishers)

    # 监视 republish_job：常量 False 时应 0 次调用
    with patch.object(worker_mod, "republish_job") as mock_republish:
        # 用 monkeypatch 钉常量为 False（防御未来其它测试改它没还原）
        monkeypatch.setattr(worker_mod, "AUTO_REPUBLISH_ON_DEAD", False)

        result = asyncio.run(execute_job(v1_id))
        assert result.success is False  # _try_publishers 桩成失败

        assert mock_republish.call_count == 0, (
            f"AUTO_REPUBLISH_ON_DEAD=False 时 republish_job 不应被调用，"
            f"实际调用 {mock_republish.call_count} 次"
        )

    # 二次确认：v1 真被标 DEAD（验 execute_job 确实走到了 DEAD 分支，而不是更上面的 FAILED 短路）
    with SessionLocal() as s:
        fresh = s.get(PublishJob, v1_id)
        assert fresh.status == JobStatus.DEAD, (
            f"v1 应被标 DEAD（attempts={fresh.attempts}/max={fresh.max_attempts}），"
            f"实际 status={fresh.status}"
        )
        assert fresh.superseded_by_job_id is None, (
            "AUTO_REPUBLISH_ON_DEAD=False 时不应建 v2，故 superseded_by 应为 None"
        )
