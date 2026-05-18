"""tests/test_worker_observability.py — Task X · scheduler 观测黑洞回归单测。

覆盖：
  1. observability.sentry.capture_exception 软依赖契约
     - sentry-sdk 未装 → 静默返回 False，不抛
     - dsn 空但 sdk 装了 → 仍走 capture（init_sentry 早就跳过，但 capture 自己
       不依赖 init，sdk 内部会无 hub 静默）→ 不抛，返回 True/False 都可接受
     - capture 内部抛 → 吞掉返回 False
  2. worker 3 处黑洞接 capture_exception 验证
     - notify 失败被 capture（scope="worker.notify"）
     - schedule_after_publish 失败被 capture（scope="worker.schedule_metrics"）
     - process_images 失败被 capture（scope="worker.image_anti_fingerprint"）

策略：
  - capture_exception 用 monkeypatch 替换 worker 模块内的 import 名（worker.py
    line 20 `from ..observability.sentry import capture_exception`）
  - 不跑真实 worker.execute_job（依赖 DB / publisher / Account 整套）；
    用 in-memory SQLite + mock publisher，仿照 test_pre_publish_check.py
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


# ---------------------------------------------------------------------------
# Part 1: capture_exception 软依赖契约
# ---------------------------------------------------------------------------


class TestCaptureExceptionSoftDep:
    """capture_exception 的软依赖契约：任何环境都不能抛。"""

    def test_no_sdk_returns_false(self, monkeypatch):
        """sentry-sdk 未装时静默返回 False，不抛异常。"""
        from ai_ops.observability import sentry as sentry_mod

        monkeypatch.setattr(sentry_mod, "_sentry_sdk_available", lambda: False)
        result = sentry_mod.capture_exception(ValueError("test"), scope="unit")
        assert result is False

    def test_sdk_installed_dsn_empty_does_not_raise(self, monkeypatch):
        """sdk 装了 + 没 init_sentry → capture 内部走 sdk no-op，不抛。

        策略：mock 一个 fake sentry_sdk，其 capture_exception 不抛（模拟 sdk
        无 hub 时的 no-op 行为）。验证 capture 不抛 + 返回 True。
        """
        from ai_ops.observability import sentry as sentry_mod

        monkeypatch.setattr(sentry_mod, "_sentry_sdk_available", lambda: True)

        captured_calls = []

        class FakeScope:
            def set_tag(self, k, v):
                captured_calls.append(("tag", k, v))

            def set_extra(self, k, v):
                captured_calls.append(("extra", k, v))

        class FakeSentry:
            @staticmethod
            def push_scope():
                class _Ctx:
                    def __enter__(self_inner):
                        return FakeScope()

                    def __exit__(self_inner, *a):
                        return False

                return _Ctx()

            @staticmethod
            def capture_exception(exc):
                captured_calls.append(("capture", type(exc).__name__, str(exc)))

        monkeypatch.setitem(sys.modules, "sentry_sdk", FakeSentry)
        result = sentry_mod.capture_exception(
            ValueError("boom"),
            scope="worker.test",
            job_id=42,
        )
        assert result is True
        # 验证 scope="worker.test" 和 job_id=42 都作为 tag 进了 scope（标量）
        assert ("tag", "scope", "worker.test") in captured_calls
        assert ("tag", "job_id", "42") in captured_calls
        # capture_exception 必被调用一次
        assert any(c[0] == "capture" for c in captured_calls)

    def test_capture_internal_failure_swallowed(self, monkeypatch):
        """sdk 装了但 capture 内部抛 → 吞掉返回 False，不影响调用方。"""
        from ai_ops.observability import sentry as sentry_mod

        monkeypatch.setattr(sentry_mod, "_sentry_sdk_available", lambda: True)

        class BoomSentry:
            @staticmethod
            def push_scope():
                raise RuntimeError("sentry hub corrupted")

        monkeypatch.setitem(sys.modules, "sentry_sdk", BoomSentry)
        result = sentry_mod.capture_exception(ValueError("x"))
        assert result is False  # 内部异常被吞


# ---------------------------------------------------------------------------
# Part 2: worker 3 处黑洞 capture 接通验证（最小化场景，不跑全链路）
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine_with_worker(monkeypatch):
    """启 in-memory engine 并把 worker 用到的 session_scope 指向它。

    返回 (SessionLocal, capture_calls)：
      - SessionLocal：测试中用来构造 Account/Article/PublishJob 等 fixture 数据
      - capture_calls：list，所有 capture_exception 调用都会 append 到这里
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    # 注：本地测试用 expire_on_commit=False，避开 worker.py line 111 在 session
    # 退出后访问 job.account_id 的 detached lazy-load（生产的 session_scope 行为
    # 与默认略有出入；本测试只关注 observability hooks，不复现完整 session 语义）
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
        expire_on_commit=False,
    )

    # mock session_scope —— worker.py 顶部 `from ..core.db import session_scope`
    # 已绑定到 worker 模块内的名字；同时核心的 mark_published / check_rate_limit
    # 等也是 worker 顶部 import 的——它们用 session 参数，不内开新 session，所以
    # 只 mock worker.session_scope 即可
    from contextlib import contextmanager

    @contextmanager
    def fake_session_scope():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    from ai_ops.scheduler import worker as worker_mod

    monkeypatch.setattr(worker_mod, "session_scope", fake_session_scope)
    # 关掉风控限流（不是本测试关注的路径）
    from ai_ops.accounts.manager import RateCheckResult

    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda s, aid: RateCheckResult(allowed=True, reason=""),
    )
    # mark_published 在本路径会触发——简单 noop 掉避免触碰 Account 计数细节
    monkeypatch.setattr(worker_mod, "mark_published", lambda s, aid: None)
    # is_paused 一律 False
    monkeypatch.setattr(worker_mod, "is_paused", lambda acc: False)

    # capture 收集器
    capture_calls: list[dict] = []

    def fake_capture(exc, **ctx):
        capture_calls.append({"exc_type": type(exc).__name__, "exc_msg": str(exc), **ctx})
        return True

    monkeypatch.setattr(worker_mod, "capture_exception", fake_capture)

    return SessionLocal, capture_calls


def _mk_job(SessionLocal, *, content_type=ContentType.IMAGE_TEXT, with_images=True):
    """构造一个最小可发布的 (account, topic, article, job) 链路。"""
    with SessionLocal() as s:
        topic = Topic(name="t_obs", keywords=[], persona={}, target_platforms=[])
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="acc_obs",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",  # get_credential 我们 mock 掉
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="测试",
            body="正文，不含污点词",
            content_type=content_type,
            status=ArticleStatus.PUBLISHING,
            extra={},
        )
        s.add(article)
        s.flush()
        if with_images:
            from ai_ops.core.models import Asset

            s.add(Asset(article_id=article.id, asset_type="image", source="local", local_path="/fake/x.jpg"))
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


class TestWorkerObservabilityHooks:
    """3 处黑洞 capture 接通验证。"""

    def test_image_processing_failure_captured(self, db_engine_with_worker, monkeypatch):
        """process_images 抛异常 → capture(scope='worker.image_anti_fingerprint')。"""
        SessionLocal, capture_calls = db_engine_with_worker
        job_id = _mk_job(SessionLocal, content_type=ContentType.IMAGE_TEXT, with_images=True)

        from ai_ops.scheduler import worker as worker_mod

        # mock 凭证拿取 + publisher（让主流程能跑到 image 处理点）
        monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})

        # 关键：注入抛异常的 process_images
        import ai_ops.content.asset_processor as ap_mod

        def boom_process_images(paths, account_id):
            raise RuntimeError("PIL exploded")

        monkeypatch.setattr(ap_mod, "process_images", boom_process_images)

        # mock publisher 让发布成功，避免触发 notify / schedule_metrics 路径污染断言
        from ai_ops.core.schemas import PublishResult

        async def fake_try_publishers(*a, **kw):
            return PublishResult(success=True, platform_post_id="p1", platform_url="http://x")

        monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)
        # 阻断 schedule_after_publish + notify 避免它们也产生 capture
        from ai_ops.scheduler import metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])
        import ai_ops.notify as notify_mod

        monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)

        asyncio.run(worker_mod.execute_job(job_id))

        image_captures = [c for c in capture_calls if c.get("scope") == "worker.image_anti_fingerprint"]
        assert len(image_captures) == 1, f"expected 1 image capture, got {capture_calls}"
        cap = image_captures[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "PIL exploded" in cap["exc_msg"]
        assert "job_id" in cap
        assert "account_id" in cap

    def test_schedule_metrics_failure_captured(self, db_engine_with_worker, monkeypatch):
        """schedule_after_publish 抛 → capture(scope='worker.schedule_metrics')。"""
        SessionLocal, capture_calls = db_engine_with_worker
        # 用纯文本路径，绕开 image 黑洞干扰
        job_id = _mk_job(SessionLocal, content_type=ContentType.LONG_ARTICLE, with_images=False)

        from ai_ops.scheduler import worker as worker_mod

        monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})

        from ai_ops.core.schemas import PublishResult

        async def fake_try_publishers(*a, **kw):
            return PublishResult(success=True, platform_post_id="p2", platform_url="http://y")

        monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)

        from ai_ops.scheduler import metrics as metrics_mod

        def boom_schedule(jid):
            raise RuntimeError("scheduler down")

        monkeypatch.setattr(metrics_mod, "schedule_after_publish", boom_schedule)

        import ai_ops.notify as notify_mod

        monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)

        asyncio.run(worker_mod.execute_job(job_id))

        sched_captures = [c for c in capture_calls if c.get("scope") == "worker.schedule_metrics"]
        assert len(sched_captures) == 1, f"expected 1 schedule capture, got {capture_calls}"
        cap = sched_captures[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "scheduler down" in cap["exc_msg"]
        assert "job_id" in cap

    def test_notify_failure_captured(self, db_engine_with_worker, monkeypatch):
        """notify 抛 → capture(scope='worker.notify') + kind 透传。"""
        SessionLocal, capture_calls = db_engine_with_worker
        job_id = _mk_job(SessionLocal, content_type=ContentType.LONG_ARTICLE, with_images=False)

        from ai_ops.scheduler import worker as worker_mod

        monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})

        from ai_ops.core.schemas import PublishResult

        async def fake_try_publishers(*a, **kw):
            return PublishResult(success=True, platform_post_id="p3", platform_url="http://z")

        monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)

        # schedule_after_publish 也 noop 避免它的 capture 污染断言
        from ai_ops.scheduler import metrics as metrics_mod

        monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])

        # 让 notify.publish_success 抛
        import ai_ops.notify as notify_mod

        def boom_notify(snap):
            raise RuntimeError("webhook 500")

        monkeypatch.setattr(notify_mod, "publish_success", boom_notify)

        asyncio.run(worker_mod.execute_job(job_id))

        notify_captures = [c for c in capture_calls if c.get("scope") == "worker.notify"]
        assert len(notify_captures) == 1, f"expected 1 notify capture, got {capture_calls}"
        cap = notify_captures[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "webhook 500" in cap["exc_msg"]
        assert cap.get("kind") == "success"
        assert "job_id" in cap
