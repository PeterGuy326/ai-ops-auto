"""tests/test_worker_integration.py — Task A · TD-X4 P0 修复回归。

战场：`src/ai_ops/core/db.py` 的 `SessionLocal` 之前默认 `expire_on_commit=True`，
导致 `src/ai_ops/scheduler/worker.py` line 110-111 在第一个 `session_scope` 退出
后访问 `job.account_id` 拼 `_try_publishers(... account_id ...)` 时触发
`DetachedInstanceError`——真发布即崩。

本测试核心契约：
  1. 用 **生产 SessionLocal**（即 `ai_ops.core.db.SessionLocal`，含 production
     的所有 kwargs，特别是 `expire_on_commit=False`）+ in-memory engine，
     不允许临时构造一个 `expire_on_commit=False` 的 sessionmaker 来"绕开"——
     必须是 production 配置生效。
  2. 跑 `execute_job` 全链路（mock 掉外部 publisher / notify / metrics 等副作用
     hook，但 worker 内的 session 进出和 ORM access 路径完全真实），验证 worker
     不再抛 `DetachedInstanceError`。
  3. 直接 assert `SessionLocal.kw['expire_on_commit'] is False`，钉死生产配置。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm.exc import DetachedInstanceError

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


# ---------------------------------------------------------------------------
# Fixture：rebind 生产 SessionLocal 到 in-memory engine（不污染 ./data/ai_ops.db）
# ---------------------------------------------------------------------------


@pytest.fixture
def production_session_in_memory(monkeypatch):
    """把生产 `db.SessionLocal` rebind 到 in-memory engine。

    关键：使用 `SessionLocal.configure(bind=engine)` 而非新建临时 sessionmaker，
    确保所有 production kwargs（特别是 `expire_on_commit=False`) 生效。

    返回：`db.SessionLocal`（已 rebind），调用方直接 `SessionLocal()` 即可。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    original_bind = db_mod.SessionLocal.kw.get("bind")
    # configure 会原地修改 kw['bind']；session 创建走的还是生产 sessionmaker，
    # 即 expire_on_commit=False 等约定一并继承
    db_mod.SessionLocal.configure(bind=engine)
    # session_scope 内部 `SessionLocal()` 引用的是 module-level 名字，
    # 不需要单独 monkeypatch；但要确保 worker 模块顶部 import 的 `session_scope`
    # 仍然指向 db_mod.session_scope（默认就是，不动）
    try:
        yield db_mod.SessionLocal
    finally:
        # 还原到生产 engine，避免污染其它 test
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _mk_publishable_chain(SessionLocal) -> int:
    """构造最小可发布链路 (topic → account → article → job)，返回 job_id。"""
    with SessionLocal() as s:
        topic = Topic(name="t_integ", keywords=[], persona={}, target_platforms=[])
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="acc_integ",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="集成测试标题",
            body="正文，不含污点词",
            content_type=ContentType.LONG_ARTICLE,
            status=ArticleStatus.PUBLISHING,
            extra={},
        )
        s.add(article)
        s.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=acc.id,
            platform=Platform.XIAOHONGSHU,
            status=JobStatus.PENDING,
            publisher_kind="test",
            attempts=0,
            max_attempts=3,
        )
        s.add(job)
        s.commit()
        return job.id


def _patch_worker_externals(monkeypatch):
    """统一桩掉 worker 外部依赖（凭证 / 限流 / 风控 / 健康 / notify / metrics）。

    只 patch 副作用 hook，**不动** session 进出 / ORM access 路径，确保
    DetachedInstanceError 路径如果仍存在会真实暴露。
    """
    from ai_ops.accounts.manager import RateCheckResult
    from ai_ops.scheduler import worker as worker_mod

    monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda s, aid: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "mark_published", lambda s, aid: None)
    monkeypatch.setattr(worker_mod, "is_paused", lambda acc: False)

    # 不让 schedule_after_publish 真起 APScheduler
    from ai_ops.scheduler import metrics as metrics_mod
    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])

    # notify 路径要真跑（_try_publishers 后 worker 会调），桩成 noop
    import ai_ops.notify as notify_mod
    monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda snap: None)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestSessionDetachedRegression:
    """TD-X4 P0 回归：跑生产 session_scope + 真实 ORM access，不应抛 DetachedInstanceError。"""

    def test_sessionmaker_expire_on_commit_is_false(self):
        """生产配置钉死：SessionLocal 必须 expire_on_commit=False。

        这是 P0 fix 的 invariant —— 任何回退到默认 True 都会让 worker 真发布炸。
        """
        # 直接 assert sessionmaker 内部 kw 字典
        assert db_mod.SessionLocal.kw.get("expire_on_commit") is False, (
            "P0 回归：SessionLocal 的 expire_on_commit 不是 False，"
            "worker.py 跨 session 访问 job.account_id 会触发 DetachedInstanceError"
        )

    def test_session_no_detached_error_after_publish_success(
        self, production_session_in_memory, monkeypatch
    ):
        """发布成功路径：worker 跑完 execute_job 不抛 DetachedInstanceError。

        关键路径：worker.py L110-111 跳出第一个 session_scope 后读 `job.account_id`
        传给 `_try_publishers`——若 expire_on_commit=True 此处必炸。
        """
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)

        _patch_worker_externals(monkeypatch)

        # mock publisher 返回 success，触发整个 success 分支（含 mark_published / notify）
        from ai_ops.core.schemas import PublishResult
        from ai_ops.scheduler import worker as worker_mod

        async def fake_try_publishers(platform, account_id, credential, content):
            # 这里隐式验证：worker 能把 account_id 传进来 = 没在 session 外 detached
            assert isinstance(account_id, int) and account_id > 0
            return PublishResult(
                success=True,
                platform_post_id="p_success",
                platform_url="http://example.com/success",
            )

        monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)

        # 真跑：任何 DetachedInstanceError 都会向上冒泡
        try:
            result = asyncio.run(worker_mod.execute_job(job_id))
        except DetachedInstanceError as e:  # pragma: no cover
            pytest.fail(f"P0 回归：worker 在 session 外 detached lazy-load: {e}")

        assert result.success is True
        assert result.platform_post_id == "p_success"

        # 副验证：job 状态确实落库为 SUCCESS
        with SessionLocal() as s:
            job = s.get(PublishJob, job_id)
            assert job is not None
            assert job.status == JobStatus.SUCCESS
            assert job.platform_post_id == "p_success"
            assert job.finished_at is not None

    def test_session_no_detached_error_after_publish_failure(
        self, production_session_in_memory, monkeypatch
    ):
        """发布失败路径：worker 跑完 execute_job 不抛 DetachedInstanceError。

        失败分支同样需要在第二个 session_scope 内回写 job.status + 出 session 后
        notify，全程不应碰到 detached。
        """
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)

        _patch_worker_externals(monkeypatch)

        from ai_ops.core.schemas import PublishResult
        from ai_ops.scheduler import worker as worker_mod

        async def fake_try_publishers(platform, account_id, credential, content):
            assert isinstance(account_id, int) and account_id > 0
            return PublishResult(success=False, error="publisher boom")

        monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)

        try:
            result = asyncio.run(worker_mod.execute_job(job_id))
        except DetachedInstanceError as e:  # pragma: no cover
            pytest.fail(f"P0 回归：worker 失败分支 detached: {e}")

        assert result.success is False
        assert result.error == "publisher boom"

        with SessionLocal() as s:
            job = s.get(PublishJob, job_id)
            assert job is not None
            # attempts=1，max_attempts=3 → 应进 RETRYING
            assert job.status == JobStatus.RETRYING
            assert job.error == "publisher boom"

    def test_orm_attribute_accessible_after_session_close(
        self, production_session_in_memory
    ):
        """裸 invariant：commit 后关闭 session，再读对象 attribute 不应抛 detached。

        这是 expire_on_commit=False 最直接的行为验证——不依赖 worker，纯 SQLAlchemy
        合约。如果这条都挂了，#2/#3 用例的"不抛 detached"就失去了底层基础。
        """
        SessionLocal = production_session_in_memory

        with SessionLocal() as s:
            topic = Topic(name="t_attr", keywords=[], persona={}, target_platforms=[])
            s.add(topic)
            s.commit()
            captured_id = topic.id
            captured_name = topic.name

        # session 已关闭 + 对象 detached——expire_on_commit=False 下 attribute 已缓存，
        # 读不会触发 refresh，因此不应抛
        assert topic.id == captured_id
        assert topic.name == "t_attr"
        assert captured_name == "t_attr"
